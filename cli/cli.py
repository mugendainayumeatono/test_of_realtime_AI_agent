# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "httpx",
# ]
# ///

"""
cli/cli.py

Realtime Aviation Multi-Agent System - Terminal Console and CLI (Platform CLI).
Features dual UI modes:
1. Interactive Menu Mode: Run without arguments to present hierarchical console menus and wizards.
2. Command-Line Arguments Mode: Pass CLI arguments to execute tasks directly.
"""

from __future__ import annotations
import argparse
import concurrent.futures
import json
import math
import os
import random
import sys
import time
import unicodedata
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

# Prefer httpx; fallback to standard library urllib.request if httpx is unavailable.
try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    import urllib.request
    import urllib.error
    HAS_HTTPX = False

EARTH_RADIUS_M = 6_371_000.0
NM_TO_M = 1852.0


# ==============================================================================
# Network Requests and Address Resolution
# ==============================================================================

def http_get(url: str, timeout: float = 5.0) -> Any:
    """Send an HTTP GET request and return parsed JSON data."""
    if HAS_HTTPX:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(url)
            resp.raise_for_status()
            return resp.json()
    else:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode("utf-8")
                data = json.loads(body)
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
            raise


def http_post(url: str, data: Optional[Dict] = None, timeout: float = 5.0) -> Dict:
    """Send an HTTP POST request and return parsed JSON data."""
    data = data or {}
    if HAS_HTTPX:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, json=data)
            resp.raise_for_status()
            return resp.json()
    else:
        payload = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))


def resolve_base_url(specified_url: Optional[str] = None) -> str:
    """Resolve target Platform service base URL."""
    if specified_url:
        return specified_url.rstrip("/")
    env_url = os.environ.get("PLATFORM_URL")
    if env_url:
        return env_url.rstrip("/")

    # Host-mapped port defaults to 8010; fallback to local development port 8000
    candidate_urls = ["http://localhost:8010", "http://localhost:8000"]
    for cand in candidate_urls:
        try:
            http_get(f"{cand}/api/controller_state", timeout=1.0)
            return cand
        except Exception:
            pass
    return "http://localhost:8010"


# ==============================================================================
# Geographic and Coordinate Calculations
# ==============================================================================

def calculate_20nm_point(airport_lat: float, airport_lon: float) -> Tuple[float, float]:
    """Calculate random coordinates on a 20 NM (37,040 m) circle around airport (lat, lon)."""
    distance_m = 20.0 * NM_TO_M
    angular_dist = distance_m / EARTH_RADIUS_M
    bearing_rad = math.radians(random.uniform(0.0, 360.0))

    lat1_rad = math.radians(airport_lat)
    lon1_rad = math.radians(airport_lon)

    lat2_rad = math.asin(
        math.sin(lat1_rad) * math.cos(angular_dist)
        + math.cos(lat1_rad) * math.sin(angular_dist) * math.cos(bearing_rad)
    )
    lon2_rad = lon1_rad + math.atan2(
        math.sin(bearing_rad) * math.sin(angular_dist) * math.cos(lat1_rad),
        math.cos(angular_dist) - math.sin(lat1_rad) * math.sin(lat2_rad)
    )
    return round(math.degrees(lat2_rad), 6), round(math.degrees(lon2_rad), 6)


def generate_random_coordinates() -> Tuple[float, float]:
    """Generate reasonable global random coordinates (lat: -60~60, lon: -180~180)."""
    lat = round(random.uniform(-60.0, 60.0), 6)
    lon = round(random.uniform(-180.0, 180.0), 6)
    return lat, lon


# ==============================================================================
# Platform API Wrappers
# ==============================================================================

# --- ON/OFF Switch Control ---
def get_simulator_status(base_url: str) -> bool:
    res = http_get(f"{base_url}/api/simulator/state")
    return bool(res.get("running", False))


def set_simulator_status(base_url: str, running: bool) -> Dict:
    return http_post(f"{base_url}/api/simulator/state", {"running": running})


def get_controller_status(base_url: str) -> bool:
    res = http_get(f"{base_url}/api/controller_state")
    return bool(res.get("enabled", False))


def set_controller_status(base_url: str, enabled: bool) -> Dict:
    return http_post(f"{base_url}/api/controller_state", {"enabled": enabled})


# --- Airport Control ---
def get_airport(base_url: str) -> Dict:
    return http_get(f"{base_url}/api/airport")


def set_airport(base_url: str, lat: float, lon: float, runway_heading: Optional[float] = None) -> Dict:
    data = {"lat": lat, "lon": lon, "runway_heading": runway_heading}
    return http_post(f"{base_url}/api/airport", data)


# --- Aircraft Entity Control ---
def get_airplanes(base_url: str) -> List[Dict]:
    res = http_get(f"{base_url}/api/objects")
    return res if isinstance(res, list) else []


def add_airplane(
    base_url: str,
    name: Optional[str] = None,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    altitude: Optional[float] = None,
    heading: Optional[float] = None,
    speed: Optional[float] = None,
) -> Tuple[Dict, str]:
    """
    Add an aircraft. If coordinates are left unspecified:
    - If airport is configured, randomly pick a point on the 20 NM perimeter circle;
    - If no airport is configured, generate random coordinates.
    Returns (response_dict, position_description).
    """
    pos_desc = ""
    if lat is None or lon is None:
        try:
            airport_data = get_airport(base_url)
            a_lat = airport_data.get("lat")
            a_lon = airport_data.get("lon")
            if a_lat is not None and a_lon is not None:
                lat, lon = calculate_20nm_point(float(a_lat), float(a_lon))
                pos_desc = f"Airport coordinates detected (Lat: {a_lat}, Lon: {a_lon}). Selected point on 20 NM circle: Lat={lat}, Lon={lon}"
            else:
                lat, lon = generate_random_coordinates()
                pos_desc = f"No airport configured. Generated random coordinates: Lat={lat}, Lon={lon}"
        except Exception:
            lat, lon = generate_random_coordinates()
            pos_desc = f"Failed to query airport. Generated random coordinates: Lat={lat}, Lon={lon}"
    else:
        pos_desc = f"Using specified coordinates: Lat={lat}, Lon={lon}"

    query_params = []
    if name:
        query_params.append(f"name={urllib.parse.quote(name)}")
    query_params.append(f"lat={lat}")
    query_params.append(f"lon={lon}")
    if speed is not None:
        query_params.append(f"speed={speed}")
    if heading is not None:
        query_params.append(f"heading={heading}")
    if altitude is not None:
        query_params.append(f"altitude={altitude}")

    url = f"{base_url}/api/add?{'&'.join(query_params)}"
    res = http_get(url)
    return res, pos_desc


