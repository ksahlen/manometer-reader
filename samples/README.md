# Sample Images

Put real manometer snapshots here while calibrating the reader.

Recommended naming:

```text
samples/raw/0,75.jpg
samples/raw/0,85.jpg
samples/raw/0,9.jpg
samples/raw/1,0.jpg
samples/raw/1,2.jpg
```

For each image, note:

- actual pressure shown by the gauge
- approximate time and lighting condition
- ESP32-CAM resolution and whether flash/LED was on
- whether the camera position changed since the previous image

Run a local read with:

```bash
python3 service/read_samples.py
```

Or run one image:

```bash
python3 service/read_image.py samples/raw/1,2.jpg \
  --debug-out samples/debug/1,2_debug.jpg \
  --no-median
```

The JSON output reports the detected needle angle, pressure, confidence, and
the calibration values used for that read.
