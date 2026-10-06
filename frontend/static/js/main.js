const DEFAULT_MAP_CENTER = [51.1315, 9.2127];
const DEFAULT_MAP_ZOOM = 5;
const MIN_OPERATION_AREA_POINTS = 3;
let map = L.map('map').setView(DEFAULT_MAP_CENTER, DEFAULT_MAP_ZOOM);
createDashboardTileLayer({maxZoom: 19, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'}).addTo(map);

let polygonPoints = [];
let polygonLayer = null;
let currentMode = null;
let cabMarkers = [];
let chargerMarkers = [];
let chainRouteLines = [];

let cabLayer = L.layerGroup().addTo(map);
let chargerLayer = L.layerGroup().addTo(map);
let proLayer = L.layerGroup().addTo(map);
let chainLayer = L.layerGroup().addTo(map);
let chainRouteLayer = L.layerGroup();
let rideMarkerLayer = L.layerGroup();
let rideLineLayer = L.layerGroup();

/** Return whether enough polygon points exist to unlock operation-area-dependent pages. */
function hasValidOperationArea() {
    return Array.isArray(polygonPoints) && polygonPoints.length >= MIN_OPERATION_AREA_POINTS;
}

/** Keep navigation guarded until the user has defined a valid operation area. */
function updateOperationAreaSecurityState() {
    const enabled = hasValidOperationArea();
    const warning = document.getElementById("operationAreaWarning");
    if (warning) {
        warning.classList.toggle("d-none", enabled);
    }

    const guardedLinks = document.querySelectorAll("a.requires-op-area");
    guardedLinks.forEach((link) => {
        if (enabled) {
            link.classList.remove("disabled");
            link.removeAttribute("aria-disabled");
            link.removeAttribute("tabindex");
            link.removeAttribute("onclick");
            link.removeAttribute("title");
        } else {
            link.classList.add("disabled");
            link.setAttribute("aria-disabled", "true");
            link.setAttribute("tabindex", "-1");
            link.setAttribute("onclick", "return false;");
            link.setAttribute("title", "Bitte zuerst Operation Area festlegen");
        }
    });
}

/** Redraw the operation-area polygon and synchronize the editable point list. */
function updatePolygonDisplay() {
    if (polygonLayer) {
        map.removeLayer(polygonLayer);
    }
    if (polygonPoints.length > 0) {
        polygonLayer = L.polygon(polygonPoints, { color: 'blue' }).addTo(map);
    }

    const list = document.getElementById("polygonList");
    if (list) {
        list.innerHTML = '';
        polygonPoints.forEach((point, index) => {
            const li = document.createElement('li');
            li.textContent = `Punkt ${index + 1}: (${point[0].toFixed(5)}, ${point[1].toFixed(5)})`;

            const btn = document.createElement('button');
            btn.className = "btn btn-sm btn-outline-danger ms-2";
            btn.innerHTML = '<span class="material-icons" style="font-size: 16px;">delete</span>';
            btn.onclick = function () {
                polygonPoints.splice(index, 1);
                updatePolygonDisplay();
                saveOperationAreaToServer();
            };

            li.appendChild(btn);
            list.appendChild(li);
        });
    }

    updateOperationAreaSecurityState();
}

/** Zoom the overview map to the current operation-area polygon. */
function focusMapOnPolygon() {
    if (!polygonPoints || polygonPoints.length === 0) return;

    const bounds = L.latLngBounds(polygonPoints);

    map.fitBounds(bounds, {
        padding: [30, 30]
    });
}

/** Enable click-to-add mode for operation-area polygon editing. */
function startPolygonMode() {
    currentMode = 'polygon';
}

/** Remove all polygon points from the map and persist the cleared operation area. */
function clearPolygon() {
    polygonPoints = [];
    if (polygonLayer) {
        map.removeLayer(polygonLayer);
        polygonLayer = null;
    }
    const list = document.getElementById("polygonList");
    if (list) list.innerHTML = '';
    updateOperationAreaSecurityState();
    saveOperationAreaToServer();
}

map.on("click", function (e) {
    if (window.readOnly) return;
    const { lat, lng } = e.latlng;

    if (currentMode === "polygon") {
        polygonPoints.push([lat, lng]);
        updatePolygonDisplay();
        saveOperationAreaToServer();
    }
});

/** Render the current fleet, charging, chaining, and ride request overlays on the overview map. */
function firstStringValue(obj, keys) {
    for (const key of keys) {
        const value = obj?.[key];
        if (value !== undefined && value !== null && value !== "") {
            return String(value);
        }
    }
    return "";
}

function parseLocationIdList(value) {
    if (Array.isArray(value)) {
        return value.map(id => String(id || "")).filter(Boolean);
    }
    const text = String(value || "").trim();
    if (!text) return [];
    try {
        const parsed = JSON.parse(text);
        if (Array.isArray(parsed)) {
            return parsed.map(id => String(id || "")).filter(Boolean);
        }
    } catch (error) {
        // Fall back to comma-separated values used by older edited routes.
    }
    return text.split(",").map(id => id.trim()).filter(Boolean);
}

function addOverviewMarkers() {
    if (!window.fleetData) return;

    cabLayer.clearLayers();
    chargerLayer.clearLayers();
    proLayer.clearLayers();
    chainLayer.clearLayers();
    chainRouteLayer.clearLayers();
    rideMarkerLayer.clearLayers();
    rideLineLayer.clearLayers();

    const icons = {
        cab: new L.Icon({
            iconUrl: '/static/vendor/leaflet-color-markers/marker-icon-blue.png',
            shadowUrl: '/static/vendor/leaflet-color-markers/marker-shadow.png',
            iconSize: [25,41], iconAnchor: [12,41]
        }),
        charger: new L.Icon({
            iconUrl: '/static/vendor/leaflet-color-markers/marker-icon-green.png',
            shadowUrl: '/static/vendor/leaflet-color-markers/marker-shadow.png',
            iconSize: [25,41], iconAnchor: [12,41]
        }),
        pro: new L.Icon({
            iconUrl: '/static/vendor/leaflet-color-markers/marker-icon-orange.png',
            shadowUrl: '/static/vendor/leaflet-color-markers/marker-shadow.png',
            iconSize: [25,41], iconAnchor: [12,41]
        }),
        chain: new L.Icon({
            iconUrl: '/static/vendor/leaflet-color-markers/marker-icon-violet.png',
            shadowUrl: '/static/vendor/leaflet-color-markers/marker-shadow.png',
            iconSize: [25,41], iconAnchor: [12,41]
        }),
        ride: new L.Icon({
            iconUrl: '/static/vendor/leaflet-color-markers/marker-icon-red.png',
            shadowUrl: '/static/vendor/leaflet-color-markers/marker-shadow.png',
            iconSize: [25,41], iconAnchor: [12,41]
        })
    };

    const d = window.fleetData;

    // Cabs
    (d.cabs || []).forEach(c => {
        const lat = c.InitialLocation?.Latitude, lng = c.InitialLocation?.Longitude;
        if (lat != null && lng != null) {
            L.marker([lat, lng], {icon: icons.cab})
                .bindPopup(`Cab: ${c.id}`)
                .addTo(cabLayer);
        }
    });

    // Charging Points
    (d.chargingPoints || []).forEach(cp => {
        const lat = cp.Location?.Latitude, lng = cp.Location?.Longitude;
        if (lat != null && lng != null) {
            L.marker([lat, lng], {icon: icons.charger})
                .bindPopup(`Charger: ${cp.id}`)
                .addTo(chargerLayer);
        }
    });

    // Pros
    (d.proSchedules || []).forEach(p => {
        const lat = p.InitialLocation?.Latitude, lng = p.InitialLocation?.Longitude;
        if (lat != null && lng != null) {
            L.marker([lat, lng], {icon: icons.pro})
                .bindPopup(`Pro: ${p.id}`)
                .addTo(proLayer);
        }
    });

    // Chaining Locations
    const chainingLocationById = {};
    (d.chainingLocations || []).forEach(loc => {
        const locId = firstStringValue(loc, ["id", "Guid", "locationId"]);
        const sLat = loc.LocationStart?.Latitude, sLng = loc.LocationStart?.Longitude;
        const eLat = loc.LocationEnd?.Latitude,   eLng = loc.LocationEnd?.Longitude;
        const startPoint = (sLat != null && sLng != null) ? [sLat, sLng] : null;
        const endPoint = (eLat != null && eLng != null) ? [eLat, eLng] : null;
        if (locId) {
            chainingLocationById[locId] = { start: startPoint, end: endPoint };
        }

        if (sLat != null && sLng != null) {
            L.marker([sLat, sLng], {icon: icons.chain})
                .bindPopup(`ChainLoc Start: ${loc.id}`)
                .addTo(chainLayer);
        }
        if (eLat != null && eLng != null) {
            L.marker([eLat, eLng], {icon: icons.chain})
                .bindPopup(`ChainLoc End: ${loc.id}`)
                .addTo(chainLayer);
        }
        if (sLat != null && sLng != null && eLat != null && eLng != null) {
            L.polyline([[sLat, sLng], [eLat, eLng]], {
                color: 'violet',
                weight: 2,
                opacity: 0.8
            }).addTo(chainLayer);
        }
    });

    if (window.demoMode) {
        (d.chainRoutes || []).forEach(route => {
            const startId = firstStringValue(route, ["StartLocation", "startLocation", "start", "fromLocation"]);
            const endId = firstStringValue(route, ["EndLocation", "endLocation", "end", "toLocation"]);
            const startLoc = chainingLocationById[startId];
            const endLoc = chainingLocationById[endId];
            const startPoint = startLoc?.start || startLoc?.end;
            const endPoint = endLoc?.end || endLoc?.start;
            if (!startPoint || !endPoint) return;

            const coords = [startPoint];
            const intermediateRaw = route.IntermediateChainingLocations || route.intermediateChainingLocations || [];
            const intermediateIds = parseLocationIdList(intermediateRaw);
            intermediateIds.forEach(id => {
                const loc = chainingLocationById[String(id || "")];
                const point = loc?.start || loc?.end;
                if (point) coords.push(point);
            });
            coords.push(endPoint);

            L.polyline(coords, {
                color: "#5b2ca3",
                weight: 4,
                opacity: 0.85,
                lineCap: "round",
            })
                .bindPopup(`Chaining Line: ${firstStringValue(route, ["Guid", "id", "routeId"]) || "ohne ID"}`)
                .addTo(chainRouteLayer);
        });
    }

    // Ride Requests
    (d.rideRequests || []).forEach(rr => {
        const sLat = rr.CurrentLocation?.Latitude, sLng = rr.CurrentLocation?.Longitude;
        const eLat = rr.TargetLocation?.Latitude,   eLng = rr.TargetLocation?.Longitude;

        if (sLat != null && sLng != null) {
            L.marker([sLat, sLng], {icon: icons.ride})
                .bindPopup(`Ride Start: ${rr.id}`)
                .addTo(rideMarkerLayer);
        }
        if (eLat != null && eLng != null) {
            L.marker([eLat, eLng], {icon: icons.ride})
                .bindPopup(`Ride End: ${rr.id}`)
                .addTo(rideMarkerLayer);
        }
        if (sLat != null && sLng != null && eLat != null && eLng != null) {
            L.polyline([[sLat, sLng], [eLat, eLng]], {
                color: 'red',
                dashArray: '4 4'
            }).addTo(rideLineLayer);
        }
    });
}

/** Persist the currently drawn operation area and time window to the Flask backend. */
function saveOperationAreaToServer() {
    const startInput = document.getElementById("startTime");
    const endInput   = document.getElementById("endTime");

    const start = startInput ? startInput.value : null;
    const end   = endInput ? endInput.value : null;

    fetch("/save-operation-area", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
            points: polygonPoints.map(p => ({ Latitude: p[0], Longitude: p[1] })),
            startTime: start,
            endTime: end
        })
    });
}