def update_airplane(
    base_url: str,
    object_id: str,
    heading: Optional[float] = None,
    altitude: Optional[float] = None,
    speed: Optional[float] = None,
) -> Dict:
    """Update aircraft kinematic targets (heading, altitude, speed)."""
    params = [f"id={urllib.parse.quote(object_id)}"]
    if heading is not None:
        params.append(f"heading={heading}")
    if altitude is not None:
        params.append(f"altitude={altitude}")
    if speed is not None:
        params.append(f"speed={speed}")
    url = f"{base_url}/api/update?{'&'.join(params)}"
    return http_get(url)


def delete_airplane(base_url: str, object_id: str) -> Dict:
    """Delete an aircraft from platform simulator by ID."""
    clean_id = str(object_id).strip()
    url = f"{base_url}/api/delete?id={urllib.parse.quote(clean_id)}"
    return http_get(url)


def resolve_aircraft_selection(planes: List[Dict], target_str: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Resolve user input into a target aircraft UUID from an active aircraft list.

    Supported input formats:
    - 0, q, quit, exit, cancel, back: Cancel operation (returns None, "cancelled")
    - 1-N: 1-indexed numeric order in displayed table
    - Exact UUID (case-insensitive) or exact name/callsign (case-insensitive)
    - Short UUID prefix (>= 4 characters, case-insensitive)

    Returns:
    - (selected_uuid, None) on success
    - (None, "cancelled") on user cancellation
    - (None, error_message) on resolution failure or ambiguity
    """
    cleaned = target_str.strip()
    if not cleaned or cleaned.lower() in ("0", "q", "quit", "exit", "cancel", "back") or (cleaned.isdigit() and int(cleaned) == 0):
        return None, "cancelled"

    # 1. Numeric index (1 to N)
    if cleaned.isdigit():
        val = int(cleaned)
        if 1 <= val <= len(planes):
            return planes[val - 1].get("id"), None
        cleaned_lower = cleaned.lower()
        for p in planes:
            if str(p.get("id", "")).lower() == cleaned_lower or str(p.get("name", "")).lower() == cleaned_lower:
                return p.get("id"), None
        if len(cleaned) >= 4:
            matches = [p for p in planes if str(p.get("id", "")).lower().startswith(cleaned_lower)]
            if len(matches) == 1:
                return matches[0].get("id"), None
            elif len(matches) > 1:
                return None, f"Ambiguous prefix '{target_str}' matches multiple aircraft ({len(matches)}). Please provide more characters or select by index."
        return None, f"Aircraft identified by '{target_str}' not found (index out of range: 1-{len(planes)})."

    # 2. Non-numeric input (UUID, short UUID prefix, or callsign string)
    cleaned_lower = cleaned.lower()
    for p in planes:
        if str(p.get("id", "")).lower() == cleaned_lower or str(p.get("name", "")).lower() == cleaned_lower:
            return p.get("id"), None

    matches = [p for p in planes if str(p.get("id", "")).lower().startswith(cleaned_lower)]
    if len(matches) == 1:
        return matches[0].get("id"), None
    elif len(matches) > 1:
        return None, f"Ambiguous prefix '{target_str}' matches multiple aircraft ({len(matches)}). Please provide full UUID or select by index."

    return None, f"Aircraft identified by '{target_str}' not found."


def print_airplanes_table(planes: List[Dict]):
    """Format and print active aircraft table."""
    if not planes:
        print("  [Notice] No active aircraft in airspace.")
        return
    print(f"  {'No.':<4} {'Name':<16} {'UUID (8-char)':<14} {'Lat':<12} {'Lon':<12} {'Alt (ft)':<12} {'Target Alt':<12} {'Hdg':<10} {'Target Hdg':<12} {'Speed (kts)':<12}")
    print("  " + "-" * 110)
    for idx, p in enumerate(planes, 1):
        name = str(p.get("name", "Unknown"))[:15]
        uuid_short = str(p.get("id", ""))[:8]
        lat = f"{p.get('lat', 0.0):.4f}"
        lon = f"{p.get('lon', 0.0):.4f}"
        alt = f"{p.get('altitude_ft', 0.0):.1f}"
        t_alt = f"{p.get('target_altitude_ft'):.1f}" if p.get('target_altitude_ft') is not None else "-"
        hdg = f"{p.get('heading', 0.0):.1f}°"
        t_hdg = f"{p.get('target_heading'):.1f}°" if p.get('target_heading') is not None else "-"
        spd = f"{p.get('speed_knots', 0.0):.1f}"
        print(f"  {idx:<4} {name:<16} {uuid_short:<14} {lat:<12} {lon:<12} {alt:<12} {t_alt:<12} {hdg:<10} {t_hdg:<12} {spd:<12}")


def monitor_airplanes_status(base_url: str, interval: float = 5.0, count: Optional[int] = None):
    """Periodically monitor aircraft status (default refresh every 5s)."""
    print(f"\n[Monitor Started] Refreshing aircraft position, altitude, and heading every {interval:.1f}s (Press Ctrl+C to stop)...")
    ticks = 0
    try:
        while True:
            cur_time = time.strftime("%Y-%m-%d %H:%M:%S")
            planes = get_airplanes(base_url)
            print(f"\n[{cur_time}] Active aircraft (Total: {len(planes)}):")
            print_airplanes_table(planes)
            ticks += 1
            if count is not None and ticks >= count:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n[Monitor Stopped] Exited aircraft status monitoring.")


# ==============================================================================
# AI Connectivity Testing (Test AI Endian)
# All communication with Controller and Pilot MUST go through Webview proxy.
# ==============================================================================

def _cell_width(s: str) -> int:
    """Calculate terminal display width for CJK and ASCII strings."""
    return sum(2 if unicodedata.east_asian_width(c) in ('F', 'W') else 1 for c in s)


def _pad_cell(s: str, target_width: int, align: str = "left") -> str:
    """Pad (or truncate) string with whitespace according to visual display width."""
    w = _cell_width(s)
    if w > target_width:
        truncated = ""
        cur_w = 0
        for ch in s:
            ch_w = 2 if unicodedata.east_asian_width(ch) in ('F', 'W') else 1
            if cur_w + ch_w > target_width:
                break
            truncated += ch
            cur_w += ch_w
        s = truncated
        w = cur_w
    pad = max(0, target_width - w)
    if align == "right":
        return " " * pad + s
    return s + " " * pad


def test_controller_ai_endian(
    platform_url: Optional[str] = None,
    timeout: float = 30.0,
    controller_url: Optional[str] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """
    Send an AI connectivity test request to Controller strictly via Webview (Platform) proxy.
    Direct connection to Controller process ports is strictly prohibited.
    """
    plat_url = resolve_base_url(platform_url)
    proxy_url = f"{plat_url}/api/controller/test_ai?timeout={timeout}"

    try:
        res = http_get(proxy_url, timeout=timeout + 5.0)
        if isinstance(res, dict):
            res_dict = dict(res)
            res_dict.setdefault("channel", f"webview proxy ({plat_url})")
            return res_dict
    except Exception as e_proxy:
        return {
            "status": "error",
            "source": "agent_controller",
            "available": False,
            "error": f"Webview proxy error: {e_proxy}",
            "channel": "failed"
        }
    return {
        "status": "error",
        "source": "agent_controller",
        "available": False,
        "error": "No valid response received from webview proxy",
        "channel": "failed"
    }


def test_pilot_ai_endian(
    platform_url: Optional[str] = None,
    timeout: float = 30.0,
    pilot_url: Optional[str] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """
    Send an AI connectivity test request to Pilot strictly via Webview (Platform) proxy.
    Direct connection to Pilot process ports is strictly prohibited.
    """
    plat_url = resolve_base_url(platform_url)
    proxy_url = f"{plat_url}/api/pilot/test_ai?timeout={timeout}"

    try:
        res = http_get(proxy_url, timeout=timeout + 5.0)
        if isinstance(res, dict):
            res_dict = dict(res)
            res_dict.setdefault("channel", f"webview proxy ({plat_url})")
            return res_dict
    except Exception as e_proxy:
        return {
            "status": "error",
            "source": "agent_pilot",
            "available": False,
            "error": f"Webview proxy error: {e_proxy}",
            "channel": "failed"
        }
    return {
        "status": "error",
        "source": "agent_pilot",
        "available": False,
        "error": "No valid response received from webview proxy",
        "channel": "failed"
    }


def test_ai_endian(
    target: str = "all",
    platform_url: Optional[str] = None,
    timeout: float = 30.0,
    controller_url: Optional[str] = None,
    pilot_url: Optional[str] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """
    Unified AI connectivity test runner (Test AI Endian).
    target options: 'all', 'controller', 'ctrl', 'pilot'
    Runs concurrently across threads to minimize end-to-end wait time.
    All communication is routed strictly through Webview proxy endpoints.
    """
    target = (target or "all").lower()
    results: Dict[str, Any] = {}

    need_controller = target in ("all", "controller", "ctrl")
    need_pilot = target in ("all", "pilot")

    if need_controller and need_pilot:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            future_ctrl = executor.submit(
                test_controller_ai_endian,
                platform_url=platform_url,
                timeout=timeout,
            )
            future_pilot = executor.submit(
                test_pilot_ai_endian,
                platform_url=platform_url,
                timeout=timeout,
            )
            results["controller"] = future_ctrl.result()
            results["pilot"] = future_pilot.result()
    elif need_controller:
        results["controller"] = test_controller_ai_endian(
            platform_url=platform_url,
            timeout=timeout,
        )
    elif need_pilot:
        results["pilot"] = test_pilot_ai_endian(
            platform_url=platform_url,
            timeout=timeout,
        )

    return results


def print_ai_test_results(results: Dict[str, Any]):
    """Format and print the AI connectivity test report."""
    hdr_svc = _pad_cell("Component / Service", 32)
    hdr_status = _pad_cell("Status", 10)
    hdr_lat = _pad_cell("Latency", 10)
    hdr_type = _pad_cell("AI Type", 12)
    hdr_chan = _pad_cell("Channel", 16)
    hdr_resp = "Response Summary"

    header_line = f"  {hdr_svc}  {hdr_status}  {hdr_lat}  {hdr_type}  {hdr_chan}  {hdr_resp}"
    sep_width = max(len(header_line) + 15, 115)
    border = "=" * sep_width
    divider = "  " + "-" * (sep_width - 2)

    print("\n" + border)
    title = "AI Connectivity Test Report (Test AI Endian Results)"
    print(title.center(sep_width))
    print(border)
    print(header_line)
    print(divider)

    all_passed = True
    total = len(results)
    passed = 0
    errors = []

    for key, res in results.items():
        name = "Controller (agent_controller)" if key == "controller" else "Pilot (agent_pilot)"
        is_ok = res.get("status") == "success" and res.get("available") is True
        if is_ok:
            passed += 1
            raw_status = "[PASS]"
            status_str = "\033[32m[PASS]\033[0m" if sys.stdout.isatty() else "[PASS]"
        else:
            all_passed = False
            raw_status = "[FAIL]"
            status_str = "\033[31m[FAIL]\033[0m" if sys.stdout.isatty() else "[FAIL]"
            err_msg = res.get("error") or res.get("response") or "Unknown error"
            errors.append((name, err_msg))

        latency_val = res.get("latency")
        latency_str = f"{latency_val:.2f}s" if isinstance(latency_val, (int, float)) else "-"
        client_type = str(res.get("client_type", "-"))

        raw_channel = str(res.get("channel", "-"))
        if "webview proxy" in raw_channel.lower():
            channel_display = "webview proxy"
        else:
            channel_display = raw_channel

        detail = res.get("response") if is_ok else (res.get("error") or "-")
        detail_str = str(detail).replace("\n", " ").strip()
        if len(detail_str) > 35:
            detail_str = detail_str[:32] + "..."

        c_name = _pad_cell(name, 32)
        status_pad = " " * max(0, 10 - _cell_width(raw_status))
        c_status = status_str + status_pad
        c_lat = _pad_cell(latency_str, 10)
        c_type = _pad_cell(client_type, 12)
        c_chan = _pad_cell(channel_display, 16)

        print(f"  {c_name}  {c_status}  {c_lat}  {c_type}  {c_chan}  {detail_str}")

    print(divider)
    summary_color = "\033[32m" if all_passed else "\033[31m"
    reset_color = "\033[0m" if sys.stdout.isatty() else ""
    summary_text = "ALL PASSED" if all_passed else "ISSUES DETECTED"
    print(f"  Test Summary: {summary_color}{summary_text} ({passed}/{total} available){reset_color}")

    if errors:
        print("\n  [Detailed Error Information]")
        for err_name, err_detail in errors:
            print(f"    - {err_name}: {err_detail}")

    print(border)


def handle_test_ai_action(
    target: str = "all",
    controller_url: Optional[str] = None,
    pilot_url: Optional[str] = None,
    platform_url: Optional[str] = None,
    timeout: float = 30.0,
    output_json: bool = False,
) -> bool:
    """Execute AI connectivity test and display results. Returns True if all passed."""
    if not output_json:
        target_name = "All (Controller + Pilot)" if target == "all" else ("Controller" if target in ("controller", "ctrl") else "Pilot")
        print(f"[Platform URL] {platform_url or 'Auto-detected'}")
        print(f"[Running Test] Sending AI connectivity test request for {target_name} via Webview...")

    results = test_ai_endian(
        target=target,
        platform_url=platform_url,
        timeout=timeout,
        controller_url=controller_url,
        pilot_url=pilot_url,
    )

    if output_json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        print_ai_test_results(results)

    all_ok = all(
        res.get("status") == "success" and res.get("available") is True
        for res in results.values()
    )
    return all_ok


# ==============================================================================
# UI Mode 1: Interactive Menu Console (English)
# ==============================================================================

def menu_on_off(base_url: str):
    """Level-1 Menu: ON/OFF - System Switch Management."""
    while True:
        try:
            sim_run = get_simulator_status(base_url)
            ctrl_en = get_controller_status(base_url)
            sim_str = "[RUNNING] (True)" if sim_run else "[STOPPED] (False)"
            ctrl_str = "[ENABLED] (True)" if ctrl_en else "[DISABLED] (False)"
        except Exception as e:
            sim_str = f"Query failed: {e}"
            ctrl_str = f"Query failed: {e}"

        print("\n" + "=" * 58)
        print("  [Level-1 Menu] ON/OFF - System Switch Management")
        print("=" * 58)
        print("  Current Status:")
        print(f"    - Flight Simulator (airplane_simulator): {sim_str}")
        print(f"    - AI Controller    (agent_controller):    {ctrl_str}")
        print("-" * 58)
        print("  [1] Show Simulator Status")
        print("  [2] Start Simulator")
        print("  [3] Stop Simulator")
        print("  [4] Show AI Controller Status")
        print("  [5] Enable AI Controller")
        print("  [6] Disable AI Controller")
        print("  [0] Return to Main Menu")
        print("=" * 58)
        choice = input("Please select an option [0-6]: ").strip()
        if choice in ("0", "q", "exit", "back"):
            break
        elif choice == "1":
            print(f"\n[Simulator Status] Flight simulator is currently: {sim_str}")
        elif choice == "2":
            res = set_simulator_status(base_url, True)
            print(f"\n[Success] Flight simulator started:\n{json.dumps(res, indent=2, ensure_ascii=False)}")
        elif choice == "3":
            res = set_simulator_status(base_url, False)
            print(f"\n[Success] Flight simulator stopped:\n{json.dumps(res, indent=2, ensure_ascii=False)}")
        elif choice == "4":
            print(f"\n[Controller Status] AI Controller is currently: {ctrl_str}")
        elif choice == "5":
            res = set_controller_status(base_url, True)
            print(f"\n[Success] AI Controller enabled:\n{json.dumps(res, indent=2, ensure_ascii=False)}")
        elif choice == "6":
            res = set_controller_status(base_url, False)
            print(f"\n[Success] AI Controller disabled:\n{json.dumps(res, indent=2, ensure_ascii=False)}")
        else:
            print("\n[Notice] Invalid option. Please try again.")
        input("\nPress Enter to continue...")


def menu_airplane(base_url: str):
    """Level-1 Menu: Airplane - Aircraft Management."""
    while True:
        print("\n" + "=" * 58)
        print("  [Level-1 Menu] Airplane - Aircraft Management")
        print("=" * 58)
        print("  [1] add    - Add new aircraft (Guided position, altitude, heading)")
        print("  [2] update - Update aircraft targets (Target heading, altitude, speed)")
        print("  [3] delete - Delete aircraft (Select by index or UUID)")
        print("  [4] status - Status monitor (Refresh positions every 5s)")
        print("  [0] Return to Main Menu")
        print("=" * 58)
        choice = input("Please select an option [0-4]: ").strip().lower()
        if choice in ("0", "q", "exit", "back"):
            break
        elif choice in ("1", "add"):
            print("\n--- Add New Aircraft (Press Enter to use default or auto-calculated values) ---")
            name = input("Enter aircraft callsign/name [Enter for auto-generated]: ").strip() or None

            alt_str = input("Enter initial altitude (ft, default 5000.0) [Enter for default]: ").strip()
            altitude = float(alt_str) if alt_str else 5000.0

            hdg_str = input("Enter initial heading (degrees 0-360, default 360.0) [Enter for default]: ").strip()
            heading = float(hdg_str) if hdg_str else 360.0

            spd_str = input("Enter initial speed (knots, default 150.0) [Enter for default]: ").strip()
            speed = float(spd_str) if spd_str else 150.0

            lat_str = input("Enter initial latitude [Enter for auto-calculation]: ").strip()
            lon_str = input("Enter initial longitude [Enter for auto-calculation]: ").strip()

            lat = float(lat_str) if lat_str else None
            lon = float(lon_str) if lon_str else None

            print("\nRegistering aircraft with platform...")
            try:
                res, pos_desc = add_airplane(
                    base_url, name=name, lat=lat, lon=lon, altitude=altitude, heading=heading, speed=speed
                )
                print(f"[Position Calculation] {pos_desc}")
                print(f"[Success] New aircraft details:\n{json.dumps(res, indent=2, ensure_ascii=False)}")
            except Exception as e:
                print(f"[Error] Failed to add aircraft: {e}", file=sys.stderr)
            input("\nPress Enter to continue...")

        elif choice in ("2", "update"):
            try:
                planes = get_airplanes(base_url)
            except Exception as e:
                print(f"[Error] Failed to query aircraft list: {e}", file=sys.stderr)
                input("\nPress Enter to continue...")
                continue

            if not planes:
                print("\n[Notice] No active aircraft found in airspace. Please add an aircraft first.")
                input("\nPress Enter to continue...")
                continue

            print(f"\nActive aircraft in airspace (Total: {len(planes)}):")
            print_airplanes_table(planes)
            target_str = input("\nEnter aircraft index (1-N) or full ID to update [Enter 0 to cancel]: ").strip()
            selected_id, err_msg = resolve_aircraft_selection(planes, target_str)
            if err_msg == "cancelled":
                print("\n[Notice] Operation cancelled.")
                input("\nPress Enter to continue...")
                continue
            if not selected_id:
                print(f"[Error] {err_msg}")
                input("\nPress Enter to continue...")
                continue

            print(f"\nUpdating aircraft (ID: {selected_id}):")
            hdg_in = input("Enter target heading (degrees 0-360) [Enter to keep unchanged]: ").strip()
            alt_in = input("Enter target altitude (ft) [Enter to keep unchanged]: ").strip()
            spd_in = input("Enter speed (knots) [Enter to keep unchanged]: ").strip()

            target_heading = float(hdg_in) if hdg_in else None
            target_altitude = float(alt_in) if alt_in else None
            target_speed = float(spd_in) if spd_in else None

            if target_heading is None and target_altitude is None and target_speed is None:
                print("\n[Notice] No parameters specified. Update cancelled.")
            else:
                try:
                    res = update_airplane(
                        base_url, selected_id, heading=target_heading, altitude=target_altitude, speed=target_speed
                    )
                    print(f"\n[Success] Update result:\n{json.dumps(res, indent=2, ensure_ascii=False)}")
                except Exception as e:
                    print(f"[Error] Failed to update aircraft: {e}", file=sys.stderr)
            input("\nPress Enter to continue...")

        elif choice in ("3", "delete", "del", "remove"):
            try:
                planes = get_airplanes(base_url)
            except Exception as e:
                print(f"[Error] Failed to query aircraft list: {e}", file=sys.stderr)
                input("\nPress Enter to continue...")
                continue

            if not planes:
                print("\n[Notice] No active aircraft found in airspace to delete.")
                input("\nPress Enter to continue...")
                continue

            print(f"\nActive aircraft in airspace (Total: {len(planes)}):")
            print_airplanes_table(planes)
            target_str = input("\nEnter aircraft index (1-N) or full ID to delete [Enter 0 to cancel]: ").strip()
            selected_id, err_msg = resolve_aircraft_selection(planes, target_str)
            if err_msg == "cancelled":
                print("\n[Notice] Operation cancelled.")
                input("\nPress Enter to continue...")
                continue
            if not selected_id:
                print(f"[Error] {err_msg}")
                input("\nPress Enter to continue...")
                continue

            try:
                res = delete_airplane(base_url, selected_id)
                if isinstance(res, dict) and res.get("status") == "error":
                    print(f"\n[Error] Failed to delete aircraft: {res.get('message', 'Unknown error')}", file=sys.stderr)
                else:
                    print(f"\n[Success] Aircraft (ID: {selected_id}) deleted successfully:\n{json.dumps(res, indent=2, ensure_ascii=False)}")
            except Exception as e:
                print(f"\n[Error] Failed to delete aircraft: {e}", file=sys.stderr)
            input("\nPress Enter to continue...")

        elif choice in ("4", "status", "Status"):
            monitor_airplanes_status(base_url, interval=5.0)
            input("\nPress Enter to continue...")
        else:
            print("\n[Notice] Invalid option. Please try again.")
            input("\nPress Enter to continue...")


def menu_airport(base_url: str):
    """Level-1 Menu: Airport - Airport Configuration."""
    while True:
        try:
            airport_info = get_airport(base_url)
        except Exception:
            airport_info = {}
        a_lat = airport_info.get("lat")
        a_lon = airport_info.get("lon")
        a_hdg = airport_info.get("runway_heading")
        info_str = f"Lat={a_lat}, Lon={a_lon}, Runway Heading={a_hdg}°" if a_lat is not None else "Not configured"

        print("\n" + "=" * 58)
        print("  [Level-1 Menu] Airport - Airport Configuration")
        print("=" * 58)
        print(f"  Current Airport: {info_str}")
        print("-" * 58)
        print("  [1] Set/Update airport coordinates (Guided input or random)")
        print("  [2] View current airport configuration")
        print("  [0] Return to Main Menu")
        print("=" * 58)
        choice = input("Please select an option [0-2]: ").strip()
        if choice in ("0", "q", "exit", "back"):
            break
        elif choice == "2":
            print(f"\n[Current Airport Configuration]\n{json.dumps(airport_info, indent=2, ensure_ascii=False)}")
            input("\nPress Enter to continue...")
        elif choice == "1":
            print("\n--- Set Airport Location (Press Enter to use random location) ---")
            lat_str = input("Enter airport latitude [Enter for random]: ").strip()
            lon_str = input("Enter airport longitude [Enter for random]: ").strip()
            hdg_str = input("Enter runway heading (degrees 0-360) [Enter for default 360]: ").strip()

            if not lat_str or not lon_str:
                lat, lon = generate_random_coordinates()
                print(f"[Random Coordinates] Empty coordinates. Generated random airport coordinates: Lat={lat}, Lon={lon}")
            else:
                lat = float(lat_str)
                lon = float(lon_str)

            runway_heading = float(hdg_str) if hdg_str else 360.0

            try:
                res = set_airport(base_url, lat=lat, lon=lon, runway_heading=runway_heading)
                print(f"\n[Success] Airport configured successfully:\n{json.dumps(res, indent=2, ensure_ascii=False)}")
            except Exception as e:
                print(f"[Error] Failed to configure airport: {e}", file=sys.stderr)
            input("\nPress Enter to continue...")
        else:
            print("\n[Notice] Invalid option. Please try again.")
            input("\nPress Enter to continue...")


def menu_test_ai_endian(base_url: str):
    """Level-1 Menu: Test AI Endian - AI Connectivity Test (Controller & Pilot)."""
    while True:
        print("\n" + "=" * 58)
        print("  [Level-1 Menu] Test AI Endian - AI Connectivity Test")
        print("=" * 58)
        print("  [1] Test All AI Endpoints (Agent Controller + Agent Pilot)")
        print("  [2] Test Controller AI Only (Agent Controller)")
        print("  [3] Test Pilot AI Only (Agent Pilot)")
        print("  [0] Return to Main Menu")
        print("=" * 58)
        choice = input("Please select an option [0-3]: ").strip().lower()
        if choice in ("0", "q", "exit", "back"):
            break
        elif choice == "1":
            handle_test_ai_action("all", platform_url=base_url)
            input("\nPress Enter to continue...")
        elif choice in ("2", "controller", "ctrl"):
            handle_test_ai_action("controller", platform_url=base_url)
            input("\nPress Enter to continue...")
        elif choice in ("3", "pilot"):
            handle_test_ai_action("pilot", platform_url=base_url)
            input("\nPress Enter to continue...")
        else:
            print("\n[Notice] Invalid option. Please try again.")
            input("\nPress Enter to continue...")


def run_interactive_mode(base_url: str):
    """Launch interactive console menu."""
    while True:
        print("\n" + "=" * 58)
        print("  Realtime Aviation Multi-Agent System - Console (Platform CLI)")
        print(f"  Target Platform URL: {base_url}")
        print("=" * 58)
        print("  [1] ON/OFF         - System Switch Management (Simulator / AI Controller)")
        print("  [2] Airplane       - Aircraft Management (Add / Update / Delete / Status Monitor)")
        print("  [3] Airport        - Airport Configuration (Coordinates / Runway Heading)")
        print("  [4] Test AI Endian - AI Connectivity Test (Controller & Pilot)")
        print("  [0] Exit Console")
        print("=" * 58)
        choice = input("Please select an option [0-4]: ").strip().lower()
        if choice in ("0", "q", "exit", "quit"):
            print("\nThank you for using Platform CLI. Exited console.")
            break
        elif choice in ("1", "on-off", "on/off", "switch"):
            menu_on_off(base_url)
        elif choice in ("2", "airplane", "plane"):
            menu_airplane(base_url)
        elif choice in ("3", "airport"):
            menu_airport(base_url)
        elif choice in ("4", "test", "test-ai", "test ai", "test-ai-endian", "test ai endian"):
            menu_test_ai_endian(base_url)
        else:
            print("\n[Notice] Invalid option. Please enter a number between 0 and 4.")
            input("Press Enter to continue...")


# ==============================================================================
# UI Mode 2: Command-Line Arguments Mode (CLI Arguments Mode)
# ==============================================================================

def build_parser() -> argparse.ArgumentParser:
    """Build argument parser supporting subcommands and mutual exclusion checks."""
    parser = argparse.ArgumentParser(
        prog="cli.py",
        description="Realtime Aviation Multi-Agent System - Terminal Console and CLI Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Example Usage:
  # Interactive Menu Mode (run without arguments):
  uv run cli/cli.py

  # Legacy controller switch commands (Control AI Controller):
  uv run cli/cli.py status
  uv run cli/cli.py on
  uv run cli/cli.py off
  uv run cli/cli.py toggle

  # ON/OFF Switch Management:
  uv run cli/cli.py on-off simulator on
  uv run cli/cli.py on-off simulator status
  uv run cli/cli.py on-off controller off

  # Aircraft Entity Management:
  uv run cli/cli.py airplane add --name Drone-1 --altitude 6000 --heading 180
  uv run cli/cli.py airplane add --random-pos
  uv run cli/cli.py airplane update --id <UUID> --heading 090 --altitude 3000
  uv run cli/cli.py airplane delete --id <UUID>
  uv run cli/cli.py airplane status
  uv run cli/cli.py airplane status --watch

  # Airport Configuration:
  uv run cli/cli.py airport set --lat 34.817 --lon -85.643 --heading 40
  uv run cli/cli.py airport set --random-pos
  uv run cli/cli.py airport status

  # AI Connectivity Test (Test AI Endian):
  uv run cli/cli.py test-ai
  uv run cli/cli.py test-ai-endian
  uv run cli/cli.py test-ai --target controller
  uv run cli/cli.py test-ai --target pilot --json
"""
    )

    parser.add_argument(
        "--url",
        dest="url",
        default=None,
        help="Platform base URL (default: auto-detect http://localhost:8010 or http://localhost:8000)"
    )

    # Top-level AI test flag and associated parameters
    parser.add_argument(
        "--test-ai",
        "--test-ai-endian",
        dest="test_ai_flag",
        action="store_true",
        help="Test AI connectivity for Controller and Pilot processes (Test AI Endian)"
    )
    parser.add_argument(
        "--target", "-t",
        dest="target",
        choices=["all", "controller", "ctrl", "pilot"],
        default="all",
        help="Target component to test (all: Controller and Pilot; controller/ctrl: Controller only; pilot: Pilot only; default: all)"
    )
    parser.add_argument(
        "--controller-url",
        dest="controller_url",
        default=None,
        help="Controller service address (optional/legacy, communication uses Platform webview proxy)"
    )
    parser.add_argument(
        "--pilot-url",
        dest="pilot_url",
        default=None,
        help="Pilot service address (optional/legacy, communication uses Platform webview proxy)"
    )
    parser.add_argument(
        "--timeout",
        dest="timeout",
        type=float,
        default=30.0,
        help="Request timeout in seconds (default: 30.0)"
    )
    parser.add_argument(
        "--json",
        dest="output_json",
        action="store_true",
        help="Output raw test results in JSON format"
    )

    subparsers = parser.add_subparsers(dest="command", help="Subcommand modules (on-off / airplane / airport / test-ai)")

    # 1. on-off / switch
    p_onoff = subparsers.add_parser("on-off", aliases=["switch"], help="System switch control (Simulator / AI Controller)")
    p_onoff.add_argument("target", choices=["simulator", "controller", "sim", "ctrl"], help="Target component")
    p_onoff.add_argument("action", choices=["status", "on", "start", "off", "stop", "toggle"], help="Action command")

    # Direct shortcut subcommands
    p_sim = subparsers.add_parser("simulator", help="Flight simulator switch shortcut command")
    p_sim.add_argument("action", choices=["status", "on", "start", "off", "stop", "toggle"], help="Action command")

    p_ctrl = subparsers.add_parser("controller", help="AI controller switch shortcut command")
    p_ctrl.add_argument("action", choices=["status", "on", "start", "off", "stop", "toggle"], help="Action command")

    # 2. airplane
    p_plane = subparsers.add_parser("airplane", aliases=["plane"], help="Aircraft management (add / update / delete / status)")
    plane_subs = p_plane.add_subparsers(dest="airplane_action", help="Aircraft subcommands")

    # airplane add
    p_padd = plane_subs.add_parser("add", help="Add new aircraft")
    p_padd.add_argument("--name", dest="name", default=None, help="Aircraft name/callsign")
    p_padd.add_argument("--altitude", dest="altitude", type=float, default=5000.0, help="Initial altitude (ft, default: 5000)")
    p_padd.add_argument("--heading", dest="heading", type=float, default=360.0, help="Initial heading (degrees 0-360, default: 360)")
    p_padd.add_argument("--speed", dest="speed", type=float, default=150.0, help="Initial speed (knots, default: 150)")
    p_padd.add_argument("--lat", dest="lat", type=float, default=None, help="Initial latitude")
    p_padd.add_argument("--lon", dest="lon", type=float, default=None, help="Initial longitude")
    p_padd.add_argument("--random-pos", dest="random_pos", action="store_true", help="Force random position (mutually exclusive with --lat/--lon)")

    # airplane update
    p_pupdate = plane_subs.add_parser("update", help="Update aircraft parameters")
    p_pupdate.add_argument("--id", dest="id", required=False, default=None, help="Target aircraft UUID (required)")
    p_pupdate.add_argument("--heading", dest="heading", type=float, default=None, help="Target heading (degrees 0-360)")
    p_pupdate.add_argument("--altitude", dest="altitude", type=float, default=None, help="Target altitude (ft)")
    p_pupdate.add_argument("--speed", dest="speed", type=float, default=None, help="Speed (knots)")

    # airplane delete
    p_pdelete = plane_subs.add_parser("delete", aliases=["del", "remove"], help="Delete aircraft by UUID")
    p_pdelete.add_argument("--id", dest="id", required=False, default=None, help="Target aircraft UUID (required)")

    # airplane status
    p_pstatus = plane_subs.add_parser("status", aliases=["list"], help="View or monitor aircraft status")
    p_pstatus.add_argument("--watch", dest="watch", action="store_true", help="Continuous monitoring mode (refreshes every 5s)")
    p_pstatus.add_argument("--interval", dest="interval", type=float, default=5.0, help="Monitoring refresh interval in seconds (default: 5.0)")
    p_pstatus.add_argument("--count", dest="count", type=int, default=None, help="Number of refreshes before exit (default: indefinite)")

    # 3. airport
    p_port = subparsers.add_parser("airport", help="Airport location configuration (set / status)")
    port_subs = p_port.add_subparsers(dest="airport_action", help="Airport subcommands")

    # airport set
    p_pset = port_subs.add_parser("set", help="Configure airport location")
    p_pset.add_argument("--lat", dest="lat", type=float, default=None, help="Airport latitude")
    p_pset.add_argument("--lon", dest="lon", type=float, default=None, help="Airport longitude")
    p_pset.add_argument("--heading", dest="heading", type=float, default=360.0, help="Runway heading (degrees 0-360, default: 360)")
    p_pset.add_argument("--random-pos", dest="random_pos", action="store_true", help="Use random airport position (mutually exclusive with --lat/--lon)")

    # airport status / get
    port_subs.add_parser("status", aliases=["get"], help="View current airport configuration")

    # 4. test-ai / test-ai-endian (Test AI Endian)
    p_test = subparsers.add_parser(
        "test-ai",
        aliases=["test-ai-endian", "test_ai", "test_ai_endian", "test"],
        help="Test AI connectivity for Controller and Pilot processes (Test AI Endian)"
    )
    p_test.add_argument(
        "--target", "-t",
        dest="target",
        choices=["all", "controller", "ctrl", "pilot"],
        default="all",
        help="Target component to test (all: Controller and Pilot; controller/ctrl: Controller only; pilot: Pilot only; default: all)"
    )
    p_test.add_argument(
        "--controller-url",
        dest="controller_url",
        default=None,
        help="Controller service address (optional/legacy, communication uses Platform webview proxy)"
    )
    p_test.add_argument(
        "--pilot-url",
        dest="pilot_url",
        default=None,
        help="Pilot service address (optional/legacy, communication uses Platform webview proxy)"
    )
    p_test.add_argument(
        "--timeout",
        dest="timeout",
        type=float,
        default=30.0,
        help="Request timeout in seconds (default: 30.0)"
    )
    p_test.add_argument(
        "--json",
        dest="output_json",
        action="store_true",
        help="Output raw test results in JSON format"
    )

    return parser


