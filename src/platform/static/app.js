const map = L.map("map").setView([34.05, -118.25], 4);
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: "&copy; OpenStreetMap contributors",
}).addTo(map);

let mapClickMode = 'none';

map.on("click", (event) => {
  const { lat, lng } = event.latlng;
  if (mapClickMode === 'airport') {
    document.getElementById("airport-lat").value = lat.toFixed(6);
    document.getElementById("airport-lon").value = lng.toFixed(6);
    makePreviewMarker(lat, lng);
  } else if (mapClickMode === 'create') {
    const panel = document.getElementById("create-panel");
    panel.style.display = "block";
    document.getElementById("new-lat").value = lat.toFixed(6);
    document.getElementById("new-lon").value = lng.toFixed(6);
    document.getElementById("new-name").focus();
    makePreviewMarker(lat, lng);
  }
});

const markers = new Map();
const tracks = new Map();
const projectedLines = new Map();
const objectHistory = new Map();
let ws;
let selectedId = null;
let currentObjects = [];
let createMarker = null;
let airportMarker = null;

let runwayLine = null;
let controllerEnabled = false;
let simulatorRunning = false;

function updateControllerButton(enabled) {
  controllerEnabled = enabled;
  const btn = document.getElementById("controller-toggle-button");
  if (btn) {
    if (enabled) {
      btn.textContent = "AI管制员: 开启";
      btn.style.background = "#28a745";
    } else {
      btn.textContent = "AI管制员: 关闭";
      btn.style.background = "#dc3545";
    }
  }
}

function updateSimulatorButton(running) {
  simulatorRunning = running;
  const btn = document.getElementById("simulator-toggle-button");
  if (btn) {
    if (running) {
      btn.textContent = "飞行模拟器: 运行中";
      btn.style.background = "#28a745";
    } else {
      btn.textContent = "飞行模拟器: 已停止";
      btn.style.background = "#dc3545";
    }
  }
}

// Load initial simulator state
fetch("api/simulator/state").then(r => r.json()).then(data => {
  if (data && typeof data.running === "boolean") {
    updateSimulatorButton(data.running);
  }
}).catch(e => console.error("Error fetching simulator state:", e));

// Load initial airport
fetch("api/airport").then(r => r.json()).then(data => {
  if (data.lat != null && data.lon != null) {
    updateAirportMarker(data.lat, data.lon, data.runway_heading);
    if (data.runway_heading != null) {
      document.getElementById("airport-heading").value = data.runway_heading;
    }
  }
});

function updateAirportMarker(lat, lon, runway_heading) {
  if (lat == null || lon == null) {
    if (airportMarker) {
      map.removeLayer(airportMarker);
      airportMarker = null;
    }
    if (runwayLine) {
      map.removeLayer(runwayLine);
      runwayLine = null;
    }
    return;
  }
  if (airportMarker) {
    airportMarker.setLatLng([lat, lon]);
  } else {
    airportMarker = L.marker([lat, lon], {
      icon: L.divIcon({
        className: "airport-square-icon",
        html: "",
        iconSize: [20, 20],
        iconAnchor: [10, 10]
      })
    }).addTo(map);
    airportMarker.bindTooltip("Airport", { permanent: true, direction: "top", opacity: 0.9, className: "object-tooltip" });
  }

  if (runway_heading != null) {
    const p1 = computeDestination(lat, lon, runway_heading, 3704); // 2NM
    const p2 = computeDestination(lat, lon, runway_heading + 180, 3704);
    if (runwayLine) {
      runwayLine.setLatLngs([p1, p2]);
    } else {
      runwayLine = L.polyline([p1, p2], { color: '#28a745', weight: 4, opacity: 0.8, dashArray: '10, 10' }).addTo(map);
    }
  } else if (runwayLine) {
    map.removeLayer(runwayLine);
    runwayLine = null;
  }
}

