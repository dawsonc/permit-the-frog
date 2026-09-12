"use strict";

// Project-type flags, in the order they appear as checkboxes. Keys mirror
// FRONTEND_FLAGS in scripts/process_somerville_data.py.
const FLAGS = [
  ["hp", "Heat pumps"],
  ["panel", "Electrical panel"],
  ["solar", "Rooftop solar"],
  ["hpwh", "Heat pump water heater"],
  ["ev", "EV charger"],
  ["ess", "Battery storage"],
  ["hvac", "Other HVAC"],
];

// Multi-select facets, whose values come from meta.json rather than being
// hardcoded — they change whenever the pipeline reruns.
const FACETS = [
  ["prop_class", "Property type"],
];

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "2025-08" -> "Aug 2025". Sorting still uses the raw value, which is ordered. */
const month = (v) => {
  if (!v) return "—";
  const [y, m] = v.split("-");
  return `${MONTHS[+m - 1]} ${y}`;
};

const money = (v) => (v == null ? "—" : "$" + Math.round(v).toLocaleString());
const num = (v) => (v == null ? "—" : v.toLocaleString());

const COLUMNS = [
  { key: "date", label: "Date", fmt: month, cls: "nowrap" },
  { key: "_project", label: "Project" },
  { key: "desc", label: "Description", cls: "desc" },
  { key: "cost", label: "Cost", numeric: true, fmt: money },
  { key: "trade", label: "Permit type", cls: "nowrap" },
  { key: "kw", label: "Size (kW)", numeric: true, fmt: (v) => (v == null ? "—" : v) },
  { key: "year_built", label: "Built", numeric: true, fmt: (v) => (v == null ? "—" : v + "s") },
  { key: "res_area", label: "Area (sqft)", numeric: true, fmt: num },
  { key: "prop_class", label: "Property type" },
  { key: "hood", label: "Neighborhood" },
  { key: "contractor", label: "Contractor" },
];

const PAGE = 200;

// "Year built" is one value; a house matches if it is within this many years.
const YEAR_SPAN = 20;

let PERMITS = [];
let UNITS = [];
let ADDRESSES = [];
let BY_ADDR = new Map();
let META = null;

// Filter values are NOT held here — the form holds them. This is only the
// table state the form has no control for.
const state = { sort: "date", dir: -1, limit: PAGE };

const $ = (id) => document.getElementById(id);
const esc = (s) =>
  String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

/** {cols, rows} -> array of plain objects, so later code reads p.cost not row[4]. */
const toObjects = ({ cols, rows }) =>
  rows.map((r) => Object.fromEntries(cols.map((c, i) => [c, r[i]])));

// --- filtering -------------------------------------------------------------

/**
 * Read every filter straight out of the form. Values stay strings on purpose:
 * res_area has a 0 bucket, and Number("0") is falsy, which would silently turn
 * "0 sqft" into "no filter". "0" is truthy, and the comparisons below coerce.
 */
function readFilters() {
  const fd = new FormData($("filter-form"));
  return {
    flags: fd.getAll("flags"),
    prop_class: fd.getAll("prop_class"),
    year: fd.get("year"),
    since: fd.get("since"),
    areaMin: fd.get("areaMin"),
    areaMax: fd.get("areaMax"),
  };
}

/** One line per filter; an empty value means that filter is not applied. */
const matches = (f) => (p) =>
  (!f.flags.length || f.flags.some((k) => p[k] === 1)) &&
  (!f.prop_class.length || f.prop_class.includes(p.prop_class)) &&
  (!f.year || Math.abs(p.year_built - f.year) <= YEAR_SPAN) &&
  (!f.since || p.date >= f.since) &&
  (!f.areaMin || p.res_area >= f.areaMin) &&
  (!f.areaMax || p.res_area <= f.areaMax);

/** Nulls always sort last, whichever direction the column is going. */
function compare(a, b) {
  const col = COLUMNS.find((c) => c.key === state.sort);
  const x = a[state.sort], y = b[state.sort];
  if (x == null && y == null) return 0;
  if (x == null) return 1;
  if (y == null) return -1;
  const cmp = col.numeric ? x - y : String(x).localeCompare(String(y));
  return cmp * state.dir;
}

const projectOf = (p) =>
  FLAGS.filter(([f]) => p[f] === 1).map(([, label]) => label).join(", ");

