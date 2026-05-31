"""
Fetch one snapshot from the ESPHome camera and optionally run the gauge reader.

Use this before the long-running service to verify that the VM can reach the
camera endpoint and that the received image is decodable.
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import cv2

from gauge_reader import GaugeCalibration, GaugeReader
from read_image import load_calibration, load_image_rotation
from service import capture_image_with_lighting, load_config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch one ESPHome camera snapshot for prototype testing."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).with_name("config.yaml"),
        help="Service config YAML",
    )
    parser.add_argument("--url", help="Override camera snapshot URL")
    parser.add_argument(
        "--out",
        type=Path,
        help="Output image path; defaults to samples/raw/live_<timestamp>.jpg",
    )
    parser.add_argument(
        "--debug-out",
        type=Path,
        help="Optional annotated debug image path",
    )
    parser.add_argument(
        "--read",
        action="store_true",
        help="Run GaugeReader on the captured snapshot",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config(str(args.config))
    camera_config = config.get("camera", {})
    camera_url = args.url or camera_config.get("url", "http://manometer-cam.local:8080/")
    timeout = camera_config.get("timeout", 10)
    lighting_config = config.get("lighting", {})

    image = capture_image_with_lighting(
        camera_url,
        timeout,
        lighting_config,
    )
    if image is None:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "capture_failed",
                    "camera_url": camera_url,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 1

    if args.out is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        args.out = Path("samples/raw") / f"live_{timestamp}.jpg"

    args.out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(args.out), image)

    result = {
        "ok": True,
        "camera_url": camera_url,
        "image": str(args.out),
        "width": image.shape[1],
        "height": image.shape[0],
    }

    if args.read:
        config_path = args.config if args.config.exists() else None
        calibration = load_calibration(
            config_path,
            GaugeCalibration,
        )
        rotation_degrees = load_image_rotation(config_path)
        reader = GaugeReader(
            calibration=calibration,
            image_rotation_degrees=rotation_degrees,
        )
        reading = reader.read(
            image,
            use_median=False,
            generate_debug=args.debug_out is not None,
        )
        if reading is None:
            result["reading"] = {
                "ok": False,
                "error": "detection_failed",
                "image_rotation_degrees": rotation_degrees,
            }
        else:
            if args.debug_out is not None and reading.debug_image is not None:
                args.debug_out.parent.mkdir(parents=True, exist_ok=True)
                cv2.imwrite(str(args.debug_out), reading.debug_image)
            result["reading"] = {
                "ok": True,
                "pressure_bar": reading.pressure_bar,
                "needle_angle_deg": reading.needle_angle_deg,
                "confidence": reading.confidence,
                "image_rotation_degrees": rotation_degrees,
                "debug_image": str(args.debug_out) if args.debug_out else None,
            }

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
