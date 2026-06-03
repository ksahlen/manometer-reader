"""
Manometer reader service.

Periodically captures an image from the ESP32-CAM,
runs the gauge reader pipeline, and publishes the
pressure reading to MQTT for Home Assistant.
"""

import time
import json
import signal
import sys
import logging
import argparse
from pathlib import Path
from datetime import datetime
from urllib.parse import quote, urlsplit

import cv2
import numpy as np
import yaml
import paho.mqtt.client as mqtt
import requests

from gauge_reader import GaugeReader, GaugeCalibration

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("manometer-service")


def load_config(path: str = "config.yaml") -> dict:
    """Load configuration from YAML file."""
    config_path = Path(path)
    if not config_path.exists():
        logger.error(f"Config file not found: {path}")
        sys.exit(1)
    
    with open(config_path) as f:
        return yaml.safe_load(f)


def capture_image(url: str, timeout: int = 10) -> np.ndarray | None:
    """Fetch a snapshot from the ESP32-CAM."""
    try:
        response = requests.get(url, timeout=timeout)
        response.raise_for_status()
        
        img_array = np.frombuffer(response.content, dtype=np.uint8)
        img = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
        
        if img is None:
            logger.error("Failed to decode image from camera")
            return None
        
        logger.debug(f"Captured image: {img.shape[1]}x{img.shape[0]}")
        return img
        
    except requests.RequestException as e:
        logger.error(f"Failed to capture image from {url}: {e}")
        return None


def _camera_base_url(camera_url: str) -> str:
    """Return scheme and host from the snapshot URL."""
    parsed = urlsplit(camera_url)
    return f"{parsed.scheme}://{parsed.netloc}"


def set_backlight(
    lighting_config: dict,
    turn_on: bool,
    camera_url: str,
) -> bool:
    """Control the ESPHome backlight through the web server API."""
    if not lighting_config.get("enabled", False):
        return True

    base_url = lighting_config.get("base_url") or _camera_base_url(camera_url)
    entity_name = quote(lighting_config.get("entity_name", "Backlight"), safe="")
    action = "turn_on" if turn_on else "turn_off"
    url = f"{base_url}/light/{entity_name}/{action}"

    params = {
        "transition": lighting_config.get("transition", 0),
    }
    if turn_on:
        params.update(
            {
                "brightness": lighting_config.get("brightness", 64),
                "r": lighting_config.get("red", 255),
                "g": lighting_config.get("green", 255),
                "b": lighting_config.get("blue", 255),
            }
        )

    try:
        requests.post(
            url,
            params=params,
            data=b"",
            timeout=lighting_config.get("timeout", 5),
        ).raise_for_status()
        logger.info("Backlight %s", "on" if turn_on else "off")
        return True
    except requests.RequestException as e:
        logger.warning("Failed to turn backlight %s: %s", "on" if turn_on else "off", e)
        return False


def capture_image_with_lighting(
    camera_url: str,
    camera_timeout: int,
    lighting_config: dict,
    warmup_snapshots: int = 0,
    warmup_delay_seconds: float = 1.0,
) -> np.ndarray | None:
    """Turn on configured light, capture a snapshot, and optionally turn it off."""
    if lighting_config.get("enabled", False):
        set_backlight(lighting_config, True, camera_url)
        settle_seconds = lighting_config.get("settle_seconds", 0.5)
        logger.info("Waiting %.1fs for light/camera to settle", settle_seconds)
        time.sleep(settle_seconds)

    try:
        for index in range(max(0, int(warmup_snapshots))):
            logger.info("Capturing warmup snapshot %s/%s", index + 1, warmup_snapshots)
            capture_image(camera_url, timeout=camera_timeout)
            if warmup_delay_seconds > 0:
                time.sleep(warmup_delay_seconds)

        logger.info("Capturing analysis snapshot")
        return capture_image(camera_url, timeout=camera_timeout)
    finally:
        if (
            lighting_config.get("enabled", False)
            and lighting_config.get("turn_off_after_snapshot", True)
        ):
            set_backlight(lighting_config, False, camera_url)


