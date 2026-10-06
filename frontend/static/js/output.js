let PAYLOAD = null;

// Same reference-counted overlay pattern as output_details.js's setLoadingState - kept as its
// own copy here rather than shared, matching how this codebase splits one JS file per template.
let loadingCounter = 0;
const loadingOverlay = () => document.getElementById("loadingOverlay");
const loadingOverlayText = () => document.getElementById("loadingOverlayText");

/** Toggle the shared loading overlay while the output payload/charts are being fetched. */
function setLoadingState(isLoading, text) {
  const overlay = loadingOverlay();
  if (!overlay) return;

  if (isLoading) {
    loadingCounter += 1;
    if (text && loadingOverlayText()) {
      loadingOverlayText().textContent = text;
    }
    overlay.style.display = "block";
    return;
  }

  loadingCounter = Math.max(loadingCounter - 1, 0);
  if (loadingCounter === 0) {
    overlay.style.display = "none";
  }
}

/** Append the demo scenario query string to app-local API URLs when present. */
function withOutputDataQuery(path) {
  const query = window.outputDataQuery || "";
  if (!query) return path;
  return `${path}${path.includes("?") ? "&" + query.slice(1) : query}`;
}

/** Load the current parsed output payload before charts and selectors are initialized. */
async function fetchOutputData() {
  const res = await fetch(withOutputDataQuery("/output-data"));
  if (!res.ok) throw new Error("output-data failed");
  PAYLOAD = await res.json();
}

/** Return all parsed runs for a raw SIM, CAB, or PRO dataset. */
function getRuns(ds) {
  return PAYLOAD?.runs?.[ds] || [];
}

/** Return KPI labels for a raw output dataset. */
function getLabels(ds) {
  return PAYLOAD?.labels?.[ds] || {};
}

/** Return KPI labels for paired CAB/PRO comparison charts. */
function getPairedLabels() {
  return PAYLOAD?.paired?.labels || {};
}

/** Read the currently selected run indices from the multi-select control. */
function getSelectedRunIndices() {
  const sel = document.getElementById("runSelect");
  return Array.from(sel?.selectedOptions || []).map(o => parseInt(o.value, 10));
}

/** Populate the run selector for raw SIM/CAB/PRO datasets and disable it for paired charts. */
function fillRunSelect() {
  const ds = document.getElementById("dataset")?.value;
  const select = document.getElementById("runSelect");
  if (!select) return;

  if (ds === "paired") {
    select.innerHTML = "";
    select.size = 1;
    select.disabled = true;
    return;
  }

  select.disabled = false;
  select.size = 4;

  const runs = getRuns(ds);
  select.innerHTML = "";
  runs.forEach((r, idx) => {
    const opt = document.createElement("option");
    opt.value = String(idx);
    opt.textContent = r.run || `Run ${idx + 1}`;
    select.appendChild(opt);
  });

  for (let i = 0; i < select.options.length; i++) {
    select.options[i].selected = true;
  }
}