// --- grouping --------------------------------------------------------------
// One project can file more than one permit: a heat pump install prices the job
// on a building permit and the wiring on an electrical permit, and the wiring
// permit read on its own looks like a $1,350 heat pump. The pipeline stamps
// such permits with a shared `gid`. They stay separate rows here — each permit
// is real, with its own date, contractor and description — and the page renders
// them as sub-rows under a summary row it derives from them. That summary row
// carries no price: the costs in the table are the ones the permits state, and
// they are all on the sub-rows.
//
// A "unit" is what the table lists: one lone permit, or one group.
//   { row, members, subs }
// `row` is what the sort and the cell renderer see, `members` what the filters
// see, `subs` the rows to indent underneath (empty for a lone permit).

/** Building permit first — it prices the job — then the rest by date. */
const bySubRow = (a, b) =>
  (a.trade === "Building Permit" ? 0 : 1) - (b.trade === "Building Permit" ? 0 : 1) ||
  String(a.date).localeCompare(String(b.date));

/**
 * The summary row for a group, shaped exactly like a permit so that compare(),
 * matches() and the cell renderer need to know nothing about groups.
 *
 * Everything here is derived from the members except `cost`: whether the two
 * permits split the work or restate it is a judgement made in
 * scripts/process_somerville_data.py, and its answer arrives in the `groups`
 * block rather than being reimplemented here.
 *
 * That `cost` is never rendered — rowHtml() blanks it — but it is kept, and
 * is not dead: it is what orders the group when the table is sorted by cost.
 * Dropping it would sort every grouped project last in both directions.
 */
function summaryOf(members, cost) {
  // Property attributes are identical across a group — same parcel — so the
  // building permit's stand for the project. A contractor that differs is
  // visible on the sub-row that reports it.
  const row = { ...members[0], cost, desc: `${members.length} permits` };
  row.date = members.reduce((a, p) => (p.date && p.date < a ? p.date : a), members[0].date);
  for (const [f] of FLAGS) row[f] = members.some((p) => p[f] === 1) ? 1 : 0;
  const kw = members.map((p) => p.kw).filter((v) => v != null);
  row.kw = kw.length ? Math.max(...kw) : null;
  // "Building Permit" + "Electrical Permit" -> "Building + Electrical".
  row.trade = [...new Set(members.map((p) => p.trade))]
    .map((t) => t.replace(/ Permit$/, ""))
    .join(" + ");
  return row;
}

function buildUnits(permits, groupCost) {
  const byGid = new Map();
  const units = [];
  for (const p of permits) {
    if (p.gid == null) {
      units.push({ row: p, members: [p], subs: [] });
    } else if (byGid.has(p.gid)) {
      byGid.get(p.gid).push(p);
    } else {
      // members and subs are the same array on purpose: a group's members ARE
      // the rows shown under it. `row` is filled in once the group is complete.
      const members = [p];
      byGid.set(p.gid, members);
      units.push({ members, subs: members });
    }
  }
  for (const u of units) {
    if (!u.subs.length) continue;
    u.subs.sort(bySubRow);
    u.row = summaryOf(u.subs, groupCost.get(u.subs[0].gid));
  }
  return units;
}

// --- rendering -------------------------------------------------------------

/** One <tr>. `cls` marks a summary row or a sub-row; a lone permit gets neither. */
const rowHtml = (p, cls) =>
  `<tr${cls ? ` class="${cls}"` : ""}>` +
  COLUMNS.map((c) => {
    // A group's cost is the project's, not any permit's, so it is not shown:
    // the sub-rows below carry the prices that were actually filed. It still
    // orders the group when the table is sorted by cost (see summaryOf).
    const raw =
      cls === "grp" && c.key === "cost" ? null :
      c.key === "_project" ? projectOf(p) : p[c.key];
    const text = c.fmt ? c.fmt(raw) : raw == null ? "—" : raw;
    return `<td${c.cls ? ` class="${c.cls}"` : ""}>${esc(text)}</td>`;
  }).join("") +
  "</tr>";

function render() {
  const f = matches(readFilters());
  // A group is kept whole: if any of its permits matches, the project shows
  // with all of them. Matching on the members rather than on the summary row
  // matters for "since", whose summary date is the earliest of the group.
  const units = UNITS.filter((u) => u.members.some(f)).sort((a, b) => compare(a.row, b.row));
  const shown = units.slice(0, state.limit);

  $("head").innerHTML = COLUMNS.map(
    (c) =>
      `<th data-key="${c.key}">${esc(c.label)}` +
      (state.sort === c.key ? (state.dir === 1 ? " ▲" : " ▼") : "") +
      `</th>`
  ).join("");

  $("body").innerHTML = shown
    .map((u) =>
      u.subs.length
        ? rowHtml(u.row, "grp") + u.subs.map((p) => rowHtml(p, "sub")).join("")
        : rowHtml(u.row)
    )
    .join("");

  // Projects, then permits: the table is a list of projects, and a reader who
  // counted the rows on screen should be able to see where the difference went.
  const permits = units.reduce((n, u) => n + u.members.length, 0);
  $("count").textContent = units.length
    ? `Showing ${shown.length.toLocaleString()} of ${units.length.toLocaleString()} ` +
      `matching projects (${permits.toLocaleString()} permits)`
    : "No permits match these filters.";
  $("more").hidden = shown.length >= units.length;

  syncAreaBounds();

  // An x that would do nothing is noise, so it only appears on a set filter.
  for (const b of $("filter-form").querySelectorAll("[data-clears]"))
    b.hidden = !isSet(b.dataset.clears.split(" "));
}