def setup_mqtt(config: dict) -> mqtt.Client:
    """Connect to MQTT broker."""
    client = mqtt.Client(
        client_id=config.get("client_id", "manometer-reader"),
        protocol=mqtt.MQTTv311,
    )
    
    username = config.get("username")
    password = config.get("password")
    if username:
        client.username_pw_set(username, password)
    
    host = config.get("host", "localhost")
    port = config.get("port", 1883)
    
    client.connect(host, port, keepalive=60)
    client.loop_start()
    
    logger.info(f"Connected to MQTT broker at {host}:{port}")
    return client


def publish_ha_discovery(client: mqtt.Client, topic_prefix: str, device_name: str):
    """Publish Home Assistant MQTT discovery messages."""
    device_info = {
        "identifiers": ["manometer_reader"],
        "name": device_name,
        "manufacturer": "DIY",
        "model": "OpenCV Gauge Reader",
        "sw_version": "1.0.0",
    }
    
    sensors = [
        {
            "name": "Pressure",
            "unique_id": "manometer_pressure",
            "state_topic": f"{topic_prefix}/state",
            "value_template": "{{ value_json.pressure_bar }}",
            "unit_of_measurement": "bar",
            "device_class": "pressure",
            "state_class": "measurement",
            "icon": "mdi:gauge",
        },
        {
            "name": "Needle angle",
            "unique_id": "manometer_needle_angle",
            "state_topic": f"{topic_prefix}/state",
            "value_template": "{{ value_json.needle_angle_deg }}",
            "unit_of_measurement": "°",
            "icon": "mdi:angle-acute",
            "entity_category": "diagnostic",
        },
        {
            "name": "Confidence",
            "unique_id": "manometer_confidence",
            "state_topic": f"{topic_prefix}/state",
            "value_template": "{{ value_json.confidence }}",
            "icon": "mdi:check-circle-outline",
            "entity_category": "diagnostic",
        },
        {
            "name": "Last reading",
            "unique_id": "manometer_last_reading",
            "state_topic": f"{topic_prefix}/state",
            "value_template": "{{ value_json.timestamp }}",
            "device_class": "timestamp",
            "entity_category": "diagnostic",
        },
    ]
    
    for sensor in sensors:
        sensor["device"] = device_info
        discovery_topic = f"homeassistant/sensor/{sensor['unique_id']}/config"
        client.publish(discovery_topic, json.dumps(sensor), retain=True)
        logger.debug(f"Published discovery for {sensor['name']}")
    
    logger.info("Published HA MQTT discovery messages")


def publish_reading(client: mqtt.Client, topic: str, reading):
    """Publish a gauge reading to MQTT."""
    payload = {
        "pressure_bar": reading.pressure_bar,
        "needle_angle_deg": reading.needle_angle_deg,
        "confidence": reading.confidence,
        "timestamp": datetime.now().isoformat(),
    }
    
    client.publish(topic, json.dumps(payload), retain=True)
    logger.info(f"Published: {reading.pressure_bar:.2f} bar "
                f"(angle={reading.needle_angle_deg}°, "
                f"confidence={reading.confidence:.0%})")


