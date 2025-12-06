Air Monitor – Raspberry Pi Air Quality Dashboard

A complete Raspberry Pi–powered air quality monitor using:

PMSA003I (PM1.0 / PM2.5 / PM10)

AHT20 (Temperature & Humidity)

Flask Web Dashboard

Chart.js Live Graphs

CSV Logging

System Metrics (CPU Temp, WiFi RSSI, Uptime, IP)

Safe Shutdown Button

Built to function as a portable spot-check air quality station, accessible from any device on the same network or hotspot.



Features:

Real-time Sensor Readings
Temperature (°F / °C)
Humidity (%RH)
PM1.0 / PM2.5 / PM10
Air Quality Category (AQI-based)
Live Dashboard
Built-in Flask server
Live-updating cards
Two live charts:
Temperature & Humidity
All particulate sizes
15-minute rolling history
Auto-refresh
Logging
Every boot creates a timestamped CSV log:
~/air_monitor/logs/session-YYYYMMDD-HHMMSS.csv


Contains all fields including AQI category.
System Status
CPU temperature
WiFi RSSI
Uptime
Local IP Address
Safe Shutdown Button
  Gracefully powers down the Raspberry Pi from the dashboard.


Hardware Required
Raspberry Pi Zero 2 W (recommended) or Pi 3/4
PMSA003I (I²C version)
AHT20 Temperature/Humidity sensor
4-pin JST ≥ I²C wiring
5V/2A USB power source


Wiring
Both sensors run on I²C (SDA/SCL):

Device	SDA	SCL	Vin	GND
AHT20	✓	✓	3.3V	GND
PMSA003I	✓	✓	5V	GND


Software Install
Install required packages:

sudo apt update
sudo apt install python3-pip python3-flask python3-venv -y
pip3 install adafruit-circuitpython-ahtx0 adafruit-circuitpython-pm25


Enable I²C:
sudo raspi-config
# Interface Options → I2C → Enable




Run the Dashboard
From the project directory:
python3 air_monitor.py


Dashboard will start on:
http://<pi-ip>:5000
(Optional) Auto-Start on Boot




Create a service file:

air_monitor.service
[Unit]
Description=Air Monitor Dashboard
After=network.target

[Service]
ExecStart=/usr/bin/python3 /home/enviro/air_monitor/air_monitor.py
WorkingDirectory=/home/enviro/air_monitor
Restart=always
User=enviro

[Install]
WantedBy=multi-user.target


Enable:

sudo cp air_monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable air_monitor
sudo systemctl start air_monitor

API Endpoints
Endpoint	Description
/api/data	Latest sensor + system info
/api/history	15-minute rolling history
/	Dashboard UI
Safe Shutdown Button

The dashboard includes a shutdown button that calls:

@app.route("/shutdown")
def shutdown():
    os.system("sudo shutdown -h now")
    return "Shutting down...", 200


Grant passwordless shutdown:
sudo visudo
Add:
enviro ALL=NOPASSWD: /sbin/shutdown

Logs

CSV logs include:
time_iso, temp_c, temp_f, humidity, pm1, pm25, pm10, aqi_category


Logs are stored at:
~/air_monitor/logs/
