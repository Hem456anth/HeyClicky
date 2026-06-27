"""Click-through blue cursor overlay.

A frameless, top-most, non-activating `QWidget` spanning the entire virtual
screen. When Claude emits `[POINT:x,y:label:screenN]`, `CompanionManager`
queues a `PointMarker` here; we map its logical coordinates onto the right
monitor (DPI-corrected), then fly a glowing blue dot along a quadratic
bezier arc from the current cursor position to the target, pulse there, and
finally dismiss the overlay.

Bezier rationale: a straight line from cursor to target reads as a teleport
on long flights, and a strict ease-out is jarring. A quadratic bezier whose
control point sits perpendicular to the cursor->target line gives a natural
arc that telegraphs "I'm pointing over there" without feeling cartoonish.

All Win32 calls go through `utils.win32`. No raw `ctypes` here.
"""
from __future__ import annotations

import math
from collections import deque

from PyQt6.QtCore import (
    QEasingCurve,
    QPoint,
    QPropertyAnimation,
    QRect,
    Qt,
    QTimer,
    pyqtProperty,
    pyqtSlot,
)
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter, QShowEvent
from PyQt6.QtWidgets import QApplication, QWidget

from ..models.message import PointMarker
from ..utils.logger import get_logger
from ..utils.win32 import (
    apply_overlay_window_styles,
    enumerate_monitors,
    get_cursor_position,
    logical_to_physical_on_monitor,
)

log = get_logger(__name__)