/** Persist the "use uploaded Pro schedule verbatim" toggle and update its warning banner. */
function bindUseOriginalProTimetableToggle() {
    const input = document.getElementById("useOriginalProTimetable");
    const warning = document.getElementById("useOriginalProTimetableWarning");
    if (!input) return;
    input.addEventListener("change", () => {
        if (warning) warning.classList.toggle("d-none", !input.checked);
        fetch("/set-use-original-pro-timetable", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ enabled: input.checked })
        });
    });
}

/** Show the XML-preprocessing settings panel only while the selected RideData file is XML,
 *  and the guarantee-feasible sub-fields only while that checkbox is on - JSON uploads never
 *  see these fields at all. The checkbox itself stays disabled until BaseData is actually
 *  available - already loaded server-side (window.fleetData, set from loaded_data) or selected
 *  in the neighboring BaseData field, about to be uploaded alongside (see uploadSelectedFiles,
 *  which always uploads BaseData before RideData when both are picked) - matching what
 *  /upload-ride-json itself requires, so unchecking client-side never has to guess at a server
 *  rejection after the fact. */
function wireXmlPipelineSettings() {
    const rideInput = document.getElementById("rideJsonUpload");
    const baseInput = document.getElementById("baseJsonUpload");
    const panel = document.getElementById("xmlPipelineSettings");
    const guaranteeCheckbox = document.getElementById("xml_guarantee_feasible");
    const guaranteeSettings = document.getElementById("xmlGuaranteeFeasibleSettings");
    const guaranteeHint = document.getElementById("xmlGuaranteeFeasibleHint");
    if (!rideInput || !panel) return;

    const hintDefaultText = guaranteeHint ? guaranteeHint.textContent : "";
    const hintMissingBaseDataText =
        "Erzwingt zusätzlich zum Standardpuffer eine Zeitfenster-Obergrenze, die von jedem " +
        "Fahrzeug ab Depot noch erreichbar ist. Benötigt BaseData: links zusätzlich auswählen " +
        "(wird zusammen mit den Fahrtdaten hochgeladen) oder vorher separat hochladen. Aktuell " +
        "ist keine BaseData verfügbar.";

    function hasBaseData() {
        const loadedOnServer =
            (window.fleetData?.cabs?.length > 0) && (window.fleetData?.chargingPoints?.length > 0);
        const selectedAlongside = baseInput?.files?.length > 0;
        return !!(loadedOnServer || selectedAlongside);
    }

    function syncPanel() {
        const file = rideInput.files[0];
        panel.style.display = file && file.name.toLowerCase().endsWith(".xml") ? "" : "none";
    }
    function syncGuaranteeSettings() {
        if (guaranteeSettings) {
            guaranteeSettings.style.display = guaranteeCheckbox?.checked ? "" : "none";
        }
    }
    function syncGuaranteeAvailability() {
        if (!guaranteeCheckbox) return;
        const available = hasBaseData();
        guaranteeCheckbox.disabled = !available;
        if (!available && guaranteeCheckbox.checked) {
            guaranteeCheckbox.checked = false;
            syncGuaranteeSettings();
        }
        if (guaranteeHint) {
            guaranteeHint.textContent = available ? hintDefaultText : hintMissingBaseDataText;
        }
    }

    rideInput.addEventListener("change", syncPanel);
    baseInput?.addEventListener("change", syncGuaranteeAvailability);
    guaranteeCheckbox?.addEventListener("change", syncGuaranteeSettings);
    syncPanel();
    syncGuaranteeSettings();
    syncGuaranteeAvailability();
}