function appendRadioLog(msg) {
  const container = document.getElementById("radio-log");
  if (!container) return;
  const isAtBottom = container.scrollHeight - container.scrollTop - container.clientHeight <= 5;
  const el = document.createElement("div");
  el.className = "log-entry";
  
  const rawTs = (typeof msg.timestamp === "number" && !isNaN(msg.timestamp)) ? msg.timestamp * 1000 : Date.now();
  const dateObj = new Date(rawTs);
  const timeStr = isNaN(dateObj.getTime()) ? new Date().toLocaleTimeString('zh-CN', { hour12: false }) : dateObj.toLocaleTimeString('zh-CN', { hour12: false });
  
  if (msg.source === "agent_controller") {
    el.classList.add("log-controller");
    el.innerHTML = `<strong>[${timeStr}] 🔊 AI管制员:</strong><br/>${msg.text || (msg.data ? JSON.stringify(msg.data, null, 2) : '')}`;
  } else if (msg.source === "agent_pilot") {
    el.classList.add("log-pilot");
    const pilotContent = msg.data ? `<pre style="margin: 4px 0 0; font-size: 0.8rem; white-space: pre-wrap; font-family: monospace;">${JSON.stringify(msg.data, null, 2)}</pre>` : (msg.text || '');
    el.innerHTML = `<strong>[${timeStr}] 🤖 Pilot (参数下发):</strong><br/>${pilotContent}`;
  } else {
    el.innerHTML = `<strong>[${timeStr}] ${msg.source}:</strong><br/>${msg.text || (msg.data ? JSON.stringify(msg.data) : '')}`;
  }
  
  container.appendChild(el);
  if (isAtBottom) {
    container.scrollTop = container.scrollHeight;
  }
}

function formatHeading(value) {
  const heading = Number(value) % 360;
  return heading === 0 ? 360 : Math.round(heading);
}

function headingToCssRotation(value) {
  const heading = Number(value) % 360;
  const normalized = heading === 0 ? 360 : heading;
  return ((normalized - 90) + 360) % 360;
}

function computeDestination(lat, lon, heading, distance) {
  const bearing = ((Number(heading) % 360) + 360) % 360;
  const headingRad = (bearing * Math.PI) / 180;
  const lat1 = (lat * Math.PI) / 180;
  const lon1 = (lon * Math.PI) / 180;
  const angularDistance = distance / 6371000;
  const lat2 = Math.asin(
    Math.sin(lat1) * Math.cos(angularDistance) +
      Math.cos(lat1) * Math.sin(angularDistance) * Math.cos(headingRad)
  );
  const lon2 =
    lon1 +
    Math.atan2(
      Math.sin(headingRad) * Math.sin(angularDistance) * Math.cos(lat1),
      Math.cos(angularDistance) - Math.sin(lat1) * Math.sin(lat2)
    );
  return [((lat2 * 180) / Math.PI), ((lon2 * 180) / Math.PI)];
}

function makeObjectIcon() {
  return L.divIcon({
    className: "object-pointer-icon",
    html: `<div class="object-dot"></div>`,
    iconSize: [14, 14],
    iconAnchor: [7, 7],
  });
}

function makePreviewMarker(lat, lon) {
  if (createMarker) {
    createMarker.setLatLng([lat, lon]);
    return;
  }
  createMarker = L.circleMarker([lat, lon], {
    radius: 8,
    color: "#0069ff",
    fillColor: "#0069ff",
    fillOpacity: 0.6,
  }).addTo(map);
}

function removePreviewMarker() {
  if (createMarker) {
    map.removeLayer(createMarker);
    createMarker = null;
  }
}

function createSocket() {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const path = window.location.pathname.endsWith("/") ? window.location.pathname + "ws" : window.location.pathname + "/ws";
  const wsUrl = `${protocol}//${window.location.host}${path}`;
  console.log("Connecting to WebSocket:", wsUrl);
  ws = new WebSocket(wsUrl);

  ws.addEventListener("open", () => {
    console.log("WebSocket connected to", wsUrl);
  });

  ws.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.type === "state") {
      renderObjects(message.objects);
    } else if (message.type === "airport") {
      updateAirportMarker(message.lat, message.lon, message.runway_heading);
      if (message.runway_heading != null) {
        document.getElementById("airport-heading").value = message.runway_heading;
      }
    } else if (message.type === "radio_log") {
      appendRadioLog(message);
    } else if (message.type === "controller_state") {
      updateControllerButton(message.enabled);
    } else if (message.type === "simulator_state") {
      updateSimulatorButton(message.running);
    }
  });

  ws.addEventListener("close", () => {
    console.log("WebSocket disconnected, reconnecting in 1.5s...");
    setTimeout(createSocket, 1500);
  });
}

