"""Independently label permits with an LLM, to audit the regex flags.

Permits are sent in small batches to `claude -p` running Haiku, which sees only
the permit type/subtype and the project description -- never the existing flags
or the patterns in process_somerville_data.py. Comparing the two labelings shows
where the regexes are wrong.

The subprocess runs with --restricted and an explicit tool denylist, which
removes Bash and the other code-running tools. That matters: given a file of
thousands of rows and the ability to run code, a model writes a keyword script
instead of reading the rows, which turns the audit into a comparison of two
regex systems. Rows arrive inline on stdin, so there is nothing to script over.

Batching is what makes a full run affordable. Each call carries ~3.5k tokens of
harness context regardless of payload, so one row per call spends most of its
budget on overhead; 25 rows amortise it. Extended thinking is deliberately left
ON -- with MAX_THINKING_TOKENS=0 output drops from ~400 tokens to 7, but quality
collapses (in a 5-case check it labelled "gas stove replacement" as solar).

Work is deduplicated to unique (type, subtype, description) triples, checkpointed
after every batch, and resumable -- rerunning labels only what is still missing.

    uv run python scripts/label_permits_llm.py --limit 100      # trial
    uv run python scripts/label_permits_llm.py --workers 16     # full run
    uv run python scripts/label_permits_llm.py --merge-only     # join to rows
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

DEFAULT_INPUT = Path("data/processed/ma/somerville/joined_data.csv")
DEFAULT_LABELS = Path("data/processed/ma/somerville/llm_labels.jsonl")
DEFAULT_OUT = Path("data/processed/ma/somerville/joined_data_llm_labeled.csv")

# Order is fixed: the model returns one character per column, in this order.
FLAG_COLS = [
    "solar_pv", "heat_pumps", "heat_pump_water_heater", "water_heater",
    "cooking", "ev_charger", "electrical_panel", "other_hvac", "ess",
]

# A handful of pathological descriptions run to several thousand characters and
# would dominate a batch; the median is 48.
MAX_DESC_CHARS = 600

# Strip the harness down. --restricted drops the code-running tools, the denylist
# removes the rest, and the other two flags drop MCP servers and the per-machine
# system prompt sections. Together: ~11,950 -> ~3,500 input tokens per call.
# Every name here must be a real tool or the CLI prints a warning onto stdout.
CLAUDE_FLAGS = [
    "--restricted",
    "--strict-mcp-config",
    "--exclude-dynamic-system-prompt-sections",
    "--disallowedTools",
    "Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebFetch", "WebSearch",
    "Task", "TodoWrite", "NotebookEdit", "BashOutput", "KillShell",
]

SYSTEM_PROMPT = f"""You label residential building permits from a US city by the kind of work they cover.

The input is a numbered list of permits. For EACH permit output one line:
the permit's number, a colon, then EXACTLY {len(FLAG_COLS)} characters, each 0 or 1, in this order:
{",".join(FLAG_COLS)}

For example: `7:001100000`

1 means the permit covers that kind of work. A permit may cover several kinds, or none (all zeros).

- solar_pv: solar photovoltaic electricity generation
- heat_pumps: heat pump equipment for space heating/cooling
- heat_pump_water_heater: a heat pump water heater specifically
- water_heater: water heating equipment of any kind, fossil or electric
- cooking: a cooking appliance is named -- stove, range, cooktop, oven, induction unit.
  A kitchen remodel, a kitchen renovation, or an addition that merely mentions a kitchen
  does NOT count unless an actual cooking appliance is named. A range hood is ventilation,
  not a cooking appliance.
- ev_charger: electric vehicle charging equipment
- electrical_panel: electrical service or panel capacity work
- other_hvac: heating/cooling/ventilation equipment that is NOT a heat pump
- ess: battery energy storage

Judge the actual scope of work. Descriptions often carry administrative noise
("WITHDRAWN PER APPLICANT", duplicate-permit references) -- ignore it. A description
that explicitly disclaims something ("No ESS", "no battery") does NOT get that flag.

