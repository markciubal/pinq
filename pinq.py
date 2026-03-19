#!/usr/bin/env python3
"""
pinq — visualize network latency as light-speed distance over cell tower data.

Fetches your public IP, geolocates it, pings a target host, then queries
OpenStreetMap for nearby cell towers and draws a circle showing how far
a signal traveling at light speed could have reached in that RTT.

Add --server to enable multilateration: a second circle is drawn from the
Heroku server's geographic location, and the overlap region shows where
the target most likely sits in physical space.

Usage:
    python pinq.py
    python pinq.py --target 1.1.1.1
    python pinq.py --target 8.8.8.8 --radius 30000 --output my_map.html
    python pinq.py --server https://your-app.herokuapp.com
"""

import math
import re
import socket
import subprocess
import sys
import platform
import argparse
import time
import webbrowser
from pathlib import Path

import requests
import folium
from rich.console import Console
from rich.table import Table
from rich import box

console = Console()

# Physical constants
SPEED_OF_LIGHT_KM_S  = 299_792.458
FIBER_SPEED_KM_S     = SPEED_OF_LIGHT_KM_S / 1.5   # n≈1.5 for single-mode fiber

OVERPASS_URL = "https://overpass-api.de/api/interpreter"


# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------

def get_public_ip() -> str:
    for url in [
        "https://api.ipify.org",
        "https://checkip.amazonaws.com",
        "https://icanhazip.com",
    ]:
        try:
            r = requests.get(url, timeout=5)
            ip = r.text.strip()
            if ip:
                return ip
        except requests.RequestException:
            continue
    raise RuntimeError("Could not determine public IP address.")


def geolocate_ip(ip: str) -> dict:
    r = requests.get(f"http://ip-api.com/json/{ip}", timeout=10)
    data = r.json()
    if data.get("status") != "success":
        raise RuntimeError(f"Geolocation failed: {data.get('message', 'unknown')}")
    return data


def ping_icmp(host: str, count: int = 4) -> float | None:
    """ICMP ping — works on local machine, blocked in containers."""
    if platform.system().lower() == "windows":
        cmd = ["ping", "-n", str(count), host]
    else:
        cmd = ["ping", "-c", str(count), host]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        output = result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        return None

    for pat in [
        r"Average\s*=\s*([\d.]+)\s*ms",
        r"rtt [^=]+=\s*[\d.]+/([\d.]+)/",
        r"round-trip [^=]+=\s*[\d.]+/([\d.]+)/",
        r"avg(?:\s*=\s*|/)([\d.]+)(?:/[\d.]+)?\s*ms",
    ]:
        m = re.search(pat, output, re.IGNORECASE)
        if m:
            return float(m.group(1))
    return None


def ping_tcp(host: str, port: int = 443, count: int = 4, timeout: float = 5.0) -> float | None:
    """
    TCP connect-time latency — works everywhere including containers.
    Returns median RTT in ms, or None if all attempts fail.
    Used when --server is active so local and remote measurements are comparable.
    """
    rtts: list[float] = []
    for _ in range(count):
        try:
            t0 = time.perf_counter()
            with socket.create_connection((host, port), timeout=timeout):
                pass
            rtts.append((time.perf_counter() - t0) * 1000)
        except (socket.timeout, OSError):
            pass

    if not rtts:
        return None
    rtts.sort()
    mid = len(rtts) // 2
    return rtts[mid] if len(rtts) % 2 else (rtts[mid - 1] + rtts[mid]) / 2.0


def remote_ping(server_url: str, target: str, port: int = 443) -> dict | None:
    """Ask the pinq-server to TCP-ping target and return the full JSON response."""
    url = f"{server_url.rstrip('/')}/ping"
    try:
        r = requests.get(url, params={"target": target, "port": port}, timeout=30)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as exc:
        console.print(f"  [red]✗[/red]  Server ping failed: {exc}")
        return None


# ---------------------------------------------------------------------------
# Physics
# ---------------------------------------------------------------------------

def light_distance_km(rtt_ms: float, vacuum: bool = False) -> float:
    """One-way distance a signal could travel in rtt_ms / 2 milliseconds."""
    speed = SPEED_OF_LIGHT_KM_S if vacuum else FIBER_SPEED_KM_S
    return (rtt_ms / 1000 / 2) * speed


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6_371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def circle_intersection_area_km2(
    lat1: float, lon1: float, r1: float,
    lat2: float, lon2: float, r2: float,
) -> float | None:
    """
    Area of intersection of two circles (km²).
    Returns None if circles don't overlap or one contains the other fully.
    Used only for the legend — not rendered as a polygon.
    """
    d = haversine_km(lat1, lon1, lat2, lon2)
    if d >= r1 + r2 or d == 0:
        return None
    if d <= abs(r1 - r2):
        # One circle fully inside the other
        smaller = min(r1, r2)
        return math.pi * smaller ** 2

    a = (r1 ** 2 * math.acos((d ** 2 + r1 ** 2 - r2 ** 2) / (2 * d * r1)) +
         r2 ** 2 * math.acos((d ** 2 + r2 ** 2 - r1 ** 2) / (2 * d * r2)) -
         0.5 * math.sqrt((-d + r1 + r2) * (d + r1 - r2) * (d - r1 + r2) * (d + r1 + r2)))
    return a