def handle_controller_action(base_url: str, action: str):
    """Execute AI Controller switch action."""
    action = action.lower()
    if action in ("status", "get", "info"):
        current = get_controller_status(base_url)
        state_text = "[ENABLED] (True)" if current else "[DISABLED] (False)"
        print(f"[Platform URL] {base_url}")
        print(f"[Current Status] AI Controller is: {state_text}")
    elif action in ("on", "start", "enable"):
        res = set_controller_status(base_url, True)
        print(f"[Platform URL] {base_url}")
        print("[Success] AI Controller set to: [ENABLED] (True)")
        print(f"[Response] {res}")
    elif action in ("off", "stop", "disable"):
        res = set_controller_status(base_url, False)
        print(f"[Platform URL] {base_url}")
        print("[Success] AI Controller set to: [DISABLED] (False)")
        print(f"[Response] {res}")
    elif action == "toggle":
        current = get_controller_status(base_url)
        new_target = not current
        res = set_controller_status(base_url, new_target)
        old_text = "ENABLED" if current else "DISABLED"
        new_text = "ENABLED" if new_target else "DISABLED"
        print(f"[Platform URL] {base_url}")
        print(f"[Success] AI Controller state toggled: {old_text} -> [{new_text}] ({new_target})")
        print(f"[Response] {res}")


