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

## Hardware

Current firmware target:

- Prokyber AI-On-The-Edge-Cam, ESP32-S3
- OV2640 camera
- 4 x WS2812B back-lighting LEDs on GPIO12
- 16 MB flash / 8 MB RAM according to the vendor listing
- Wi-Fi enabled for the first prototype; Ethernet/PoE config will be added once the Ethernet pins are confirmed

Camera pinout currently configured in `esphome/manometer-cam.yaml`:

| Signal | GPIO |
|--------|------|
| D0-D7 / CAM_Y2-CAM_Y9 | 11, 9, 8, 10, 47, 18, 17, 16 |
| XCLK / CAM_MCLK | 15 |
| PCLK | 13 |
| HREF / CAM_HSYNCH | 7 |
| VSYNC / CAM_VSYNCH | 6 |
| SIOD / SIOC | 4 / 5 |
| WS2812B back-light | 12 |
| Peripheral enable / PER_EN | 46 |

SD card pins are documented for later use but not configured in ESPHome yet:
CS 3, SCLK 40, MISO 41, MOSI 42.

GPIO0 is the user/boot button, and reset is connected to EN.

## Quick start

### 1. Flash the ESP32-CAM

Install [ESPHome](https://esphome.io/guides/installing_esphome.html), then:

```bash
cp esphome/secrets.example.yaml esphome/secrets.yaml
# Edit esphome/secrets.yaml with Wi-Fi credentials.

cd esphome
esphome run manometer-cam.yaml
```

Verify the camera works with:

- Web UI: `http://radiator-pressure-ai.local/` or `http://192.168.6.35/`
- Snapshot endpoint: `http://radiator-pressure-ai.local:8080/` or `http://192.168.6.35:8080/`
- Framing/focus stream: `http://radiator-pressure-ai.local:8081/` or `http://192.168.6.35:8081/`

### 2. Configure the service

```bash
cp service/config.yaml service/config.local.yaml
```

Edit `config.local.yaml` with your MQTT credentials if your broker requires
authentication. The default camera and
MQTT hosts are currently set for this installation:

- ESPHome camera: `http://192.168.6.35:8080/`
- ESPHome backlight API: `http://192.168.6.35`
- MQTT broker/Home Assistant: `192.168.4.11`

If the camera is mounted sideways, set `camera.rotate_degrees` to `90` or `270`
so the reader normalizes snapshots before analysis.

The VM can pulse the ESPHome backlight around each snapshot:

```yaml
camera:
  rotate_degrees: 90
  warmup_snapshots: 1
  warmup_delay_seconds: 1.0

lighting:
  enabled: true
  brightness: 255
  red: 255
  green: 255
  blue: 255
  settle_seconds: 3.0
  turn_off_after_snapshot: true
```

If the ESPHome firmware still uses a very low `idle_framerate`, increase
`settle_seconds` temporarily, for example to `12.0`, so at least one fresh
illuminated frame is available before the analysis snapshot is fetched.

Test one snapshot from the VM:

```bash
python3 -m venv .venv
.venv/bin/pip install -r service/requirements.txt
.venv/bin/python service/capture_snapshot.py \
  --config service/config.local.yaml \
  --out samples/raw/prototype.jpg \
  --read \
  --debug-out samples/debug/prototype_debug.jpg
```

Test one full MQTT publish:

```bash
.venv/bin/python service/service.py --config service/config.local.yaml --once
```

For image/debug troubleshooting, enable raw/debug image saving in
`service/config.local.yaml`:

```yaml
reading:
  save_debug_images: true
  save_raw_images: true
  debug_image_dir: "/tmp/manometer-debug"
```

With Docker Compose, these files appear on the VM under `./debug-images/`.

The reader also rejects implausible radiator-circuit values before publishing
to MQTT. Values above `reading.max_plausible_bar` default to being skipped, and
large jumps must be confirmed by repeated readings:

```yaml
reading:
  max_plausible_bar: 1.8
  max_jump_bar: 0.45
  jump_confirmation_count: 2
  jump_confirmation_tolerance_bar: 0.12
```

### 3. Run with Docker (recommended)

Docker Compose is the preferred VM deployment path. It keeps Python/OpenCV
dependencies inside the container, makes upgrades predictable, and avoids
manually managing a virtualenv under `/opt`.

```bash
docker compose up -d
```

Useful checks on the VM:

```bash
docker compose logs -f manometer-reader
docker compose run --rm manometer-reader python service.py --once
```

If the Ubuntu VM only has `root` today, create a normal admin user first and
deploy from that account:

```bash
adduser manometer
usermod -aG sudo manometer
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

Start calibration offline with real snapshots before wiring the service to MQTT.

```bash
python3 -m venv .venv
.venv/bin/pip install -r service/requirements.txt
```

Run all sample images:

```bash
.venv/bin/python service/read_samples.py
```

Or run one image:

```bash
.venv/bin/python service/read_image.py samples/raw/1,2.jpg \
  --debug-out samples/debug/1,2_debug.jpg \
  --no-median
```

To test a sideways camera without editing the config file:

```bash
.venv/bin/python service/read_image.py samples/raw/prototype.jpg \
  --rotate-degrees 90 \
  --debug-out samples/debug/prototype_rotated_debug.jpg \
  --no-median
```

The command prints JSON with the detected needle angle, pressure, confidence,
and calibration values. The debug image shows which circle and needle line were
used for the reading.

The gauge is calibrated by setting three values in `config.yaml`:

| Parameter | Description | Default |
|-----------|-------------|---------|
| `angle_0bar` | Needle angle at 0 bar (degrees, 0=up, clockwise) | 229.3 |
| `total_sweep` | Angular sweep from 0 bar to max bar | 250.7 |
| `max_bar` | Maximum gauge value | 3.0 |

To calibrate: enable `save_debug_images: true`, observe the reported `needle_angle_deg` at a known pressure, and adjust `angle_0bar` and `total_sweep` accordingly. Two known pressure points are enough for precise calibration.

## Home Assistant

Sensors appear automatically under MQTT integration after the first reading. Optional template sensors and automations (low/high pressure alerts) are in `homeassistant/`.

## Operations

The current live deployment state, VM commands, local configuration expectations,
and troubleshooting notes are documented in `docs/operations.md`.

## Project structure

```
manometer-reader/
├── esphome/
│   └── manometer-cam.yaml       # ESP32-CAM firmware config
├── service/
│   ├── capture_snapshot.py       # Fetch one ESPHome snapshot for prototype testing
│   ├── gauge_reader.py           # OpenCV gauge reading pipeline
│   ├── read_image.py             # Offline read for one local image
│   ├── read_samples.py           # Batch read/calibration for sample images
│   ├── service.py                # Main service (capture → analyze → publish)
│   ├── config.yaml               # Configuration template
│   └── requirements.txt
├── homeassistant/
│   ├── configuration.yaml        # Template sensors
│   └── automations.yaml          # Alert automations
├── docs/
│   └── operations.md             # Current deployment and runbook notes
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
6. **Thin-tip scoring** — prefers the narrow measuring tip over the broad counterweight
7. **Angle calculation** — calculates the tip angle
8. **Median filter** — smooths readings over a sliding window

## License

MIT
