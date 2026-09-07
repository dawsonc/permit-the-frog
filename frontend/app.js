"use strict";

// Project-type flags, in the order they appear as checkboxes. Keys mirror
// FRONTEND_FLAGS in scripts/process_somerville_data.py.
const FLAGS = [
  ["hp", "Heat pumps"],
  ["panel", "Panel upgrade"],
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
  ["hood", "Neighborhood"],
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
    hood: fd.getAll("hood"),
    year: fd.get("year"),
    areaMin: fd.get("areaMin"),
    areaMax: fd.get("areaMax"),
  };
}

/** One line per filter; an empty value means that filter is not applied. */
const matches = (f) => (p) =>
  (!f.flags.length || f.flags.some((k) => p[k] === 1)) &&
  (!f.prop_class.length || f.prop_class.includes(p.prop_class)) &&
  (!f.hood.length || f.hood.includes(p.hood)) &&
  (!f.year || Math.abs(p.year_built - f.year) <= YEAR_SPAN) &&
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

// --- rendering -------------------------------------------------------------

function render() {
  const rows = PERMITS.filter(matches(readFilters())).sort(compare);
  const shown = rows.slice(0, state.limit);

  $("head").innerHTML = COLUMNS.map(
    (c) =>
      `<th data-key="${c.key}">${esc(c.label)}` +
      (state.sort === c.key ? (state.dir === 1 ? " ▲" : " ▼") : "") +
      `</th>`
  ).join("");

  $("body").innerHTML = shown
    .map(
      (p) =>
        "<tr>" +
        COLUMNS.map((c) => {
          const raw = c.key === "_project" ? projectOf(p) : p[c.key];
          const text = c.fmt ? c.fmt(raw) : raw == null ? "—" : raw;
          return `<td${c.cls ? ` class="${c.cls}"` : ""}>${esc(text)}</td>`;
        }).join("") +
        "</tr>"
    )
    .join("");

  $("count").textContent = rows.length
    ? `Showing ${shown.length.toLocaleString()} of ${rows.length.toLocaleString()} matching permits`
    : "No permits match these filters.";
  $("more").hidden = shown.length >= rows.length;
}

function resetPage() {
  state.limit = PAGE;
  render();
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
      `<span class="facet"><label for="f-${key}">${esc(label)}</label>` +
      `<select id="f-${key}" name="${key}" multiple size="5">` +
      META.facets[key].map((v) => `<option value="${esc(v)}">${esc(v)}</option>`).join("") +
      `</select></span>`
  ).join("");

  const areas = buckets("res_area");
  fillOptions($("year-built"), buckets("year_built"), (v) => v + "s");
  fillOptions($("area-min"), areas, num);
  fillOptions($("area-max"), areas, num);

  // One listener for every filter control, present and future.
  const form = $("filter-form");
  form.addEventListener("change", resetPage);
  // No submit button, but Enter in the address input would navigate away.
  form.addEventListener("submit", (e) => e.preventDefault());

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

  // Wireframe order, not meta.json key order.
  $("cards").innerHTML = ["hp", "panel", "solar"]
    .map((k) => META.headline[k])
    .map(
      (h) =>
        `<div class="card"><div class="card-label">${esc(h.label)}</div>` +
        `<div class="card-value">${money(h.value)}${esc(h.unit)}</div>` +
        `<div class="card-n">median of ${h.n.toLocaleString()} permits</div></div>`
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

Promise.all(
  ["data/permits.json", "data/addresses.json", "data/meta.json"].map((u) =>
    fetch(u).then((r) => {
      if (!r.ok) throw new Error(`${u}: ${r.status}`);
      return r.json();
    })
  )
)
  .then(([permits, addresses, meta]) => {
    PERMITS = toObjects(permits);
    ADDRESSES = toObjects(addresses);
    META = meta;
    BY_ADDR = new Map(ADDRESSES.map((a) => [a.addr.toUpperCase(), a]));

    buildControls();
    setupAddress();
    render();
  })
  .catch((err) => {
    $("count").textContent = `Could not load data: ${err.message}`;
    console.error(err);
  });
