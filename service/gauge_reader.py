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
    
    def __init__(
        self,
        calibration: Optional[GaugeCalibration] = None,
        image_rotation_degrees: int = 0,
    ):
        self.calibration = calibration or GaugeCalibration()
        self.image_rotation_degrees = self._normalize_rotation(image_rotation_degrees)
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
        image = self._rotate_image(image)

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

        if not self._angle_in_scale_sweep(angle):
            logger.warning(
                "Detected angle %.1f° is outside calibrated gauge sweep",
                angle,
            )
            return None
        
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

    @staticmethod
    def _normalize_rotation(rotation_degrees: int) -> int:
        """Normalize clockwise image rotation to one of 0, 90, 180, or 270."""
        rotation = int(rotation_degrees) % 360
        if rotation not in (0, 90, 180, 270):
            raise ValueError("image_rotation_degrees must be 0, 90, 180, or 270")
        return rotation

    def _rotate_image(self, img: np.ndarray) -> np.ndarray:
        """Rotate image clockwise before analysis, if configured."""
        if self.image_rotation_degrees == 0:
            return img
        if self.image_rotation_degrees == 90:
            return cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
        if self.image_rotation_degrees == 180:
            return cv2.rotate(img, cv2.ROTATE_180)
        return cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE)
    
    def _find_gauge_circle(self, img: np.ndarray) -> Optional[tuple[int, int, int]]:
        """Find the main circular gauge face using Hough circles."""
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        h, w = img.shape[:2]
        min_r = min(h, w) // 6
        max_r = min(h, w) // 2

        attempts = [
            # Primary pass: strict enough to avoid false positives on clear images.
            ((9, 9), min_r, 40),
            # Fallback passes for dim/noisy live snapshots. Use a larger minimum
            # radius so we do not lock on to text or the center hub.
            ((5, 5), min(h, w) // 4, 25),
            ((5, 5), min(h, w) // 4, 20),
            ((5, 5), min(h, w) // 4, 15),
        ]

        for blur_size, attempt_min_r, param2 in attempts:
            blurred = cv2.GaussianBlur(gray, blur_size, 2)
            circles = cv2.HoughCircles(
                blurred, cv2.HOUGH_GRADIENT, dp=1.2,
                minDist=min(h, w) // 2,
                param1=100, param2=param2,
                minRadius=attempt_min_r, maxRadius=max_r
            )

            if circles is None:
                continue

            circles = np.round(circles[0]).astype(int)
            image_center = np.array([w / 2, h / 2])
            best = max(
                circles,
                key=lambda c: int(c[2]) - 0.15 * np.linalg.norm(c[:2] - image_center),
            )
            logger.debug(
                "Found gauge circle with param2=%s: center=(%s,%s), radius=%s",
                param2, int(best[0]), int(best[1]), int(best[2])
            )
            return (int(best[0]), int(best[1]), int(best[2]))

        return None
    
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
        best_angle = None
        best_line_length = 0.0
        
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

            # Start with the endpoint furthest from center, then compare that
            # direction with its opposite direction using the thin-tip score.
            d1 = math.sqrt((x1 - cx) ** 2 + (y1 - cy) ** 2)
            d2 = math.sqrt((x2 - cx) ** 2 + (y2 - cy) ** 2)
            if d1 > d2:
                tip_x, tip_y = x1, y1
            else:
                tip_x, tip_y = x2, y2
            
            raw_angle = self._angle_from_center(cx, cy, tip_x, tip_y)
            opposite_angle = (raw_angle + 180.0) % 360.0
            raw_tip_score = self._score_thin_needle_ray(
                thresh, mask, cx, cy, radius, raw_angle
            )
            opposite_tip_score = self._score_thin_needle_ray(
                thresh, mask, cx, cy, radius, opposite_angle
            )

            if opposite_tip_score > raw_tip_score:
                candidate_angle = opposite_angle
                tip_score = opposite_tip_score
            else:
                candidate_angle = raw_angle
                tip_score = raw_tip_score

            score = (length / (dist + 1)) * (0.15 + 3.0 * tip_score)
            if not self._angle_in_scale_sweep(candidate_angle, margin_deg=10.0):
                score *= 0.15

            if score > best_score:
                best_score = score
                best_line = line[0]
                best_angle = candidate_angle
                best_line_length = length
        
        if best_line is None or best_angle is None:
            return None
        
        # Confidence based on line length relative to radius
        confidence = min(1.0, best_line_length / (radius * 0.6))
        
        return best_angle, best_line, confidence, thresh

    @staticmethod
    def _angle_from_center(cx: int, cy: int, x: int, y: int) -> float:
        """Calculate angle where 0=up and clockwise is positive."""
        angle_rad = math.atan2(x - cx, -(y - cy))
        angle_deg = math.degrees(angle_rad)
        if angle_deg < 0:
            angle_deg += 360
        return angle_deg

    def _angle_in_scale_sweep(self, angle_deg: float, margin_deg: float = 0.0) -> bool:
        """Return true if an angle is plausibly on the calibrated gauge scale."""
        shifted = angle_deg - self.calibration.angle_0bar
        if shifted < 0:
            shifted += 360
        return shifted <= self.calibration.total_sweep + margin_deg

    @staticmethod
    def _score_thin_needle_ray(thresh, mask, cx, cy, radius, angle_deg: float) -> float:
        """
        Score how much a ray looks like the thin measuring needle tip.

        The counterweight/back end of this gauge is broad and dark. A true
        measuring tip is a narrow dark feature that continues toward the outer
        tick marks, so this score rewards a dark core with lighter side bands
        and weights the outer half of the ray more heavily.
        """
        angle_rad = math.radians(angle_deg)
        ux = math.sin(angle_rad)
        uy = -math.cos(angle_rad)
        px = math.cos(angle_rad)
        py = math.sin(angle_rad)

        core_half_width = max(2, int(radius * 0.010))
        side_inner = max(core_half_width + 3, int(radius * 0.025))
        side_outer = max(side_inner + 5, int(radius * 0.060))

        weighted_scores = []
        weights = []
        outer_hits = 0
        outer_samples = 0
        start_r = radius * 0.18
        end_r = radius * 0.82

        for rr in np.linspace(start_r, end_r, 170):
            core_values = []
            side_values = []

            for offset in range(-side_outer, side_outer + 1, 2):
                x = int(round(cx + ux * rr + px * offset))
                y = int(round(cy + uy * rr + py * offset))
                if not (0 <= x < thresh.shape[1] and 0 <= y < thresh.shape[0]):
                    continue
                if mask[y, x] == 0:
                    continue

                dark = 1.0 if thresh[y, x] > 0 else 0.0
                if abs(offset) <= core_half_width:
                    core_values.append(dark)
                elif side_inner <= abs(offset) <= side_outer:
                    side_values.append(dark)

            if not core_values:
                continue

            core_fraction = sum(core_values) / len(core_values)
            side_fraction = sum(side_values) / len(side_values) if side_values else 0.0
            thin_score = max(0.0, core_fraction * (1.0 - side_fraction))
            radius_fraction = (rr - start_r) / (end_r - start_r)
            weight = 0.35 + 0.65 * radius_fraction
            weighted_scores.append(thin_score * weight)
            weights.append(weight)

            if rr >= radius * 0.52:
                outer_samples += 1
                if core_fraction > 0.30 and side_fraction < 0.65:
                    outer_hits += 1

        if not weighted_scores:
            return 0.0

        outer_coverage = outer_hits / max(1, outer_samples)
        return float(
            (sum(weighted_scores) / sum(weights))
            * min(1.0, outer_coverage * 2.5)
        )
    
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