/** Populate KPI axis selectors based on the selected dataset and available labels. */
function fillKpiSelects() {
  const ds = document.getElementById("dataset")?.value;
  const xSel = document.getElementById("xVar");
  const ySel = document.getElementById("yVar");
  const y2Sel = document.getElementById("yVar2");
  if (!xSel || !ySel || !y2Sel) return;

  /** Fill one KPI select while preserving the previous selection where possible. */
  function fillSelect(sel, keys, labels, includeNone = false) {
    const current = sel.value;
    sel.innerHTML = "";

    if (includeNone) {
      const noneOpt = document.createElement("option");
      noneOpt.value = "(none)";
      noneOpt.textContent = "(none)";
      sel.appendChild(noneOpt);
    }

    keys.forEach(k => {
      const opt = document.createElement("option");
      opt.value = k;
      opt.textContent = labels?.[k] || k;
      sel.appendChild(opt);
    });

    if (current && Array.from(sel.options).some(o => o.value === current)) {
      sel.value = current;
    } else if (includeNone) {
      sel.value = "(none)";
    } else if (sel.options.length) {
      sel.value = sel.options[0].value;
    }
  }

  if (ds === "paired") {
    const labels = getPairedLabels();
    const keys = Object.keys(labels).filter(k => k !== "numCabs");
    // demo-mode-only guard rail (empty/absent outside demo mode): KPIs that stay visible in the
    // table but make a misleading chart axis (see frontend/demo_chart_exclusions.txt).
    const chartIneligible = new Set(PAYLOAD?.paired?.chartIneligible || []);
    const chartKeys = keys.filter(k => !chartIneligible.has(k));

    xSel.innerHTML = `<option value="numCabs">${labels["numCabs"] || "Number of Cabs"}</option>`;
    xSel.value = "numCabs";

    fillSelect(ySel, chartKeys, labels, false);
    fillSelect(y2Sel, chartKeys, labels, true);
    // Default to a pairing that holds up as a genuine (non-degenerate) tradeoff curve in the
    // Pareto view: rejects (service quality) vs. dailyFleetCostEur (fleet cost) both vary
    // independently across the whole growth range, unlike e.g. profit/revenue which saturate
    // together with rejects and collapse the frontier to a single point.
    if (chartKeys.includes("rejects")) {
      ySel.value = "rejects";
    }
    y2Sel.value = chartKeys.includes("dailyFleetCostEur") ? "dailyFleetCostEur" : "(none)";
    return;
  }

  const labels = getLabels(ds);
  const keys = Object.keys(labels);

  if (ds === "sim") {
    xSel.innerHTML = `<option value="index">${labels["index"] || "Step Index"}</option>`;
    xSel.value = "index";
    fillSelect(ySel, keys, labels, false);
    fillSelect(y2Sel, keys, labels, true);
    return;
  }

  // cab / pro
  fillSelect(xSel, keys, labels, true);
  fillSelect(ySel, keys, labels, false);
  fillSelect(y2Sel, keys, labels, true);
}

/** Draw the SIM iteration chart using the selected KPI axis. */
async function drawSIM() {
  const yKey = document.getElementById("yVar")?.value;
  const res = await fetch(withOutputDataQuery(`/chart-data?dataset=sim&y1=${encodeURIComponent(yKey || "")}`));
  if (!res.ok) return;
  const data = await res.json();
  Plotly.newPlot("chart", data.traces, data.layout, { responsive: true });
}

/** Draw a scatter or bar chart for one raw output dataset using the selected KPI axes. */
function drawRunScatterOrBar(dataset) {
  const xKey = document.getElementById("xVar")?.value;
  const yKey = document.getElementById("yVar")?.value;
  const labels = getLabels(dataset);
  const indices = getSelectedRunIndices();
  const runs = getRuns(dataset);

  const selRuns = indices.map(i => runs[i]).filter(Boolean);
  const names = selRuns.map((r, idx) => r.run || `Run ${idx + 1}`);

  if (xKey === "(none)") {
    const y = selRuns.map(r => (r.kpis?.[yKey] ?? null));
    Plotly.newPlot("chart", [{ x: names, y, type: "bar", name: labels[yKey] || yKey }], {
      margin: { l: 60, r: 30, t: 30, b: 80 },
      xaxis: { title: "Run" },
      yaxis: { title: labels[yKey] || yKey },
    }, { displayModeBar: true, responsive: true });
    return;
  }

  const x = selRuns.map(r => (r.kpis?.[xKey] ?? null));
  const y = selRuns.map(r => (r.kpis?.[yKey] ?? null));
  Plotly.newPlot("chart", [{
    x, y, mode: "markers+text", type: "scatter",
    text: names, textposition: "top center"
  }], {
    margin: { l: 60, r: 30, t: 30, b: 60 },
    xaxis: { title: labels[xKey] || xKey },
    yaxis: { title: labels[yKey] || yKey },
  }, { displayModeBar: true, responsive: true });
}

/** Draw a CAB KPI chart using the shared raw-dataset chart renderer. */
function drawCAB() { drawRunScatterOrBar("cab"); }