def save_image_artifact(
    image: np.ndarray,
    output_dir: str,
    prefix: str,
    max_keep: int = 50,
) -> Path | None:
    """Save an image artifact with timestamp and prune old files."""
    path = Path(output_dir)
    path.mkdir(parents=True, exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = path / f"{prefix}_{timestamp}.jpg"
    if not cv2.imwrite(str(filepath), image):
        logger.warning("Failed to save %s image to %s", prefix, filepath)
        return None
    
    images = sorted(path.glob(f"{prefix}_*.jpg"))
    for old in images[:-max_keep]:
        old.unlink()

    logger.info("Saved %s image: %s", prefix, filepath)
    return filepath


def save_debug_image(image: np.ndarray, output_dir: str):
    """Save annotated debug image with timestamp."""
    return save_image_artifact(image, output_dir, "debug")


def save_raw_image(image: np.ndarray, output_dir: str):
    """Save raw camera snapshot with timestamp."""
    return save_image_artifact(image, output_dir, "raw")


def should_accept_pressure_reading(
    reading,
    last_accepted_pressure: float | None,
    jump_state: dict,
    read_config: dict,
) -> tuple[bool, str, bool]:
    """Return whether a raw pressure reading is plausible enough to publish."""
    pressure = reading.pressure_bar
    max_plausible = read_config.get("max_plausible_bar", 1.8)
    if max_plausible is not None and pressure > float(max_plausible):
        jump_state.clear()
        return (
            False,
            f"implausible pressure {pressure:.2f} bar > "
            f"{float(max_plausible):.2f} bar",
            False,
        )

    if last_accepted_pressure is None:
        jump_state.clear()
        return True, "", False

    max_jump = float(read_config.get("max_jump_bar", 0.45))
    if max_jump <= 0:
        jump_state.clear()
        return True, "", False

    jump = abs(pressure - last_accepted_pressure)
    if jump <= max_jump:
        jump_state.clear()
        return True, "", False

    required_count = max(1, int(read_config.get("jump_confirmation_count", 2)))
    if required_count <= 1:
        jump_state.clear()
        return True, "", True

    tolerance = float(read_config.get("jump_confirmation_tolerance_bar", 0.12))
    pending_pressure = jump_state.get("pressure")
    pending_count = int(jump_state.get("count", 0))
    same_pending_jump = (
        pending_pressure is not None
        and abs(pressure - pending_pressure) <= tolerance
        and (pressure - last_accepted_pressure)
        * (pending_pressure - last_accepted_pressure)
        > 0
    )

    if same_pending_jump:
        pending_count += 1
    else:
        pending_count = 1

    jump_state["pressure"] = pressure
    jump_state["count"] = pending_count

    if pending_count >= required_count:
        jump_state.clear()
        return (
            True,
            f"confirmed pressure jump from {last_accepted_pressure:.2f} "
            f"to {pressure:.2f} bar",
            True,
        )

    return (
        False,
        f"unconfirmed pressure jump from {last_accepted_pressure:.2f} "
        f"to {pressure:.2f} bar",
        False,
    )


def apply_accepted_median_filter(
    reading,
    pressure_history: list[float],
    median_window: int,
) -> None:
    """Median-filter accepted raw readings in-place before publishing."""
    pressure_history.append(reading.pressure_bar)
    if len(pressure_history) > median_window:
        pressure_history[:] = pressure_history[-median_window:]
    reading.pressure_bar = round(float(np.median(pressure_history)), 2)


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments for local and VM runs."""
    parser = argparse.ArgumentParser(description="Run the manometer reader service.")
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to service config YAML",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Capture, read, publish once, then exit",
    )
    return parser.parse_args()


def main(config_path: str = "config.yaml", run_once: bool = False):
    config = load_config(config_path)
    
    # Camera config
    cam_config = config.get("camera", {})
    cam_url = cam_config.get("url", "http://manometer-cam.local:8080/")
    cam_timeout = cam_config.get("timeout", 10)
    image_rotation_degrees = cam_config.get("rotate_degrees", 0)
    warmup_snapshots = cam_config.get("warmup_snapshots", 0)
    warmup_delay_seconds = cam_config.get("warmup_delay_seconds", 1.0)
    lighting_config = config.get("lighting", {})
    
    # Gauge calibration
    cal_config = config.get("calibration", {})
    calibration = GaugeCalibration(
        angle_0bar=cal_config.get("angle_0bar", 220.0),
        total_sweep=cal_config.get("total_sweep", 270.0),
        max_bar=cal_config.get("max_bar", 3.0),
    )
    
    # Reading config
    read_config = config.get("reading", {})
    interval = read_config.get("interval_seconds", 300)
    min_confidence = read_config.get("min_confidence", 0.3)
    median_window = max(1, int(read_config.get("median_window", 5)))
    save_debug = read_config.get("save_debug_images", False)
    save_raw = read_config.get("save_raw_images", save_debug)
    debug_dir = read_config.get("debug_image_dir", "/tmp/manometer-debug")
    
    # MQTT
    mqtt_config = config.get("mqtt", {})
    topic_prefix = mqtt_config.get("topic_prefix", "manometer")
    device_name = mqtt_config.get("device_name", "Manometer Reader")
    
    client = setup_mqtt(mqtt_config)
    publish_ha_discovery(client, topic_prefix, device_name)
    
    # Gauge reader
    reader = GaugeReader(
        calibration=calibration,
        image_rotation_degrees=image_rotation_degrees,
    )
    reader.median_window = median_window
    
    # Graceful shutdown
    running = True
    
    def shutdown(signum, frame):
        nonlocal running
        logger.info("Shutting down...")
        running = False
    
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    
    logger.info(
        "Starting manometer reader "
        f"(interval={interval}s, camera={cam_url}, "
        f"rotation={reader.image_rotation_degrees}°, "
        f"lighting={'on' if lighting_config.get('enabled', False) else 'off'}, "
        f"max_plausible={read_config.get('max_plausible_bar', 1.8)} bar)"
    )
    
    consecutive_failures = 0
    max_failures = 10
    accepted_pressure_history: list[float] = []
    last_accepted_pressure: float | None = None
    jump_state: dict = {}
    
    while running:
        image = capture_image_with_lighting(
            cam_url,
            cam_timeout,
            lighting_config,
            warmup_snapshots=warmup_snapshots,
            warmup_delay_seconds=warmup_delay_seconds,
        )
        
        if image is not None:
            if save_raw:
                save_raw_image(image, debug_dir)

            reading = reader.read(
                image,
                use_median=False,
                generate_debug=save_debug,
            )
            
            skip_reason = ""
            accept_note = ""
            reset_median_history = False
            if reading is None:
                skip_reason = "detection failed"
            elif reading.confidence < min_confidence:
                skip_reason = "low confidence"
            else:
                accepted, accept_note, reset_median_history = (
                    should_accept_pressure_reading(
                        reading,
                        last_accepted_pressure,
                        jump_state,
                        read_config,
                    )
                )
                if not accepted:
                    skip_reason = accept_note

            if reading and not skip_reason:
                raw_pressure = reading.pressure_bar
                if accept_note:
                    logger.info(accept_note)
                last_accepted_pressure = raw_pressure
                if reset_median_history:
                    accepted_pressure_history.clear()
                apply_accepted_median_filter(
                    reading,
                    accepted_pressure_history,
                    median_window,
                )
                publish_reading(client, f"{topic_prefix}/state", reading)
                consecutive_failures = 0
                
                if save_debug and reading.debug_image is not None:
                    save_debug_image(reading.debug_image, debug_dir)
            else:
                consecutive_failures += 1
                logger.warning(f"Skipped reading ({skip_reason}), "
                               f"failures={consecutive_failures}/{max_failures}")
        else:
            consecutive_failures += 1
            logger.warning(f"No image captured, failures={consecutive_failures}/{max_failures}")
        
        if consecutive_failures >= max_failures:
            logger.error(f"{max_failures} consecutive failures, check camera and gauge")
            # Publish error state
            client.publish(f"{topic_prefix}/state", json.dumps({
                "pressure_bar": None,
                "error": "consecutive_failures",
                "timestamp": datetime.now().isoformat(),
            }), retain=True)
            consecutive_failures = 0
        
        if run_once:
            break

        # Wait for next cycle
        for _ in range(interval):
            if not running:
                break
            time.sleep(1)
    
    client.loop_stop()
    client.disconnect()
    logger.info("Service stopped")


if __name__ == "__main__":
    args = parse_args()
    main(config_path=args.config, run_once=args.once)