document.addEventListener("DOMContentLoaded", () => {
    updateOperationAreaSecurityState();
    setTimeout(() => map.invalidateSize(), 0);
    if (window.fleetData) {
        if (window.fleetData.operationArea && window.fleetData.operationArea.points) {
            polygonPoints = window.fleetData.operationArea.points.map(p => [p.Latitude, p.Longitude]);
            updatePolygonDisplay();
        }

        if (polygonPoints.length > 0) {
            focusMapOnPolygon();
        }

        const startInput = document.getElementById("startTime");
        const endInput   = document.getElementById("endTime");

        if (startInput && window.fleetData.startTime) {
            startInput.value = window.fleetData.startTime;
        }
        if (endInput && window.fleetData.endTime) {
            endInput.value = window.fleetData.endTime;
        }
    }
    const startInput = document.getElementById("startTime");
    const endInput = document.getElementById("endTime");

    if (!window.readOnly) {
        if (startInput) startInput.addEventListener("change", saveOperationAreaToServer);
        if (endInput) endInput.addEventListener("change", saveOperationAreaToServer);
        bindUseOriginalProTimetableToggle();
        wireXmlPipelineSettings();
        wireXmlUploadStatusPolling();
        setInterval(tickXmlUploadElapsed, 1000);
    }

    const legendControl = L.control({ position: 'bottomleft' });

    legendControl.onAdd = function () {
        const div = document.getElementById("legend");
        if (div) div.style.display = "block";
        return div;
    };

    legendControl.addTo(map);

    const q = (id) => document.getElementById(id);

    function syncLegendOption(input) {
        const option = input?.closest(".map-legend-option");
        if (option) {
            option.classList.toggle("map-legend-option-active", input.checked);
        }
    }

    function bindLayerToggle(id, layer) {
        const input = q(id);
        if (!input) return;
        input.checked ? map.addLayer(layer) : map.removeLayer(layer);
        syncLegendOption(input);
        input.addEventListener("change", e => {
            e.target.checked ? map.addLayer(layer) : map.removeLayer(layer);
            syncLegendOption(e.target);
        });
    }

    bindLayerToggle("toggleCabs", cabLayer);
    bindLayerToggle("toggleChargers", chargerLayer);
    bindLayerToggle("togglePros", proLayer);
    bindLayerToggle("toggleChains", chainLayer);
    bindLayerToggle("toggleChainRoutes", chainRouteLayer);
    bindLayerToggle("toggleRides", rideMarkerLayer);
    bindLayerToggle("toggleRideLines", rideLineLayer);

    addOverviewMarkers();
});