/** Draw a PRO KPI chart using the shared raw-dataset chart renderer. */
function drawPRO() { drawRunScatterOrBar("pro"); }

/** Find a paired dashboard row by its detailIdx (raw output-run index), not array position. */
function findPairedRowByDetailIdx(idx) {
  const runs = PAYLOAD?.paired?.runs || [];
  return runs.find(r => r.detailIdx === idx) ?? null;
}

// Explicit key -> group overrides for the summary table. Request-outcome, cost, and energy KPIs
// are unprefixed camelCase keys, so the sim_/cab_/pro_ prefix fallback below would put them all
// into "Weitere".
const SERVICE_QUALITY_KEYS = new Set(["rejects", "rejectionRate"]);
// totalEnergyCostEur is a direct addend of dailyFleetCostEur (alongside dailyVehicleCostEur),
// so the whole cost chain sits in one group that reads top-to-bottom as the arithmetic it
// represents.
const COST_KEYS = new Set([
  "dailyVehicleCostEur", "totalEnergyCostEur", "dailyFleetCostEur", "totalRevenueEur",
  "profitEur", "averageCustomerTripCostEur", "averageProfitPerTripEur",
]);

/** Group a paired-dashboard KPI key for the summary table: explicit overrides first (service
 * quality / cost / energy), then the sim_/cab_/pro_ prefix as a fallback for anything else. */
function pairedKeyGroup(key) {
  if (SERVICE_QUALITY_KEYS.has(key)) return "Servicequalität";
  if (COST_KEYS.has(key)) return "Kosten";
  if (key.startsWith("sim_")) return "Simulation";
  if (key.startsWith("cab_")) return "Cab";
  if (key.startsWith("pro_")) return "PRO";
  return "Weitere";
}

/** Return true if a higher value of this (possibly sim_/cab_/pro_-prefixed) metric is better. */
function metricPrefersHigher(key) {
  const higher = PAYLOAD?.paired?.higherIsBetter || [];
  let base = key;
  for (const prefix of ["sim_", "cab_", "pro_"]) {
    if (key.startsWith(prefix)) { base = key.slice(prefix.length); break; }
  }
  return higher.includes(base);
}

/** Look up the row currently chosen in a fleet selector, or null if unset ("kein Vergleich"). */
function currentPairedRow(selectId) {
  const val = document.getElementById(selectId)?.value;
  if (!val) return null;
  return findPairedRowByDetailIdx(parseInt(val, 10));
}

/** Build the fleet-setup badge row (cabs/pro vehicles configured) for one selected iteration.
 *
 * Deliberately setup-only: how many vehicles the fleet-planning search configured for this
 * iteration, not how the simulation performed with them. Outcome/result numbers (rejects,
 * how many PRO vehicles ended up actually chaining a cab, ...) belong in the KPI table below,
 * not here - and fixed scenario facts that never vary between iterations (PRO lines, charging
 * stations, total requests) belong in the page-level banner, not in a per-iteration badge.
 */
function compositionBadgeHtml(row, prefixLabel) {
  const numCabs = row.numCabs;
  const numPros = row.pro_number_of_pros;
  const prefix = prefixLabel ? `<span class="text-muted small me-1">${prefixLabel}</span>` : "";
  return prefix + [
    `<span class="badge text-bg-secondary">Iteration ${row.detailIdx}</span>`,
    `<span class="badge text-bg-primary">${numCabs != null ? Math.round(numCabs) : "–"} Cabs</span>`,
    `<span class="badge text-bg-info text-dark">${numPros != null ? Math.round(numPros) : "–"} Pro-Fahrzeuge</span>`,
  ].join("");
}

/** Open the full schedule/map detail view for one iteration in a new tab. */
function openDetailsFor(idx) {
  if (idx == null) return;
  window.open(withOutputDataQuery(`/output/details/${idx}`), "_blank");
}