function resetPage() {
  state.limit = PAGE;
  syncUrl();
  render();
}

// --- URL state -------------------------------------------------------------
// The form is the filter state, and FormData -> URLSearchParams serializes it
// directly, repeated keys and all. The address input has no name, so it never
// reaches the URL: a shared link carries the cohort, not the person's address.

/** Rewrite the query string from the form. replaceState, so filtering does not
    fill the back button with one entry per click. */
function syncUrl() {
  const params = new URLSearchParams();
  for (const [k, v] of new FormData($("filter-form"))) if (v !== "") params.append(k, v);
  history.replaceState(null, "", params.toString() ? "?" + params : location.pathname);
}

/** Reset just the named form fields — the per-filter x buttons. */
function clearFields(names) {
  for (const el of $("filter-form").elements) {
    if (!names.includes(el.name)) continue;
    if (el.type === "checkbox") el.checked = false;
    else if (el.multiple) for (const o of el.options) o.selected = false;
    else el.value = "";
  }
}

/** Does any of these fields currently hold a value? Drives the x buttons. */
const isSet = (names) =>
  [...$("filter-form").elements].some(
    (el) =>
      names.includes(el.name) &&
      (el.type === "checkbox" ? el.checked : el.multiple ? el.selectedOptions.length : el.value !== "")
  );

/**
 * Keep the living-area range self-consistent: hide options in each select that
 * would invert the range. The "any" option always stays, so either end can be
 * cleared, and a selected option is never hidden — an inconsistent pair from a
 * hand-edited URL should read oddly rather than look blank.
 */
function syncAreaBounds() {
  const min = $("area-min").value;
  const max = $("area-max").value;
  for (const o of $("area-min").options)
    o.hidden = o.value !== "" && max !== "" && +o.value > +max && !o.selected;
  for (const o of $("area-max").options)
    o.hidden = o.value !== "" && min !== "" && +o.value < +min && !o.selected;
}

/** Populate the form from the query string. Must run after buildControls(),
    since it can only select options that already exist. */
function applyUrl() {
  const params = new URLSearchParams(location.search);
  if (![...params.keys()].length) return;

  for (const el of $("filter-form").elements) {
    if (!el.name) continue;
    const want = params.getAll(el.name);
    if (el.type === "checkbox") el.checked = want.includes(el.value);
    else if (el.multiple) for (const o of el.options) o.selected = want.includes(o.value);
    else el.value = want[0] ?? "";
  }
}

// --- controls --------------------------------------------------------------

/** Distinct non-null values of a column, ascending — the bucket grid actually present. */
const buckets = (key) =>
  [...new Set(PERMITS.map((p) => p[key]).filter((v) => v != null))].sort((a, b) => a - b);

/** Options set by JS must carry no `selected` attribute, or form.reset() keeps them. */
function fillOptions(el, values, fmt) {
  el.innerHTML =
    `<option value="">any</option>` +
    values.map((v) => `<option value="${esc(v)}">${esc(fmt(v))}</option>`).join("");
}

