"""PySide6 ground-station GUI that displays live video, telemetry, and perception data from the drone.

Keyboard input is translated to command messages published over iceoryx2, while a background
QThread polls image, telemetry, perception, and action service topics and emits Qt signals to
update the UI.
"""

import logging
import math
import os
import signal
import sys
import time
import tomllib
from collections import deque
from pathlib import Path

import click
import iceoryx2
import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QBrush, QColor, QFont, QImage, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from client.common.blackboards import CONFIG
from client.common.constants import (
    IMAGE_HEIGHT,
    IMAGE_SCALING_FACTOR,
    IMAGE_SIZE,
    IMAGE_WIDTH,
    MAX_ALTITUDE,
    MIN_ALTITUDE,
    AppStatus,
    EventId,
    FlightState,
    KeyCode,
    ServiceName,
)
from client.common.node import Node
from client.common.payloads import ActionData, CommandData, ImageData, PerceptionData, TelemetryData
from client.common.utils import setup_logging

ROOT_DIR = Path(__file__).resolve().parents[2]
NODE_NAME = "gui_node"

logger = setup_logging(NODE_NAME, level=logging.DEBUG)

_SHORTCUTS = {
    "Space": "Take off / Land",
    "Esc": "Emergency stop",
    "T": "Toggle tracking mode",
    "C": "Stabilise (stop movement, hold altitude)",
    "M": "Motor test — spin motors briefly (ground only)",
    "↑ / ↓": "Forward / Backward",
    "← / →": "Strafe left / right",
    "Shift+↑↓←→": "Fast forward / backward / strafe",
    "A / D": "Yaw right / left",
    "Shift+A / D": "Fast yaw",
    "W / S": "Altitude up / down",
    "Shift+W / S": "Larger altitude step",
    "Ctrl+Q": "Exit",
    "Ctrl+P": "Process images",
    "Ctrl+S": "Save images",
    "Ctrl+G": "Gesture flight enabled (disable for gesture accuracy experiment)",
    "Ctrl+/": "Keyboard shortcuts",
}

APP_STATUS_TEXT = {
    AppStatus.CONNECTED: "Connected",
    AppStatus.DISCONNECTED: "Disconnected",
    AppStatus.SIMULATING: "Simulating",
    AppStatus.RECONNECTING: "Reconnecting",
}

# FPS colour thresholds
_FPS_GREEN = 7
_FPS_ORANGE = 4

# Battery voltage levels (V)
_VBAT_MAX = 4.2  # fully charged
_VBAT_NOMINAL = 3.7  # nominal / colour gradient inflection
_VBAT_CRIT = 2.9  # enter red critical warning (clears at _VBAT_CRIT + _VBAT_HYSTERESIS)
_VBAT_HYSTERESIS = 0.1


def _battery_color(vbat: float) -> str:
    """Return CSS hex colour: red at _VBAT_CRIT → orange at _VBAT_NOMINAL → green at _VBAT_MAX."""
    if vbat >= _VBAT_NOMINAL:
        t = min(1.0, (vbat - _VBAT_NOMINAL) / (_VBAT_MAX - _VBAT_NOMINAL))
        r, g, b = (
            round(251 + (74 - 251) * t),
            round(146 + (222 - 146) * t),
            round(60 + (128 - 60) * t),
        )
    else:
        t = max(0.0, (vbat - _VBAT_CRIT) / (_VBAT_NOMINAL - _VBAT_CRIT))
        r, g, b = (
            round(239 + (251 - 239) * t),
            round(68 + (146 - 68) * t),
            round(68 + (60 - 68) * t),
        )
    return f"#{r:02x}{g:02x}{b:02x}"


# Maps Qt key → (KeyCode, uses_shift). Second element False = shift never forwarded.
_KEY_MAP: dict = {
    Qt.Key.Key_Space: (KeyCode.SPACE, False),
    Qt.Key.Key_Escape: (KeyCode.ESC, False),
    Qt.Key.Key_Up: (KeyCode.UP, True),
    Qt.Key.Key_Down: (KeyCode.DOWN, True),
    Qt.Key.Key_Left: (KeyCode.LEFT, True),
    Qt.Key.Key_Right: (KeyCode.RIGHT, True),
    Qt.Key.Key_W: (KeyCode.W, True),
    Qt.Key.Key_A: (KeyCode.A, True),
    Qt.Key.Key_S: (KeyCode.S, True),
    Qt.Key.Key_D: (KeyCode.D, True),
    Qt.Key.Key_T: (KeyCode.T, False),
    Qt.Key.Key_C: (KeyCode.C, True),
    Qt.Key.Key_M: (KeyCode.M, False),
}