/** Toggle the upload processing alert and disable upload controls while a request is active. */
function setUploadBusy(isBusy, message) {
    const alertBox = document.getElementById("uploadProcessingAlert");
    const alertText = document.getElementById("uploadProcessingText");
    if (alertBox) {
        if (isBusy) {
            if (alertText && message) alertText.textContent = message;
            alertBox.classList.remove("d-none");
        } else {
            alertBox.classList.add("d-none");
        }
    }

    const baseInput = document.getElementById("baseJsonUpload");
    const rideInput = document.getElementById("rideJsonUpload");
    if (baseInput) baseInput.disabled = !!isBusy;
    if (rideInput) rideInput.disabled = !!isBusy;

    const baseBtn = document.getElementById("baseUploadBtn");
    const rideBtn = document.getElementById("rideUploadBtn");
    [baseBtn, rideBtn].forEach((btn) => {
        if (!btn) return;
        if (isBusy) {
            btn.classList.add("disabled");
            btn.setAttribute("aria-disabled", "true");
        } else {
            btn.classList.remove("disabled");
            btn.removeAttribute("aria-disabled");
        }
    });
}

/** Poll the background XML-conversion job's status/error box in place - same server-rendered-
 *  fragment + poll-while-running pattern simulation_overview.html's simulationJobsSection
 *  already uses for the running-simulations table (see /upload/xml-job-fragment). The initial
 *  data-status comes from the page's own server render, so this picks a job back up correctly
 *  even after navigating away and back mid-conversion - not just right after clicking upload. */
