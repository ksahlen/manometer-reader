"""
Gauge reader for Beulco 0-3 bar manometer.

Uses OpenCV to detect the needle angle via adaptive thresholding
and Hough line detection, then maps the angle to a pressure value.

Calibration:
    The gauge scale is defined by two parameters:
    - angle_0bar: the needle angle (degrees, 0=up, clockwise) at 0 bar
    - total_sweep: the total angular sweep from 0 bar to max bar (clockwise)
    
    These can be calibrated by providing known pressure/angle pairs.
"""

import cv2
import numpy as np
import math
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class GaugeReading:
    """Result of a gauge reading attempt."""
    pressure_bar: float
    needle_angle_deg: float
    confidence: float  # 0-1, based on line detection quality
    raw_image: Optional[np.ndarray] = None
    debug_image: Optional[np.ndarray] = None


@dataclass
class GaugeCalibration:
    """Calibration parameters for the gauge scale."""
    angle_0bar: float = 220.0    # Needle angle at 0 bar (degrees, 0=up, CW)
    total_sweep: float = 270.0   # Total angular sweep for full scale (degrees)
    max_bar: float = 3.0         # Maximum value on gauge scale
    
    def angle_to_bar(self, angle_deg: float) -> float:
        """Convert needle angle to bar value."""
        shifted = angle_deg - self.angle_0bar
        if shifted < 0:
            shifted += 360
        bar = (shifted / self.total_sweep) * self.max_bar
        return max(0, min(self.max_bar, bar))
    
    def bar_to_angle(self, bar: float) -> float:
        """Convert bar value to expected needle angle."""
        angle = self.angle_0bar + (bar / self.max_bar) * self.total_sweep
        if angle >= 360:
            angle -= 360
        return angle