class GuiNode(Node, QThread):
    """Background QThread that polls iceoryx2 service topics and forwards data to the GUI via Qt signals."""

    status_changed = Signal(str)
    image_received = Signal(object)
    telemetry_updated = Signal(dict)
    perception_updated = Signal(bool, str, float)  # hand_detected, gesture, confidence
    flight_state_updated = Signal(str)  # FlightState name
    tracking_distance_updated = Signal(float)  # estimated distance m (0.0 = not tracking)

    def __init__(self, parent=None):
        QThread.__init__(self, parent)
        Node.__init__(self, NODE_NAME, level=logging.DEBUG, handle_signals=False)

    def _drain_action(self) -> None:
        """Consume all pending action samples and emit the latest flight state and estimated tracking distance."""
        if self.action_port is None or self.action_port.subscriber is None:
            return
        latest_state = None
        latest_dist = None
        while True:
            sample = self.action_port.subscriber.receive()
            if sample is None:
                break
            data = sample.payload()
            latest_state = int(data.contents.state)
            latest_dist = float(data.contents.estimated_distance)
            del data, sample
        if latest_state is not None:
            try:
                state_name = FlightState(latest_state).name
            except ValueError:
                state_name = str(latest_state)
            self.flight_state_updated.emit(state_name)
            self.tracking_distance_updated.emit(latest_dist)

    def _drain_images(self, process_images: bool) -> None:
        """Consume all pending raw image samples and emit the latest frame's pixel array.

        When process_images is True the frame is suppressed here because the processed
        frame will arrive via _drain_perception instead.
        """
        latest = None
        while True:
            s = self.image_port.subscriber.receive()
            if s is None:
                break
            if latest is not None:
                del latest
            latest = s
        if latest is not None and not process_images:
            data = latest.payload()
            pixels = np.ctypeslib.as_array(data.contents.pixels).copy()
            del data, latest
            self.image_received.emit(pixels)
        elif latest is not None:
            del latest

    def _drain_perception(self, process_images: bool) -> None:
        """Consume all pending perception samples, emit hand/gesture state, and optionally emit the processed frame.

        When process_images is True the annotated RGB pixels from vision_node are forwarded
        to the video display instead of the raw grayscale frame.
        """
        if self.perception_port is None or self.perception_port.subscriber is None:
            return
        last_hand_detected = None
        last_gesture = "NONE"
        last_confidence = 0.0
        latest_pixels = None
        while True:
            sample = self.perception_port.subscriber.receive()
            if sample is None:
                break
            data = sample.payload()
            last_hand_detected = bool(data.contents.hand_detected)
            last_gesture = data.contents.gesture_name.rstrip(b"\x00").decode("utf-8")
            last_confidence = float(data.contents.gesture_confidence)
            if process_images:
                latest_pixels = np.ctypeslib.as_array(data.contents.processed_pixels).copy()
            del data, sample
        if latest_pixels is not None:
            self.image_received.emit(latest_pixels)
        if last_hand_detected is not None:
            self.perception_updated.emit(last_hand_detected, last_gesture, last_confidence)

    def _drain_telemetry(self) -> None:
        """Consume all pending telemetry samples and emit status text and telemetry dict for each one."""
        while True:
            sample = self.telemetry_port.subscriber.receive()
            if sample is None:
                break
            data = sample.payload()
            c = data.contents
            try:
                status_val = AppStatus(c.status)
            except ValueError:
                status_val = AppStatus.DISCONNECTED
            fps = c.fps
            tele_data = {
                "status": status_val,
                "fps": fps,
                "x": c.x,
                "y": c.y,
                "z": c.z,
                "vx": c.vx,
                "vy": c.vy,
                "vz": c.vz,
                "roll": c.roll,
                "pitch": c.pitch,
                "yaw": c.yaw,
                "m1": c.m1,
                "m2": c.m2,
                "m3": c.m3,
                "m4": c.m4,
                "vbat": c.vbat,
            }
            del c, data, sample
            status_text = APP_STATUS_TEXT.get(status_val, "Unknown State")
            if fps > 0:
                status_text = f"{status_text} — {fps:.1f} fps"
            self.status_changed.emit(status_text)
            self.telemetry_updated.emit(tele_data)

    def run(self):
        self.logger.info(f"{NODE_NAME} running")
        self.status_changed.emit("Initializing...")

        self.image_port = self.create_subscriber(
            ServiceName.IMAGE,
            ImageData,
            EventId.IMAGE_READY,
            check_interruption=self.isInterruptionRequested,
        )
        self.telemetry_port = self.create_subscriber(
            ServiceName.TELEMETRY,
            TelemetryData,
            EventId.TELEMETRY_READY,
            check_interruption=self.isInterruptionRequested,
        )
        self.perception_port = self.create_subscriber(
            ServiceName.PERCEPTION,
            PerceptionData,
            EventId.PERCEPTION_READY,
            check_interruption=self.isInterruptionRequested,
        )
        self.action_port = self.create_subscriber(
            ServiceName.ACTION,
            ActionData,
            EventId.ACTION_READY,
            check_interruption=self.isInterruptionRequested,
        )

        if self.image_port.subscriber is None or self.telemetry_port.subscriber is None:
            self.status_changed.emit(APP_STATUS_TEXT[AppStatus.DISCONNECTED])
            return

        local_blackboard_reader = self.create_blackboard_reader(
            "/config", CONFIG, check_interruption=self.isInterruptionRequested
        )

        # Status remains WAITING until first telemetry payload arrives

        # TODO: Replace sleep-based polling with WaitSet once the
        # iceoryx2 spinning bug is fixed (see GitHub issue in thesis/Iceoryx2.md).
        # Intended WaitSet code (3 attachments — image, telemetry, perception):
        #
        #   waitset = iceoryx2.WaitSetBuilder.new().create(iceoryx2.ServiceType.Ipc)
        #   image_guard = waitset.attach_notification(self.image_port.listener)
        #   telemetry_guard = waitset.attach_notification(self.telemetry_port.listener)
        #   if self.perception_port is not None ...:
        #       perception_guard = waitset.attach_notification(self.perception_port.listener)
        #   while ...:
        #       ids, result = waitset.wait_and_process_with_timeout(Duration.from_millis(10))
        #       for event_id in ids:
        #           if event_id.has_event_from(image_guard):
        #               sample = image_port.subscriber.receive(); ...emit raw pixels
        #           elif event_id.has_event_from(telemetry_guard):
        #               sample = telemetry_port.subscriber.receive(); ...emit status
        #           elif event_id.has_event_from(perception_guard):
        #               sample = perception_port.subscriber.receive(); ...emit processed pixels
        #   finally: [all guards].delete(); waitset.delete()

        try:
            while not self.isInterruptionRequested() and self.running:
                # Sleep-based polling — avoids WaitSet spinning bug AND any
                # potential issues with timed_wait_one blocking Qt rendering.
                # Display node: 10ms polling (~100 Hz) is sufficient.
                time.sleep(0.010)
                # Yield GIL so Qt main thread can process key/mouse events.
                time.sleep(0)

                process_images = local_blackboard_reader is not None and self.blackboard_read(
                    local_blackboard_reader, "process_images"
                )

                self._drain_images(process_images)
                self._drain_perception(process_images)
                self._drain_telemetry()
                self._drain_action()

        except KeyboardInterrupt:
            pass
        except (iceoryx2.NodeWaitFailure, iceoryx2.ListenerWaitError) as e:
            self.logger.warning(f"iceoryx2 wait interrupted: {e}")
        except Exception as e:
            self.logger.error(f"{NODE_NAME} run error: {e}", exc_info=True)
        finally:
            self.stop()
            self.logger.info(f"{NODE_NAME} shut down")
            self.status_changed.emit(APP_STATUS_TEXT[AppStatus.DISCONNECTED])