function wireXmlUploadStatusPolling() {
    let section = document.getElementById("xmlUploadStatusSection");
    if (!section) return;

    const rideInput = document.getElementById("rideJsonUpload");
    const rideBtn = document.getElementById("rideUploadBtn");

    function isPending(el) {
        return el.dataset.status === "queued" || el.dataset.status === "running";
    }

    // A second XML upload is rejected server-side while one is in flight (never queued behind
    // it, unlike FleetPlanning jobs) - disabling the field/button here just keeps the UI from
    // inviting a click that can only ever fail, on the initial render too, not just after a poll.
    function syncRideUploadDisabled() {
        const pending = isPending(section);
        if (rideInput) rideInput.disabled = pending;
        if (rideBtn) {
            rideBtn.classList.toggle("disabled", pending);
            rideBtn.toggleAttribute("aria-disabled", pending);
        }
    }

    function refresh() {
        fetch("/upload/xml-job-fragment")
            .then((response) => response.text())
            .then((html) => {
                const wrapper = document.createElement("div");
                wrapper.innerHTML = html;
                const updated = wrapper.querySelector("#xmlUploadStatusSection");
                if (!updated) return;
                const wasPending = isPending(section);
                section.replaceWith(updated);
                section = updated;
                syncRideUploadDisabled();
                if (isPending(section)) {
                    setTimeout(refresh, 1500);
                } else if (wasPending && section.dataset.status === "completed") {
                    // just finished while being watched - reload to reflect the newly loaded
                    // RideData everywhere else on the page (map, instance summary badges).
                    location.reload();
                }
            })
            .catch(() => setTimeout(refresh, 1500));
    }

    syncRideUploadDisabled();
    if (isPending(section)) {
        setTimeout(refresh, 1500);
    }

    // Delegated on document, not the button directly - refresh() replaces the section (and any
    // button inside it) wholesale on every poll, which would silently drop a direct listener.
    document.addEventListener("click", (event) => {
        if (!event.target.closest("#xmlUploadCancelBtn")) return;
        fetch("/cancel-xml-upload", {method: "POST"}).then(refresh).catch(refresh);
    });
}

