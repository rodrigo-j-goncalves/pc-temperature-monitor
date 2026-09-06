// Static Plotly dashboard for the temperature-monitor daemon.
// Must be served over HTTP (not opened as a file:// URL) since `fetch` of
// local files is blocked by CORS otherwise. See README for the serve command.

// Which machine's data/ directory to read -- e.g. ?data_dir=data_HC -- since
// each machine logs into its own data_<name>/ (see config_HC.yaml /
// config_JC.yaml). Defaults to plain "data" for a single-machine setup.
const DATA_DIR = new URLSearchParams(window.location.search).get("data_dir") || "data";
const CSV_PATH = `../${DATA_DIR}/temperatures.csv`;
const LATEST_JSON_PATH = `../${DATA_DIR}/latest.json`;
const LATEST_REFRESH_MS = 5000;
const CSV_REFRESH_MS = 30000;

const RANGE_BUTTONS = [
  { label: "Last day", ms: 24 * 60 * 60 * 1000 },
  { label: "Last 3 days", ms: 3 * 24 * 60 * 60 * 1000 },
  { label: "Last 7 days", ms: 7 * 24 * 60 * 60 * 1000 },
  { label: "All data", ms: null },
];

// A gap between two consecutive rows counts as "no data" (line breaks, region
// shaded) once it's more than GAP_MULTIPLIER times the data's own typical
// sampling interval (computed from the data itself, so this adapts
// automatically if sample_interval_seconds in config.yaml changes) -- with a
// floor of MIN_GAP_MS so a single slightly-late tick is never flagged.
const GAP_MULTIPLIER = 3;
const MIN_GAP_MS = 60 * 60 * 1000;

let sensorLabels = {}; // sensor_id -> human-readable label
let plotDiv = null;
let lastGaps = []; // [{start: Date, end: Date}, ...] for the currently loaded data

// Data is stored/transmitted in UTC; the dashboard displays it in the
// viewer's local time. Plotly's date axis shows whatever string it's given
// literally (no timezone awareness), so conversion happens before the data
// ever reaches it.
function toLocalNaive(date) {
  const pad = (n, w = 2) => String(n).padStart(w, "0");
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    `T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}` +
    `.${pad(date.getMilliseconds(), 3)}`
  );
}

// Gaps are found on the raw row-to-row deltas, then a null row is spliced in
// right after the last point before each gap: combined with `connectgaps:
// false` in renderChart, that breaks the line instead of drawing one long
// segment straight across days of missing data.
function computeGapThreshold(dates) {
  if (dates.length < 3) return Infinity;
  const deltas = [];
  for (let i = 1; i < dates.length; i++) deltas.push(dates[i] - dates[i - 1]);
  deltas.sort((a, b) => a - b);
  const median = deltas[Math.floor(deltas.length / 2)];
  return Math.max(MIN_GAP_MS, median * GAP_MULTIPLIER);
}

function splitOnGaps(dates, series, sensorIds, threshold) {
  const outDates = [dates[0]];
  const outSeries = {};
  for (const id of sensorIds) outSeries[id] = [series[id][0]];
  const gaps = [];
  for (let i = 1; i < dates.length; i++) {
    const delta = dates[i] - dates[i - 1];
    if (delta > threshold) {
      gaps.push({ start: dates[i - 1], end: dates[i] });
      outDates.push(new Date(dates[i - 1].getTime() + 1));
      for (const id of sensorIds) outSeries[id].push(null);
    }
    outDates.push(dates[i]);
    for (const id of sensorIds) outSeries[id].push(series[id][i]);
  }
  return { dates: outDates, series: outSeries, gaps };
}

function parseCsv(text) {
  const lines = text.split(/\r\n|\n/).filter((line) => line.length > 0);
  if (lines.length === 0) {
    return { timestamps: [], sensorIds: [], series: {}, gaps: [] };
  }
  const header = lines[0].split(",");
  const sensorIds = header.slice(1);
  const dates = [];
  const rawSeries = {};
  for (const id of sensorIds) rawSeries[id] = [];
  for (let i = 1; i < lines.length; i++) {
    const cols = lines[i].split(",");
    if (cols.length !== header.length) continue; // skip a possibly torn last line
    dates.push(new Date(cols[0]));
    for (let j = 0; j < sensorIds.length; j++) {
      const raw = cols[j + 1];
      rawSeries[sensorIds[j]].push(raw === "" ? null : Number(raw));
    }
  }
  if (dates.length === 0) {
    return { timestamps: [], sensorIds, series: rawSeries, gaps: [] };
  }
  const threshold = computeGapThreshold(dates);
  const { dates: splitDates, series: splitSeries, gaps } = splitOnGaps(
    dates,
    rawSeries,
    sensorIds,
    threshold
  );
  const timestamps = splitDates.map(toLocalNaive);
  return { timestamps, sensorIds, series: splitSeries, gaps };
}

function friendlyLabel(sensorId) {
  return sensorLabels[sensorId] || sensorId;
}

function setStatus(message) {
  const el = document.getElementById("status-banner");
  if (message) {
    el.textContent = message;
    el.hidden = false;
  } else {
    el.hidden = true;
  }
}

async function fetchLatest() {
  try {
    const res = await fetch(LATEST_JSON_PATH, { cache: "no-store" });
    if (!res.ok) throw new Error(res.statusText);
    setStatus(null);
    return await res.json();
  } catch (err) {
    setStatus("Cannot read latest.json -- is the daemon running?");
    return null;
  }
}