/** Confirm-guard one of the two per-dropdown "Als Eingabedaten laden" forms before it submits -
 * destructive (overwrites the live BaseData/RideData draft), so it needs an explicit OK. */
function wireLoadIterationInputForm(formId, fieldId) {
  document.getElementById(formId)?.addEventListener("submit", (event) => {
    const idx = document.getElementById(fieldId)?.value;
    const confirmed = window.confirm(
      `Flotte/Fahrtanfragen aus Iteration ${idx} als Basis-/Fahrtdaten übernehmen?\n\n` +
      "Das ersetzt den aktuellen Entwurf auf der Daten-Upload-Seite (aktuell geladene bzw. " +
      "manuell bearbeitete Basis-/Fahrtdaten gehen dabei verloren)."
    );
    if (!confirmed) {
      event.preventDefault();
    }
  });
}

/** Render the inline KPI summary (and optional side-by-side comparison) for the current selection. */
function renderFleetSummary() {
  const card = document.getElementById("fleetSummaryCard");
  const badges = document.getElementById("fleetCompositionBadges");
  const tbody = document.querySelector("#fleetSummaryTable tbody");
  if (!card || !badges || !tbody) return;

  const rowA = currentPairedRow("fleetSelect");
  const rowB = currentPairedRow("fleetCompareSelect");

  // "Als Eingabedaten laden" tracks its own dropdown independently - each button/field pair
  // only exists when that dropdown actually has a selection, regardless of the other one.
  const loadInputBtnA = document.getElementById("btnLoadIterationInputA");
  const loadInputFieldA = document.getElementById("loadIterationInputIterationA");
  if (loadInputBtnA) loadInputBtnA.disabled = !rowA;
  if (loadInputFieldA) loadInputFieldA.value = rowA ? String(rowA.detailIdx) : "";
  const loadInputBtnB = document.getElementById("btnLoadIterationInputB");
  const loadInputFieldB = document.getElementById("loadIterationInputIterationB");
  if (loadInputBtnB) loadInputBtnB.disabled = !rowB;
  if (loadInputFieldB) loadInputFieldB.value = rowB ? String(rowB.detailIdx) : "";

  if (!rowA) {
    badges.innerHTML = "";
    tbody.innerHTML = "";
    return;
  }

  // Fleet composition (cabs/pros) is always shown regardless of KPI curation,
  // same principle already used for the Pareto hover text: it's structural
  // context, not a selectable KPI.
  badges.innerHTML = `<div class="d-flex flex-wrap align-items-center gap-2 mb-1">${compositionBadgeHtml(rowA, "")}</div>`
    + (rowB ? `<div class="d-flex flex-wrap align-items-center gap-2">${compositionBadgeHtml(rowB, "Vergleich:")}</div>` : "");

  const labels = getPairedLabels();
  // numCabs/pro_number_of_pros stay out of the table since they're already shown as
  // fleet-setup badges above; pro_number_of_active_pros is a result, not setup, so it
  // stays in the table like any other KPI.
  const OMIT = new Set(["numCabs", "pro_number_of_pros"]);
  const groups = {};
  Object.keys(labels).filter(k => !OMIT.has(k)).forEach(key => {
    const g = pairedKeyGroup(key);
    if (!groups[g]) groups[g] = [];
    groups[g].push(key);
  });

  const fmt = val => typeof val === "number"
    ? val.toLocaleString(undefined, { maximumFractionDigits: 3 })
    : (val ?? "–");

  // "Details öffnen" always lives here in the header, one column per chosen iteration,
  // whether comparing or not - previously there was a separate standalone button that only
  // existed for the single-iteration case and got hidden while comparing, which made it
  // unclear which iteration a shared button would even open once two were selected.
  let html = `<tr class="table-light">
    <th>KPI</th>
    <th class="text-end">Iteration ${rowA.detailIdx}
      <button type="button" class="btn btn-sm btn-outline-secondary py-0 px-1 ms-1" onclick="openDetailsFor(${rowA.detailIdx})" title="Details öffnen">↗</button>
    </th>`;
  if (rowB) {
    html += `<th class="text-end">Iteration ${rowB.detailIdx}
        <button type="button" class="btn btn-sm btn-outline-secondary py-0 px-1 ms-1" onclick="openDetailsFor(${rowB.detailIdx})" title="Details öffnen">↗</button>
      </th>
      <th class="text-end">Δ (B−A)</th>`;
  }
  html += `</tr>`;

  ["Servicequalität", "Kosten", "Cab", "PRO", "Simulation", "Weitere"].forEach(g => {
    const keys = groups[g];
    if (!keys || !keys.length) return;
    html += `<tr><th colspan="${rowB ? 4 : 2}" class="small text-uppercase text-muted pt-3">${g}</th></tr>`;
    keys.forEach(key => {
      const valA = rowA[key];
      if (!rowB) {
        html += `<tr><td>${labels[key]}</td><td class="text-end fw-semibold">${fmt(valA)}</td></tr>`;
        return;
      }
      const valB = rowB[key];
      let deltaHtml = "–";
      if (typeof valA === "number" && typeof valB === "number") {
        const delta = valB - valA;
        const improved = delta === 0 ? null : (metricPrefersHigher(key) ? delta > 0 : delta < 0);
        const cls = improved == null ? "text-muted" : (improved ? "text-success" : "text-danger");
        const sign = delta > 0 ? "+" : "";
        const pct = valA ? ` (${sign}${((delta / Math.abs(valA)) * 100).toFixed(1)}%)` : "";
        deltaHtml = `<span class="${cls}">${sign}${delta.toLocaleString(undefined, { maximumFractionDigits: 3 })}${pct}</span>`;
      }
      html += `<tr><td>${labels[key]}</td><td class="text-end">${fmt(valA)}</td><td class="text-end">${fmt(valB)}</td><td class="text-end">${deltaHtml}</td></tr>`;
    });
  });
  tbody.innerHTML = html;
}