class AttitudeIndicator(QWidget):
    """Artificial horizon widget: roll shown as horizon tilt, pitch as vertical offset.

    Outer ring changes colour with roll severity: green < 20°, orange 20–40°, red > 40°.
    Roll arc ticks are labelled at ±30° and ±60°.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(165, 120)
        self._roll = 0.0
        self._pitch = 0.0

    def update_attitude(self, roll: float, pitch: float) -> None:
        if self._roll != roll or self._pitch != pitch:
            self._roll = roll
            self._pitch = pitch
            self.update()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)

        w, h = self.width(), self.height()
        cx, cy = w / 2.0, h / 2.0
        r = min(cx, cy) - 5  # leave room for ring

        # ── Severity ring (no clip) ──────────────────────────────────
        roll_abs = abs(self._roll)
        if roll_abs < 20:
            ring_col = QColor(74, 222, 128)  # green
        elif roll_abs < 40:
            ring_col = QColor(251, 146, 60)  # orange
        else:
            ring_col = QColor(239, 68, 68)  # red
        p.setPen(QPen(ring_col, 3))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPointF(cx, cy), r + 3, r + 3)

        # ── Sky / ground (with clip, rotated) ───────────────────────
        clip = QPainterPath()
        clip.addEllipse(QPointF(cx, cy), r, r)
        p.setClipPath(clip)

        p.translate(cx, cy)
        p.rotate(-self._roll)
        pitch_px = self._pitch * r / 30.0  # ±30° range

        p.fillRect(QRectF(-r - 1, -r - 1, (r + 1) * 2, r + 1 + pitch_px), QColor(28, 90, 170))
        p.fillRect(QRectF(-r - 1, pitch_px, (r + 1) * 2, r + 1), QColor(110, 70, 30))

        # Horizon line
        p.setPen(QPen(QColor(255, 255, 255), 1))
        p.drawLine(QPointF(-r, pitch_px), QPointF(r, pitch_px))

        # Pitch ticks (±10°, ±20°)
        p.setPen(QPen(QColor(255, 255, 255, 160), 1))
        for deg in (-20, -10, 10, 20):
            y = pitch_px - deg * r / 30.0
            hw = 18 if abs(deg) == 20 else 12
            p.drawLine(QPointF(-hw, y), QPointF(hw, y))

        # ── Overlay (unrotated) ──────────────────────────────────────
        p.resetTransform()
        p.setClipping(False)
        p.translate(cx, cy)

        arc_r = r - 1
        tick_font = QFont("Monospace", 5)
        p.setFont(tick_font)

        for tick in (-60, -45, -30, -20, -10, 0, 10, 20, 30, 45, 60):
            angle_rad = math.radians(-tick - 90)
            cos_a, sin_a = math.cos(angle_rad), math.sin(angle_rad)
            x1, y1 = arc_r * cos_a, arc_r * sin_a
            tl = 8 if tick % 30 == 0 else (5 if tick % 10 == 0 else 4)
            x2, y2 = (arc_r - tl) * cos_a, (arc_r - tl) * sin_a
            p.setPen(QPen(QColor(255, 255, 255, 140), 1))
            p.drawLine(QPointF(x1, y1), QPointF(x2, y2))
            # Label at ±30° and ±60°
            if abs(tick) in (30, 60):
                lx = (arc_r - tl - 10) * cos_a
                ly = (arc_r - tl - 10) * sin_a
                p.setPen(QColor(200, 200, 200, 200))
                p.drawText(QRectF(lx - 8, ly - 5, 16, 10), Qt.AlignmentFlag.AlignCenter, str(abs(tick)))

        # Roll pointer (yellow triangle, rotates with roll)
        p.save()
        p.rotate(-self._roll)
        tri_y = -(arc_r - 1)
        tri = QPainterPath()
        tri.moveTo(0, tri_y)
        tri.lineTo(-4, tri_y + 9)
        tri.lineTo(4, tri_y + 9)
        tri.closeSubpath()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(255, 220, 0)))
        p.drawPath(tri)
        p.restore()

        # Fixed aircraft crosshair
        p.setPen(QPen(QColor(255, 220, 0), 2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawLine(QPointF(-28, 0), QPointF(-8, 0))
        p.drawLine(QPointF(8, 0), QPointF(28, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(255, 220, 0)))
        p.drawEllipse(QPointF(0, 0), 3, 3)

        p.end()


class PositionTrace(QWidget):
    """Top-down XY position history display."""

    _MAX_POINTS = 500

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(195, 195)
        self._points: deque[tuple[float, float]] = deque(maxlen=self._MAX_POINTS)
        self._yaw: float = 0.0

    def add_point(self, x: float, y: float) -> None:
        self._points.append((x, y))
        self.update()

    def update_yaw(self, yaw: float) -> None:
        self._yaw = yaw
        self.update()

    def clear(self) -> None:
        self._points.clear()
        self.update()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        w, h = self.width(), self.height()
        margin = 12

        p.fillRect(0, 0, w, h, QColor(18, 18, 18))

        p.setPen(QPen(QColor(45, 45, 45), 1))
        p.drawLine(w // 2, 0, w // 2, h)
        p.drawLine(0, h // 2, w, h // 2)

        if not self._points:
            p.setPen(QColor(80, 80, 80))
            p.setFont(QFont("Monospace", 7))
            p.drawText(QRectF(0, 0, w, h), Qt.AlignmentFlag.AlignCenter, "No position data")
            p.end()
            return

        xs = [pt[0] for pt in self._points]
        ys = [pt[1] for pt in self._points]
        span = max(2.0, max(abs(v) for v in xs + ys)) * 1.15
        scale = (min(w, h) / 2.0 - margin) / span

        def to_screen(x: float, y: float) -> QPointF:
            return QPointF(w / 2.0 + x * scale, h / 2.0 - y * scale)

        pts = [to_screen(x, y) for x, y in self._points]
        p.setPen(QPen(QColor(90, 140, 230, 180), 1))
        for i in range(1, len(pts)):
            p.drawLine(pts[i - 1], pts[i])

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(240, 80, 80)))
        p.drawEllipse(pts[-1], 4, 4)

        # Yaw arrow at current position (yaw=0 → north/up in map frame)
        arrow_len = 12.0
        yaw_rad = math.radians(-self._yaw)  # negate: CW yaw → screen coords
        ax = pts[-1].x() + arrow_len * math.sin(yaw_rad)
        ay = pts[-1].y() - arrow_len * math.cos(yaw_rad)
        p.setPen(QPen(QColor(255, 220, 0), 2))
        p.drawLine(pts[-1], QPointF(ax, ay))
        # Arrowhead
        tip = QPointF(ax, ay)
        side_len = 5.0
        left_rad = yaw_rad - 2.5
        right_rad = yaw_rad + 2.5
        head = QPainterPath()
        head.moveTo(tip)
        head.lineTo(tip.x() - side_len * math.sin(left_rad), tip.y() + side_len * math.cos(left_rad))
        head.lineTo(tip.x() - side_len * math.sin(right_rad), tip.y() + side_len * math.cos(right_rad))
        head.closeSubpath()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(255, 220, 0)))
        p.drawPath(head)

        origin = to_screen(0.0, 0.0)
        p.setPen(QPen(QColor(80, 80, 80), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(origin, 3, 3)

        p.setPen(QColor(70, 70, 70))
        p.setFont(QFont("Monospace", 7))
        p.drawText(QRectF(2, h - 14, w - 4, 12), Qt.AlignmentFlag.AlignLeft, f"±{span:.1f} m")

        p.end()


class AltitudeBar(QWidget):
    """Thin vertical bar showing current altitude vs min/max limits."""

    _WIDTH = 16

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(self._WIDTH)
        self.setMinimumHeight(60)
        self._z = 0.0
        self._z_min = MIN_ALTITUDE
        self._z_max = MAX_ALTITUDE
        self._live = False

    def update_altitude(self, z: float, z_min: float, z_max: float, live: bool) -> None:
        if self._z != z or self._z_min != z_min or self._z_max != z_max or self._live != live:
            self._z = z
            self._z_min = z_min
            self._z_max = z_max
            self._live = live
            self.update()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        w, h = self.width(), self.height()
        margin = 4

        p.fillRect(0, 0, w, h, QColor(18, 18, 18))

        inner_h = h - 2 * margin
        if inner_h <= 0:
            p.end()
            return

        span = max(0.01, self._z_max - self._z_min)

        def to_y(z: float) -> float:
            frac = max(0.0, min(1.0, (z - self._z_min) / span))
            return margin + inner_h * (1.0 - frac)

        # Fill from bottom to current Z
        if self._live and self._z > self._z_min:
            fill_top = to_y(self._z)
            fill_bot = to_y(self._z_min)
            frac = (self._z - self._z_min) / span
            if frac < 0.4:
                fill_color = QColor(74, 222, 128)
            elif frac < 0.8:
                fill_color = QColor(251, 146, 60)
            else:
                fill_color = QColor(239, 68, 68)
            p.fillRect(2, int(fill_top), w - 4, int(fill_bot - fill_top), fill_color)

        # Border
        p.setPen(QPen(QColor(80, 80, 80), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(1, margin, w - 2, inner_h)

        # Min/max tick lines
        p.setPen(QPen(QColor(100, 100, 100), 1))
        y_max = int(to_y(self._z_max))
        y_min = int(to_y(self._z_min))
        p.drawLine(0, y_max, w, y_max)
        p.drawLine(0, y_min, w, y_min)

        # Current level line
        if self._live:
            p.setPen(QPen(QColor(255, 255, 255, 180), 1))
            y_cur = int(to_y(self._z))
            p.drawLine(0, y_cur, w, y_cur)

        p.end()


class ShortcutsDialog(QDialog):
    """Read-only dialog that displays the keyboard shortcut reference table."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Keyboard Shortcuts")
        self.resize(340, 420)

        table = QTableWidget(len(_SHORTCUTS), 2, self)
        table.setHorizontalHeaderLabels(["Shortcut", "Action"])
        table.horizontalHeader().setStretchLastSection(True)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)

        for row, (key, action) in enumerate(_SHORTCUTS.items()):
            table.setItem(row, 0, QTableWidgetItem(key))
            table.setItem(row, 1, QTableWidgetItem(action))

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(table)
        layout.addWidget(buttons)


