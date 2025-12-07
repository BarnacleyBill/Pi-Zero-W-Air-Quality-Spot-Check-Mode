#!/usr/bin/env python3
import os
import time
import math
import csv
import threading
import socket
import subprocess
from datetime import datetime

import board
import busio
import adafruit_ahtx0
from adafruit_pm25.i2c import PM25_I2C

from flask import Flask, jsonify, render_template_string

# =========================
# CONFIG
# =========================
READ_INTERVAL_SEC = 5            # how often to read sensors
HISTORY_SECONDS = 15 * 60        # keep last 15 minutes in memory
LOG_DIR = os.path.expanduser("~/air_monitor/logs")

os.makedirs(LOG_DIR, exist_ok=True)
SESSION_START = datetime.now()
LOG_PATH = os.path.join(
    LOG_DIR,
    SESSION_START.strftime("session-%Y%m%d-%H%M%S.csv")
)

# =========================
# SENSOR SETUP
# =========================

i2c = busio.I2C(board.SCL, board.SDA)

# AHT20 at 0x38
aht = adafruit_ahtx0.AHTx0(i2c, address=0x38)

# PMSA003I via Adafruit PM25 I2C driver (addr 0x12)
reset_pin = None
pm25 = PM25_I2C(i2c, reset_pin)

# =========================
# DATA STORAGE
# =========================

history = []          # list of dicts: {ts, temp_f, humidity, pm1, pm25, pm10, aqi_category}
history_lock = threading.Lock()
last_row = None       # latest reading dict


def aqi_category_from_pm25(pm25_value):
    """Return a simple AQI category string based on PM2.5 µg/m³."""
    if pm25_value is None:
        return "Unknown"
    v = float(pm25_value)
    if v <= 12.0:
        return "Good"
    elif v <= 35.4:
        return "Moderate"
    elif v <= 55.4:
        return "Unhealthy for Sensitive Groups"
    elif v <= 150.4:
        return "Unhealthy"
    elif v <= 250.4:
        return "Very Unhealthy"
    else:
        return "Hazardous"


def read_sensors_once():
    """Read AHT20 + PMSA003I, return dict (or raise)."""
    # AHT20
    temp_c = aht.temperature
    temp_f = temp_c * 9.0 / 5.0 + 32.0
    humidity = aht.relative_humidity

    # PMSA003I / PM25
    aqdata = pm25.read()
    pm1 = aqdata.get("pm10 standard")
    pm25_std = aqdata.get("pm25 standard")
    pm10 = aqdata.get("pm100 standard")

    ts = time.time()
    aqi_cat = aqi_category_from_pm25(pm25_std)

    return {
        "ts": ts,
        "temp_c": round(temp_c, 1),
        "temp_f": round(temp_f, 1),
        "humidity": round(humidity, 1),
        "pm1": pm1,
        "pm25": pm25_std,
        "pm10": pm10,
        "aqi_category": aqi_cat,
    }


def append_to_csv(row):
    """Append row to session CSV."""
    file_exists = os.path.isfile(LOG_PATH)
    try:
        with open(LOG_PATH, "a", newline="") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "time_iso",
                    "temp_c",
                    "temp_f",
                    "humidity",
                    "pm1",
                    "pm25",
                    "pm10",
                    "aqi_category",
                ],
            )
            if not file_exists:
                writer.writeheader()
            dt = datetime.fromtimestamp(row["ts"]).isoformat(timespec="seconds")
            writer.writerow(
                {
                    "time_iso": dt,
                    "temp_c": row["temp_c"],
                    "temp_f": row["temp_f"],
                    "humidity": row["humidity"],
                    "pm1": row["pm1"],
                    "pm25": row["pm25"],
                    "pm10": row["pm10"],
                    "aqi_category": row["aqi_category"],
                }
            )
    except Exception as e:
        print("CSV write error:", e)


def sensor_loop():
    """Background loop: read sensors, keep 15 min history + CSV log."""
    global last_row
    while True:
        try:
            row = read_sensors_once()
            with history_lock:
                history.append(row)
                last_row = row
                # keep only last HISTORY_SECONDS based on timestamp
                cutoff = row["ts"] - HISTORY_SECONDS
                history[:] = [r for r in history if r["ts"] >= cutoff]
            append_to_csv(row)
            print("Logged:", row)
        except Exception as e:
            print("Sensor read error:", e)

        time.sleep(READ_INTERVAL_SEC)


