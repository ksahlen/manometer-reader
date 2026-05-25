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
from pathlib import Path
from datetime import datetime

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


def save_debug_image(image: np.ndarray, output_dir: str):
    """Save debug image with timestamp."""
    path = Path(output_dir)
    path.mkdir(parents=True, exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = path / f"debug_{timestamp}.jpg"
    cv2.imwrite(str(filepath), image)
    
    # Keep only the last N debug images
    images = sorted(path.glob("debug_*.jpg"))
    max_keep = 50
    for old in images[:-max_keep]:
        old.unlink()


def main():
    config = load_config()
    
    # Camera config
    cam_config = config.get("camera", {})
    cam_url = cam_config.get("url", "http://manometer-cam.local:8080/")
    
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
    save_debug = read_config.get("save_debug_images", False)
    debug_dir = read_config.get("debug_image_dir", "/tmp/manometer-debug")
    
    # MQTT
    mqtt_config = config.get("mqtt", {})
    topic_prefix = mqtt_config.get("topic_prefix", "manometer")
    device_name = mqtt_config.get("device_name", "Manometer Reader")
    
    client = setup_mqtt(mqtt_config)
    publish_ha_discovery(client, topic_prefix, device_name)
    
    # Gauge reader
    reader = GaugeReader(calibration=calibration)
    reader.median_window = read_config.get("median_window", 5)
    
    # Graceful shutdown
    running = True
    
    def shutdown(signum, frame):
        nonlocal running
        logger.info("Shutting down...")
        running = False
    
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    
    logger.info(f"Starting manometer reader (interval={interval}s, camera={cam_url})")
    
    consecutive_failures = 0
    max_failures = 10
    
    while running:
        image = capture_image(cam_url)
        
        if image is not None:
            reading = reader.read(
                image,
                use_median=True,
                generate_debug=save_debug,
            )
            
            if reading and reading.confidence >= min_confidence:
                publish_reading(client, f"{topic_prefix}/state", reading)
                consecutive_failures = 0
                
                if save_debug and reading.debug_image is not None:
                    save_debug_image(reading.debug_image, debug_dir)
            else:
                consecutive_failures += 1
                reason = "low confidence" if reading else "detection failed"
                logger.warning(f"Skipped reading ({reason}), "
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
        
        # Wait for next cycle
        for _ in range(interval):
            if not running:
                break
            time.sleep(1)
    
    client.loop_stop()
    client.disconnect()
    logger.info("Service stopped")


if __name__ == "__main__":
    main()