/** Populate the fleet/comparison selectors from paired runs, ordered by iteration (detailIdx). */
function fillFleetSelect() {
  const sel = document.getElementById("fleetSelect");
  const compareSel = document.getElementById("fleetCompareSelect");
  const card = document.getElementById("fleetSummaryCard");
  if (!sel || !compareSel || !card) return;
  const runs = (PAYLOAD?.paired?.runs || []).slice().sort((a, b) => (a.detailIdx ?? 0) - (b.detailIdx ?? 0));
  const optionsHtml = runs.map(r => {
    const cabs = r.numCabs != null ? Math.round(r.numCabs) : "–";
    const pros = r.pro_number_of_pros != null ? Math.round(r.pro_number_of_pros) : "–";
    return `<option value="${r.detailIdx}">Iteration ${r.detailIdx} — ${cabs} Cabs, ${pros} Pros</option>`;
  }).join("");
  sel.innerHTML = optionsHtml;
  compareSel.innerHTML = `<option value="">– kein Vergleich –</option>` + optionsHtml;
  card.hidden = runs.length === 0;
}

/** Draw the paired dashboard charts and wire plot clicks to the output detail view. */
async function drawPaired() {
  const y1Key = document.getElementById("yVar")?.value;
  const y2Key = document.getElementById("yVar2")?.value;
  const view = document.getElementById("chartView")?.value || "progression";

  const res = await fetch(withOutputDataQuery(`/chart-data?dataset=paired&y1=${encodeURIComponent(y1Key || "")}&y2=${encodeURIComponent(y2Key || "")}&view=${encodeURIComponent(view)}`));
  if (!res.ok) return;

  const data = await res.json();
  if (!data.chartMain) return;

  Plotly.newPlot("chartMain", data.chartMain.traces, data.chartMain.layout, { responsive: true }).then(gd => {
    gd.on("plotly_click", ev => {
      const p = ev?.points?.[0];
      const idx = p?.customdata ?? p?.pointIndex ?? p?.pointNumber ?? p?.x;
      if (idx == null) return;
      const sel = document.getElementById("fleetSelect");
      if (sel) sel.value = String(idx);
      renderFleetSummary();
    });
  });

  const utilChartEl = document.getElementById("chartUtil");
  if (!window.demoMode && data.chartUtil && utilChartEl) {
    Plotly.newPlot(utilChartEl, data.chartUtil.traces, data.chartUtil.layout, { responsive: true });
  }
}