# =========================
# SYSTEM INFO HELPERS
# =========================

def get_cpu_temp_f():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            milli = int(f.read().strip())
        c = milli / 1000.0
        f_deg = c * 9.0 / 5.0 + 32.0
        return round(f_deg, 1)
    except Exception:
        return None


def get_uptime_seconds():
    try:
        with open("/proc/uptime") as f:
            secs = float(f.read().split()[0])
        return int(secs)
    except Exception:
        return None


def format_uptime(seconds):
    if seconds is None:
        return "N/A"
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def get_ip_address():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "N/A"


def get_wifi_rssi():
    """Return WiFi RSSI in dBm or None."""
    try:
        result = subprocess.run(
            ["iwconfig"],
            capture_output=True,
            text=True,
            check=False,
        )
        out = result.stdout
        for line in out.splitlines():
            if "Signal level=" in line:
                # e.g. "Signal level=-62 dBm"
                parts = line.split("Signal level=")[1]
                dbm_str = parts.split(" ")[0]
                return int(dbm_str)
        return None
    except Exception:
        return None


def get_system_info():
    uptime = get_uptime_seconds()
    return {
        "cpu_temp_f": get_cpu_temp_f(),
        "uptime": format_uptime(uptime),
        "ip": get_ip_address(),
        "wifi_rssi": get_wifi_rssi(),
    }


# =========================
# FLASK APP
# =========================

app = Flask(__name__)

