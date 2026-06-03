# Operations Runbook

This document captures the current installation state so the project can be
picked up without conversation history. It intentionally avoids secrets.

## Current Deployment

- ESPHome camera: `radiator-pressure-ai`, currently reachable at `192.168.6.35`.
- Snapshot endpoint: `http://192.168.6.35:8080/`.
- Backlight API: `http://192.168.6.35/light/Backlight/...`.
- VM: Ubuntu host at `192.168.4.13`, deployed as user `manometer`.
- MQTT broker / Home Assistant: `192.168.4.11:1883`.
- Runtime: Docker Compose from `/home/manometer/manometer-reader`.
- Service config: `/home/manometer/manometer-reader/service/config.local.yaml`.
- Debug image bind mount: `/home/manometer/manometer-reader/debug-images`.

## Current Working Behavior

The service is run in pull mode:

1. Turn on ESPHome WS2812B backlight.
2. Wait for camera/light settling.
3. Fetch one warmup snapshot and discard it.
4. Fetch one analysis snapshot.
5. Turn off backlight.
6. Rotate image 90 degrees clockwise.
7. Detect the gauge face and the narrow measuring needle tip.
8. Reject implausible radiator-circuit values and unconfirmed jumps.
9. Median-filter accepted readings.
10. Publish pressure to MQTT for Home Assistant discovery sensors.

The gauge has a broad counterweight on the opposite side of the needle. The
reader must use the narrow needle tip, not the broad black back end.

## Important Local Settings

The tracked `service/config.yaml` is a safe template. The live VM uses
`service/config.local.yaml`, which is not committed.

Expected live values:

```yaml
camera:
  url: "http://192.168.6.35:8080/"
  rotate_degrees: 90
  warmup_snapshots: 1
  warmup_delay_seconds: 1.0

lighting:
  enabled: true
  base_url: "http://192.168.6.35"
  entity_name: "Backlight"
  brightness: 255
  red: 255
  green: 255
  blue: 255
  transition: 0
  settle_seconds: 12.0
  turn_off_after_snapshot: true

reading:
  interval_seconds: 900
  max_plausible_bar: 1.8
  max_jump_bar: 0.45
  jump_confirmation_count: 2
  jump_confirmation_tolerance_bar: 0.12
  save_debug_images: false
  save_raw_images: false

mqtt:
  host: "192.168.4.11"
  port: 1883
```

`settle_seconds: 12.0` is conservative for the currently deployed camera
firmware. If the ESPHome device is reflashed with the repo config using
`idle_framerate: 1fps`, test lowering `settle_seconds` to `3.0`.

## Common Commands On The VM

Update code:

```bash
cd ~/manometer-reader
git pull
docker compose build manometer-reader
docker compose up -d
```

Run one measurement:

```bash
cd ~/manometer-reader
docker compose run --rm manometer-reader python service.py --once
```

Follow logs:

```bash
cd ~/manometer-reader
docker compose logs -f manometer-reader
```

Stop service:

```bash
cd ~/manometer-reader
docker compose down
```

## Debugging Images

Image saving should normally be off. To troubleshoot, temporarily set this in
`service/config.local.yaml`:

```yaml
reading:
  save_debug_images: true
  save_raw_images: true
```

Then run one measurement. Images appear in:

```text
/home/manometer/manometer-reader/debug-images/
```

The service keeps at most 50 `raw_*.jpg` and 50 `debug_*.jpg` files. Turn image
saving off again when done.

Fetch a debug image to the Mac:

```bash
scp manometer@192.168.4.13:/home/manometer/manometer-reader/debug-images/raw_*.jpg ~/Downloads/
```

## ESPHome Notes

The repo ESPHome config targets a Prokyber AI-On-The-Edge-Cam with OV2640.
Current pin mapping is documented in `README.md` and
`esphome/manometer-cam.yaml`.

Secrets must stay local in `esphome/secrets.yaml`.

The repo config uses:

```yaml
esp32_camera:
  resolution: 1280x1024
  jpeg_quality: 10
  max_framerate: 1fps
  idle_framerate: 1fps
```

If avoiding extra ESP load is more important than shorter measurement cycles,
keep the device firmware at a lower idle framerate and keep the VM
`settle_seconds` higher.

## Known Good Reading

On 2026-05-31, the end-to-end VM test produced:

```text
Reading: 1.04 bar (angle=313.2 deg, confidence=83%)
Published: 1.04 bar
```

After the thin-tip scoring change, local tests against live snapshots produced
approximately `1.04-1.05 bar`.

The radiator circuit should not publish 3 bar. Readings above 1.8 bar are
treated as implausible image interpretation errors and skipped.

## Safety And Publishing

Do not commit:

- `service/config.local.yaml`
- `esphome/secrets.yaml`
- `debug-images/*`
- real raw sample photos unless intentionally anonymized

The repo already ignores these paths.
