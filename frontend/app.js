"use strict";

// Project-type flags, in the order they appear as checkboxes. Keys mirror
// FRONTEND_FLAGS in scripts/process_somerville_data.py.
const FLAGS = [
  ["hp", "Heat pumps"],
  ["panel", "Panel upgrade"],
  ["solar", "Rooftop solar"],
  ["hpwh", "Heat pump water heater"],
  ["ev", "EV charger"],
  ["hvac", "Other HVAC"],
  ["ess", "Battery storage"],
];

// Every one of these is "pick any of the values meta.json lists for this key",
// so they share one control builder and one predicate.
const FACETS = [
  ["prop_class", "Property type"],
  ["hood", "Neighborhood"],
  ["trade", "Permit type"],
  ["status", "Status"],
];

const money = (v) => (v == null ? "—" : "$" + Math.round(v).toLocaleString());
const num = (v) => (v == null ? "—" : v.toLocaleString());

const COLUMNS = [
  { key: "date", label: "Date" },
  { key: "_project", label: "Project" },
  { key: "desc", label: "Description", cls: "desc" },
  { key: "cost", label: "Cost", numeric: true, fmt: money },
  { key: "kw", label: "Size (kW)", numeric: true, fmt: (v) => (v == null ? "—" : v) },
  { key: "year_built", label: "Built", numeric: true, fmt: (v) => (v == null ? "—" : v + "s") },
  { key: "res_area", label: "Area (sqft)", numeric: true, fmt: num },
  { key: "prop_class", label: "Property type" },
  { key: "hood", label: "Neighborhood" },
  { key: "contractor", label: "Contractor" },
  { key: "trade", label: "Permit type" },
  { key: "status", label: "Status" },
];

const PAGE = 200;

let PERMITS = [];
let ADDRESSES = [];
let BY_ADDR = new Map();
let META = null;

const state = {
  flags: new Set(),
  facets: {},          // key -> array of selected values ([] = no constraint)
  yearMin: null, yearMax: null,
  areaMin: null, areaMax: null,
  q: "",
  sort: "date", dir: -1,
  limit: PAGE,
};

const $ = (id) => document.getElementById(id);
const esc = (s) =>
  String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

/** {cols, rows} -> array of plain objects, so later code reads p.cost not row[4]. */
const toObjects = ({ cols, rows }) =>
  rows.map((r) => Object.fromEntries(cols.map((c, i) => [c, r[i]])));

// --- filtering -------------------------------------------------------------

function matches(p) {
  if (state.flags.size && ![...state.flags].some((f) => p[f] === 1)) return false;

  for (const [key] of FACETS) {
    const sel = state.facets[key];
    if (sel.length && !sel.includes(p[key])) return false;
  }

  if (state.yearMin != null && !(p.year_built >= state.yearMin)) return false;
  if (state.yearMax != null && !(p.year_built <= state.yearMax)) return false;
  if (state.areaMin != null && !(p.res_area >= state.areaMin)) return false;
  if (state.areaMax != null && !(p.res_area <= state.areaMax)) return false;

  if (state.q && !p.desc.toLowerCase().includes(state.q)) return false;
  return true;
}

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
  const rows = PERMITS.filter(matches).sort(compare);
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

// --- controls --------------------------------------------------------------

/** Distinct non-null values of a column, ascending — the bucket grid actually present. */
const buckets = (key) =>
  [...new Set(PERMITS.map((p) => p[key]).filter((v) => v != null))].sort((a, b) => a - b);

function fillRange(el, values, fmt, blank) {
  el.innerHTML =
    `<option value="">${blank}</option>` +
    values.map((v) => `<option value="${v}">${fmt(v)}</option>`).join("");
}

function buildControls() {
  $("flags").innerHTML = FLAGS.map(
    ([f, label]) =>
      `<label><input type="checkbox" value="${f}"> ${esc(label)}</label>`
  ).join("");
  $("flags").addEventListener("change", (e) => {
    state.flags[e.target.checked ? "add" : "delete"](e.target.value);
    resetPage();
  });

  $("facets").innerHTML = FACETS.map(
    ([key, label]) =>
      `<span class="facet"><label for="f-${key}">${esc(label)}</label>` +
      `<select id="f-${key}" data-key="${key}" multiple size="5">` +
      META.facets[key].map((v) => `<option value="${esc(v)}">${esc(v)}</option>`).join("") +
      `</select></span>`
  ).join("");
  for (const [key] of FACETS) {
    state.facets[key] = [];
    $("f-" + key).addEventListener("change", (e) => {
      state.facets[key] = [...e.target.selectedOptions].map((o) => o.value);
      resetPage();
    });
  }

  const years = buckets("year_built");
  const areas = buckets("res_area");
  fillRange($("year-min"), years, (v) => v + "s", "earliest");
  fillRange($("year-max"), years, (v) => v + "s", "latest");
  fillRange($("area-min"), areas, num, "any");
  fillRange($("area-max"), areas, num, "any");

  const bind = (id, field) =>
    $(id).addEventListener("change", (e) => {
      state[field] = e.target.value === "" ? null : Number(e.target.value);
      resetPage();
    });
  bind("year-min", "yearMin");
  bind("year-max", "yearMax");
  bind("area-min", "areaMin");
  bind("area-max", "areaMax");

  $("q").addEventListener("input", (e) => {
    state.q = e.target.value.trim().toLowerCase();
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

  $("clear").addEventListener("click", clearFilters);

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

  $("meta").textContent =
    `${META.n_permits.toLocaleString()} permits, ${META.date_range[0]} to ${META.date_range[1]}. ` +
    `Somerville permit extract ${META.permit_extract}, assessor ${META.assessor_vintage}. ` +
    `Generated ${META.generated}.`;
}

function resetPage() {
  state.limit = PAGE;
  render();
}

function clearFilters() {
  state.flags.clear();
  for (const [key] of FACETS) state.facets[key] = [];
  Object.assign(state, { yearMin: null, yearMax: null, areaMin: null, areaMax: null, q: "" });
  document.querySelectorAll("#flags input").forEach((el) => (el.checked = false));
  document.querySelectorAll("#facets select").forEach((el) => (el.selectedIndex = -1));
  ["year-min", "year-max", "area-min", "area-max", "q", "addr"].forEach((id) => ($(id).value = ""));
  $("addr-note").textContent = "";
  resetPage();
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

function applyAddress(a) {
  if (a.prop_class) {
    state.facets.prop_class = [a.prop_class];
    for (const o of $("f-prop_class").options) o.selected = o.value === a.prop_class;
  }
  state.yearMin = state.yearMax = a.year_built;
  state.areaMin = state.areaMax = a.res_area;
  $("year-min").value = $("year-max").value = a.year_built ?? "";
  $("area-min").value = $("area-max").value = a.res_area ?? "";

  $("addr-note").textContent =
    `${a.prop_class ?? "unknown type"}, built ${a.year_built ? a.year_built + "s" : "?"}, ` +
    `${a.res_area ? num(a.res_area) + " sqft" : "unknown area"} — filters set to match. Adjust below.`;
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