function buildControls() {
  $("flags").innerHTML = FLAGS.map(
    ([f, label]) =>
      `<label><input type="checkbox" name="flags" value="${f}"> ${esc(label)}</label>`
  ).join("");

  $("facets").innerHTML = FACETS.map(
    ([key, label]) =>
      `<span class="facet"><span class="facet-head">` +
      `<label for="f-${key}">${esc(label)}</label>` +
      `<button type="button" class="x" data-clears="${key}" ` +
      `title="Clear ${esc(label.toLowerCase())}" aria-label="Clear ${esc(label.toLowerCase())}">&times;</button>` +
      `</span><select id="f-${key}" name="${key}" multiple size="5">` +
      META.facets[key].map((v) => `<option value="${esc(v)}">${esc(v)}</option>`).join("") +
      `</select></span>`
  ).join("");

  const areas = buckets("res_area");
  fillOptions($("year-built"), buckets("year_built"), (v) => v + "s");
  fillOptions($("area-min"), areas, num);
  fillOptions($("area-max"), areas, num);

  // Permit years, newest first — "since 2025" is a likelier pick than "since 2014".
  // Never seeded by applyAddress: this is about the project, not the property.
  const permitYears = [...new Set(PERMITS.map((p) => p.date && p.date.slice(0, 4)).filter(Boolean))]
    .sort()
    .reverse();
  fillOptions($("since"), permitYears, (v) => v);

  // One listener for every filter control, present and future.
  const form = $("filter-form");
  form.addEventListener("change", resetPage);
  // No submit button, but Enter in the address input would navigate away.
  form.addEventListener("submit", (e) => e.preventDefault());

  form.addEventListener("click", (e) => {
    const names = e.target.dataset.clears;
    if (!names) return;
    clearFields(names.split(" "));
    resetPage();
  });

  $("clear").addEventListener("click", () => {
    form.reset();
    $("addr-note").textContent = "";
    resetPage();
  });

  $("head").addEventListener("click", (e) => {
    const key = e.target.dataset.key;
    if (!key) return;
    if (state.sort === key) state.dir *= -1;
    else {
      state.sort = key;
      state.dir = COLUMNS.find((c) => c.key === key).numeric ? -1 : 1;
    }
    resetPage();
  });

  $("more").addEventListener("click", () => {
    state.limit += PAGE;
    render();
  });

}

/** Needs meta.json only — 1.5 KB, so the header paints without the big files. */
function buildCards() {
  // Wireframe order, not meta.json key order.
  $("cards").innerHTML = ["hp", "panel", "solar"]
    .map((k) => META.headline[k])
    .map(
      (h) =>
        `<div class="card"><div class="card-label">${esc(h.label)}</div>` +
        `<div class="card-value">${money(h.value)}${esc(h.unit)}</div>` +
        `<div class="card-n">citywide median; n=${h.n.toLocaleString()}</div></div>`
    )
    .join("");
}

// --- address prefill -------------------------------------------------------
// addresses.json cannot be joined back to permits (the pipeline enforces that).
// The address only seeds the property filters; they stay editable afterward.

function setupAddress() {
  const input = $("addr");
  const list = $("addr-list");

  input.addEventListener("input", () => {
    const hit = BY_ADDR.get(input.value.trim().toUpperCase());
    if (hit) return applyAddress(hit);

    const q = input.value.trim().toLowerCase();
    const opts = q.length < 2 ? [] : ADDRESSES.filter((a) => a.norm.startsWith(q)).slice(0, 10);
    list.innerHTML = opts.map((a) => `<option value="${esc(a.addr)}">`).join("");
  });
}

/** Seeds the controls, which are the state — so there is nothing else to update. */
function applyAddress(a) {
  for (const o of $("f-prop_class").options) o.selected = o.value === a.prop_class;
  $("year-built").value = a.year_built ?? "";
  $("area-min").value = $("area-max").value = a.res_area ?? "";

  $("addr-note").textContent =
    `${a.prop_class ?? "unknown type"}, built ${a.year_built ? a.year_built + "s" : "?"}, ` +
    `${a.res_area ? num(a.res_area) + " sqft" : "unknown area"}`;
  resetPage();
}

// --- boot ------------------------------------------------------------------
// All three requests start together — no waterfall — but the page renders in
// stages as they land, so the 1.5 KB header is not held up by the 3 MB table.

const fetchJson = (u) =>
  fetch(u).then((r) => {
    if (!r.ok) throw new Error(`${u}: ${r.status}`);
    return r.json();
  });

const metaReq = fetchJson("data/meta.json");
const permitsReq = fetchJson("data/permits.json");
const addressesReq = fetchJson("data/addresses.json");

const fail = (err) => {
  $("count").textContent = `Could not load data: ${err.message}`;
  console.error(err);
};

// Stage 1 — headline cards, as soon as meta.json arrives.
metaReq
  .then((meta) => {
    META = meta;
    buildCards();
  })
  .catch(fail);

// Stage 2 — the filters and the table. The controls are built in one shot so
// there is never a half-populated form for applyUrl() to race against.
Promise.all([metaReq, permitsReq])
  .then(([, permits]) => {
    PERMITS = toObjects(permits);
    UNITS = buildUnits(PERMITS, new Map(permits.groups.rows));
    buildControls();
    applyUrl();
    render();
  })
  .catch(fail);

// Stage 3 — the address box, which is the only thing addresses.json feeds.
addressesReq
  .then((addresses) => {
    ADDRESSES = toObjects(addresses);
    BY_ADDR = new Map(ADDRESSES.map((a) => [a.addr.toUpperCase(), a]));
    setupAddress();
    $("addr").disabled = false;
  })
  .catch(fail);
