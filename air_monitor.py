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

PAGE = """<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Air Monitor</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <meta name="viewport" content="width=device-width, initial-scale=1">
...
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
    t = threading.Thread(target=sensor_loop, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=5000, debug=False)