# ---------------------------------------------------------------------------
# OpenStreetMap / Overpass
# ---------------------------------------------------------------------------

def query_cell_towers(lat: float, lon: float, radius_m: int) -> list[dict]:
    query = f"""
[out:json][timeout:30];
(
  node["man_made"="tower"]["tower:type"="communication"](around:{radius_m},{lat},{lon});
  node["man_made"="mast"]["tower:type"="communication"](around:{radius_m},{lat},{lon});
  node["tower:type"="communication"](around:{radius_m},{lat},{lon});
  node["communication:mobile_phone"="yes"](around:{radius_m},{lat},{lon});
);
out body;
"""
    r = requests.post(OVERPASS_URL, data={"data": query}, timeout=40)
    r.raise_for_status()

    seen: set[int] = set()
    towers: list[dict] = []
    for el in r.json().get("elements", []):
        if "lat" in el and "lon" in el and el["id"] not in seen:
            seen.add(el["id"])
            towers.append({
                "id":   el["id"],
                "lat":  el["lat"],
                "lon":  el["lon"],
                "tags": el.get("tags", {}),
            })
    return towers


# ---------------------------------------------------------------------------
# Map rendering
# ---------------------------------------------------------------------------

# Each probe is: {lat, lon, rtt_ms, label, color, fill_color}
Probe = dict