function sendMessage(payload) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(payload));
  }
}

function showCreatePanel() {
  const panel = document.getElementById("create-panel");
  panel.style.display = "block";
}

function hideCreatePanel() {
  const panel = document.getElementById("create-panel");
  panel.style.display = "none";
}

function setSelected(id) {
  selectedId = id;
  const selected = currentObjects.find((o) => o.id === id);
  if (selected) {
    map.panTo([selected.lat, selected.lon], { animate: true, duration: 0.4 });
  }
  renderObjects(currentObjects);
}

function renderDetails(obj) {
  const details = document.getElementById("object-details");
  if (!obj) {
    details.style.display = "none";
    details.innerHTML = "";
    details.removeAttribute("data-selected-id");
    return;
  }

  const currentSelected = details.getAttribute("data-selected-id");
  if (currentSelected !== obj.id) {
    details.dataset.selectedId = obj.id;
    details.style.display = "block";
    details.innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:center;">
        <strong id="detail-name">${obj.name}</strong>
        <span id="detail-status" style="font-size:0.8rem;padding:2px 8px;border-radius:4px;background:${obj.status === 'ground' ? '#6c757d' : '#007bff'};color:#fff;">${obj.status === 'ground' ? '地面 (Ground)' : '空中 (Airborne)'}</span>
      </div>
      <div id="detail-gps" style="margin:8px 0;color:#333;font-size:0.95rem;">GPS: ${obj.lat.toFixed(6)}, ${obj.lon.toFixed(6)}</div>
      <label class="target-param-label">航速 (节)</label>
      <input id="detail-speed" class="target-param-input" type="number" step="0.1" min="0" value="${obj.speed_knots}" />
      <label>当前航向 (度，北=360)</label>
      <input id="detail-heading" type="number" step="1" min="0" max="360" value="${formatHeading(obj.heading)}" />
      <label class="target-param-label">目标航向 (度，北=360)</label>
      <input id="detail-target-heading" class="target-param-input" type="number" step="1" min="0" max="360" value="${obj.target_heading !== undefined && obj.target_heading !== null ? formatHeading(obj.target_heading) : ''}" placeholder="选填" />
      <label>转向率 (度/秒)</label>
      <input id="detail-turn-rate" type="number" step="0.1" min="0" value="${(obj.turn_rate_deg_sec || 3.0)}" />
      <label>当前高度 (英尺)</label>
      <div style="display:flex;gap:8px;align-items:center;">
        <input id="detail-altitude" type="number" step="0.1" value="${(obj.altitude_ft ?? 0).toFixed(2)}" />
        <span id="detail-altitude-meter" style="color:#555;font-size:0.9rem;white-space:nowrap;">(${(((obj.altitude_ft ?? 0) * 0.3048).toFixed(2))} m)</span>
      </div>
      <label class="target-param-label">目标高度 (英尺)</label>
      <div style="display:flex;gap:8px;align-items:center;">
        <input id="detail-target-altitude" class="target-param-input" type="number" step="0.1" value="${obj.target_altitude_ft !== undefined && obj.target_altitude_ft !== null ? obj.target_altitude_ft : ''}" placeholder="选填" />
        <span id="detail-target-altitude-meter" style="color:#555;font-size:0.9rem;white-space:nowrap;">(${obj.target_altitude_ft !== undefined && obj.target_altitude_ft !== null ? (obj.target_altitude_ft * 0.3048).toFixed(2) + ' m' : '未设置'})</span>
      </div>
      <label>垂直速度 (英尺/分钟)</label>
      <div style="display:flex;gap:8px;align-items:center;">
        <input id="detail-vertical-speed" type="number" step="0.1" value="${(obj.vertical_speed_ft_min || 400.0)}" />
        <span id="detail-vertical-speed-meter" style="color:#555;font-size:0.9rem;white-space:nowrap;">(${((obj.vertical_speed_ft_min || 400.0) * 0.3048).toFixed(2)} m/min)</span>
      </div>
      <div style="display:flex;gap:8px;margin-top:12px;">
        <button id="detail-save-button" type="button">保存变更</button>
        <button id="detail-remove-button" type="button" style="background:#777;">删除物体</button>
      </div>
    `;

    document.getElementById("detail-save-button").addEventListener("click", () => {
      const speed = Number(document.getElementById("detail-speed").value) || 0.0;
      const heading = Number(document.getElementById("detail-heading").value) || 0.0;
      const target_heading_str = document.getElementById("detail-target-heading").value;
      const target_heading = target_heading_str === "" ? null : Number(target_heading_str);
      const turn_rate_deg_sec = Number(document.getElementById("detail-turn-rate").value) || 3.0;
      const altitude_ft = Number(document.getElementById("detail-altitude").value) || 0.0;
      const target_str = document.getElementById("detail-target-altitude").value;
      const target_altitude_ft = target_str === "" ? null : Number(target_str);
      const vertical = Number(document.getElementById("detail-vertical-speed").value) || 0.0;
      sendMessage({ 
        type: "update", 
        id: obj.id, 
        speed_knots: speed, 
        heading, 
        target_heading, 
        turn_rate_deg_sec, 
        altitude_ft, 
        target_altitude_ft, 
        vertical_speed_ft_min: vertical 
      });
    });
    document.getElementById("detail-remove-button").addEventListener("click", () => {
      sendMessage({ type: "remove", id: obj.id });
      selectedId = null;
      renderDetails(null);
    });
  }

  const nameEl = document.getElementById("detail-name");
  const gpsEl = document.getElementById("detail-gps");
  const statusEl = document.getElementById("detail-status");
  const speedEl = document.getElementById("detail-speed");
  const altitudeEl = document.getElementById("detail-altitude");
  const headingEl = document.getElementById("detail-heading");
  const verticalEl = document.getElementById("detail-vertical-speed");
  const verticalMeterEl = document.getElementById("detail-vertical-speed-meter");
  const targetAltitudeEl = document.getElementById("detail-target-altitude");
  const targetAltitudeMeterEl = document.getElementById("detail-target-altitude-meter");
  const targetHeadingEl = document.getElementById("detail-target-heading");
  const turnRateEl = document.getElementById("detail-turn-rate");

  if (nameEl) nameEl.textContent = obj.name;
  if (gpsEl) gpsEl.textContent = `GPS: ${obj.lat.toFixed(6)}, ${obj.lon.toFixed(6)}`;
  if (statusEl) {
    statusEl.textContent = obj.status === 'ground' ? '地面 (Ground)' : '空中 (Airborne)';
    statusEl.style.background = obj.status === 'ground' ? '#6c757d' : '#007bff';
  }

  if (speedEl && document.activeElement !== speedEl) {
    speedEl.value = obj.speed_knots;
  }
  if (headingEl && document.activeElement !== headingEl) {
    headingEl.value = formatHeading(obj.heading);
  }
  if (targetHeadingEl && document.activeElement !== targetHeadingEl) {
    targetHeadingEl.value = obj.target_heading !== undefined && obj.target_heading !== null ? formatHeading(obj.target_heading) : '';
  }
  if (turnRateEl && document.activeElement !== turnRateEl) {
    turnRateEl.value = obj.turn_rate_deg_sec || 3.0;
  }
  if (altitudeEl && document.activeElement !== altitudeEl) {
    altitudeEl.value = (obj.altitude_ft ?? 0).toFixed(2);
  }
  if (targetAltitudeEl && document.activeElement !== targetAltitudeEl) {
    targetAltitudeEl.value = obj.target_altitude_ft !== undefined && obj.target_altitude_ft !== null ? obj.target_altitude_ft : '';
  }
  if (targetAltitudeMeterEl) {
    targetAltitudeMeterEl.textContent = obj.target_altitude_ft !== undefined && obj.target_altitude_ft !== null ? `(${(obj.target_altitude_ft * 0.3048).toFixed(2)} m)` : '(未设置)';
  }
  if (verticalEl && document.activeElement !== verticalEl) {
    verticalEl.value = obj.vertical_speed_ft_min || 400.0;
  }
  if (verticalMeterEl) {
    const verticalValue = obj.vertical_speed_ft_min || 400.0;
    verticalMeterEl.textContent = `(${(verticalValue * 0.3048).toFixed(2)} m/min)`;
  }
  const altitudeMeterEl = document.getElementById("detail-altitude-meter");
  if (altitudeMeterEl) {
    const altitudeValue = obj.altitude_ft ?? 0;
    altitudeMeterEl.textContent = `(${(altitudeValue * 0.3048).toFixed(2)} m)`;
  }
}

function getTooltipOptions(heading) {
  let h = heading % 360;
  if (h < 0) h += 360;
  if (h >= 45 && h < 135) {
    return { direction: 'bottom', offset: [0, 25] };
  } else if (h >= 135 && h < 225) {
    return { direction: 'left', offset: [-25, 0] };
  } else if (h >= 225 && h < 315) {
    return { direction: 'top', offset: [0, -25] };
  } else {
    return { direction: 'right', offset: [25, 0] };
  }
}

function renderObjects(objects) {
  const list = document.getElementById("object-list");
  const existingIds = new Set(objects.map((o) => o.id));
  currentObjects = objects;
  list.innerHTML = "";

  for (const obj of objects) {
    let distStr = "";
    if (airportMarker) {
      const distMeters = map.distance([obj.lat, obj.lon], airportMarker.getLatLng());
      const distNM = distMeters / 1852.0;
      distStr = `<br/>距机场: ${distNM.toFixed(1)} NM`;
    }

    const isGround = obj.status === 'ground';
    const statusBadge = isGround
      ? '<span style="color:#ffc107;font-weight:bold;">[地面]</span>'
      : '<span style="color:#00d2ff;font-weight:bold;">[空中]</span>';

    const tooltipText = `${obj.name} ${statusBadge}<br/>` +
      `GPS: ${obj.lat.toFixed(6)}, ${obj.lon.toFixed(6)}<br/>` +
      `航速: ${obj.speed_knots.toFixed(1)} 节<br/>` +
      `航向: ${formatHeading(obj.heading)}°<br/>` +
      `高度: ${(obj.altitude_ft ?? 0).toFixed(1)} 英尺 (${((obj.altitude_ft ?? 0) * 0.3048).toFixed(2)} m)` +
      distStr;

    const now = Date.now();
    const history = (objectHistory.get(obj.id) || []).filter((entry) => entry.ts >= now - 60000);
    history.push({ lat: obj.lat, lon: obj.lon, ts: now });
    objectHistory.set(obj.id, history);

    const path = history.map((entry) => [entry.lat, entry.lon]);
    if (path.length > 1) {
      if (tracks.has(obj.id)) {
        tracks.get(obj.id).setLatLngs(path);
      } else {
        const trackLine = L.polyline(path, { color: '#0069ff', weight: 2, opacity: 0.4 });
        trackLine.addTo(map);
        tracks.set(obj.id, trackLine);
      }
    } else if (tracks.has(obj.id)) {
      tracks.get(obj.id).setLatLngs(path);
    }

    const predictedPoint = computeDestination(obj.lat, obj.lon, obj.heading, (obj.speed_knots || 0) * 0.514444 * 10);
    if (projectedLines.has(obj.id)) {
      projectedLines.get(obj.id).setLatLngs([[obj.lat, obj.lon], predictedPoint]);
    } else {
      const line = L.polyline([[obj.lat, obj.lon], predictedPoint], {
        color: '#0069ff',
        weight: 2,
        opacity: 0.7,
        dashArray: '6,6',
      }).addTo(map);
      projectedLines.set(obj.id, line);
    }

    const marker = markers.get(obj.id);
    if (marker) {
      marker.setLatLng([obj.lat, obj.lon]);
      marker.setIcon(makeObjectIcon(obj.heading));
      marker.setPopupContent(`${obj.name}<br/>${obj.lat.toFixed(6)}, ${obj.lon.toFixed(6)}`);
      const newOpts = getTooltipOptions(obj.heading);
      if (marker.getTooltip()) {
        const currentTooltip = marker.getTooltip();
        if (currentTooltip.options.direction !== newOpts.direction) {
          marker.unbindTooltip();
          marker.bindTooltip(tooltipText, Object.assign({ permanent: true, opacity: 0.9, className: 'object-tooltip' }, newOpts));
        } else {
          marker.setTooltipContent(tooltipText);
        }
      } else {
        marker.bindTooltip(tooltipText, Object.assign({ permanent: true, opacity: 0.9, className: 'object-tooltip' }, newOpts));
      }
    } else {
      const newOpts = getTooltipOptions(obj.heading);
      const newMarker = L.marker([obj.lat, obj.lon], { icon: makeObjectIcon(obj.heading) }).addTo(map);
      newMarker.bindPopup(`${obj.name}<br/>${obj.lat.toFixed(6)}, ${obj.lon.toFixed(6)}`);
      newMarker.bindTooltip(tooltipText, Object.assign({ permanent: true, opacity: 0.9, className: 'object-tooltip' }, newOpts));
      markers.set(obj.id, newMarker);
    }

    const row = document.createElement("div");
    row.className = "object-item";
    if (obj.id === selectedId) {
      row.classList.add("selected");
    }
    row.style.display = "flex";
    row.style.justifyContent = "space-between";
    row.style.alignItems = "center";
    row.innerHTML = `<span>${obj.name}</span><span style="font-size:0.75rem;padding:2px 6px;border-radius:3px;background:${isGround ? '#6c757d' : '#007bff'};color:#fff;">${isGround ? '地面' : '空中'}</span>`;
    row.addEventListener("click", () => {
      setSelected(obj.id);
    });
    list.appendChild(row);
  }

  if (selectedId && !existingIds.has(selectedId)) {
    selectedId = null;
  }

  for (const id of markers.keys()) {
    if (!existingIds.has(id)) {
      map.removeLayer(markers.get(id));
      markers.delete(id);
      if (tracks.has(id)) {
        map.removeLayer(tracks.get(id));
        tracks.delete(id);
      }
      if (projectedLines.has(id)) {
        map.removeLayer(projectedLines.get(id));
        projectedLines.delete(id);
      }
      objectHistory.delete(id);
    }
  }

  const selected = currentObjects.find((o) => o.id === selectedId);
  renderDetails(selected);
}

document.getElementById("open-create-button").addEventListener("click", () => {
  mapClickMode = 'create';
  document.getElementById("airport-panel").style.display = "none";
  showCreatePanel();
});

document.getElementById("open-airport-button").addEventListener("click", () => {
  mapClickMode = 'airport';
  document.getElementById("airport-panel").style.display = "block";
  document.getElementById("create-panel").style.display = "none";
});

document.getElementById("save-airport-button").addEventListener("click", () => {
  const lat = Number(document.getElementById("airport-lat").value);
  const lon = Number(document.getElementById("airport-lon").value);
  const headingStr = document.getElementById("airport-heading").value;
  const runway_heading = headingStr === "" ? null : Number(headingStr);
  fetch("api/airport", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ lat, lon, runway_heading })
  });
  document.getElementById("airport-panel").style.display = "none";
  removePreviewMarker();
  mapClickMode = 'none';
});

document.getElementById("cancel-airport-button").addEventListener("click", () => {
  document.getElementById("airport-panel").style.display = "none";
  removePreviewMarker();
  mapClickMode = 'none';
});

document.getElementById("create-object-button").addEventListener("click", () => {
  const name = document.getElementById("new-name").value || `object-${Date.now()}`;
  const lon = Number(document.getElementById("new-lon").value) || 0.0;
  const lat = Number(document.getElementById("new-lat").value) || 0.0;
  const speed_knots = Number(document.getElementById("new-speed").value) || 0.0;
  const heading = Number(document.getElementById("new-heading").value) || 0.0;
  const target_heading_str = document.getElementById("new-target-heading").value;
  const target_heading = target_heading_str === "" ? null : Number(target_heading_str);
  const turn_rate_deg_sec = Number(document.getElementById("new-turn-rate").value) || 3.0;
  const altitude_ft = Number(document.getElementById("new-altitude").value) || 0.0;
  const target_str = document.getElementById("new-target-altitude").value;
  const target_altitude_ft = target_str === "" ? null : Number(target_str);
  const vertical_speed_ft_min = Number(document.getElementById("new-vertical-speed").value) || 0.0;
  sendMessage({
    type: "add",
    lat,
    lon,
    name,
    speed_knots,
    heading,
    target_heading,
    turn_rate_deg_sec,
    altitude_ft,
    target_altitude_ft,
    vertical_speed_ft_min,
  });
  hideCreatePanel();
  removePreviewMarker();
  mapClickMode = 'none';
});

document.getElementById("cancel-create-button").addEventListener("click", () => {
  hideCreatePanel();
  removePreviewMarker();
  mapClickMode = 'none';
});

document.getElementById("simulator-toggle-button")?.addEventListener("click", () => {
  const newState = !simulatorRunning;
  fetch("api/simulator/state", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ running: newState })
  });
});

document.getElementById("controller-toggle-button")?.addEventListener("click", () => {
  const newState = !controllerEnabled;
  fetch("api/controller_state", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled: newState })
  });
});

document.getElementById("save-state-button")?.addEventListener("click", () => {
  fetch("api/state/save", {
    method: "POST",
    headers: { "Content-Type": "application/json" }
  })
    .then(r => r.json())
    .then(data => {
      if (data.status === "success") {
        alert("场景状态已成功保存到 state.json！(包含机场坐标及 " + (data.objects_count || 0) + " 架飞机的完整飞行状态)");
      } else {
        alert("保存状态失败: " + (data.message || JSON.stringify(data)));
      }
    })
    .catch(err => {
      alert("保存状态请求错误: " + err);
    });
});

document.getElementById("load-state-button")?.addEventListener("click", () => {
  fetch("api/state/load", {
    method: "POST",
    headers: { "Content-Type": "application/json" }
  })
    .then(r => r.json())
    .then(data => {
      if (data.status === "success") {
        alert("场景状态已成功从 state.json 读取并还原！(包含机场及 " + (data.objects_count || 0) + " 架飞机的完整飞行状态)");
        if (data.airport) {
          updateAirportMarker(data.airport.lat, data.airport.lon, data.airport.runway_heading);
          if (data.airport.runway_heading != null) {
            const hInput = document.getElementById("airport-heading");
            if (hInput) hInput.value = data.airport.runway_heading;
          }
        }
        if (Array.isArray(data.objects)) {
          renderObjects(data.objects);
        }
      } else {
        alert("读取状态失败: " + (data.message || JSON.stringify(data)));
      }
    })
    .catch(err => {
      alert("读取状态请求错误: " + err);
    });
});

document.getElementById("reset-button")?.addEventListener("click", () => {
  if (!confirm("确定要重置场景吗？将通知模拟器删除全部飞机与机场，并停止飞行模拟器和AI管制员。")) {
    return;
  }
  fetch("api/reset", {
    method: "POST",
    headers: { "Content-Type": "application/json" }
  })
    .then(r => r.json())
    .then(data => {
      if (data.status === "success") {
        updateAirportMarker(null, null, null);
        const latInput = document.getElementById("airport-lat");
        const lonInput = document.getElementById("airport-lon");
        const headingInput = document.getElementById("airport-heading");
        if (latInput) latInput.value = "0.0";
        if (lonInput) lonInput.value = "0.0";
        if (headingInput) headingInput.value = "360";

        updateSimulatorButton(false);
        updateControllerButton(false);
        renderObjects([]);
        alert("场景重置成功！已删除全部飞机与机场，并停止飞行模拟器和AI管制员。");
      } else {
        alert("重置场景失败: " + (data.message || JSON.stringify(data)));
      }
    })
    .catch(err => {
      alert("重置场景请求错误: " + err);
    });
});

createSocket();