/** Ticks the elapsed counter on the XML-upload status banner every second, purely client-side -
 *  the fragment poll above still owns the authoritative status. */
function tickXmlUploadElapsed() {
    // Same MM:SS/HH:MM:SS format the running-simulations runtime counter uses (formatElapsed
    // in simulation_overview.html) - matching it means the client-side tick between polls never
    // visibly jumps format against the server-rendered runtimeText the fragment poll just showed.
    document.querySelectorAll(".js-xml-elapsed[data-started-at]").forEach((el) => {
        const startedAt = el.dataset.startedAt;
        if (!startedAt) return;
        const started = new Date(startedAt);
        if (isNaN(started.getTime())) return;
        const totalSeconds = Math.max(0, Math.floor((Date.now() - started.getTime()) / 1000));
        const hours = Math.floor(totalSeconds / 3600);
        const minutes = Math.floor((totalSeconds % 3600) / 60);
        const seconds = totalSeconds % 60;
        const pad = (n) => String(n).padStart(2, "0");
        el.textContent = hours > 0 ? `${pad(hours)}:${pad(minutes)}:${pad(seconds)}` : `${pad(minutes)}:${pad(seconds)}`;
    });
}

/** Upload one file to a backend route, throwing with the server's own error message on failure.
 *  `extraFields`, if given, is a plain object of additional form fields sent alongside the file
 *  (used for the XML-preprocessing settings on a RideData XML upload). */