/** Dispatch chart rendering to the correct dataset-specific draw function. */
function redraw() {
  const ds = document.getElementById("dataset")?.value;
  if (ds === "sim") return drawSIM();
  if (ds === "cab") return drawCAB();
  if (ds === "pro") return drawPRO();
  return drawPaired();
}

/** Upload all selected solver output files that match the expected naming pattern. Returns the
 * parsed response on success (so the caller can navigate to the job it created/updated), or null
 * on failure. */
async function uploadMatchingFiles(selectedFiles) {
  const formData = new FormData();
  for (const f of (selectedFiles || [])) {
    formData.append("files[]", f, f.webkitRelativePath || f.name);
  }

  const resp = await fetch("/upload-output-auto", { method: "POST", body: formData });
  const text = await resp.text();

  let data = null;
  try { data = JSON.parse(text); } catch { data = null; }

  if (!resp.ok || !data || !["ok", "success"].includes((data.status || "").toLowerCase())) {
    alert("Upload fehlgeschlagen: " + (data?.message || resp.statusText || resp.status));
    return null;
  }
  return data;
}

// Matches every schema-compliant per-iteration file type the server actually recognizes (see
// UPLOAD_REQUIRED_FILE_TYPES/UPLOAD_OPTIONAL_FILE_TYPES in app.py) plus a bare/prefixed
// simulation_metadata.json - keep these two lists in sync with the server's own, or files that
// upload_output_auto() would happily accept never even make it off this page.
const OUTPUT_UPLOAD_REQUIRED_RX = /(?:^|_)(input_base_file|input_req_file|output_cab|output_sim|output_pro)_\d+\.json$/i;
const OUTPUT_UPLOAD_OPTIONAL_RX = /(?:^|_)(past_entries_fleet|request_results_log)_\d+\.json$/i;
const OUTPUT_UPLOAD_METADATA_RX = /(?:^|_)simulation_metadata\.json$/i;

/** Filter an arbitrary file list down to solver outputs and matching input files, then import
 * them automatically. Accepts any FileList/array - not just the folder-picker input's own -
 * so both the button (input.files) and drag-and-drop (event.dataTransfer.files) share this. */
async function uploadAuto(fileList) {
  const dbg = document.getElementById("uploadDebug");
  if (dbg) dbg.textContent = "";

  const files = fileList || document.getElementById("folderUpload")?.files;
  if (!files?.length) {
    alert("Bitte einen Ordner oder mehrere Dateien auswählen.");
    return;
  }

  const allJson = Array.from(files).filter(f => f.name.toLowerCase().endsWith(".json"));
  const relPath = f => (f.webkitRelativePath || f.name).replace(/\\/g, "/");
  const matches = (f, rx) => rx.test(relPath(f));
  const matching = allJson.filter(f =>
    matches(f, OUTPUT_UPLOAD_REQUIRED_RX) || matches(f, OUTPUT_UPLOAD_OPTIONAL_RX) || matches(f, OUTPUT_UPLOAD_METADATA_RX)
  );

  if (!matching.some(f => matches(f, OUTPUT_UPLOAD_REQUIRED_RX))) {
    alert("Keine passenden output_cab/output_pro/output_sim JSON-Dateien gefunden.");
    return;
  }

  const result = await uploadMatchingFiles(matching);
  if (!result) return;

  if (result.redirect_job_id) {
    // The page may currently be scoped (via ?sim_job_id=) to a different, older job - a plain
    // AJAX refresh would keep showing that job's data/metadata forever. Navigate to the job the
    // upload actually created/updated so both the charts and the server-rendered metadata panel
    // reflect it.
    window.location.href = "/output?sim_job_id=" + encodeURIComponent(result.redirect_job_id);
    return;
  }

  await fetchOutputData();
  fillRunSelect();
  fillKpiSelects();
  fillFleetSelect();
  renderFleetSummary();
  redraw();
}