class GaugeReader:
    """Reads pressure from a Beulco manometer image using computer vision."""
    
    def __init__(self, calibration: Optional[GaugeCalibration] = None):
        self.calibration = calibration or GaugeCalibration()
        self._history: list[float] = []
        self.median_window = 5  # Number of readings to median-filter
    
    def read(self, image: np.ndarray, use_median: bool = True,
             generate_debug: bool = False) -> Optional[GaugeReading]:
        """
        Read pressure from a gauge image.
        
        Args:
            image: BGR image (from cv2.imread or camera capture)
            use_median: Apply median filter over recent readings
            generate_debug: Generate annotated debug image
            
        Returns:
            GaugeReading or None if detection failed
        """
        # Step 1: Find the circular gauge face
        circle = self._find_gauge_circle(image)
        if circle is None:
            logger.warning("Could not find gauge circle in image")
            return None
        
        cx, cy, radius = circle
        logger.debug(f"Found gauge circle: center=({cx},{cy}), radius={radius}")
        
        # Step 2: Crop to gauge area
        cropped, mask, ncx, ncy = self._crop_gauge(image, cx, cy, radius)
        
        # Step 3: Detect needle angle
        result = self._detect_needle(cropped, mask, ncx, ncy, radius)
        if result is None:
            logger.warning("Could not detect needle in image")
            return None
        
        angle, line, confidence, thresh = result
        
        # Step 4: Convert angle to bar
        bar = self.calibration.angle_to_bar(angle)
        
        # Step 5: Median filter
        if use_median:
            self._history.append(bar)
            if len(self._history) > self.median_window:
                self._history = self._history[-self.median_window:]
            bar = float(np.median(self._history))
        
        logger.info(f"Reading: {bar:.2f} bar (angle={angle:.1f}°, confidence={confidence:.2f})")
        
        # Step 6: Generate debug image if requested
        debug_img = None
        if generate_debug:
            debug_img = self._draw_debug(cropped, ncx, ncy, radius, angle, bar, line, confidence)
        
        return GaugeReading(
            pressure_bar=round(bar, 2),
            needle_angle_deg=round(angle, 1),
            confidence=round(confidence, 2),
            raw_image=cropped,
            debug_image=debug_img,
        )
    
    def reset_history(self):
        """Clear the median filter history."""
        self._history.clear()
    
    def _find_gauge_circle(self, img: np.ndarray) -> Optional[tuple[int, int, int]]:
        """Find the main circular gauge face using Hough circles."""
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        blurred = cv2.GaussianBlur(gray, (9, 9), 2)
        
        h, w = img.shape[:2]
        min_r = min(h, w) // 6
        max_r = min(h, w) // 2
        
        circles = cv2.HoughCircles(
            blurred, cv2.HOUGH_GRADIENT, dp=1.2,
            minDist=min(h, w) // 2,
            param1=100, param2=40,
            minRadius=min_r, maxRadius=max_r
        )
        
        if circles is None:
            return None
        
        circles = np.round(circles[0]).astype(int)
        best = max(circles, key=lambda c: int(c[2]))
        return (int(best[0]), int(best[1]), int(best[2]))
    
    def _crop_gauge(self, img, cx, cy, r):
        """Crop image to gauge area and create interior mask."""
        size = int(r * 2.2)
        x1 = max(0, cx - size // 2)
        y1 = max(0, cy - size // 2)
        x2 = min(img.shape[1], cx + size // 2)
        y2 = min(img.shape[0], cy + size // 2)
        
        cropped = img[y1:y2, x1:x2].copy()
        new_cx = cx - x1
        new_cy = cy - y1
        
        mask = np.zeros(cropped.shape[:2], dtype=np.uint8)
        cv2.circle(mask, (new_cx, new_cy), int(r * 0.82), 255, -1)
        cv2.circle(mask, (new_cx, new_cy), int(r * 0.06), 0, -1)
        
        return cropped, mask, new_cx, new_cy
    
    def _detect_needle(self, img, mask, cx, cy, radius):
        """
        Detect needle using adaptive threshold + Hough line detection.
        Returns (angle_deg, line, confidence, threshold_image) or None.
        """
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        
        # CLAHE for contrast normalization (handles glare)
        clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
        enhanced = clahe.apply(gray)
        
        # Adaptive threshold
        thresh = cv2.adaptiveThreshold(
            enhanced, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY_INV, 31, 10
        )
        thresh = cv2.bitwise_and(thresh, mask)
        
        # Morphological cleanup
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
        thresh = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel, iterations=1)
        
        # Edge detection and line detection
        edges = cv2.Canny(thresh, 50, 150)
        lines = cv2.HoughLinesP(
            edges, rho=1, theta=np.pi / 180, threshold=30,
            minLineLength=int(radius * 0.25),
            maxLineGap=int(radius * 0.05)
        )
        
        if lines is None:
            return None
        
        # Score lines: prefer long lines that pass near center
        best_line = None
        best_score = 0.0
        
        for line in lines:
            x1, y1, x2, y2 = line[0]
            length = math.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2)
            
            # Point-to-line distance from center
            dx, dy = x2 - x1, y2 - y1
            denom = dx * dx + dy * dy + 1e-8
            t = max(0.0, min(1.0, ((cx - x1) * dx + (cy - y1) * dy) / denom))
            closest_x = x1 + t * dx
            closest_y = y1 + t * dy
            dist = math.sqrt((closest_x - cx) ** 2 + (closest_y - cy) ** 2)
            
            if dist > radius * 0.15:
                continue
            
            score = length * (1.0 / (dist + 1))
            if score > best_score:
                best_score = score
                best_line = line[0]
        
        if best_line is None:
            return None
        
        x1, y1, x2, y2 = best_line
        
        # Determine tip direction (further from center)
        d1 = math.sqrt((x1 - cx) ** 2 + (y1 - cy) ** 2)
        d2 = math.sqrt((x2 - cx) ** 2 + (y2 - cy) ** 2)
        
        if d1 > d2:
            tip_x, tip_y = x1, y1
        else:
            tip_x, tip_y = x2, y2
        
        # Angle: 0=up, clockwise positive
        dx = tip_x - cx
        dy = tip_y - cy
        angle_rad = math.atan2(dx, -dy)
        angle_deg = math.degrees(angle_rad)
        if angle_deg < 0:
            angle_deg += 360
        
        # Confidence based on line length relative to radius
        line_length = math.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2)
        confidence = min(1.0, line_length / (radius * 0.6))
        
        return angle_deg, best_line, confidence, thresh
    
    def _draw_debug(self, img, cx, cy, radius, angle, bar, line, confidence):
        """Generate annotated debug image."""
        debug = img.copy()
        
        # Draw gauge circle and scanning zone
        cv2.circle(debug, (cx, cy), radius, (0, 255, 0), 2)
        cv2.circle(debug, (cx, cy), int(radius * 0.82), (255, 255, 0), 1)
        cv2.circle(debug, (cx, cy), 3, (0, 255, 0), -1)
        
        # Draw detected Hough line
        if line is not None:
            x1, y1, x2, y2 = line
            cv2.line(debug, (x1, y1), (x2, y2), (255, 0, 0), 2)
        
        # Draw needle direction
        tip_x = int(cx + radius * 0.8 * math.sin(math.radians(angle)))
        tip_y = int(cy - radius * 0.8 * math.cos(math.radians(angle)))
        cv2.line(debug, (cx, cy), (tip_x, tip_y), (0, 0, 255), 3)
        
        # Draw scale reference dots
        for bv in [0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0]:
            ref_angle = self.calibration.bar_to_angle(bv)
            rx = int(cx + radius * 0.9 * math.sin(math.radians(ref_angle)))
            ry = int(cy - radius * 0.9 * math.cos(math.radians(ref_angle)))
            cv2.circle(debug, (rx, ry), 4, (255, 0, 255), -1)
            cv2.putText(debug, f"{bv}", (rx + 6, ry + 4),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 0, 255), 1)
        
        # Info text
        cv2.putText(debug, f"{bar:.2f} bar  conf={confidence:.0%}",
                    (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
        
        return debug