async function fetchCsv() {
  try {
    const res = await fetch(CSV_PATH, { cache: "no-store" });
    if (!res.ok) throw new Error(res.statusText);
    setStatus(null);
    return parseCsv(await res.text());
  } catch (err) {
    setStatus("Cannot read temperatures.csv -- is the daemon running?");
    return null;
  }
}

function renderCurrentValues(latest) {
  sensorLabels = {};
  for (const [id, info] of Object.entries(latest.sensors)) {
    sensorLabels[id] = info.label;
  }
  const container = document.getElementById("current-values");
  container.innerHTML = "";
  for (const id of Object.keys(latest.sensors).sort()) {
    const info = latest.sensors[id];
    const card = document.createElement("div");
    card.className = "sensor-card";
    card.innerHTML =
      `<div class="sensor-label" title="${info.label}">${info.label}</div>` +
      `<div class="sensor-value">${info.value_c.toFixed(1)}&deg;C</div>`;
    container.appendChild(card);
  }
  const localTime = new Date(latest.timestamp).toLocaleString();
  document.getElementById("last-updated").textContent = `Last updated: ${localTime}`;
}

function isDarkTheme() {
  return document.documentElement.getAttribute("data-theme") === "dark";
}

function gapShapes() {
  const dark = isDarkTheme();
  return lastGaps.map((g) => ({
    type: "rect",
    xref: "x",
    yref: "paper",
    x0: toLocalNaive(g.start),
    x1: toLocalNaive(g.end),
    y0: 0,
    y1: 1,
    fillcolor: dark ? "rgba(255,255,255,0.08)" : "rgba(0,0,0,0.08)",
    line: { width: 0 },
    layer: "below",
  }));
}

function gapAnnotations() {
  const dark = isDarkTheme();
  return lastGaps.map((g) => ({
    x: toLocalNaive(new Date((g.start.getTime() + g.end.getTime()) / 2)),
    y: 1,
    yref: "paper",
    yanchor: "bottom",
    text: "No data",
    showarrow: false,
    font: { size: 10, color: dark ? "#aaaaaa" : "#666666" },
  }));
}

function plotLayout() {
  const dark = isDarkTheme();
  return {
    paper_bgcolor: dark ? "#1e1e1e" : "#ffffff",
    plot_bgcolor: dark ? "#1e1e1e" : "#ffffff",
    font: { color: dark ? "#e0e0e0" : "#202020" },
    margin: { t: 30, r: 20, l: 60, b: 40 },
    xaxis: { title: "Time (local)", type: "date" },
    yaxis: { title: "Temperature (°C)" },
    legend: { orientation: "h" },
    shapes: gapShapes(),
    annotations: gapAnnotations(),
    uirevision: "keep", // preserve zoom/pan/legend visibility across data refreshes
  };
}

function renderChart(csvData) {
  lastGaps = csvData.gaps;
  const traces = csvData.sensorIds.map((id) => ({
    x: csvData.timestamps,
    y: csvData.series[id],
    name: friendlyLabel(id),
    mode: "lines",
    type: "scatter",
    connectgaps: false,
  }));
  Plotly.react(plotDiv, traces, plotLayout(), { responsive: true, displaylogo: false });
}

function applyRange(ms) {
  if (ms === null) {
    Plotly.relayout(plotDiv, { "xaxis.autorange": true });
    return;
  }
  const now = new Date();
  const from = new Date(now.getTime() - ms);
  Plotly.relayout(plotDiv, { "xaxis.range": [toLocalNaive(from), toLocalNaive(now)] });
}

function applyDateRange() {
  const fromValue = document.getElementById("range-from").value;
  const toValue = document.getElementById("range-to").value;
  if (!fromValue || !toValue) return;
  const from = new Date(`${fromValue}T00:00:00`);
  const to = new Date(`${toValue}T23:59:59.999`);
  Plotly.relayout(plotDiv, { "xaxis.range": [toLocalNaive(from), toLocalNaive(to)] });
}

function toggleTheme() {
  const next = isDarkTheme() ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", next);
  localStorage.setItem("theme", next);
  Plotly.relayout(plotDiv, plotLayout());
}

function exportPng() {
  Plotly.downloadImage(plotDiv, { format: "png", filename: "temperatures", width: 1600, height: 900 });
}

async function refreshLatest() {
  const latest = await fetchLatest();
  if (latest) renderCurrentValues(latest);
}

async function refreshCsv() {
  const csvData = await fetchCsv();
  if (csvData) renderChart(csvData);
}

function setupControls() {
  const rangeContainer = document.getElementById("range-buttons");
  for (const { label, ms } of RANGE_BUTTONS) {
    const btn = document.createElement("button");
    btn.textContent = label;
    btn.addEventListener("click", () => applyRange(ms));
    rangeContainer.appendChild(btn);
  }
  document.getElementById("theme-toggle").addEventListener("click", toggleTheme);
  document.getElementById("export-png").addEventListener("click", exportPng);
  document.getElementById("apply-range").addEventListener("click", applyDateRange);
}

async function init() {
  document.documentElement.setAttribute("data-theme", localStorage.getItem("theme") || "light");
  plotDiv = document.getElementById("chart");
  setupControls();

  await refreshLatest();
  await refreshCsv();

  setInterval(refreshLatest, LATEST_REFRESH_MS);
  setInterval(refreshCsv, CSV_REFRESH_MS);
}

document.addEventListener("DOMContentLoaded", init);