Output one line per permit and nothing else: no preamble, no blank lines, no explanation.
Output a line for every permit in the list, in order, even if the description is uninformative."""

ANSWER_RE = re.compile(rf"^\s*(\d+)\s*[:.]\s*([01]{{{len(FLAG_COLS)}}})\s*$")


def build_worklist(input_path: Path) -> pd.DataFrame:
    """Unique (type, subtype, description) triples, with a stable id."""
    df = pd.read_csv(input_path, low_memory=False)
    df["project_description"] = df["project_description"].fillna("").str.strip()
    triples = (
        df.loc[df["project_description"] != "",
               ["application_type", "application_subtype", "project_description"]]
        .drop_duplicates()
        .sort_values(["application_type", "application_subtype", "project_description"])
        .reset_index(drop=True)
    )
    triples.insert(0, "rid", range(1, len(triples) + 1))
    return triples


def render_batch(rows: list[dict]) -> str:
    """One permit per line, numbered from 1. Newlines in descriptions are collapsed."""
    lines = []
    for n, row in enumerate(rows, 1):
        desc = " ".join(str(row["project_description"]).split())[:MAX_DESC_CHARS]
        lines.append(f"{n}. [{row['application_type']} / {row['application_subtype']}] {desc}")
    return "\n".join(lines)


def call_claude(payload: str, model: str, timeout: int) -> str:
    """Prompt goes on stdin: --disallowedTools is variadic and would eat a positional."""
    done = subprocess.run(
        ["claude", "-p", "--model", model, "--system-prompt", SYSTEM_PROMPT, *CLAUDE_FLAGS],
        input=payload, capture_output=True, text=True, timeout=timeout,
    )
    return done.stdout


def label_batch(rows: list[dict], model: str, timeout: int, retries: int) -> dict[int, str]:
    """Map rid -> flag string. Missing entries are rows the model never answered."""
    got: dict[int, str] = {}
    pending = list(rows)
    for attempt in range(retries + 1):
        if not pending:
            break
        try:
            out = call_claude(render_batch(pending), model, timeout)
        except subprocess.TimeoutExpired:
            out = ""
        answered = {}
        for line in out.splitlines():
            m = ANSWER_RE.match(line)
            if m:
                idx = int(m.group(1))
                if 1 <= idx <= len(pending):
                    answered[idx] = m.group(2)
        for idx, flags in answered.items():
            got[pending[idx - 1]["rid"]] = flags
        pending = [r for r in pending if r["rid"] not in got]
        if pending and attempt < retries:
            time.sleep(2 * (attempt + 1))
    return got


def run_labeling(work: pd.DataFrame, labels_path: Path, model: str, workers: int,
                 batch_size: int, timeout: int, retries: int) -> None:
    done: set[int] = set()
    if labels_path.exists():
        with labels_path.open() as fh:
            for line in fh:
                try:
                    done.add(json.loads(line)["rid"])
                except (json.JSONDecodeError, KeyError):
                    continue
        print(f"Resuming: {len(done):,} rows already labeled")

    todo = work[~work["rid"].isin(done)].to_dict("records")
    if not todo:
        print("Nothing left to label.")
        return

    batches = [todo[i:i + batch_size] for i in range(0, len(todo), batch_size)]
    print(f"Labeling {len(todo):,} rows as {len(batches):,} batches of {batch_size} "
          f"with {model} across {workers} workers")
    labels_path.parent.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()
    tally = {"ok": 0, "missing": 0, "batches": 0}
    started = time.time()

    def worker(batch: list[dict]) -> None:
        got = label_batch(batch, model, timeout, retries)
        with lock, labels_path.open("a") as fh:
            for row in batch:
                if row["rid"] in got:
                    fh.write(json.dumps({"rid": row["rid"], "flags": got[row["rid"]]}) + "\n")
            tally["ok"] += len(got)
            tally["missing"] += len(batch) - len(got)
            tally["batches"] += 1
            if tally["batches"] % 10 == 0 or tally["batches"] == len(batches):
                seen = tally["ok"] + tally["missing"]
                rate = seen / (time.time() - started)
                eta = (len(todo) - seen) / rate / 60 if rate else 0
                print(f"  {seen:,}/{len(todo):,} rows  {rate:.1f}/s  eta {eta:.0f}m  "
                      f"unanswered {tally['missing']}", flush=True)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(worker, batches))

    print(f"Done: {tally['ok']:,} labeled, {tally['missing']:,} unanswered "
          f"in {(time.time() - started) / 60:.1f}m")
    if tally["missing"]:
        print("Unanswered rows are not recorded -- rerun to retry just those.")


def merge(input_path: Path, work: pd.DataFrame, labels_path: Path, out_path: Path) -> None:
    """Attach the LLM labels back onto every permit row and write the dataset."""
    records = []
    with labels_path.open() as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("flags"):
                records.append({"rid": rec["rid"],
                                **{c: int(v) for c, v in zip(FLAG_COLS, rec["flags"])}})
    labels = pd.DataFrame(records).drop_duplicates("rid")
    print(f"{len(labels):,} of {len(work):,} unique triples have labels")

    keys = ["application_type", "application_subtype", "project_description"]
    mapping = work.merge(labels, on="rid").drop(columns="rid")

    df = pd.read_csv(input_path, low_memory=False)
    df["project_description"] = df["project_description"].fillna("").str.strip()
    out = df.merge(mapping, on=keys, how="left", suffixes=("", "_llm"))
    # A row is only comparable where the LLM actually returned a label.
    out["llm_labeled"] = out[FLAG_COLS[0] + "_llm"].notna()
    for col in FLAG_COLS:
        out[col + "_llm"] = out[col + "_llm"].astype("boolean")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(out_path, index=False)
    print(f"Wrote {len(out):,} rows ({out['llm_labeled'].sum():,} LLM-labeled) to {out_path}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    p.add_argument("--labels", type=Path, default=DEFAULT_LABELS)
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--model", default="haiku")
    p.add_argument("--workers", type=int, default=12)
    p.add_argument("--batch-size", type=int, default=25, help="permits per claude call")
    p.add_argument("--timeout", type=int, default=300, help="seconds per call")
    p.add_argument("--retries", type=int, default=2)
    p.add_argument("--limit", type=int, default=None, help="label only the first N rows")
    p.add_argument("--merge-only", action="store_true", help="skip labeling, just join")
    args = p.parse_args()

    work = build_worklist(args.input)
    print(f"{len(work):,} unique (type, subtype, description) triples")

    if not args.merge_only:
        subset = work.head(args.limit) if args.limit else work
        if args.limit:
            print(f"--limit {args.limit}: labeling a {len(subset):,}-row slice")
        run_labeling(subset, args.labels, args.model, args.workers,
                     args.batch_size, args.timeout, args.retries)

    if args.labels.exists():
        merge(args.input, work, args.labels, args.out)
    else:
        print(f"No labels at {args.labels}; nothing to merge.", file=sys.stderr)


if __name__ == "__main__":
    main()
