#!/usr/bin/env python3
"""
pinq-server — Heroku service that measures TCP round-trip time from a
remote geographic location, enabling multilateration with the pinq CLI.

Deploy to Heroku:
    heroku create your-app-name
    git push heroku main

Endpoints:
    GET /              health check
    GET /location      returns this server's geolocation
    GET /ping?target=HOST[&port=443][&count=4]
                       TCP-pings HOST from this server, returns RTT + location
"""

import os
import socket
import time

import requests
from flask import Flask, jsonify, request as flask_request

app = Flask(__name__)

# Cache geolocation on first request — ip-api.com free tier allows 45 req/min
_server_location: dict | None = None


def _fetch_location() -> dict:
    global _server_location
    if _server_location is not None:
        return _server_location
    try:
        r = requests.get("http://ip-api.com/json/", timeout=10)
        d = r.json()
        if d.get("status") == "success":
            _server_location = {
                "ip":      d.get("query"),
                "lat":     d.get("lat"),
                "lon":     d.get("lon"),
                "city":    d.get("city"),
                "region":  d.get("regionName"),
                "country": d.get("country"),
                "isp":     d.get("isp"),
            }
        else:
            _server_location = {"error": "geolocation failed"}
    except Exception as exc:
        _server_location = {"error": str(exc)}
    return _server_location


def tcp_ping(host: str, port: int, count: int, timeout: float = 5.0) -> float | None:
    """
    Measure TCP connection latency to host:port.

    Returns the median RTT in milliseconds, or None if all attempts failed.
    Median is used instead of mean to resist outliers from transient congestion.
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


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return jsonify(service="pinq-server", status="ok", version="1.0")


@app.route("/location")
def location():
    return jsonify(_fetch_location())


@app.route("/ping")
def ping():
    target = flask_request.args.get("target", "").strip()
    if not target:
        return jsonify(error="missing required parameter: target"), 400

    try:
        port  = int(flask_request.args.get("port",  443))
        count = int(flask_request.args.get("count", 4))
    except ValueError:
        return jsonify(error="port and count must be integers"), 400

    count = max(1, min(count, 10))  # clamp 1–10

    rtt = tcp_ping(target, port, count)
    loc = _fetch_location()

    return jsonify(
        target  = target,
        port    = port,
        count   = count,
        rtt_ms  = rtt,
        server  = loc,
    )


# ---------------------------------------------------------------------------
# Entry point (for local testing; Heroku uses gunicorn via Procfile)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    _fetch_location()  # warm up cache before first request
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