def handle_simulator_action(base_url: str, action: str):
    """Execute Flight Simulator switch action."""
    action = action.lower()
    if action in ("status", "get", "info"):
        current = get_simulator_status(base_url)
        state_text = "[RUNNING] (True)" if current else "[STOPPED] (False)"
        print(f"[Platform URL] {base_url}")
        print(f"[Current Status] Flight Simulator is: {state_text}")
    elif action in ("on", "start", "enable"):
        res = set_simulator_status(base_url, True)
        print(f"[Platform URL] {base_url}")
        print("[Success] Flight Simulator started: [RUNNING] (True)")
        print(f"[Response] {res}")
    elif action in ("off", "stop", "disable"):
        res = set_simulator_status(base_url, False)
        print(f"[Platform URL] {base_url}")
        print("[Success] Flight Simulator stopped: [STOPPED] (False)")
        print(f"[Response] {res}")
    elif action == "toggle":
        current = get_simulator_status(base_url)
        new_target = not current
        res = set_simulator_status(base_url, new_target)
        old_text = "RUNNING" if current else "STOPPED"
        new_text = "RUNNING" if new_target else "STOPPED"
        print(f"[Platform URL] {base_url}")
        print(f"[Success] Flight Simulator state toggled: {old_text} -> [{new_text}] ({new_target})")
        print(f"[Response] {res}")


