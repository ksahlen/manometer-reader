# Manometer Reader

Reads an analog Beulco 0–3 bar manometer using an ESP32-CAM and OpenCV, publishing pressure readings to Home Assistant via MQTT.

## Architecture

```
ESP32-CAM                    Python service                 Home Assistant
┌──────────┐  HTTP snapshot  ┌───────────────┐  MQTT        ┌──────────┐
│ ESPHome  │ ──────────────► │ OpenCV gauge  │ ───────────► │ Sensor   │
│ camera   │                 │ reader        │              │ + alerts │
└──────────┘                 └───────────────┘              └──────────┘
```

The ESP32-CAM captures images and serves them via HTTP. A Python service running on your server (VM, Raspberry Pi, or Docker) fetches snapshots, detects the needle angle using adaptive thresholding and Hough line detection, maps it to bar, and publishes to MQTT with Home Assistant auto-discovery.

## Quick start

### 1. Flash the ESP32-CAM

Install [ESPHome](https://esphome.io/guides/installing_esphome.html), then:

```bash
cd esphome
esphome run manometer-cam.yaml
```

Verify the camera works by visiting `http://manometer-cam.local/` in your browser.

### 2. Configure the service

```bash
cp service/config.yaml service/config.local.yaml
```

Edit `config.local.yaml` with your MQTT credentials and camera URL.

### 3. Run with Docker (recommended)

```bash
docker compose up -d
```

### 3b. Run with systemd (alternative)

```bash
# Install
sudo mkdir -p /opt/manometer-reader
sudo cp -r service/ /opt/manometer-reader/
sudo cp systemd/manometer-reader.service /etc/systemd/system/

# Create venv
sudo python3 -m venv /opt/manometer-reader/venv
sudo /opt/manometer-reader/venv/bin/pip install -r /opt/manometer-reader/service/requirements.txt

# Create service user
sudo useradd -r -s /usr/sbin/nologin manometer

# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable --now manometer-reader
```

## Calibration

The gauge is calibrated by setting three values in `config.yaml`:

| Parameter | Description | Default |
|-----------|-------------|---------|
| `angle_0bar` | Needle angle at 0 bar (degrees, 0=up, clockwise) | 220.0 |
| `total_sweep` | Angular sweep from 0 bar to max bar | 270.0 |
| `max_bar` | Maximum gauge value | 3.0 |

To calibrate: enable `save_debug_images: true`, observe the reported `needle_angle_deg` at a known pressure, and adjust `angle_0bar` and `total_sweep` accordingly. Two known pressure points are enough for precise calibration.

## Home Assistant

Sensors appear automatically under MQTT integration after the first reading. Optional template sensors and automations (low/high pressure alerts) are in `homeassistant/`.

## Project structure

```
manometer-reader/
├── esphome/
│   └── manometer-cam.yaml       # ESP32-CAM firmware config
├── service/
│   ├── gauge_reader.py           # OpenCV gauge reading pipeline
│   ├── service.py                # Main service (capture → analyze → publish)
│   ├── config.yaml               # Configuration template
│   └── requirements.txt
├── homeassistant/
│   ├── configuration.yaml        # Template sensors
│   └── automations.yaml          # Alert automations
├── systemd/
│   └── manometer-reader.service
├── docker-compose.yml
├── Dockerfile
└── README.md
```

## How the detection works

1. **Circle detection** — HoughCircles finds the gauge face
2. **Crop and mask** — isolates the dial interior, excluding rim and center hub
3. **CLAHE** — normalizes contrast to handle glare and uneven lighting
4. **Adaptive threshold** — extracts dark features (needle, text, markings)
5. **Hough line detection** — finds straight lines passing near the center
6. **Angle calculation** — picks the best line, determines tip direction, calculates angle
7. **Median filter** — smooths readings over a sliding window

## License

MIT