def render_map(
    probes: list[Probe],
    towers: list[dict],
    target: str,
    output_path: str,
) -> None:
    """
    Render a folium map.

    probes[0] is always the user's local measurement.
    probes[1], if present, is the remote server measurement.
    Cell towers are classified relative to the local (user) probe.
    """
    user = probes[0]
    center_lat, center_lon = user["lat"], user["lon"]
    dist_fiber_km  = light_distance_km(user["rtt_ms"], vacuum=False)
    dist_vacuum_km = light_distance_km(user["rtt_ms"], vacuum=True)

    # Zoom out a bit when two widely-separated probes are present
    zoom = 10 if len(probes) == 1 else 5

    m = folium.Map(location=[center_lat, center_lon], zoom_start=zoom, tiles="CartoDB positron")

    # Draw each probe's fiber circle
    colors = ["#e74c3c", "#8e44ad", "#e67e22"]
    for i, probe in enumerate(probes):
        color  = colors[i % len(colors)]
        r_km   = light_distance_km(probe["rtt_ms"], vacuum=False)
        r_vac  = light_distance_km(probe["rtt_ms"], vacuum=True)
        label  = probe["label"]

        # Fiber circle (solid fill)
        folium.Circle(
            location=[probe["lat"], probe["lon"]],
            radius=r_km * 1000,
            color=color,
            weight=2,
            fill=True,
            fill_color=color,
            fill_opacity=0.06,
            tooltip=f"{label} — fiber radius: {r_km:,.1f} km (RTT {probe['rtt_ms']:.1f} ms to {target})",
        ).add_to(m)

        # Vacuum circle (dashed, no fill)
        folium.Circle(
            location=[probe["lat"], probe["lon"]],
            radius=r_vac * 1000,
            color=color,
            weight=1,
            fill=False,
            dash_array="6 4",
            opacity=0.4,
            tooltip=f"{label} — vacuum radius: {r_vac:,.1f} km",
        ).add_to(m)

        # Probe location marker
        folium.Marker(
            location=[probe["lat"], probe["lon"]],
            tooltip=f"{label}<br>({probe['lat']:.4f}, {probe['lon']:.4f})",
            icon=folium.Icon(color="red" if i == 0 else "purple", icon="tower-broadcast", prefix="fa"),
        ).add_to(m)

    # Cell towers — classified against the LOCAL probe's fiber circle
    inside_count = 0
    for t in towers:
        d = haversine_km(center_lat, center_lon, t["lat"], t["lon"])
        inside = d <= dist_fiber_km
        if inside:
            inside_count += 1

        tags  = t["tags"]
        label = tags.get("name") or tags.get("operator") or f"Tower #{t['id']}"
        tip   = (
            f"<b>{label}</b><br>"
            f"Distance from you: {d:.2f} km<br>"
            f"{'✔ within' if inside else '✘ outside'} local fiber radius"
        )
        folium.CircleMarker(
            location=[t["lat"], t["lon"]],
            radius=6,
            color="#27ae60" if inside else "#7f8c8d",
            fill=True,
            fill_color="#27ae60" if inside else "#95a5a6",
            fill_opacity=0.85,
            tooltip=folium.Tooltip(tip),
        ).add_to(m)

    # Build legend
    probe_rows = ""
    for i, probe in enumerate(probes):
        color = colors[i % len(colors)]
        r_km  = light_distance_km(probe["rtt_ms"], vacuum=False)
        probe_rows += (
            f'<span style="color:{color}">&#11044;</span>&nbsp;'
            f'{probe["label"]}: <b>{r_km:,.1f} km</b> '
            f'({probe["rtt_ms"]:.1f} ms)<br>'
        )

    intersection_row = ""
    if len(probes) == 2:
        p0, p1 = probes[0], probes[1]
        r0 = light_distance_km(p0["rtt_ms"])
        r1 = light_distance_km(p1["rtt_ms"])
        area = circle_intersection_area_km2(p0["lat"], p0["lon"], r0, p1["lat"], p1["lon"], r1)
        if area is not None:
            # Compare to single-circle area
            single = math.pi * r0 ** 2
            reduction_pct = (1 - area / single) * 100
            intersection_row = (
                f'<hr style="margin:6px 0;border-color:#eee">'
                f'Overlap area: <b>{area:,.0f} km²</b><br>'
                f'vs single circle: <b>−{reduction_pct:.0f}%</b> search area'
            )

    legend = f"""
<div style="
    position: fixed; bottom: 30px; left: 30px; z-index: 1000;
    background: white; padding: 14px 18px; border-radius: 8px;
    box-shadow: 0 2px 10px rgba(0,0,0,0.25);
    font-family: 'Courier New', monospace; font-size: 13px; line-height: 1.8;
    max-width: 280px;
">
  <b style="font-size:15px">pinq</b><br>
  Target: <code>{target}</code><br>
  <hr style="margin:6px 0;border-color:#eee">
  {probe_rows}
  {intersection_row}
  <hr style="margin:6px 0;border-color:#eee">
  <span style="color:#27ae60">&#11044;</span>&nbsp;Towers in local radius: <b>{inside_count}</b><br>
  <span style="color:#95a5a6">&#11044;</span>&nbsp;Towers outside: <b>{len(towers) - inside_count}</b>
</div>
"""
    m.get_root().html.add_child(folium.Element(legend))
    m.save(output_path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="pinq",
        description="Visualize network latency as light-speed distance over cell tower data.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--target", default="8.8.8.8",
        help="Host to ping (default: 8.8.8.8)"
    )
    parser.add_argument(
        "--server",
        metavar="URL",
        help="pinq-server URL for remote multilateration (e.g. https://your-app.herokuapp.com)"
    )
    parser.add_argument(
        "--port", type=int, default=443,
        help="TCP port to use when --server is active (default: 443)"
    )
    parser.add_argument(
        "--radius", type=int, default=15_000,
        help="Cell tower search radius in meters (default: 15000)"
    )
    parser.add_argument(
        "--output", default="pinq_map.html",
        help="Output HTML map file (default: pinq_map.html)"
    )
    parser.add_argument(
        "--ip",
        help="Override IP address instead of auto-detecting"
    )
    parser.add_argument(
        "--no-open", action="store_true",
        help="Don't open the map in a browser"
    )
    args = parser.parse_args()

    use_server = bool(args.server)

    console.print()
    console.print("[bold cyan]pinq[/bold cyan]  —  network latency × cell tower geolocation")
    if use_server:
        console.print("  [dim]multilateration mode: two vantage points[/dim]")
    console.print()

    # 1. Public IP + geolocation
    with console.status("Detecting public IP..."):
        ip = args.ip or get_public_ip()
    console.print(f"  [green]✓[/green]  IP: [bold]{ip}[/bold]")

    with console.status("Geolocating IP..."):
        geo = geolocate_ip(ip)
    lat, lon = geo["lat"], geo["lon"]
    console.print(
        f"  [green]✓[/green]  Location: [bold]{geo.get('city', '?')}, "
        f"{geo.get('regionName', '?')}, {geo.get('country', '?')}[/bold]"
    )
    console.print(f"      Coords: ({lat:.4f}, {lon:.4f})  |  ISP: {geo.get('isp', '?')}")

    # 2. Local ping
    if use_server:
        console.print(f"\n  TCP-pinging [bold]{args.target}[/bold] (port {args.port}) locally...")
        local_rtt = ping_tcp(args.target, args.port)
    else:
        console.print(f"\n  Pinging [bold]{args.target}[/bold]...")
        local_rtt = ping_icmp(args.target)

    if local_rtt is None:
        console.print("  [red]✗[/red]  Local ping failed — try a different --target or --port.")
        sys.exit(1)
    console.print(f"  [green]✓[/green]  Local RTT:  [bold]{local_rtt:.1f} ms[/bold]")

    local_dist = light_distance_km(local_rtt)
    console.print(
        f"      → fiber light-speed radius: [bold]{local_dist:,.1f} km[/bold]  "
        f"(vacuum: {light_distance_km(local_rtt, vacuum=True):,.1f} km)"
    )

    probes: list[Probe] = [{
        "lat":    lat,
        "lon":    lon,
        "rtt_ms": local_rtt,
        "label":  f"You ({geo.get('city', ip)})",
    }]

    # 3. Remote ping via pinq-server (multilateration)
    if use_server:
        console.print(f"\n  Asking [bold]{args.server}[/bold] to ping {args.target}...")
        resp = remote_ping(args.server, args.target, args.port)
        if resp and resp.get("rtt_ms") is not None:
            srv      = resp.get("server", {})
            srv_rtt  = resp["rtt_ms"]
            srv_lat  = srv.get("lat")
            srv_lon  = srv.get("lon")
            srv_city = srv.get("city", "server")

            if srv_lat is not None and srv_lon is not None:
                console.print(
                    f"  [green]✓[/green]  Server RTT: [bold]{srv_rtt:.1f} ms[/bold]  "
                    f"from {srv_city}, {srv.get('country', '?')} "
                    f"({srv_lat:.4f}, {srv_lon:.4f})"
                )
                srv_dist = light_distance_km(srv_rtt)
                console.print(f"      → fiber light-speed radius: [bold]{srv_dist:,.1f} km[/bold]")

                probes.append({
                    "lat":    srv_lat,
                    "lon":    srv_lon,
                    "rtt_ms": srv_rtt,
                    "label":  f"Server ({srv_city})",
                })

                # Multilateration summary
                sep = haversine_km(lat, lon, srv_lat, srv_lon)
                area = circle_intersection_area_km2(lat, lon, local_dist, srv_lat, srv_lon, srv_dist)
                console.print(f"\n  [bold]Multilateration[/bold]")
                console.print(f"    Vantage point separation: [bold]{sep:,.0f} km[/bold]")
                if area is not None:
                    single = math.pi * local_dist ** 2
                    pct = (1 - area / single) * 100
                    console.print(f"    Overlap area: [bold]{area:,.0f} km²[/bold]")
                    console.print(
                        f"    Search area reduced by [bold green]{pct:.0f}%[/bold green] "
                        f"vs single circle"
                    )
                else:
                    console.print(
                        "    [yellow]Circles don't overlap — target may be outside both radii "
                        "(routing adds significant non-geographic latency)[/yellow]"
                    )
            else:
                console.print("  [yellow]⚠[/yellow]  Server location unavailable, skipping second circle.")
        else:
            console.print("  [yellow]⚠[/yellow]  Remote ping returned no result, continuing with single probe.")

    # 4. Cell towers (centered on user location)
    with console.status(f"Querying cell towers within {args.radius / 1000:.0f} km..."):
        towers = query_cell_towers(lat, lon, args.radius)

    towers_d = sorted(
        [{**t, "dist_km": haversine_km(lat, lon, t["lat"], t["lon"])} for t in towers],
        key=lambda t: t["dist_km"],
    )
    console.print(f"\n  [green]✓[/green]  Found [bold]{len(towers)}[/bold] cell towers")

    if towers_d:
        dist_fiber = light_distance_km(local_rtt)
        table = Table(box=box.SIMPLE, show_header=True, header_style="bold")
        table.add_column("Tower",      style="dim", max_width=28)
        table.add_column("Dist (km)",  justify="right")
        table.add_column("In radius?", justify="center")
        table.add_column("Operator",   style="dim", max_width=22)

        for t in towers_d[:12]:
            tags   = t["tags"]
            name   = tags.get("name") or tags.get("operator") or f"#{t['id']}"
            inside = t["dist_km"] <= dist_fiber
            table.add_row(
                name,
                f"{t['dist_km']:.2f}",
                "[green]✔[/green]" if inside else "[dim]✘[/dim]",
                tags.get("operator", "—"),
            )
        console.print()
        console.print(table)

    # 5. Render map
    output = args.output
    with console.status(f"Rendering map → {output}..."):
        render_map(probes, towers, args.target, output)
    abs_path = Path(output).resolve()
    console.print(f"  [green]✓[/green]  Map saved: [bold]{abs_path}[/bold]")

    if not args.no_open:
        webbrowser.open(abs_path.as_uri())

    console.print()


if __name__ == "__main__":
    main()