def run_cli_args_mode(args: argparse.Namespace, base_url: str):
    """Execute CLI arguments mode."""
    cmd = args.command

    # 1. ON/OFF Switch Control
    if cmd in ("on-off", "switch"):
        target = args.target.lower()
        if target in ("simulator", "sim"):
            handle_simulator_action(base_url, args.action)
        else:
            handle_controller_action(base_url, args.action)
    elif cmd == "simulator":
        handle_simulator_action(base_url, args.action)
    elif cmd == "controller":
        handle_controller_action(base_url, args.action)

    # 2. Airplane Control
    elif cmd in ("airplane", "plane"):
        p_act = args.airplane_action
        if not p_act:
            print("[Error] Missing airplane subcommand (options: add, update, delete, status). Use --help for usage.", file=sys.stderr)
            sys.exit(2)

        if p_act == "add":
            # Mutual exclusion and missing check
            if args.random_pos and (args.lat is not None or args.lon is not None):
                print("[Error] Mutually exclusive options: --random-pos and --lat / --lon cannot be used together!", file=sys.stderr)
                sys.exit(2)
            if (args.lat is not None and args.lon is None) or (args.lat is None and args.lon is not None):
                print("[Error] Missing parameter: --lon must be specified when --lat is provided (or leave both empty).", file=sys.stderr)
                sys.exit(2)

            res, pos_desc = add_airplane(
                base_url,
                name=args.name,
                lat=args.lat,
                lon=args.lon,
                altitude=args.altitude,
                heading=args.heading,
                speed=args.speed,
            )
            print(f"[Position Calculation] {pos_desc}")
            print(f"[Success] Aircraft added successfully:\n{json.dumps(res, indent=2, ensure_ascii=False)}")

        elif p_act == "update":
            # Missing parameter check
            if not args.id:
                print("[Error] Missing parameter: update requires --id to specify aircraft UUID!", file=sys.stderr)
                sys.exit(2)
            if args.heading is None and args.altitude is None and args.speed is None:
                print("[Error] Missing parameter: update requires at least one of --heading, --altitude, or --speed!", file=sys.stderr)
                sys.exit(2)

            res = update_airplane(
                base_url,
                object_id=args.id,
                heading=args.heading,
                altitude=args.altitude,
                speed=args.speed,
            )
            print(f"[Success] Aircraft updated:\n{json.dumps(res, indent=2, ensure_ascii=False)}")

        elif p_act in ("delete", "del", "remove"):
            if not args.id or not args.id.strip():
                print("[Error] Missing parameter: delete requires --id to specify aircraft UUID!", file=sys.stderr)
                sys.exit(2)

            target_id = args.id.strip()
            res = delete_airplane(base_url, object_id=target_id)
            if isinstance(res, dict) and res.get("status") == "error":
                print(f"[Error] Failed to delete aircraft: {res.get('message', 'Aircraft not found')}", file=sys.stderr)
                sys.exit(1)
            print(f"[Success] Aircraft deleted successfully:\n{json.dumps(res, indent=2, ensure_ascii=False)}")

        elif p_act in ("status", "list"):
            if args.watch:
                monitor_airplanes_status(base_url, interval=args.interval, count=args.count)
            else:
                planes = get_airplanes(base_url)
                print(f"[Active Aircraft] (Total: {len(planes)}):")
                print_airplanes_table(planes)

    # 3. Airport Control
    elif cmd == "airport":
        a_act = args.airport_action
        if not a_act or a_act in ("status", "get"):
            airport_info = get_airport(base_url)
            print(f"[Current Airport Configuration]\n{json.dumps(airport_info, indent=2, ensure_ascii=False)}")
        elif a_act == "set":
            # Mutual exclusion and missing check
            if args.random_pos and (args.lat is not None or args.lon is not None):
                print("[Error] Mutually exclusive options: --random-pos and --lat / --lon cannot be used together!", file=sys.stderr)
                sys.exit(2)
            if (args.lat is not None and args.lon is None) or (args.lat is None and args.lon is not None):
                print("[Error] Missing parameter: --lon must be specified when --lat is provided (or use --random-pos).", file=sys.stderr)
                sys.exit(2)

            if args.random_pos or (args.lat is None and args.lon is None):
                lat, lon = generate_random_coordinates()
                print(f"[Random Coordinates] Generated random airport coordinates: Lat={lat}, Lon={lon}")
            else:
                lat = args.lat
                lon = args.lon

            res = set_airport(base_url, lat=lat, lon=lon, runway_heading=args.heading)
            print(f"[Success] Airport configured successfully:\n{json.dumps(res, indent=2, ensure_ascii=False)}")

    # 4. Test AI Endian Control
    elif cmd in ("test-ai", "test-ai-endian", "test_ai", "test_ai_endian", "test") or getattr(args, "test_ai_flag", False):
        all_ok = handle_test_ai_action(
            target=getattr(args, "target", "all"),
            controller_url=getattr(args, "controller_url", None),
            pilot_url=getattr(args, "pilot_url", None),
            platform_url=base_url,
            timeout=getattr(args, "timeout", 30.0),
            output_json=getattr(args, "output_json", False),
        )
        if not all_ok:
            sys.exit(1)


