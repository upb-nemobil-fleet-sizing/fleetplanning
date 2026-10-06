// Fleet movement animation section on the output-details page: checks whether an animation
// already exists for this fleet size, and in normal mode offers to generate one (behind an
// explicit confirmation, since the resulting file can be several megabytes and generation can
// take a while). Generation itself runs in a background thread server-side (see
// output_details_movement_generate in app.py) - this polls for completion rather than waiting
// on one long request, the same pattern the dashboard already uses for XML upload conversion.
//
// The existence check is a HEAD request (headers only, no body) - and even once we know the
// file exists, it is never loaded into the iframe automatically. Both a first-time generation
// and viewing an already-cached file require an explicit click, since either one means pulling
// down that multi-megabyte file.
document.addEventListener("DOMContentLoaded", () => {
  const numCabs = document.body.dataset.numCabs;
  const viewUrl = withOutputDataQuery(`/output/details/movement/${numCabs}`);
  const generateUrl = withOutputDataQuery(`/output/details/movement/${numCabs}/generate`);
  const statusUrl = withOutputDataQuery(`/output/details/movement/${numCabs}/status`);
  const POLL_INTERVAL_MS = 2000;

  const section = document.getElementById("movementVizSection");
  const hint = document.getElementById("movementVizHint");
  const loadBtn = document.getElementById("movementVizLoadBtn");
  const generateBtn = document.getElementById("movementVizGenerateBtn");
  const confirmBtn = document.getElementById("movementVizConfirmBtn");
  const spinner = document.getElementById("movementVizSpinner");
  const loadSpinner = document.getElementById("movementVizLoadSpinner");
  const frame = document.getElementById("movementVizFrame");
  const elapsedEl = document.getElementById("movementVizElapsed");
  const frameHeight = frame.style.height;

  // the file itself can be tens of megabytes, so the iframe's own load (not just the
  // generate step) needs a visible indicator too, not just an instant src swap.
  // While loading, the frame is collapsed and invisible but not display:none: Firefox lays out
  // a display:none frame at the wrong size, so Leaflet measures the map wrongly and never
  // re-measures it (blank or partial map, missing vehicles). A collapsed frame keeps its width.
  function showFrame() {
    section.style.display = "";
    hint.style.display = "none";
    spinner.style.display = "none";
    loadBtn.style.display = "none";
    generateBtn.style.display = "none";
    frame.style.display = "block";
    frame.style.visibility = "hidden";
    frame.style.height = "0";
    loadSpinner.style.display = "";
    frame.onload = () => {
      loadSpinner.style.display = "none";
      frame.style.height = frameHeight;
      frame.style.visibility = "visible";
    };
    frame.src = viewUrl;
  }

  function showReadyNotLoaded() {
    section.style.display = "";
    hint.textContent = "Eine Bewegungsanimation ist verf\u00FCgbar. Die Datei kann mehrere Megabyte gro\u00DF sein.";
    hint.style.display = "";
    spinner.style.display = "none";
    loadSpinner.style.display = "none";
    generateBtn.style.display = "none";
    loadBtn.style.display = "";
  }

  function showMissing() {
    if (window.demoMode) {
      // demo mode never generates - if a curator hasn't placed the file, there is nothing to show
      section.style.display = "none";
      return;
    }
    section.style.display = "";
    hint.textContent = "F\u00FCr diese Flottenkonfiguration wurde noch keine Bewegungsanimation erzeugt.";
    hint.style.display = "";
    spinner.style.display = "none";
    loadSpinner.style.display = "none";
    loadBtn.style.display = "none";
    generateBtn.style.display = "";
  }

  function showRunning(startedAt) {
    section.style.display = "";
    hint.style.display = "none";
    loadBtn.style.display = "none";
    generateBtn.style.display = "none";
    loadSpinner.style.display = "none";
    spinner.style.display = "";
    if (startedAt) elapsedEl.dataset.startedAt = startedAt;
  }

  function showError(message) {
    spinner.style.display = "none";
    loadSpinner.style.display = "none";
    hint.textContent = message;
    hint.style.display = "";
    generateBtn.style.display = "";
  }

  function pollStatus() {
    fetch(statusUrl)
      .then(res => res.json())
      .then(body => {
        if (body.status === "completed") {
          // this completion is itself the result of an explicit generate click, so showing
          // it right away is fine - the "always require a click" rule is about a file that
          // already existed before the user did anything on this page load.
          showFrame();
        } else if (body.status === "failed") {
          showError("Erzeugung fehlgeschlagen: " + (body.error || "unbekannter Fehler"));
        } else {
          // "running" (or a not-yet-registered job right after starting) - keep waiting
          showRunning(body.startedAt);
          setTimeout(pollStatus, POLL_INTERVAL_MS);
        }
      })
      .catch(() => showError("Erzeugung fehlgeschlagen."));
  }

  // initial check: does this fleet size already have a cached animation? HEAD only - never
  // pull the file itself just to find out it exists.
  fetch(viewUrl, { method: "HEAD" })
    .then(res => (res.ok ? showReadyNotLoaded() : showMissing()))
    .catch(() => showMissing());

  loadBtn.addEventListener("click", showFrame);

  confirmBtn.addEventListener("click", () => {
    showRunning(null);
    fetch(generateUrl, { method: "POST" })
      .then(res => res.json().then(body => ({ ok: res.ok, body })))
      .then(({ ok, body }) => {
        if (!ok) {
          showError("Erzeugung fehlgeschlagen: " + (body.error || "unbekannter Fehler"));
        } else if (body.status === "completed") {
          showFrame();
        } else {
          showRunning(body.startedAt);
          // first poll right away, not after a full interval - a fast run could otherwise
          // finish before ever being checked once, and the timer would never show a value
          pollStatus();
        }
      })
      .catch(() => showError("Erzeugung fehlgeschlagen."));
  });

  // Ticks the elapsed counter every second, purely client-side - the poll above still owns
  // the authoritative status. Same MM:SS/HH:MM:SS format as the dashboard's other two
  // backgrounded-job timers (XML upload conversion, running simulations), so it never jumps
  // format against them.
  setInterval(() => {
    const startedAt = elapsedEl.dataset.startedAt;
    if (!startedAt) return;
    const started = new Date(startedAt);
    if (isNaN(started.getTime())) return;
    const totalSeconds = Math.max(0, Math.floor((Date.now() - started.getTime()) / 1000));
    const hours = Math.floor(totalSeconds / 3600);
    const minutes = Math.floor((totalSeconds % 3600) / 60);
    const seconds = totalSeconds % 60;
    const pad = (n) => String(n).padStart(2, "0");
    elapsedEl.textContent = hours > 0 ? `${pad(hours)}:${pad(minutes)}:${pad(seconds)}` : `${pad(minutes)}:${pad(seconds)}`;
  }, 1000);
});
