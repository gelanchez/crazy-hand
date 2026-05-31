"""PySide6 ground-station GUI that displays live video, telemetry, and perception data from the drone.

Keyboard input is translated to command messages published over iceoryx2, while a background
QThread polls image, telemetry, perception, and action service topics and emits Qt signals to
update the UI.
"""

import logging
import os
import signal
import sys
import time
import tomllib
from pathlib import Path

import click
import iceoryx2
import numpy as np
from PySide6.QtCore import Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QFont, QImage, QPixmap
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
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from client.common.blackboards import CONFIG
from client.common.constants import (
    IMAGE_HEIGHT,
    IMAGE_SCALING_FACTOR,
    IMAGE_SIZE,
    IMAGE_WIDTH,
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


def _battery_color(vbat: float) -> str:
    """Return a CSS hex colour interpolated from red (3.0 V) through orange (3.7 V) to green (4.2 V)."""
    if vbat >= 3.7:
        t = min(1.0, (vbat - 3.7) / (4.2 - 3.7))
        r, g, b = (
            round(251 + (74 - 251) * t),
            round(146 + (222 - 146) * t),
            round(60 + (128 - 60) * t),
        )
    else:
        t = max(0.0, (vbat - 3.0) / (3.7 - 3.0))
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
    flight_state_updated = Signal(str)              # FlightState name
    tracking_distance_updated = Signal(float)       # estimated distance m (0.0 = not tracking)

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
        while True:
            sample = self.perception_port.subscriber.receive()
            if sample is None:
                break
            data = sample.payload()
            last_hand_detected = bool(data.contents.hand_detected)
            last_gesture = data.contents.gesture_name.rstrip(b"\x00").decode("utf-8")
            last_confidence = float(data.contents.gesture_confidence)
            if process_images:
                pixels = np.ctypeslib.as_array(data.contents.processed_pixels).copy()
                del data, sample
                self.image_received.emit(pixels)
            else:
                del data, sample
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

    def __init__(self, writer, reader, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(320)
        self._writer = writer
        self._reader = reader

        layout = QVBoxLayout(self)
        layout.addWidget(self._flight_group())
        layout.addWidget(self._gesture_group())
        layout.addWidget(self._tracking_group())

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


class MainWindow(QMainWindow):
    """Main application window containing the live video panel, telemetry sidebar, menus, and keyboard handling."""

    PANEL_WIDTH = 185
    WINDOW_WIDTH = IMAGE_WIDTH * IMAGE_SCALING_FACTOR + PANEL_WIDTH
    WINDOW_HEIGHT = IMAGE_HEIGHT * IMAGE_SCALING_FACTOR

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

        # Menu bar setup
        self._setup_menus()

        # UI Layout setup
        self._setup_ui()

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

        video_panel = QFrame()
        video_layout = QVBoxLayout(video_panel)
        video_layout.setContentsMargins(0, 0, 0, 0)
        video_layout.setSpacing(0)
        self.video_label = QLabel("📷  Waiting for video stream...")
        self.video_label.setAlignment(Qt.AlignCenter)
        self.video_label.setStyleSheet("color: #666; font-size: 13px;")
        self.video_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        video_layout.addWidget(self.video_label)
        main_layout.addWidget(video_panel, 1)
        main_layout.addWidget(self._setup_sidebar())

        hints = QLabel("Space: Take off/Land  Esc: Emergency  ↑↓←→: Move  A/D: Yaw  W/S: Alt  T: Tracking")
        hints.setStyleSheet("color: #777; font-size: 9px; padding-right: 6px;")
        self.statusBar().addPermanentWidget(hints)

    def _setup_sidebar(self) -> QFrame:
        tele_panel = QFrame()
        tele_panel.setObjectName("tele_panel")
        tele_panel.setFixedWidth(MainWindow.PANEL_WIDTH)
        tele_panel.setFrameShape(QFrame.Shape.NoFrame)
        tele_panel.setStyleSheet("#tele_panel { border-left: 1px solid #444; }")
        tele_layout = QVBoxLayout(tele_panel)
        tele_layout.setContentsMargins(10, 10, 10, 10)
        tele_layout.setSpacing(2)

        self._tele_labels: dict[str, QLabel] = {}
        val_font = QFont("Monospace", 10)
        val_font.setStyleHint(QFont.StyleHint.Monospace)

        def _add_section(header: str):
            h = QLabel(header)
            h.setFont(QFont("Outfit", 9, QFont.Weight.Bold))
            h.setStyleSheet("color: #999; padding-top: 5px; border-top: 1px solid #444;")
            tele_layout.addWidget(h)

        def _add_row(label: str, key: str):
            row_widget = QWidget()
            row_layout = QHBoxLayout(row_widget)
            row_layout.setContentsMargins(4, 1, 4, 1)
            lbl = QLabel(label)
            lbl.setFont(val_font)
            lbl.setStyleSheet("color: #aaa;")
            val = QLabel("—")
            val.setFont(val_font)
            val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            row_layout.addWidget(lbl)
            row_layout.addStretch()
            row_layout.addWidget(val)
            tele_layout.addWidget(row_widget)
            self._tele_labels[key] = val

        _add_row("Status", "status")
        _add_row("FPS", "fps")
        _add_row("State", "state")
        self._battery_bar = QProgressBar()
        self._battery_bar.setRange(0, 100)
        self._battery_bar.setValue(0)
        self._battery_bar.setFixedHeight(6)
        self._battery_bar.setTextVisible(False)
        self._battery_bar.setContentsMargins(4, 2, 4, 2)
        self._battery_bar.setStyleSheet("""
            QProgressBar {
                border: 1px solid #666; border-radius: 2px; background: #1e1e1e;
            }
            QProgressBar::chunk { background: #4ade80; border-radius: 1px; }
        """)
        _add_row("VBat", "vbat")
        tele_layout.addWidget(self._battery_bar)

        _add_section("POSITION (m)")
        _add_row("X", "x")
        _add_row("Y", "y")
        _add_row("Z", "z")

        _add_section("VELOCITY (m/s)")
        _add_row("Vx", "vx")
        _add_row("Vy", "vy")
        _add_row("Vz", "vz")

        _add_section("ATTITUDE (°)")
        _add_row("Roll", "roll")
        _add_row("Pitch", "pitch")
        _add_row("Yaw", "yaw")

        _add_section("MOTORS (%)")
        motor_widget = QWidget()
        motor_grid = QGridLayout(motor_widget)
        motor_grid.setContentsMargins(4, 1, 4, 1)
        motor_grid.setVerticalSpacing(2)
        motor_grid.setHorizontalSpacing(8)
        motor_grid.setColumnStretch(2, 1)
        for i, key in enumerate(("m1", "m2", "m3", "m4")):
            row, col = divmod(i, 2)
            grid_col = col * 3
            mlbl = QLabel(f"M{i + 1}")
            mlbl.setFont(val_font)
            mlbl.setStyleSheet("color: #aaa;")
            mval = QLabel("—")
            mval.setFont(val_font)
            mval.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            motor_grid.addWidget(mlbl, row, grid_col)
            motor_grid.addWidget(mval, row, grid_col + 1)
            self._tele_labels[key] = mval
        tele_layout.addWidget(motor_widget)

        _add_section("VISION")
        _add_row("Hand", "hand")
        _add_row("Gesture", "gesture")
        _add_row("Distance", "dist")

        tele_layout.addStretch()
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
        self.statusBar().showMessage(text)
        for _, name in APP_STATUS_TEXT.items():
            if text.startswith(name):
                self.setWindowTitle(f"Crazyflie GS — {name}")
                break
        if text.startswith(APP_STATUS_TEXT[AppStatus.DISCONNECTED]):
            self.video_label.clear()
            self.video_label.setText("📷  Waiting for video stream...")

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
            _set("x", f"{data.get('x', 0.0):+.2f}")
            _set("y", f"{data.get('y', 0.0):+.2f}")
            _set("z", f"{data.get('z', 0.0):+.2f}")
            _set("vx", f"{data.get('vx', 0.0):+.2f}")
            _set("vy", f"{data.get('vy', 0.0):+.2f}")
            _set("vz", f"{data.get('vz', 0.0):+.2f}")
            _set("roll", f"{data.get('roll', 0.0):+.1f}°")
            _set("pitch", f"{data.get('pitch', 0.0):+.1f}°")
            _set("yaw", f"{data.get('yaw', 0.0):+.1f}°")
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

        vbat = data.get("vbat", 0.0)
        _set("vbat", f"{vbat:.2f} V" if vbat > 0 else "—")
        if vbat > 0:
            bar_color = _battery_color(vbat)
            _color("vbat", bar_color)
            pct = max(0, min(100, round((vbat - 3.0) / (4.2 - 3.0) * 100)))
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
        dlg = SettingsDialog(self.blackboard_writer, self.blackboard_reader, self)
        dlg.exec()

    @Slot(str)
    def _on_flight_state_updated(self, state: str):
        """Update the flight-state label with a colour-coded indicator matching the current FlightState name."""
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
