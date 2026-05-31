"""
Batch calibration helper for manometer sample images.

Reads all images in a sample directory, writes debug overlays, compares detected
pressure against the pressure encoded in each filename, and suggests calibration
values from the detected needle angles.
"""

import argparse
import json
import re
import sys
from pathlib import Path

import cv2

from gauge_reader import GaugeCalibration, GaugeReader
from read_image import load_calibration, load_image_rotation

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


def parse_expected_pressure(path: Path) -> float | None:
    """Parse a pressure value from a filename stem."""
    name = path.stem.lower()
    if name.startswith(("live_", "prototype")):
        return None

    cleaned = (
        name.replace("pressure", "")
        .replace("bar", "")
        .replace(" ", "")
        .strip("_-")
    )
    match = re.search(r"\d+(?:[.,_]\d+)?", cleaned)
    if not match:
        return None
    return float(match.group(0).replace(",", ".").replace("_", "."))


def image_paths(input_dir: Path) -> list[Path]:
    return sorted(
        path
        for path in input_dir.iterdir()
        if (
            path.is_file()
            and path.suffix.lower() in IMAGE_SUFFIXES
            and not path.stem.lower().startswith(("live_", "prototype"))
        )
    )


def fit_calibration(samples: list[dict], max_bar: float) -> dict | None:
    """Fit angle = angle_0bar + pressure * degrees_per_bar."""
    points = sorted(
        (sample["expected_bar"], sample["needle_angle_deg"])
        for sample in samples
        if sample["ok"] and sample["expected_bar"] is not None
    )
    if len(points) < 2:
        return None

    unwrapped = []
    previous_angle = None
    offset = 0.0
    for expected, angle in points:
        adjusted = angle + offset
        if previous_angle is not None:
            while adjusted - previous_angle > 180:
                offset -= 360
                adjusted = angle + offset
            while previous_angle - adjusted > 180:
                offset += 360
                adjusted = angle + offset
        unwrapped.append((expected, adjusted))
        previous_angle = adjusted

    expected_values = [p[0] for p in unwrapped]
    angle_values = [p[1] for p in unwrapped]
    expected_mean = sum(expected_values) / len(expected_values)
    angle_mean = sum(angle_values) / len(angle_values)
    denominator = sum((value - expected_mean) ** 2 for value in expected_values)
    if denominator == 0:
        return None

    degrees_per_bar = sum(
        (expected - expected_mean) * (angle - angle_mean)
        for expected, angle in unwrapped
    ) / denominator
    angle_0bar = angle_mean - degrees_per_bar * expected_mean
    total_sweep = degrees_per_bar * max_bar

    return {
        "angle_0bar": round(angle_0bar % 360, 1),
        "total_sweep": round(total_sweep, 1),
        "max_bar": max_bar,
        "degrees_per_bar": round(degrees_per_bar, 2),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read and calibrate a directory of manometer sample images."
    )
    parser.add_argument(
        "input_dir",
        nargs="?",
        type=Path,
        default=Path("samples/raw"),
        help="Directory containing sample images named with expected bar values",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).with_name("config.yaml"),
        help="Service config YAML with current calibration values",
    )
    parser.add_argument(
        "--debug-dir",
        type=Path,
        default=Path("samples/debug"),
        help="Directory for debug overlays and per-image JSON files",
    )
    parser.add_argument(
        "--summary-out",
        type=Path,
        help="Optional JSON summary output path",
    )
    parser.add_argument(
        "--no-debug",
        action="store_true",
        help="Do not write debug overlay images",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    calibration = load_calibration(
        args.config if args.config.exists() else None,
        GaugeCalibration,
    )
    config_path = args.config if args.config.exists() else None
    rotation_degrees = load_image_rotation(config_path)
    reader = GaugeReader(
        calibration=calibration,
        image_rotation_degrees=rotation_degrees,
    )

    paths = image_paths(args.input_dir)
    if not paths:
        print(f"No sample images found in {args.input_dir}", file=sys.stderr)
        return 1

    args.debug_dir.mkdir(parents=True, exist_ok=True)
    samples = []
    for path in paths:
        expected = parse_expected_pressure(path)
        image = cv2.imread(str(path))
        if image is None:
            samples.append(
                {
                    "image": str(path),
                    "expected_bar": expected,
                    "ok": False,
                    "error": "image_decode_failed",
                }
            )
            continue

        debug_path = args.debug_dir / f"{path.stem}_debug.jpg"
        reading = reader.read(
            image,
            use_median=False,
            generate_debug=not args.no_debug,
        )
        if reading is None:
            samples.append(
                {
                    "image": str(path),
                    "expected_bar": expected,
                    "ok": False,
                    "error": "detection_failed",
                }
            )
            continue

        if not args.no_debug and reading.debug_image is not None:
            cv2.imwrite(str(debug_path), reading.debug_image)

        sample = {
            "image": str(path),
            "expected_bar": expected,
            "ok": True,
            "pressure_bar": reading.pressure_bar,
            "needle_angle_deg": reading.needle_angle_deg,
            "confidence": reading.confidence,
            "error_bar": round(reading.pressure_bar - expected, 3)
            if expected is not None
            else None,
            "debug_image": str(debug_path) if not args.no_debug else None,
        }
        samples.append(sample)

        json_path = args.debug_dir / f"{path.stem}.json"
        json_path.write_text(json.dumps(sample, indent=2, sort_keys=True) + "\n")

    suggested = fit_calibration(samples, calibration.max_bar)
    summary = {
        "samples": samples,
        "current_calibration": {
            "angle_0bar": calibration.angle_0bar,
            "total_sweep": calibration.total_sweep,
            "max_bar": calibration.max_bar,
        },
        "image_rotation_degrees": rotation_degrees,
        "suggested_calibration": suggested,
    }

    print("image expected detected angle confidence error")
    for sample in samples:
        expected = sample["expected_bar"]
        expected_text = f"{expected:.2f}" if expected is not None else "-"
        if not sample["ok"]:
            print(f"{Path(sample['image']).name} {expected_text} - - - {sample['error']}")
            continue
        error = sample["error_bar"]
        error_text = f"{error:+.2f}" if error is not None else "-"
        print(
            f"{Path(sample['image']).name} "
            f"{expected_text} "
            f"{sample['pressure_bar']:.2f} "
            f"{sample['needle_angle_deg']:.1f} "
            f"{sample['confidence']:.2f} "
            f"{error_text}"
        )

    if suggested is not None:
        print(
            "suggested "
            f"angle_0bar={suggested['angle_0bar']} "
            f"total_sweep={suggested['total_sweep']} "
            f"max_bar={suggested['max_bar']}"
        )
    print(f"image_rotation_degrees={rotation_degrees}")

    if args.summary_out is not None:
        args.summary_out.parent.mkdir(parents=True, exist_ok=True)
        args.summary_out.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
