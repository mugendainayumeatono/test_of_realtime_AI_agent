const map = L.map("map").setView([34.05, -118.25], 4);
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: "&copy; OpenStreetMap contributors",
}).addTo(map);

map.on("click", (event) => {
  const { lat, lng } = event.latlng;
  const panel = document.getElementById("create-panel");
  panel.style.display = "block";
  document.getElementById("new-lat").value = lat.toFixed(6);
  document.getElementById("new-lon").value = lng.toFixed(6);
  document.getElementById("new-name").focus();
  makePreviewMarker(lat, lng);
});

const markers = new Map();
const tracks = new Map();
const projectedLines = new Map();
const objectHistory = new Map();
let ws;
let selectedId = null;
let currentObjects = [];
let createMarker = null;

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
      <strong id="detail-name">${obj.name}</strong>
      <div id="detail-gps" style="margin:8px 0;color:#333;font-size:0.95rem;">GPS: ${obj.lat.toFixed(6)}, ${obj.lon.toFixed(6)}</div>
      <label>航速 (节)</label>
      <input id="detail-speed" type="number" step="0.1" min="0" value="${obj.speed_knots}" />
      <label>当前航向 (度，北=360)</label>
      <input id="detail-heading" type="number" step="1" min="0" max="360" value="${formatHeading(obj.heading)}" />
      <label>目标航向 (度，北=360)</label>
      <input id="detail-target-heading" type="number" step="1" min="0" max="360" value="${obj.target_heading !== undefined && obj.target_heading !== null ? formatHeading(obj.target_heading) : ''}" placeholder="选填" />
      <label>转向率 (度/秒)</label>
      <input id="detail-turn-rate" type="number" step="0.1" min="0" value="${(obj.turn_rate_deg_sec || 3.0)}" />
      <label>当前高度 (英尺)</label>
      <div style="display:flex;gap:8px;align-items:center;">
        <input id="detail-altitude" type="number" step="0.1" value="${(obj.altitude_ft ?? 0).toFixed(2)}" />
        <span id="detail-altitude-meter" style="color:#555;font-size:0.9rem;white-space:nowrap;">(${(((obj.altitude_ft ?? 0) * 0.3048).toFixed(2))} m)</span>
      </div>
      <label>目标高度 (英尺)</label>
      <div style="display:flex;gap:8px;align-items:center;">
        <input id="detail-target-altitude" type="number" step="0.1" value="${obj.target_altitude_ft !== undefined && obj.target_altitude_ft !== null ? obj.target_altitude_ft : ''}" placeholder="选填" />
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

function renderObjects(objects) {
  const list = document.getElementById("object-list");
  const existingIds = new Set(objects.map((o) => o.id));
  currentObjects = objects;
  list.innerHTML = "";

  for (const obj of objects) {
    const tooltipText = `${obj.name}<br/>` +
      `GPS: ${obj.lat.toFixed(6)}, ${obj.lon.toFixed(6)}<br/>` +
      `航速: ${obj.speed_knots.toFixed(1)} 节<br/>` +
      `航向: ${formatHeading(obj.heading)}°<br/>` +
      `高度: ${(obj.altitude_ft ?? 0).toFixed(1)} 英尺 (${((obj.altitude_ft ?? 0) * 0.3048).toFixed(2)} m)`;

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
      if (marker.getTooltip()) {
        marker.setTooltipContent(tooltipText);
      } else {
        marker.bindTooltip(tooltipText, { permanent: true, direction: 'right', opacity: 0.9, className: 'object-tooltip' });
      }
    } else {
      const newMarker = L.marker([obj.lat, obj.lon], { icon: makeObjectIcon(obj.heading) }).addTo(map);
      newMarker.bindPopup(`${obj.name}<br/>${obj.lat.toFixed(6)}, ${obj.lon.toFixed(6)}`);
      newMarker.bindTooltip(tooltipText, { permanent: true, direction: 'right', opacity: 0.9, className: 'object-tooltip' });
      markers.set(obj.id, newMarker);
    }

    const row = document.createElement("div");
    row.className = "object-item";
    if (obj.id === selectedId) {
      row.classList.add("selected");
    }
    row.textContent = obj.name;
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
  showCreatePanel();
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
});

document.getElementById("cancel-create-button").addEventListener("click", () => {
  hideCreatePanel();
  removePreviewMarker();
});

createSocket();
