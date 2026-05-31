"""
Offline helper for reading one manometer image.

This is intended for calibration and debugging before the service is wired
to the ESP32-CAM and MQTT loop.
"""

import argparse
import json
import sys
from pathlib import Path


def load_config(config_path: Path | None) -> dict:
    """Load service YAML config, if provided."""
    if config_path is None:
        return {}

    import yaml

    with config_path.open() as f:
        return yaml.safe_load(f) or {}


def load_calibration(config_path: Path | None, calibration_cls):
    """Load gauge calibration from a service YAML config, if provided."""
    default = calibration_cls()
    config = load_config(config_path)

    cal_config = config.get("calibration", {})
    return calibration_cls(
        angle_0bar=cal_config.get("angle_0bar", default.angle_0bar),
        total_sweep=cal_config.get("total_sweep", default.total_sweep),
        max_bar=cal_config.get("max_bar", default.max_bar),
    )


def load_image_rotation(config_path: Path | None) -> int:
    """Load clockwise image rotation from service config."""
    config = load_config(config_path)
    camera_config = config.get("camera", {})
    return int(camera_config.get("rotate_degrees", 0))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read a pressure value from a local manometer image."
    )
    parser.add_argument("image", type=Path, help="Path to a JPG/PNG image")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).with_name("config.yaml"),
        help="Service config YAML with calibration values",
    )
    parser.add_argument(
        "--debug-out",
        type=Path,
        help="Optional path for an annotated debug image",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        help="Optional path for the JSON result; stdout is always written",
    )
    parser.add_argument(
        "--min-confidence",
        type=float,
        default=0.0,
        help="Exit with code 3 if detection confidence is below this value",
    )
    parser.add_argument(
        "--no-median",
        action="store_true",
        help="Disable median filtering for this single-image read",
    )
    parser.add_argument("--angle-0bar", type=float, help="Override 0 bar angle")
    parser.add_argument("--total-sweep", type=float, help="Override gauge sweep angle")
    parser.add_argument("--max-bar", type=float, help="Override maximum gauge value")
    parser.add_argument(
        "--rotate-degrees",
        type=int,
        choices=(0, 90, 180, 270),
        help="Override clockwise image rotation before analysis",
    )
    return parser.parse_args()


def emit_result(result: dict, json_out: Path | None) -> None:
    payload = json.dumps(result, indent=2, sort_keys=True)
    print(payload)

    if json_out is not None:
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(payload + "\n")


def main() -> int:
    args = parse_args()

    try:
        import cv2
        from gauge_reader import GaugeCalibration, GaugeReader
    except ImportError as exc:
        emit_result(
            {
                "ok": False,
                "error": "missing_dependency",
                "detail": str(exc),
                "hint": "Install service/requirements.txt before reading images.",
            },
            args.json_out,
        )
        return 1

    image = cv2.imread(str(args.image))
    if image is None:
        emit_result(
            {
                "ok": False,
                "error": "image_decode_failed",
                "image": str(args.image),
            },
            args.json_out,
        )
        return 1

    calibration = load_calibration(
        args.config if args.config.exists() else None,
        GaugeCalibration,
    )
    if args.angle_0bar is not None:
        calibration.angle_0bar = args.angle_0bar
    if args.total_sweep is not None:
        calibration.total_sweep = args.total_sweep
    if args.max_bar is not None:
        calibration.max_bar = args.max_bar

    config_path = args.config if args.config.exists() else None
    rotation_degrees = (
        args.rotate_degrees
        if args.rotate_degrees is not None
        else load_image_rotation(config_path)
    )

    reader = GaugeReader(
        calibration=calibration,
        image_rotation_degrees=rotation_degrees,
    )
    reading = reader.read(
        image,
        use_median=not args.no_median,
        generate_debug=args.debug_out is not None,
    )

    if reading is None:
        emit_result(
            {
                "ok": False,
                "error": "detection_failed",
                "image": str(args.image),
            },
            args.json_out,
        )
        return 2

    if args.debug_out is not None and reading.debug_image is not None:
        args.debug_out.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(args.debug_out), reading.debug_image)

    result = {
        "ok": True,
        "image": str(args.image),
        "pressure_bar": reading.pressure_bar,
        "needle_angle_deg": reading.needle_angle_deg,
        "confidence": reading.confidence,
        "accepted": reading.confidence >= args.min_confidence,
        "debug_image": str(args.debug_out) if args.debug_out else None,
        "image_rotation_degrees": rotation_degrees,
        "calibration": {
            "angle_0bar": calibration.angle_0bar,
            "total_sweep": calibration.total_sweep,
            "max_bar": calibration.max_bar,
        },
    }
    emit_result(result, args.json_out)

    if reading.confidence < args.min_confidence:
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