/** Wire drag-and-drop of individual/multiple files onto the folder-upload dropzone. A drop only
 * stages the files into #folderUpload itself (same end state as picking them via the native file
 * dialog) - it never uploads anything by itself. Review via "Dateiliste anzeigen", then upload
 * via the existing button, same as a manual selection. Native folder drag-and-drop (dragging an
 * actual folder onto a webkitdirectory input) and the native file-picker dialog both keep working
 * entirely untouched. */
function wireOutputUploadDropzone() {
  const dropzone = document.getElementById("folderUploadDropzone");
  const input = document.getElementById("folderUpload");
  if (!dropzone || !input) return;

  ["dragenter", "dragover"].forEach(evtName => {
    dropzone.addEventListener(evtName, (event) => {
      event.preventDefault();
      dropzone.classList.add("border", "border-primary");
    });
  });
  ["dragleave", "drop"].forEach(evtName => {
    dropzone.addEventListener(evtName, (event) => {
      event.preventDefault();
      dropzone.classList.remove("border", "border-primary");
    });
  });
  dropzone.addEventListener("drop", (event) => {
    const dropped = event.dataTransfer?.files;
    if (!dropped?.length) return;
    const dt = new DataTransfer();
    Array.from(dropped).forEach(f => dt.items.add(f));
    input.files = dt.files;
  });
}

document.addEventListener("DOMContentLoaded", async () => {
  document.getElementById("btnUploadAuto")?.addEventListener("click", () => uploadAuto());
  wireOutputUploadDropzone();

  const scanBtn = document.getElementById("btnScanList");
  const dbg = document.getElementById("uploadDebug");
  let listVisible = false;

  scanBtn?.addEventListener("click", () => {
    const input = document.getElementById("folderUpload");
    if (!dbg) return;
    if (!input?.files) {
      dbg.textContent = "Keine Dateien ausgewählt.";
      return;
    }
    listVisible = !listVisible;
    if (listVisible) {
      dbg.textContent = "Gefundene Dateien:\n" + Array.from(input.files).map(f => f.name).slice(0, 500).join("\n");
      scanBtn.textContent = "Dateiliste ausblenden";
    } else {
      dbg.textContent = "";
      scanBtn.textContent = "Dateiliste anzeigen";
    }
  });

  document.getElementById("btnReset")?.addEventListener("click", async () => {
    setLoadingState(true, "Daten werden geladen …");
    await fetch("/clear-output-runs", { method: "POST" });
    await fetchOutputData();
    fillRunSelect();
    fillKpiSelects();
    fillFleetSelect();
    renderFleetSummary();
    Plotly.purge("chart");
    setLoadingState(false);
  });

  document.getElementById("dataset")?.addEventListener("change", () => { fillRunSelect(); fillKpiSelects(); redraw(); });
  document.getElementById("runSelect")?.addEventListener("change", redraw);
  document.getElementById("xVar")?.addEventListener("change", redraw);
  document.getElementById("yVar")?.addEventListener("change", redraw);
  document.getElementById("yVar2")?.addEventListener("change", redraw);
  document.getElementById("chartView")?.addEventListener("change", redraw);

  document.getElementById("fleetSelect")?.addEventListener("change", renderFleetSummary);
  document.getElementById("fleetCompareSelect")?.addEventListener("change", renderFleetSummary);

  wireLoadIterationInputForm("loadIterationInputFormA", "loadIterationInputIterationA");
  wireLoadIterationInputForm("loadIterationInputFormB", "loadIterationInputIterationB");

  setLoadingState(true, "Daten werden geladen …");
  await fetchOutputData();
  fillRunSelect();
  fillKpiSelects();
  fillFleetSelect();
  renderFleetSummary();
  await redraw();
  setLoadingState(false);
});