async function _uploadJsonFile(url, file, extraFields) {
    const formData = new FormData();
    formData.append("file", file);
    if (extraFields) {
        Object.entries(extraFields).forEach(([key, value]) => formData.append(key, value));
    }
    const res = await fetch(url, {method: "POST", body: formData});
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.status === "error") {
        throw new Error(data.message || "Upload fehlgeschlagen.");
    }
    return data;
}

/** Collect the XML-preprocessing settings form into a plain object, only if the selected
 *  RideData file is an XML file - a JSON upload ignores these fields server-side anyway, but
 *  skipping them here keeps the request payload meaningful. */
function _collectXmlPipelineOptions(rideFile) {
    if (!rideFile || !rideFile.name.toLowerCase().endsWith(".xml")) {
        return null;
    }
    const options = {
        sample_size: document.getElementById("xml_sample_size")?.value || "",
        seed: document.getElementById("xml_seed")?.value || "",
        tw_minutes: document.getElementById("xml_tw_minutes")?.value || "",
        drop_prebooking: document.getElementById("xml_drop_prebooking")?.checked ? "on" : "",
        guarantee_feasible: document.getElementById("xml_guarantee_feasible")?.checked ? "on" : "",
        depot_lat: document.getElementById("xml_depot_lat")?.value || "",
        depot_lon: document.getElementById("xml_depot_lon")?.value || "",
        service_seconds: document.getElementById("xml_service_seconds")?.value || "",
        max_shift_minutes: document.getElementById("xml_max_shift_minutes")?.value || "",
        horizon_start: document.getElementById("xml_horizon_start")?.value || "",
        horizon_end: document.getElementById("xml_horizon_end")?.value || "",
    };
    return options;
}

/**
 * Upload whichever of BaseData/RideData is currently selected, in one pass, then reload once.
 * Both "Hochladen" buttons call this - previously each uploaded only its own file and reloaded
 * immediately, which silently dropped the other field's selection if both were picked before
 * clicking either button (the reload cleared the file input before its own upload ever fired).
 */
async function uploadSelectedFiles() {
    const baseInput = document.getElementById("baseJsonUpload");
    const rideInput = document.getElementById("rideJsonUpload");
    const baseFile = baseInput.files[0];
    const rideFile = rideInput.files[0];
    if (!baseFile && !rideFile) {
        return alert("Bitte BaseData- und/oder Fahrtdaten-Datei (JSON oder XML) auswaehlen");
    }
    setUploadBusy(true, "Dateien werden hochgeladen und verarbeitet. Bitte warten...");
    try {
        if (baseFile) {
            const data = await _uploadJsonFile("/upload-base-json", baseFile);
            console.log("BaseData Upload:", data);
        }
        if (rideFile) {
            const data = await _uploadJsonFile("/upload-ride-json", rideFile, _collectXmlPipelineOptions(rideFile));
            console.log("RideData Upload:", data);
            // XML: this only means the background conversion was queued, not that it's done -
            // the reload below re-renders the page with that "queued"/"running" status already
            // showing, and wireXmlUploadStatusPolling() (run again on that fresh load) takes it
            // from there, reloading a second time itself once the job actually completes.
        }
        location.reload();
    } catch (err) {
        alert(err.message || "Upload fehlgeschlagen.");
        // A rejected/failed upload never reloads the page, so nothing else resets these -
        // clear both selections rather than leaving a rejected (or already-uploaded) filename
        // sitting in the field looking like it's still pending. Dispatching "change" re-runs
        // the XML-settings-panel visibility logic instead of duplicating it here.
        baseInput.value = "";
        rideInput.value = "";
        baseInput.dispatchEvent(new Event("change"));
        rideInput.dispatchEvent(new Event("change"));
    } finally {
        setUploadBusy(false);
    }
}

/** Trigger the BaseData JSON download from the backend export route. */
function exportBaseJson() {
    window.location.href = "/export-base-json";
}

/** Trigger the RideData JSON download from the backend export route. */
function exportRideJson() {
    window.location.href = "/export-ride-json";
}