PAGE = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Air Monitor</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    body {
      background: #0b0d18;
      color: #f2f2f2;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      margin: 0;
      padding: 1.5rem;
    }
    h1 {
      margin: 0 0 0.25rem 0;
      font-size: 1.6rem;
    }
    .subtitle {
      color: #9aa4c6;
      font-size: 0.9rem;
      margin-bottom: 1.2rem;
    }
    .grid-main {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
      gap: 1rem;
      margin-bottom: 1rem;
    }
    .grid-system {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
      gap: 0.75rem;
      margin-bottom: 1.5rem;
    }
    .card {
      background: #151a2b;
      border-radius: 0.75rem;
      padding: 0.9rem 1.1rem;
      box-shadow: 0 12px 25px rgba(0,0,0,0.45);
      border: 1px solid #22263a;
    }
    .label {
      font-size: 0.78rem;
      text-transform: uppercase;
      letter-spacing: 0.09em;
      color: #a1aac7;
      margin-bottom: 0.35rem;
    }
    .value {
      font-size: 1.9rem;
      font-weight: 600;
      display: flex;
      align-items: baseline;
      gap: 0.25rem;
    }
    .unit {
      font-size: 0.8rem;
      color: #9aa4c6;
    }
    .small-value {
      font-size: 1.1rem;
    }
    .aq-good { color: #4caf50; }
    .aq-moderate { color: #ffc107; }
    .aq-unhealthy-sg { color: #ff9800; }
    .aq-unhealthy { color: #f44336; }
    .aq-very-unhealthy { color: #e040fb; }
    .aq-hazardous { color: #9c27b0; }

    .status-line {
      margin-bottom: 1rem;
      font-size: 0.8rem;
      color: #9aa4c6;
    }
    .status-bad { color: #ff7961; }

    .charts {
      display: grid;
      grid-template-columns: 1fr;
      gap: 1rem;
      max-width: 1000px;
    }
    canvas {
      background: #131726;
      border-radius: 0.75rem;
      padding: 0.5rem;
      border: 1px solid #1f2437;
    }
    @media (min-width: 900px) {
      .charts {
        grid-template-columns: 1fr 1fr;
      }
    }
  </style>
</head>
<body>
  <h1>Air Monitor</h1>
<button id="shutdownBtn" style="
    background:#c0392b;
    color:white;
    padding:10px 16px;
    border:none;
    border-radius:6px;
    font-size:14px;
    margin-bottom: 1rem;
    cursor:pointer;">
    Shutdown Pi
</button>
  <div class="subtitle">Raspberry Pi • AHT20 • PMSA003I &mdash; Spot-check mode</div>

  <div class="grid-main">
    <div class="card">
      <div class="label">Temperature</div>
      <div class="value"><span id="temp_f">--</span><span class="unit">°F</span></div>
    </div>
    <div class="card">
      <div class="label">Humidity</div>
      <div class="value"><span id="humidity">--</span><span class="unit">% RH</span></div>
    </div>
    <div class="card">
      <div class="label">PM1.0</div>
      <div class="value"><span id="pm1">--</span><span class="unit">µg/m³</span></div>
    </div>
    <div class="card">
      <div class="label">PM2.5</div>
      <div class="value"><span id="pm25">--</span><span class="unit">µg/m³</span></div>
    </div>
    <div class="card">
      <div class="label">PM10</div>
      <div class="value"><span id="pm10">--</span><span class="unit">µg/m³</span></div>
    </div>
    <div class="card">
      <div class="label">Air Quality</div>
      <div class="value small-value"><span id="aqi_cat">--</span></div>
    </div>
  </div>

  <div class="grid-system">
    <div class="card">
      <div class="label">CPU Temp</div>
      <div class="value small-value"><span id="cpu_temp">--</span><span class="unit">°F</span></div>
    </div>
    <div class="card">
      <div class="label">WiFi Signal</div>
      <div class="value small-value"><span id="wifi_rssi">--</span><span class="unit">dBm</span></div>
    </div>
    <div class="card">
      <div class="label">Uptime</div>
      <div class="value small-value"><span id="uptime">--:--:--</span></div>
    </div>
    <div class="card">
      <div class="label">Pi IP</div>
      <div class="value small-value"><span id="ip_addr">--</span></div>
    </div>
  </div>

  <div id="status" class="status-line">Waiting for data…</div>

  <div class="charts">
    <canvas id="tempHumChart" height="160"></canvas>
    <canvas id="pmChart" height="160"></canvas>
  </div>

<script>
const REFRESH_MS = 5000;

let tempHumChart, pmChart;

function aqiClass(cat) {
  if (!cat) return "";
  const c = cat.toLowerCase();
  if (c.startsWith("good")) return "aq-good";
  if (c.startsWith("moderate")) return "aq-moderate";
  if (c.startsWith("unhealthy for sensitive")) return "aq-unhealthy-sg";
  if (c.startsWith("unhealthy")) return "aq-unhealthy";
  if (c.startsWith("very unhealthy")) return "aq-very-unhealthy";
  if (c.startsWith("hazardous")) return "aq-hazardous";
  return "";
}

function initCharts() {
  const thCtx = document.getElementById("tempHumChart").getContext("2d");
  const pmCtx = document.getElementById("pmChart").getContext("2d");

  tempHumChart = new Chart(thCtx, {
    type: "line",
    data: {
      labels: [],
      datasets: [
        { label: "Temp (°F)", data: [], borderWidth: 2, tension: 0.2 },
        { label: "Humidity (%RH)", data: [], borderWidth: 2, tension: 0.2 }
      ]
    },
    options: {
      responsive: true,
      animation: false,
      scales: {
        x: {
          ticks: { color: "#aaa", maxRotation: 45, minRotation: 45 },
          grid: { display: false }
        },
        y: {
          ticks: { color: "#aaa" },
          grid: { color: "#333" }
        }
      },
      plugins: {
        legend: { labels: { color: "#eee" } }
      }
    }
  });

  pmChart = new Chart(pmCtx, {
    type: "line",
    data: {
      labels: [],
      datasets: [
        { label: "PM1.0", data: [], borderWidth: 2, tension: 0.2 },
        { label: "PM2.5", data: [], borderWidth: 2, tension: 0.2 },
        { label: "PM10", data: [], borderWidth: 2, tension: 0.2 }
      ]
    },
    options: {
      responsive: true,
      animation: false,
      scales: {
        x: {
          ticks: { color: "#aaa", maxRotation: 45, minRotation: 45 },
          grid: { display: false }
        },
        y: {
          ticks: { color: "#aaa" },
          grid: { color: "#333" }
        }
      },
      plugins: {
        legend: { labels: { color: "#eee" } }
      }
    }
  });
}

function updateCards(data) {
  const d = data.data;
  const sys = data.system;

  document.getElementById("temp_f").textContent = d.temp_f.toFixed(1);
  document.getElementById("humidity").textContent = d.humidity.toFixed(1);
  document.getElementById("pm1").textContent = d.pm1;
  document.getElementById("pm25").textContent = d.pm25;
  document.getElementById("pm10").textContent = d.pm10;

  const aqiEl = document.getElementById("aqi_cat");
  aqiEl.textContent = d.aqi_category;
  aqiEl.className = aqiClass(d.aqi_category);

  document.getElementById("cpu_temp").textContent =
    sys.cpu_temp_f != null ? sys.cpu_temp_f.toFixed(1) : "--";
  document.getElementById("wifi_rssi").textContent =
    sys.wifi_rssi != null ? sys.wifi_rssi : "--";
  document.getElementById("uptime").textContent = sys.uptime || "--:--:--";
  document.getElementById("ip_addr").textContent = sys.ip || "--";

  const statusEl = document.getElementById("status");
  const when = new Date(d.ts * 1000).toLocaleTimeString();
  statusEl.textContent = "Last update " + when + " • 15-minute live window";
  statusEl.className = "status-line";
}

function updateCharts(points) {
  const labels = points.map(p => new Date(p.ts * 1000).toLocaleTimeString());
  const temps = points.map(p => p.temp_f);
  const hums  = points.map(p => p.humidity);
  const pm1s  = points.map(p => p.pm1);
  const pm25s = points.map(p => p.pm25);
  const pm10s = points.map(p => p.pm10);

  tempHumChart.data.labels = labels;
  tempHumChart.data.datasets[0].data = temps;
  tempHumChart.data.datasets[1].data = hums;
  tempHumChart.update("none");

  pmChart.data.labels = labels;
  pmChart.data.datasets[0].data = pm1s;
  pmChart.data.datasets[1].data = pm25s;
  pmChart.data.datasets[2].data = pm10s;
  pmChart.update("none");
}

async function refreshAll() {
  const statusEl = document.getElementById("status");
  try {
    const [latestRes, histRes] = await Promise.all([
      fetch("/api/data"),
      fetch("/api/history")
    ]);

    const latestJson = await latestRes.json();
    const histJson = await histRes.json();

    if (!latestJson.ok) {
      statusEl.textContent = "Error: " + (latestJson.error || "unknown");
      statusEl.className = "status-line status-bad";
      return;
    }
    if (!histJson.ok) {
      statusEl.textContent = "Error: " + (histJson.error || "history failed");
      statusEl.className = "status-line status-bad";
      return;
    }

    updateCards(latestJson);
    updateCharts(histJson.points || []);
  } catch (e) {
    statusEl.textContent = "Error talking to Pi: " + e;
    statusEl.className = "status-line status-bad";
  }
}

window.addEventListener("load", () => {
  initCharts();
  refreshAll();
  setInterval(refreshAll, REFRESH_MS);
});

// Shutdown button handler
document.getElementById("shutdownBtn").onclick = function () {
    if (!confirm("Are you sure you want to shut down the Pi?")) return;

    fetch("/shutdown")
        .then(response => response.text())
        .then(msg => alert(msg))
        .catch(err => alert("Shutdown request failed."));
};
</script>
</body>
</html>
"""
@app.route("/api/data")
def api_data():
    global last_row
    with history_lock:
        row = last_row
    if not row:
        return jsonify({"ok": False, "error": "No data yet"}), 503

    sysinfo = get_system_info()
    out = dict(row)  # copy
    # ensure ts is present as float
    out["ts"] = float(row["ts"])
    return jsonify({"ok": True, "data": out, "system": sysinfo})


@app.route("/api/history")
def api_history():
    with history_lock:
        pts = list(history)
    return jsonify({"ok": True, "points": pts})


@app.route("/")
def index():
    return render_template_string(PAGE)


@app.route("/shutdown")
def shutdown():
    os.system("sudo shutdown -h now")
    return "Shutting down...", 200


if __name__ == "__main__":
    # start sensor thread
    t = threading.Thread(target=sensor_loop, daemon=True)
    t.start()
    # run web server
    app.run(host="0.0.0.0", port=5000, debug=False)