class SettingsDialog(QDialog):
    """Modal dialog for editing runtime flight, gesture, and tracking parameters via the shared blackboard."""

    def __init__(self, writer, reader, display_state: dict | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(320)
        self._writer = writer
        self._reader = reader
        self._display_state = display_state or {}

        layout = QVBoxLayout(self)
        layout.addWidget(self._flight_group())
        layout.addWidget(self._gesture_group())
        layout.addWidget(self._tracking_group())
        layout.addWidget(self._display_group())

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _float_spin(self, key, min_val, max_val, step, decimals, suffix="", tooltip=""):
        """Create a QDoubleSpinBox pre-loaded from the blackboard key that writes back on every value change."""
        sb = QDoubleSpinBox()
        sb.setRange(min_val, max_val)
        sb.setSingleStep(step)
        sb.setDecimals(decimals)
        if suffix:
            sb.setSuffix(f" {suffix}")
        if tooltip:
            sb.setToolTip(tooltip)
        sb.setValue(Node.blackboard_read(self._reader, key))
        sb.valueChanged.connect(lambda v: Node.blackboard_write(self._writer, key, v))
        return sb

    def _int_spin(self, key, min_val, max_val, step, suffix="", tooltip=""):
        """Create a QSpinBox pre-loaded from the blackboard key that writes back on every value change."""
        sb = QSpinBox()
        sb.setRange(min_val, max_val)
        sb.setSingleStep(step)
        if suffix:
            sb.setSuffix(f" {suffix}")
        if tooltip:
            sb.setToolTip(tooltip)
        sb.setValue(Node.blackboard_read(self._reader, key))
        sb.valueChanged.connect(lambda v: Node.blackboard_write(self._writer, key, v))
        return sb

    def _flight_group(self):
        group = QGroupBox("Flight Control")
        form = QFormLayout(group)
        form.addRow(
            "Speed:", self._float_spin("speed_factor", 0.01, 2.0, 0.05, 2, "m/s", "Normal movement speed (↑↓←→ keys)")
        )
        form.addRow(
            "Fast speed:",
            self._float_spin("fast_speed_factor", 0.01, 2.0, 0.05, 2, "m/s", "Movement speed when holding Shift"),
        )
        form.addRow("Yaw rate:", self._float_spin("yaw_rate", 1.0, 360.0, 5.0, 1, "°/s", "Rotation speed (A/D keys)"))
        form.addRow(
            "Fast yaw:",
            self._float_spin("yaw_rate_fast", 1.0, 360.0, 5.0, 1, "°/s", "Rotation speed when holding Shift"),
        )
        form.addRow(
            "Max altitude:",
            self._float_spin("max_altitude", 0.1, 5.0, 0.1, 1, "m", "Altitude ceiling — drone refuses to go higher"),
        )
        form.addRow(
            "Min altitude:",
            self._float_spin("min_altitude", 0.01, 1.0, 0.01, 2, "m", "Altitude floor — drone refuses to go lower"),
        )
        return group

    def _gesture_group(self):
        group = QGroupBox("Gesture")
        form = QFormLayout(group)
        form.addRow(
            "Threshold:",
            self._float_spin(
                "gesture_threshold",
                0.0,
                1.0,
                0.05,
                2,
                "",
                "Minimum MediaPipe confidence to accept a gesture (0–1). Lower = more sensitive, more false positives",
            ),
        )
        form.addRow(
            "Debounce:",
            self._int_spin(
                "gesture_debounce_ms",
                0,
                2000,
                50,
                "ms",
                "How long a gesture must be held continuously before it is confirmed",
            ),
        )
        form.addRow(
            "Hysteresis:",
            self._int_spin(
                "gesture_hysteresis_ms",
                0,
                2000,
                50,
                "ms",
                "Cooldown after a gesture fires — prevents rapidly switching between gestures",
            ),
        )
        cb = QCheckBox()
        cb.setToolTip("Adaptive histogram equalisation — improves hand detection in low or uneven light")
        cb.setChecked(Node.blackboard_read(self._reader, "clahe_enabled"))
        cb.toggled.connect(lambda v: Node.blackboard_write(self._writer, "clahe_enabled", v))
        form.addRow("CLAHE:", cb)
        return group

    def _tracking_group(self):
        group = QGroupBox("Tracking")
        form = QFormLayout(group)
        form.addRow(
            "Max speed:",
            self._float_spin(
                "tracking_max_speed",
                0.05,
                0.5,
                0.01,
                2,
                "m/s",
                "Maximum lateral and forward speed during tracking. Caps the initial burst when the hand "
                "appears far from centre. Lower = smoother but slower to acquire. Independent of manual flight speed.",
            ),
        )
        form.addRow(
            "Speed scale:",
            self._float_spin(
                "tracking_speed_scale",
                0.0001,
                0.05,
                0.0001,
                4,
                "m/s/px",
                "Lateral velocity per pixel of hand offset from frame centre. Higher = more aggressive tracking",
            ),
        )
        form.addRow(
            "Alt scale:",
            self._float_spin(
                "tracking_alt_scale",
                0.0001,
                0.01,
                0.0001,
                4,
                "m/px",
                "Altitude change per pixel of vertical hand offset. Higher = more sensitive altitude tracking",
            ),
        )
        form.addRow(
            "Target distance:",
            self._float_spin(
                "tracking_distance",
                0.0,
                5.0,
                0.1,
                1,
                "m",
                "Target distance from hand (forward/back). 0 = disabled — drone stays put in x axis",
            ),
        )
        form.addRow(
            "Distance gain:",
            self._float_spin(
                "tracking_distance_scale",
                0.01,
                2.0,
                0.05,
                2,
                "m/s/m",
                "Forward speed per metre of distance error. Higher = faster approach/retreat",
            ),
        )
        form.addRow(
            "Span at 1 m:",
            self._float_spin(
                "tracking_hand_span_at_1m",
                0.05,
                0.6,
                0.01,
                2,
                "norm",
                "Calibration: normalised wrist-to-fingertip span when hand is 1 m from camera. "
                "Measure by holding hand flat at 1 m and reading vision_node logs",
            ),
        )
        return group

    def _display_group(self):
        group = QGroupBox("Display")
        form = QFormLayout(group)
        _labels = {
            "show_attitude": "Attitude indicator",
            "show_position_trace": "Position trace",
        }
        for key, label in _labels.items():
            if key not in self._display_state:
                continue
            getter, setter = self._display_state[key]
            cb = QCheckBox()
            cb.setChecked(getter())
            cb.toggled.connect(setter)
            form.addRow(f"{label}:", cb)
        return group


class MainWindow(QMainWindow):
    """Main application window containing the live video panel, telemetry sidebar, menus, and keyboard handling."""

    PANEL_WIDTH = 215
    WINDOW_WIDTH = IMAGE_WIDTH * IMAGE_SCALING_FACTOR + PANEL_WIDTH
    WINDOW_HEIGHT = IMAGE_HEIGHT * IMAGE_SCALING_FACTOR + 100  # add menu + toolbar + status bar

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Crazyflie GUI")

        with open(ROOT_DIR / "pyproject.toml", "rb") as f:
            self.project_config = tomllib.load(f)

        self.resize(MainWindow.WINDOW_WIDTH, MainWindow.WINDOW_HEIGHT)

        # Command Node (Main Thread): Publishes commands instantly on UI events
        self.command_node = Node("gui_command", level=logging.DEBUG, handle_signals=False)

        # Receiver Thread (Background): Blocks while waiting for high-frequency images
        self.image_receiver = GuiNode()

        # Writer first, then reader
        self.blackboard_writer = self.image_receiver.create_blackboard_writer("/config", CONFIG)
        self.blackboard_reader = self.image_receiver.create_blackboard_reader("/config", CONFIG)

        # Command publisher setup
        self.command_port = self.command_node.create_publisher(ServiceName.COMMAND, CommandData, EventId.COMMAND_READY)

        self.image_receiver.status_changed.connect(self._on_status_changed)
        self.image_receiver.image_received.connect(self.update_image)
        self.image_receiver.telemetry_updated.connect(self._on_telemetry_updated)
        self.image_receiver.perception_updated.connect(self._on_perception_updated)
        self.image_receiver.flight_state_updated.connect(self._on_flight_state_updated)
        self.image_receiver.tracking_distance_updated.connect(self._on_tracking_distance_updated)
        self.image_receiver.start()

        self.statusBar().showMessage("Initializing...")
        self._shortcuts_dialog = ShortcutsDialog(self)

        self._show_attitude = True
        self._show_position_trace = True
        self._vbat_warn_state = "none"  # "none" | "warn" | "crit"
        self._flight_state_name = "IDLE"
        self._app_connected = False
        self._reconnect_dots = 0
        self._reconnect_timer = QTimer()
        self._reconnect_timer.setInterval(600)
        self._reconnect_timer.timeout.connect(self._tick_reconnect)

        # Menu bar setup
        self._setup_menus()

        # UI Layout setup (creates self._sim_badge, self._altitude_bar, etc.)
        self._setup_ui()
        self._setup_toolbar()

    def _setup_menus(self):
        menu_bar = self.menuBar()
        file_menu = menu_bar.addMenu("File")
        exit_action = QAction("Exit", self)
        exit_action.setShortcut("Ctrl+Q")
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        settings_menu = menu_bar.addMenu("Settings")
        settings_menu.setToolTipsVisible(True)

        process_images_enabled = self.image_receiver.blackboard_read(self.blackboard_reader, "process_images")
        self.process_images_action = QAction("Process images", self)
        self.process_images_action.setCheckable(True)
        self.process_images_action.setShortcut("Ctrl+P")
        self.process_images_action.setChecked(process_images_enabled)
        self.process_images_action.setToolTip(
            "Run MediaPipe gesture recognition on incoming frames.\nRequired for gesture commands and tracking mode."
        )
        self.process_images_action.toggled.connect(
            lambda checked: self._on_toggle_blackboard("process_images", checked)
        )
        settings_menu.addAction(self.process_images_action)

        save_images_enabled = self.image_receiver.blackboard_read(self.blackboard_reader, "save_images")
        self.save_images_action = QAction("Save images", self)
        self.save_images_action.setCheckable(True)
        self.save_images_action.setShortcut("Ctrl+S")
        self.save_images_action.setChecked(save_images_enabled)
        self.save_images_action.setToolTip("Save every incoming camera frame to disk for logging and replay.")
        self.save_images_action.toggled.connect(lambda checked: self._on_toggle_blackboard("save_images", checked))
        settings_menu.addAction(self.save_images_action)

        gesture_flight_enabled = self.image_receiver.blackboard_read(self.blackboard_reader, "gesture_flight_enabled")
        self.gesture_flight_action = QAction("Gesture flight enabled", self)
        self.gesture_flight_action.setCheckable(True)
        self.gesture_flight_action.setShortcut("Ctrl+G")
        self.gesture_flight_action.setChecked(gesture_flight_enabled)
        self.gesture_flight_action.setToolTip(
            "Allow gesture commands (Thumb_Up/Down/Victory) to trigger flight state changes.\n"
            "Disable during gesture accuracy experiment to prevent unintended takeoffs."
        )
        self.gesture_flight_action.toggled.connect(
            lambda checked: self._on_toggle_blackboard("gesture_flight_enabled", checked)
        )
        settings_menu.addAction(self.gesture_flight_action)

        settings_menu.addSeparator()
        settings_action = QAction("Settings...", self)
        settings_action.triggered.connect(self._show_settings)
        settings_menu.addAction(settings_action)

        help_menu = menu_bar.addMenu("Help")
        shortcuts_action = QAction("Keyboard Shortcuts", self)
        shortcuts_action.setShortcut("Ctrl+/")
        shortcuts_action.triggered.connect(self._shortcuts_dialog.show)
        help_menu.addAction(shortcuts_action)
        help_menu.addSeparator()
        about_action = QAction("About", self)
        about_action.triggered.connect(self._show_about)
        help_menu.addAction(about_action)

    def _setup_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QHBoxLayout(central_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Video panel — QGridLayout so sim badge can overlay top-right corner
        video_panel = QFrame()
        video_grid = QGridLayout(video_panel)
        video_grid.setContentsMargins(0, 0, 0, 0)
        video_grid.setSpacing(0)

        self.video_label = QLabel("📷  Waiting for video stream...")
        self.video_label.setAlignment(Qt.AlignCenter)
        self.video_label.setStyleSheet("color: #666; font-size: 13px;")
        self.video_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        video_grid.addWidget(self.video_label, 0, 0)

        main_layout.addWidget(video_panel, 1)
        main_layout.addWidget(self._setup_sidebar())

        hints = QLabel("Space: Take off/Land  Esc: Emergency  ↑↓←→: Move  A/D: Yaw  W/S: Alt  T: Tracking")
        hints.setStyleSheet("color: #777; font-size: 9px; padding-right: 6px;")
        self.statusBar().addPermanentWidget(hints)

    def _setup_toolbar(self) -> None:
        tb = QToolBar("Controls")
        tb.setMovable(False)
        tb.setFloatable(False)
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.addToolBar(tb)

        self._takeoff_land_action = QAction("▲  Take Off", self)
        self._takeoff_land_action.setToolTip("Take off / Land (Space)")
        self._takeoff_land_action.triggered.connect(self._on_toolbar_takeoff_land)
        tb.addAction(self._takeoff_land_action)

        self._stop_action = QAction("■  Stop", self)
        self._stop_action.setToolTip("Emergency stop (Esc)")
        self._stop_action.triggered.connect(lambda: self._publish_command(KeyCode.ESC, True))
        tb.addAction(self._stop_action)

        tb.addSeparator()

        self._stabilise_action = QAction("◻  Hold", self)
        self._stabilise_action.setToolTip("Stop all movement — hold current position and altitude (C)")
        self._stabilise_action.triggered.connect(
            lambda: (
                self._publish_command(KeyCode.C, True),
                self._publish_command(KeyCode.C, False),
            )
        )
        tb.addAction(self._stabilise_action)

        self._tracking_action = QAction("⊕  Track", self)
        self._tracking_action.setCheckable(True)
        self._tracking_action.setToolTip("Toggle hand-tracking mode (T)")
        self._tracking_action.triggered.connect(
            lambda: (
                self._publish_command(KeyCode.T, True),
                self._publish_command(KeyCode.T, False),
            )
        )
        tb.addAction(self._tracking_action)

        tb.addSeparator()

        self._proc_images_action = QAction("👁  Process", self)
        self._proc_images_action.setCheckable(True)
        self._proc_images_action.setChecked(
            self.image_receiver.blackboard_read(self.blackboard_reader, "process_images")
        )
        self._proc_images_action.setToolTip("Process images / gesture recognition (Ctrl+P)")
        self._proc_images_action.toggled.connect(lambda checked: self._on_toggle_blackboard("process_images", checked))
        tb.addAction(self._proc_images_action)

        self._save_images_tb_action = QAction("💾  Save", self)
        self._save_images_tb_action.setCheckable(True)
        self._save_images_tb_action.setChecked(
            self.image_receiver.blackboard_read(self.blackboard_reader, "save_images")
        )
        self._save_images_tb_action.setToolTip("Save camera frames to disk (Ctrl+S)")
        self._save_images_tb_action.toggled.connect(lambda checked: self._on_toggle_blackboard("save_images", checked))
        tb.addAction(self._save_images_tb_action)

        # Keep toolbar actions in sync with menu actions
        self.process_images_action.toggled.connect(self._proc_images_action.setChecked)
        self._proc_images_action.toggled.connect(self.process_images_action.setChecked)
        self.save_images_action.toggled.connect(self._save_images_tb_action.setChecked)
        self._save_images_tb_action.toggled.connect(self.save_images_action.setChecked)

    def _update_toolbar_state(self) -> None:
        state = self._flight_state_name
        connected = self._app_connected
        airborne = state in ("AIRBORNE", "TRACKING")
        can_takeoff = connected and state == "IDLE"
        can_land = connected and airborne
        self._takeoff_land_action.setEnabled(can_takeoff or can_land)
        self._stop_action.setEnabled(connected)
        self._stabilise_action.setEnabled(connected and airborne)
        self._tracking_action.setEnabled(connected and airborne)

    def _on_toolbar_takeoff_land(self) -> None:
        self._publish_command(KeyCode.SPACE, True)
        self._publish_command(KeyCode.SPACE, False)

    def _setup_sidebar(self) -> QFrame:
        tele_panel = QFrame()
        tele_panel.setObjectName("tele_panel")
        tele_panel.setFixedWidth(MainWindow.PANEL_WIDTH)
        tele_panel.setFrameShape(QFrame.Shape.NoFrame)
        tele_panel.setStyleSheet("#tele_panel { border-left: 1px solid #444; }")

        self._tele_labels: dict[str, QLabel] = {}
        val_font = QFont("Monospace", 10)
        val_font.setStyleHint(QFont.StyleHint.Monospace)

        def _section(layout: QVBoxLayout, header: str):
            h = QLabel(header)
            h.setFont(QFont("Outfit", 9, QFont.Weight.Bold))
            h.setStyleSheet("color: #999; padding-top: 5px; border-top: 1px solid #444;")
            layout.addWidget(h)

        def _row(layout: QVBoxLayout, label: str, key: str):
            rw = QWidget()
            rl = QHBoxLayout(rw)
            rl.setContentsMargins(4, 1, 4, 1)
            lbl = QLabel(label)
            lbl.setFont(val_font)
            lbl.setStyleSheet("color: #aaa;")
            val = QLabel("—")
            val.setFont(val_font)
            val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            rl.addWidget(lbl)
            rl.addStretch()
            rl.addWidget(val)
            layout.addWidget(rw)
            self._tele_labels[key] = val

        def _scrollable(widget: QWidget) -> QScrollArea:
            sa = QScrollArea()
            sa.setWidgetResizable(True)
            sa.setFrameShape(QFrame.Shape.NoFrame)
            sa.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            sa.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            sa.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            sa.setWidget(widget)
            return sa

        # ── Flight tab ───────────────────────────────────────────────
        flight_w = QWidget()
        fl = QVBoxLayout(flight_w)
        fl.setContentsMargins(10, 6, 10, 6)
        fl.setSpacing(2)

        _row(fl, "Status", "status")
        _row(fl, "FPS", "fps")
        _row(fl, "State", "state")

        self._battery_bar = QProgressBar()
        self._battery_bar.setRange(0, 100)
        self._battery_bar.setValue(0)
        self._battery_bar.setFixedHeight(6)
        self._battery_bar.setTextVisible(False)
        self._battery_bar.setContentsMargins(4, 2, 4, 2)
        self._battery_bar.setStyleSheet("""
            QProgressBar { border: 1px solid #666; border-radius: 2px; background: #1e1e1e; }
            QProgressBar::chunk { background: #4ade80; border-radius: 1px; }
        """)
        _row(fl, "VBat", "vbat")
        fl.addWidget(self._battery_bar)

        _section(fl, "POSITION (m)")
        _row(fl, "X", "x")
        _row(fl, "Y", "y")
        _row(fl, "Z", "z")

        _section(fl, "VELOCITY (m/s)")
        _row(fl, "Vx", "vx")
        _row(fl, "Vy", "vy")
        _row(fl, "Vz", "vz")

        _section(fl, "ATTITUDE (°)")
        _row(fl, "Roll", "roll")
        _row(fl, "Pitch", "pitch")
        _row(fl, "Yaw", "yaw")

        _section(fl, "MOTORS (%)")
        motor_w = QWidget()
        mg = QGridLayout(motor_w)
        mg.setContentsMargins(4, 1, 4, 1)
        mg.setVerticalSpacing(2)
        mg.setHorizontalSpacing(8)
        mg.setColumnStretch(2, 1)
        for i, key in enumerate(("m1", "m2", "m3", "m4")):
            r, c = divmod(i, 2)
            gc = c * 3
            mlbl = QLabel(f"M{i + 1}")
            mlbl.setFont(val_font)
            mlbl.setStyleSheet("color: #aaa;")
            mval = QLabel("—")
            mval.setFont(val_font)
            mval.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            mg.addWidget(mlbl, r, gc)
            mg.addWidget(mval, r, gc + 1)
            self._tele_labels[key] = mval
        fl.addWidget(motor_w)
        fl.addStretch()

        # ── Nav tab ──────────────────────────────────────────────────
        nav_w = QWidget()
        nl = QVBoxLayout(nav_w)
        nl.setContentsMargins(10, 6, 10, 6)
        nl.setSpacing(4)

        stop_btn = QPushButton("■  EMERGENCY STOP")
        stop_btn.setFixedHeight(36)
        stop_btn.setStyleSheet("""
            QPushButton {
                background: #dc2626; color: white; font-weight: bold;
                font-size: 12px; border-radius: 4px; border: none;
            }
            QPushButton:hover  { background: #ef4444; }
            QPushButton:pressed { background: #b91c1c; }
        """)
        stop_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        stop_btn.clicked.connect(lambda: self._publish_command(KeyCode.ESC, True))
        nl.addWidget(stop_btn)

        att_hdr = QLabel("ATTITUDE / ALTITUDE")
        att_hdr.setFont(QFont("Outfit", 9, QFont.Weight.Bold))
        att_hdr.setStyleSheet("color: #999; padding-top: 6px;")
        nl.addWidget(att_hdr)
        att_row = QWidget()
        att_hl = QHBoxLayout(att_row)
        att_hl.setContentsMargins(0, 0, 0, 0)
        att_hl.setSpacing(4)
        self.attitude_widget = AttitudeIndicator()
        self.attitude_widget.setVisible(self._show_attitude)
        att_hl.addWidget(self.attitude_widget)
        self._altitude_bar = AltitudeBar()
        att_hl.addWidget(self._altitude_bar)
        nl.addWidget(att_row)

        trace_hdr = QLabel("POSITION TRACE")
        trace_hdr.setFont(QFont("Outfit", 9, QFont.Weight.Bold))
        trace_hdr.setStyleSheet("color: #999; padding-top: 6px;")
        nl.addWidget(trace_hdr)
        self.position_trace = PositionTrace()
        self.position_trace.setVisible(self._show_position_trace)
        nl.addWidget(self.position_trace)
        nl.addStretch()

        # ── Vision tab ───────────────────────────────────────────────
        vision_w = QWidget()
        vl = QVBoxLayout(vision_w)
        vl.setContentsMargins(10, 6, 10, 6)
        vl.setSpacing(2)

        _row(vl, "Hand", "hand")
        _row(vl, "Gesture", "gesture")
        _row(vl, "Distance", "dist")
        vl.addStretch()

        # ── Assemble ─────────────────────────────────────────────────
        tabs = QTabWidget()
        tabs.setDocumentMode(True)
        tabs.tabBar().setFocusPolicy(Qt.FocusPolicy.NoFocus)
        tabs.tabBar().setExpanding(False)
        tabs.addTab(_scrollable(flight_w), "Flight")
        tabs.addTab(_scrollable(nav_w), "Nav")
        tabs.addTab(_scrollable(vision_w), "Vision")

        outer = QVBoxLayout(tele_panel)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(tabs)

        return tele_panel

    def _publish_command(self, key: KeyCode, is_pressed: bool, shift: bool = False):
        if self.command_port is None:
            logger.warning("Command port is None — command dropped")
            return
        try:
            sample = self.command_port.publisher.loan_uninit()
            p = sample.payload().contents
            p.key = key
            p.is_pressed = is_pressed
            p.shift = shift
            sample.assume_init().send()
            self.command_port.notifier.notify_with_custom_event_id(self.command_port.event)
        except Exception as e:
            logger.warning(f"Command publish failed: {e}")

    @Slot(str)
    def _on_status_changed(self, text: str):
        is_reconnecting = text.startswith(APP_STATUS_TEXT[AppStatus.RECONNECTING])
        self._app_connected = text.startswith(APP_STATUS_TEXT[AppStatus.CONNECTED]) or text.startswith(
            APP_STATUS_TEXT[AppStatus.SIMULATING]
        )

        if is_reconnecting:
            if not self._reconnect_timer.isActive():
                self._reconnect_dots = 0
                self._reconnect_timer.start()
        else:
            self._reconnect_timer.stop()
            self.statusBar().showMessage(text)

        self._update_toolbar_state()

        for _, name in APP_STATUS_TEXT.items():
            if text.startswith(name):
                self.setWindowTitle(f"Crazyflie GS — {name}")
                break
        if text.startswith(APP_STATUS_TEXT[AppStatus.DISCONNECTED]):
            self.video_label.clear()
            self.video_label.setText("📷  Waiting for video stream...")

    def _tick_reconnect(self) -> None:
        self._reconnect_dots = (self._reconnect_dots + 1) % 4
        self.statusBar().showMessage(f"Reconnecting{'.' * self._reconnect_dots}")

    @Slot(dict)
    def _on_telemetry_updated(self, data: dict):
        def _set(key: str, text: str):
            lbl = self._tele_labels.get(key)
            if lbl:
                lbl.setText(text)

        def _color(key: str, color: str):
            lbl = self._tele_labels.get(key)
            if lbl:
                lbl.setStyleSheet(f"color: {color};" if color else "")

        status_val = data.get("status", AppStatus.DISCONNECTED)
        _set("status", APP_STATUS_TEXT.get(status_val, "—"))
        _color(
            "status",
            {
                AppStatus.CONNECTED: "#4ade80",
                AppStatus.SIMULATING: "#fb923c",
                AppStatus.DISCONNECTED: "#f87171",
                AppStatus.RECONNECTING: "#facc15",
            }.get(status_val, "#888"),
        )

        live = status_val != AppStatus.DISCONNECTED

        fps = data.get("fps", 0.0)
        _set("fps", f"{fps:.1f}" if fps > 0 else "—")
        if fps > 0:
            _color(
                "fps",
                "#4ade80" if fps >= _FPS_GREEN else "#fb923c" if fps >= _FPS_ORANGE else "#ef4444",
            )
        else:
            _color("fps", "")

        if live:
            x, y = data.get("x", 0.0), data.get("y", 0.0)
            z, yaw = data.get("z", 0.0), data.get("yaw", 0.0)
            roll, pitch = data.get("roll", 0.0), data.get("pitch", 0.0)
            _set("x", f"{x:+.2f}")
            _set("y", f"{y:+.2f}")
            _set("z", f"{z:+.2f}")
            _set("vx", f"{data.get('vx', 0.0):+.2f}")
            _set("vy", f"{data.get('vy', 0.0):+.2f}")
            _set("vz", f"{data.get('vz', 0.0):+.2f}")
            _set("roll", f"{roll:+.1f}°")
            _set("pitch", f"{pitch:+.1f}°")
            _set("yaw", f"{yaw:+.1f}°")
            self.attitude_widget.update_attitude(roll, pitch)
            self.position_trace.add_point(x, y)
            self.position_trace.update_yaw(yaw)
            z_min = self.image_receiver.blackboard_read(self.blackboard_reader, "min_altitude")
            z_max = self.image_receiver.blackboard_read(self.blackboard_reader, "max_altitude")
            self._altitude_bar.update_altitude(z, z_min, z_max, live=True)
            for key in ("m1", "m2", "m3", "m4"):
                pct = data.get(key, 0)
                _set(key, f"{pct}%" if pct > 0 else "0%")
        else:
            for key in (
                "x",
                "y",
                "z",
                "vx",
                "vy",
                "vz",
                "roll",
                "pitch",
                "yaw",
                "m1",
                "m2",
                "m3",
                "m4",
            ):
                _set(key, "—")
            self._altitude_bar.update_altitude(0.0, MIN_ALTITUDE, MAX_ALTITUDE, live=False)

        vbat = data.get("vbat", 0.0)
        _set("vbat", f"{vbat:.2f} V" if vbat > 0 else "—")
        if vbat > 0:
            bar_color = _battery_color(vbat)
            _color("vbat", bar_color)
            pct = max(0, min(100, round((vbat - _VBAT_CRIT) / (_VBAT_MAX - _VBAT_CRIT) * 100)))
            self._battery_bar.setValue(pct)
            self._battery_bar.setStyleSheet(f"""
                QProgressBar {{
                    border: 1px solid #666; border-radius: 2px; background: #1e1e1e;
                }}
                QProgressBar::chunk {{ background: {bar_color}; border-radius: 1px; }}
            """)
        else:
            _color("vbat", "")
            self._battery_bar.setValue(0)

        if live and vbat > 0:
            # Hysteresis: enter state when crossing threshold down, exit only when
            # vbat recovers by _VBAT_HYSTERESIS above that threshold.
            if vbat <= _VBAT_CRIT:
                self._vbat_warn_state = "crit"
            elif vbat >= _VBAT_CRIT + _VBAT_HYSTERESIS:
                self._vbat_warn_state = "none"

            if self._vbat_warn_state == "crit":
                self.statusBar().showMessage(f"⚠ CRITICAL BATTERY {vbat:.2f} V — LAND NOW")
                self.statusBar().setStyleSheet("QStatusBar { background: #991b1b; color: white; font-weight: bold; }")
            else:
                self.statusBar().setStyleSheet("")

    @Slot(object)
    def update_image(self, pixels: np.ndarray):
        # logger.debug(f"Frame pixel sum: {pixels.sum()}") # Uncomment to verify if drone is sending identical frames
        if pixels.size == IMAGE_SIZE * 3:
            # RGB processed image from vision_node
            rgb = pixels.reshape((IMAGE_HEIGHT, IMAGE_WIDTH, 3))
            qt_img = QImage(
                rgb.data,
                IMAGE_WIDTH,
                IMAGE_HEIGHT,
                IMAGE_WIDTH * 3,
                QImage.Format.Format_RGB888,
            )
        else:
            # Grayscale raw image from wifi_node
            qt_img = QImage(
                pixels.data,
                IMAGE_WIDTH,
                IMAGE_HEIGHT,
                IMAGE_WIDTH,
                QImage.Format.Format_Grayscale8,
            )
        pixmap = QPixmap.fromImage(qt_img).scaledToWidth(
            self.video_label.width(), Qt.TransformationMode.SmoothTransformation
        )
        self.video_label.setPixmap(pixmap)

    def _show_settings(self):
        display_state = {
            "show_attitude": (lambda: self._show_attitude, self._set_show_attitude),
            "show_position_trace": (lambda: self._show_position_trace, self._set_show_position_trace),
        }
        dlg = SettingsDialog(self.blackboard_writer, self.blackboard_reader, display_state, self)
        dlg.exec()

    def _set_show_attitude(self, show: bool) -> None:
        self._show_attitude = show
        self.attitude_widget.setVisible(show)

    def _set_show_position_trace(self, show: bool) -> None:
        self._show_position_trace = show
        self.position_trace.setVisible(show)

    @Slot(str)
    def _on_flight_state_updated(self, state: str):
        """Update the flight-state label with a colour-coded indicator matching the current FlightState name."""
        self._flight_state_name = state
        lbl = self._tele_labels.get("state")
        if lbl:
            color = {
                "IDLE": "#888",
                "AIRBORNE": "#4ade80",
                "TRACKING": "#fb923c",
                "LANDING": "#facc15",
                "MOTOR_TESTING": "#60a5fa",
            }.get(state, "#888")
            lbl.setText(state)
            lbl.setStyleSheet(f"color: {color};")
        # Toolbar button updates
        if state == "IDLE":
            self._takeoff_land_action.setText("▲  Take Off")
            self._takeoff_land_action.setEnabled(True)
            self.position_trace.clear()
        elif state in ("AIRBORNE", "TRACKING"):
            self._takeoff_land_action.setText("▼  Land")
            self._takeoff_land_action.setEnabled(True)
        elif state == "LANDING":
            self._takeoff_land_action.setText("Landing…")
            self._takeoff_land_action.setEnabled(False)
        else:
            self._takeoff_land_action.setEnabled(False)
        self._tracking_action.setChecked(state == "TRACKING")
        self._update_toolbar_state()

    @Slot(bool, str, float)
    def _on_perception_updated(self, hand_detected: bool, gesture: str, confidence: float):
        """Refresh the hand-detection and gesture sidebar labels based on the latest perception result."""
        hand_lbl = self._tele_labels.get("hand")
        if hand_lbl:
            if hand_detected:
                hand_lbl.setText("detected")
                hand_lbl.setStyleSheet("color: #4ade80;")
            else:
                hand_lbl.setText("none")
                hand_lbl.setStyleSheet("color: #f87171;")
        gesture_lbl = self._tele_labels.get("gesture")
        if gesture_lbl:
            if gesture and gesture not in ("NONE", ""):
                gesture_lbl.setText(f"{gesture} {confidence:.0%}")
            else:
                gesture_lbl.setText("—")

    @Slot(float)
    def _on_tracking_distance_updated(self, dist: float):
        """Update the distance sidebar label; hides the value (dash) when not actively tracking."""
        lbl = self._tele_labels.get("dist")
        if lbl:
            if dist > 0:
                lbl.setText(f"{dist:.2f} m")
                lbl.setStyleSheet("color: #fb923c;")
            else:
                lbl.setText("—")
                lbl.setStyleSheet("color: #aaa;")

    def _on_toggle_blackboard(self, key: str, enabled: bool) -> None:
        self.image_receiver.blackboard_write(self.blackboard_writer, key, enabled)
        logger.info(f"{key} set to {enabled}")
        if key == "process_images" and not enabled:
            self._on_perception_updated(False, "NONE", 0.0)

    def _show_about(self):
        project = self.project_config["project"]
        name = project["name"]
        version = project["version"]
        description = project["description"]
        author = project["authors"][0]["name"]
        homepage = project["urls"]["Homepage"]

        dlg = QDialog(self)
        dlg.setWindowTitle(f"About {name}")
        dlg.setFixedWidth(460)
        layout = QVBoxLayout(dlg)
        layout.setSpacing(10)
        layout.setContentsMargins(32, 28, 32, 24)

        title = QLabel(name)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setFont(QFont("Outfit", 18, QFont.Weight.Bold))
        layout.addWidget(title)

        ver = QLabel(f"v{version}")
        ver.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ver.setStyleSheet("color: #888; font-size: 11px; margin-bottom: 4px;")
        layout.addWidget(ver)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(sep)

        desc = QLabel(description)
        desc.setWordWrap(True)
        desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        desc.setStyleSheet("font-size: 12px; padding: 10px 8px;")
        layout.addWidget(desc)

        stack = QLabel("Python · iceoryx2 · PySide6 · cflib · MediaPipe · OpenCV")
        stack.setAlignment(Qt.AlignmentFlag.AlignCenter)
        stack.setStyleSheet("color: #888; font-size: 10px; padding-bottom: 4px;")
        layout.addWidget(stack)

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(sep2)

        auth = QLabel(f"Created by {author}")
        auth.setAlignment(Qt.AlignmentFlag.AlignCenter)
        auth.setStyleSheet("color: #aaa; font-size: 11px; padding-top: 4px;")
        layout.addWidget(auth)

        link = QLabel(f'<a href="{homepage}">{homepage}</a>')
        link.setOpenExternalLinks(True)
        link.setAlignment(Qt.AlignmentFlag.AlignCenter)
        link.setStyleSheet("font-size: 12px; padding-bottom: 6px;")
        layout.addWidget(link)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)

        dlg.adjustSize()
        dlg.exec()

    def _handle_key(self, event, is_pressed: bool) -> None:
        entry = _KEY_MAP.get(event.key())
        if entry is None:
            return
        keycode, uses_shift = entry
        shift = is_pressed and uses_shift and bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        self._publish_command(keycode, is_pressed, shift)

    def keyPressEvent(self, event):
        if event.isAutoRepeat():
            super().keyPressEvent(event)
            return
        self._handle_key(event, True)
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if event.isAutoRepeat():
            super().keyReleaseEvent(event)
            return
        self._handle_key(event, False)
        super().keyReleaseEvent(event)

    def closeEvent(self, event):
        self._publish_command(KeyCode.WINDOW_CLOSED, True)
        self.image_receiver.requestInterruption()
        self.image_receiver.quit()
        self.image_receiver.wait(3000)
        super().closeEvent(event)


@click.command()
@click.option("--sim", is_flag=True, expose_value=False, help="Run in simulation mode")
def main():
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        logger.error("No display available (DISPLAY/WAYLAND_DISPLAY not set)")
        sys.exit(1)

    app = QApplication(sys.argv[:1])

    try:
        window = MainWindow()

        # Allow Ctrl+C to work by setting up a signal handler and a timer
        # This must be done AFTER window creation as Node overrides signals
        signal.signal(signal.SIGINT, lambda *args: app.quit())
        timer = QTimer()
        timer.timeout.connect(lambda: None)  # Let the interpreter run
        timer.start(500)

        window.show()
        sys.exit(app.exec())

    except Exception as e:
        logger.error(f"GUI error: {e}")
    finally:
        logger.info("GUI shut down")


if __name__ == "__main__":
    main()