class CursorOverlay(QWidget):
    """Top-most click-through window covering the virtual desktop."""

    PULSE_BASE_RADIUS = 18.0
    PULSE_COLOR = QColor(46, 144, 250)   # Clicky blue
    FLIGHT_DURATION_MS_BASE = 600         # minimum flight time
    FLIGHT_DURATION_MS_PER_1000_PX = 250  # scale up for long flights
    HOLD_AT_TARGET_MS = 2_200             # pulse time before dismiss / next
    INTER_POINT_GAP_MS = 250              # breathing room between queued points

    # ---- breathing animation tuning ----
    # Slow enough to read as a breath, not a heartbeat. One full inhale-
    # exhale cycle. Both the radius pulse and the halo-alpha pulse use
    # this period so they stay synchronized — peak brightness at peak
    # size, dim and small together.
    BREATH_PERIOD_MS = 2_400
    # Peak radius as a fraction of PULSE_BASE_RADIUS at the breath's mid-
    # point. Kept subtle (~25% growth) so the resting dot feels alive
    # without distracting the user from whatever is being pointed at.
    PULSE_PEAK_RADIUS_RATIO = 1.25
    # Halo alpha range. The dim end is the resting baseline; the bright
    # end is the breath peak. Both lower than the flight-time halo so
    # at-rest reads as calm vs. in-motion energy.
    HALO_BASE_ALPHA = 50
    HALO_PEAK_ALPHA = 110
    # Halo alpha used while the dot is actively flying. Kept constant
    # during flight because a breathing halo would compete with the
    # comet trail (cycle 10) for attention.
    HALO_FLIGHT_ALPHA = 95

    # ---- comet trail tuning ----
    # Length of the trail in samples. At 60fps a 12-sample trail covers
    # ~200ms of motion, which reads as a clear smear without obscuring
    # half the screen on long flights.
    TRAIL_LENGTH = 12
    # The smallest trail blob is this fraction of the dot's radius. Larger
    # values make the trail blockier; smaller values make it pin-thin.
    TRAIL_MIN_RADIUS_RATIO = 0.35
    # Alpha of the trail blob closest to the dot. The oldest blob fades to
    # zero linearly from this value.
    TRAIL_HEAD_ALPHA = 130

    def __init__(self) -> None:
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        # Cover the virtual desktop (all monitors combined).
        virtual_geometry = QApplication.primaryScreen().virtualGeometry()
        self.setGeometry(virtual_geometry)

        # ---- per-flight state ----
        # Animation progress in [0, 1]; the bezier helper consumes it.
        self._flight_progress: float = 0.0
        self._flight_start_widget: QPoint | None = None
        self._flight_end_widget: QPoint | None = None
        self._flight_control_widget: QPoint | None = None
        self._current_label: str = ""
        self._pulse_radius: float = self.PULSE_BASE_RADIUS
        # Animated halo opacity (0-255) driven by the at-rest breath
        # animation. Initialized to the peak so the very first paint
        # (before the breath animation has had a frame to fire) doesn't
        # show an unexpectedly dim halo.
        self._halo_alpha: int = self.HALO_PEAK_ALPHA
        # Comet trail — bounded ring buffer of recent dot positions in
        # widget-local coords. Appended to while the flight animation is
        # running; cleared on flight-finished and on dismiss so the at-rest
        # pulse stays visually clean.
        self._trail_points: deque[QPoint] = deque(maxlen=self.TRAIL_LENGTH)

        # ---- queue ----
        self._marker_queue: deque[PointMarker] = deque()
        self._busy: bool = False

        # ---- animations ----
        self._flight_animation = QPropertyAnimation(self, b"flightProgress", self)
        self._flight_animation.setStartValue(0.0)
        self._flight_animation.setEndValue(1.0)
        self._flight_animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._flight_animation.finished.connect(self._on_flight_finished)

        self._pulse_animation = QPropertyAnimation(self, b"pulseRadius", self)
        self._pulse_animation.setDuration(self.BREATH_PERIOD_MS)
        self._pulse_animation.setStartValue(self.PULSE_BASE_RADIUS)
        self._pulse_animation.setKeyValueAt(
            0.5, self.PULSE_BASE_RADIUS * self.PULSE_PEAK_RADIUS_RATIO,
        )
        self._pulse_animation.setEndValue(self.PULSE_BASE_RADIUS)
        self._pulse_animation.setLoopCount(-1)
        self._pulse_animation.setEasingCurve(QEasingCurve.Type.InOutSine)

        # Halo brightness breath, synchronized with the radius pulse so
        # the dot grows brighter as it grows larger and dims as it shrinks
        # — reads as a single breath, not two competing animations.
        self._halo_alpha_animation = QPropertyAnimation(self, b"haloAlpha", self)
        self._halo_alpha_animation.setDuration(self.BREATH_PERIOD_MS)
        self._halo_alpha_animation.setStartValue(self.HALO_BASE_ALPHA)
        self._halo_alpha_animation.setKeyValueAt(0.5, self.HALO_PEAK_ALPHA)
        self._halo_alpha_animation.setEndValue(self.HALO_BASE_ALPHA)
        self._halo_alpha_animation.setLoopCount(-1)
        self._halo_alpha_animation.setEasingCurve(QEasingCurve.Type.InOutSine)

        self._dismiss_or_next_timer = QTimer(self)
        self._dismiss_or_next_timer.setSingleShot(True)
        self._dismiss_or_next_timer.timeout.connect(self._advance_queue)

        # Logged exactly once so cycle-9 verification can confirm from
        # the log that the click-through / non-activating flags are being
        # installed by the OS — not just silently ORed by ourselves.
        self._overlay_styles_logged_once: bool = False

    # ---- Qt properties driven by QPropertyAnimation ----
    def get_flight_progress(self) -> float:
        return self._flight_progress

    def set_flight_progress(self, value: float) -> None:
        self._flight_progress = value
        self.update()

    flightProgress = pyqtProperty(
        float, fget=get_flight_progress, fset=set_flight_progress,
    )

    def get_pulse_radius(self) -> float:
        return self._pulse_radius

    def set_pulse_radius(self, value: float) -> None:
        self._pulse_radius = value
        self.update()

    pulseRadius = pyqtProperty(float, fget=get_pulse_radius, fset=set_pulse_radius)

    def get_halo_alpha(self) -> int:
        return self._halo_alpha

    def set_halo_alpha(self, value: int) -> None:
        # Clamp defensively — Qt's animation interpolation can briefly
        # overshoot the endpoint values by a hair on InOutSine.
        self._halo_alpha = max(0, min(255, int(value)))
        self.update()

    haloAlpha = pyqtProperty(int, fget=get_halo_alpha, fset=set_halo_alpha)

    # ---- native window setup ----
    def showEvent(self, event: QShowEvent) -> None:  # type: ignore[override]
        """Re-install the click-through + non-activating ex-styles.

        Qt occasionally strips Win32 extended styles when a frameless
        widget is hidden and re-shown (the platform plugin reparents the
        HWND in some scenarios). Re-OR'ing the flags on every show closes
        that hole. `apply_overlay_window_styles` is idempotent, so the
        cost of doing this on every show is negligible.

        On the very first show we also log the HWND once for cycle-9
        verification — operators can grep `logs/heybuddy.log` for
        "overlay ex-styles applied" to confirm the call fired.
        """
        super().showEvent(event)
        try:
            hwnd = int(self.winId())
            apply_overlay_window_styles(hwnd)
            if not self._overlay_styles_logged_once:
                self._overlay_styles_logged_once = True
                log.info(
                    "overlay ex-styles applied to hwnd=%#x "
                    "(WS_EX_LAYERED | WS_EX_TRANSPARENT | "
                    "WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)",
                    hwnd,
                )
        except Exception:
            log.exception("apply_overlay_window_styles raised in showEvent")

    # ---- public API ----
    @pyqtSlot(object)
    def point_at(self, marker: PointMarker) -> None:
        """Queue a single point for flight + pulse.

        Always queue-then-drain so two points fired in quick succession both
        get shown rather than the second clobbering the first.
        """
        self._marker_queue.append(marker)
        if not self._busy:
            self._advance_queue()

    @pyqtSlot()
    def dismiss(self) -> None:
        self._flight_animation.stop()
        self._pulse_animation.stop()
        self._halo_alpha_animation.stop()
        self._dismiss_or_next_timer.stop()
        self._marker_queue.clear()
        self._busy = False
        self._flight_start_widget = None
        self._flight_end_widget = None
        self._flight_control_widget = None
        self._current_label = ""
        self._trail_points.clear()
        self.hide()

    # ---- queue driver ----
    def _advance_queue(self) -> None:
        if not self._marker_queue:
            self._finish_and_hide()
            return
        marker = self._marker_queue.popleft()
        self._begin_flight(marker)

    def _begin_flight(self, marker: PointMarker) -> None:
        monitors = enumerate_monitors()
        if not monitors:
            log.warning("No monitors enumerated; cannot point.")
            self._advance_queue()
            return
        monitor_index = max(1, min(marker.screen_index, len(monitors)))
        monitor = monitors[monitor_index - 1]
        target_physical_x, target_physical_y = logical_to_physical_on_monitor(
            marker.x, marker.y, monitor,
        )
        # Cycle-12 verification log. Lets operators confirm from
        # logs/heybuddy.log that the bezier flight is consuming the
        # right marker AND that the per-monitor DPI scale was applied.
        # Format chosen for grep-friendliness: `POINT flight: ...`.
        log.info(
            "POINT flight: screen=%d logical=(%d,%d) "
            "physical=(%d,%d) dpi=%.2fx label=%r",
            monitor.index, marker.x, marker.y,
            target_physical_x, target_physical_y,
            monitor.dpi_scale, marker.label,
        )

        # Convert physical desktop coords to widget-local coords. Start point
        # is the current physical cursor location so the dot "flies from the
        # mouse" — gives the user a clear visual anchor.
        widget_origin = self.geometry()
        cursor_x, cursor_y = get_cursor_position()
        start_widget = QPoint(
            cursor_x - widget_origin.x(),
            cursor_y - widget_origin.y(),
        )
        end_widget = QPoint(
            target_physical_x - widget_origin.x(),
            target_physical_y - widget_origin.y(),
        )

        # If a previous flight is still on screen, fly from where the dot
        # currently is, not from the system cursor — that's more visually
        # coherent during a multi-point response.
        if self._flight_end_widget is not None and self._busy:
            start_widget = self._flight_end_widget

        self._flight_start_widget = start_widget
        self._flight_end_widget = end_widget
        self._flight_control_widget = self._compute_bezier_control(
            start_widget, end_widget,
        )
        self._current_label = marker.label
        self._busy = True
        # Fresh trail for this flight — leftover points from a prior flight
        # would smear visually across the cut.
        self._trail_points.clear()

        # Show window, raise, then run the animation. The ex-style re-apply
        # happens automatically inside `showEvent` (centralized in cycle 9 so
        # any future code path that calls `show()` is also covered).
        self.show()
        self.raise_()
        self._pulse_animation.stop()
        self._halo_alpha_animation.stop()
        self._flight_animation.stop()
        self._flight_animation.setDuration(
            self._duration_for_flight(start_widget, end_widget),
        )
        self.set_flight_progress(0.0)
        self._flight_animation.start()

    def _on_flight_finished(self) -> None:
        # Park at the destination and breathe for HOLD_AT_TARGET_MS, then
        # advance to the next queued point or dismiss the overlay.
        # Drop the comet trail here so the at-rest breath stays clean — the
        # tail reads as "still in motion" and would compete with the breath.
        self._trail_points.clear()
        self._pulse_animation.start()
        self._halo_alpha_animation.start()
        self._dismiss_or_next_timer.start(
            self.HOLD_AT_TARGET_MS + self.INTER_POINT_GAP_MS,
        )

    def _finish_and_hide(self) -> None:
        self._busy = False
        self._pulse_animation.stop()
        self._halo_alpha_animation.stop()
        self._dismiss_or_next_timer.stop()
        self._flight_start_widget = None
        self._flight_end_widget = None
        self._flight_control_widget = None
        self._current_label = ""
        self._trail_points.clear()
        self.hide()

    # ---- bezier math ----
    @staticmethod
    def _compute_bezier_control(start: QPoint, end: QPoint) -> QPoint:
        """Return a quadratic bezier control point above the start->end line.

        We push the control point perpendicular to the line by an amount
        proportional to the line's length, so short flights barely curve and
        long flights make a noticeable arc.
        """
        midpoint_x = (start.x() + end.x()) / 2
        midpoint_y = (start.y() + end.y()) / 2
        dx = end.x() - start.x()
        dy = end.y() - start.y()
        length = math.hypot(dx, dy) or 1.0
        # Perpendicular unit vector (rotated -90deg so the arc consistently
        # bows toward the screen's upper half — feels more natural than dipping
        # below the line).
        perp_x = -dy / length
        perp_y = dx / length
        bow_strength = min(length * 0.35, 220.0)
        # If the perpendicular points downward (positive y), flip it so the
        # arc always bows upward in screen coords (y grows downward in Qt).
        if perp_y > 0:
            perp_x, perp_y = -perp_x, -perp_y
        return QPoint(
            int(midpoint_x + perp_x * bow_strength),
            int(midpoint_y + perp_y * bow_strength),
        )

    @staticmethod
    def _quadratic_bezier(p0: QPoint, p1: QPoint, p2: QPoint, t: float) -> QPoint:
        """Quadratic bezier interpolation at parameter t in [0, 1]."""
        one_minus_t = 1.0 - t
        x = (one_minus_t ** 2) * p0.x() + 2 * one_minus_t * t * p1.x() + (t ** 2) * p2.x()
        y = (one_minus_t ** 2) * p0.y() + 2 * one_minus_t * t * p1.y() + (t ** 2) * p2.y()
        return QPoint(int(x), int(y))

    def _duration_for_flight(self, start: QPoint, end: QPoint) -> int:
        distance = math.hypot(end.x() - start.x(), end.y() - start.y())
        scaled = (distance / 1000.0) * self.FLIGHT_DURATION_MS_PER_1000_PX
        return int(max(self.FLIGHT_DURATION_MS_BASE, scaled))

    # ---- paint ----
    def paintEvent(self, _event) -> None:  # type: ignore[override]
        if (
            self._flight_start_widget is None
            or self._flight_end_widget is None
            or self._flight_control_widget is None
        ):
            return
        current = self._quadratic_bezier(
            self._flight_start_widget,
            self._flight_control_widget,
            self._flight_end_widget,
            self._flight_progress,
        )

        # Only sample the comet trail while the flight animation is
        # actually running. The pulse animation also triggers paint events
        # at rest, and appending the same position 60x/sec would fill the
        # deque with a stack of identical points (no visible trail).
        if self._flight_animation.state() == QPropertyAnimation.State.Running:
            self._trail_points.append(current)

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)

        # Comet trail — drawn FIRST so the halo and solid dot paint on
        # top. Iterate oldest-to-newest so the alpha+radius lerp matches
        # deque order (index 0 = faintest tip, index N-1 = brightest blob
        # right behind the dot).
        trail_len = len(self._trail_points)
        if trail_len > 0:
            for index, trail_point in enumerate(self._trail_points):
                # `fade` in (0, 1]: 0 = oldest (faintest), 1 = newest.
                # Divide by max(1, trail_len) so a single-point trail
                # still draws something rather than zeroing out.
                fade = (index + 1) / max(1, trail_len)
                radius = self._pulse_radius * (
                    self.TRAIL_MIN_RADIUS_RATIO
                    + fade * (1.0 - self.TRAIL_MIN_RADIUS_RATIO)
                )
                alpha = int(self.TRAIL_HEAD_ALPHA * fade)
                trail_color = QColor(self.PULSE_COLOR)
                trail_color.setAlpha(alpha)
                painter.setBrush(trail_color)
                painter.drawEllipse(trail_point, radius, radius)

        # Soft glow halo.
        # At rest (flight done), the alpha is animated by the breath
        # animation. During flight, hold it at HALO_FLIGHT_ALPHA so it
        # doesn't compete with the comet trail.
        at_rest = self._flight_progress >= 0.999
        halo_color = QColor(self.PULSE_COLOR)
        halo_color.setAlpha(self._halo_alpha if at_rest else self.HALO_FLIGHT_ALPHA)
        painter.setBrush(halo_color)
        halo_radius = self._pulse_radius * 1.8
        painter.drawEllipse(current, halo_radius, halo_radius)

        # Solid blue dot.
        painter.setBrush(self.PULSE_COLOR)
        painter.drawEllipse(current, self._pulse_radius, self._pulse_radius)

        # Label bubble shown only at rest (flight done).
        if self._current_label and self._flight_progress >= 0.999:
            label_font = QFont("Segoe UI", 11, QFont.Weight.DemiBold)
            painter.setFont(label_font)
            metrics = QFontMetrics(label_font)
            text_width = metrics.horizontalAdvance(self._current_label) + 16
            text_height = metrics.height() + 8
            bubble_x = current.x() + int(self._pulse_radius) + 12
            bubble_y = current.y() - text_height // 2
            bubble_rect = QRect(bubble_x, bubble_y, text_width, text_height)

            painter.setBrush(QColor(20, 30, 48, 220))
            painter.drawRoundedRect(bubble_rect, 8, 8)
            painter.setPen(QColor(255, 255, 255))
            painter.drawText(
                bubble_rect, Qt.AlignmentFlag.AlignCenter, self._current_label,
            )