# ==============================================================================
# Application Entry Point
# ==============================================================================

def main():
    # 1. If executed without arguments, enter interactive menu mode (UI Mode 1)
    if len(sys.argv) == 1:
        base_url = resolve_base_url(None)
        try:
            run_interactive_mode(base_url)
        except (KeyboardInterrupt, EOFError):
            print("\nExited console.")
        sys.exit(0)

    # 2. Check for legacy single actions (e.g., cli.py status / on / off / toggle / test-ai)
    legacy_actions = {"status", "get", "info", "on", "start", "enable", "off", "stop", "disable", "toggle"}
    test_ai_actions = {"test-ai", "test-ai-endian", "test_ai", "test_ai_endian"}
    first_arg = sys.argv[1].lower()

    # Filter out possible --url overrides
    specified_url = None
    clean_argv = []
    i = 1
    while i < len(sys.argv):
        if sys.argv[i] == "--url" and i + 1 < len(sys.argv):
            specified_url = sys.argv[i + 1]
            i += 2
        elif sys.argv[i].startswith("--url="):
            specified_url = sys.argv[i].split("=", 1)[1]
            i += 1
        else:
            clean_argv.append(sys.argv[i])
            i += 1

    try:
        base_url = resolve_base_url(specified_url)
    except Exception as e:
        print(f"[Error] Unable to connect to Platform service: {e}", file=sys.stderr)
        sys.exit(1)

    # If cleansed arguments contain only a single legacy action, execute directly
    if len(clean_argv) == 1 and clean_argv[0].lower() in legacy_actions:
        try:
            handle_controller_action(base_url, clean_argv[0])
            sys.exit(0)
        except Exception as e:
            print(f"[Error] Action '{clean_argv[0]}' failed: {e}", file=sys.stderr)
            sys.exit(1)

    if len(clean_argv) == 1 and clean_argv[0].lower() in test_ai_actions:
        try:
            all_ok = handle_test_ai_action(
                target="all",
                platform_url=base_url,
                timeout=30.0,
                output_json=False,
            )
            sys.exit(0 if all_ok else 1)
        except Exception as e:
            print(f"[Error] AI test failed: {e}", file=sys.stderr)
            sys.exit(1)

    # 3. Otherwise use argparse to parse full subcommands and options (UI Mode 2)
    parser = build_parser()
    args = parser.parse_args()

    # If --test-ai / --test-ai-endian specified via top-level flag, run test directly
    if getattr(args, "test_ai_flag", False) or args.command in ("test-ai", "test-ai-endian", "test_ai", "test_ai_endian", "test"):
        try:
            run_cli_args_mode(args, base_url)
        except Exception as e:
            print(f"[Error] Execution failed: {e}", file=sys.stderr)
            sys.exit(1)
        return

    if not args.command:
        parser.print_help()
        sys.exit(0)

    try:
        run_cli_args_mode(args, base_url)
    except Exception as e:
        print(f"[Error] Execution failed: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
