

import os
import sys
import time
import click
import numpy as np
import iceoryx2
import logging

from common.payloads import ImageData, CommandData
from common.constants import (
    ServiceName,
    EventId,
    IMAGE_HEIGHT, IMAGE_WIDTH,
    SPEED_FACTOR, DEFAULT_HEIGHT,
)
from common.utils import setup_logging, setup_iceoryx2_config
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtCore import Qt, QTimer, Signal, Slot, QThread
from enum import StrEnum
from PySide6.QtGui import QAction, QFont, QImage, QPixmap

NODE_NAME = "gui_node"

_SHORTCUTS = {
    "Ctrl+Q":   "Exit",
    "Ctrl+/":   "Keyboard Shortcuts",
    "Space":    "Arm / Disarm",
    "Esc":      "Emergency stop",
    "↑ / ↓":   "Forward / Backward",
    "← / →":   "Strafe left / right",
    "A / D":    "Yaw left / right",
    "Z / X":    "Fast yaw left / right",
    "W / S":    "Altitude up / down",
}

logger = setup_logging(NODE_NAME, logging.DEBUG)


class DroneStatus(StrEnum):
    INITIALIZING = "Initializing..."
    WAITING = "Waiting for services..."
    CONNECTED = "Connected — receiving frames"
    DISCONNECTED = "Disconnected"


class GuiNode(QThread):
    status_changed = Signal(str)
    image_received = Signal(object)

    def __init__(self):
        super().__init__()
        setup_iceoryx2_config()
        logger.info(f"{NODE_NAME} initialized")

    def run(self):
        logger.info(f"{NODE_NAME} running")
        self.node = (
            iceoryx2.NodeBuilder.new()
            .name(iceoryx2.NodeName.new(NODE_NAME))
            .create(iceoryx2.ServiceType.Ipc)
        )

        self.status_changed.emit(DroneStatus.WAITING)
        logger.info("Waiting for image service...")
        while not self.isInterruptionRequested():
            try:
                self.image_service = (
                    self.node.service_builder(iceoryx2.ServiceName.new(ServiceName.IMAGE))
                    .publish_subscribe(ImageData)
                    .open_or_create()
                )
                break
            except iceoryx2.PublishSubscribeOpenError:
                time.sleep(0.1)

        if self.isInterruptionRequested():
            return

        logger.info("Image service connected")
        self.image_subscriber = self.image_service.subscriber_builder().create()

        while not self.isInterruptionRequested():
            try:
                self.image_event = (
                    self.node.service_builder(iceoryx2.ServiceName.new(ServiceName.IMAGE))
                    .event()
                    .open_or_create()
                )
                break
            except Exception:
                time.sleep(0.1)

        if self.isInterruptionRequested():
            return

        logger.info("Image event connected")
        self.status_changed.emit(DroneStatus.CONNECTED)

        self.image_listener = self.image_event.listener_builder().create()
        self.image_ready_event = iceoryx2.EventId.new(EventId.IMAGE_READY)

        sample = None
        try:
            while not self.isInterruptionRequested():
                sample = None  # release any previous borrow before blocking
                event_id = self.image_listener.timed_wait_one(
                    iceoryx2.Duration.from_millis(10)
                )
                # Explicitly yield the GIL so the Qt main thread can process
                # key/mouse events without waiting for iceoryx2's blocking call.
                time.sleep(0)
                if event_id != self.image_ready_event:
                    continue
                try:
                    sample = self.image_subscriber.receive()
                except Exception as e:
                    logger.warning(f"Image receive error: {e}")
                    continue
                if sample is not None:
                    data = sample.payload()
                    pixels = np.ctypeslib.as_array(data.contents.pixels).copy()
                    del data, sample
                    sample = None
                    self.image_received.emit(pixels)
        except iceoryx2.NodeWaitFailure:
            pass
        except Exception as e:
            logger.error(f"{NODE_NAME} run error: {e}", exc_info=True)

        self.status_changed.emit(DroneStatus.DISCONNECTED)


class ShortcutsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Keyboard Shortcuts")
        self.resize(340, 200)

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


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Crazyflie GUI")
        video_w = IMAGE_WIDTH * 2
        self.resize(video_w + 300, IMAGE_HEIGHT * 2)

        # Flight state
        self._active = False
        self._hover = {"vx": 0.0, "vy": 0.0, "yawrate": 0.0, "zdistance": DEFAULT_HEIGHT}

        self.gui_node = GuiNode()

        # iceoryx2 command publisher (main thread)
        self._cmd_node = None
        self._cmd_publisher = None
        self._cmd_notifier = None
        self._cmd_ready_event = None
        try:
            self._cmd_node = (
                iceoryx2.NodeBuilder.new()
                .name(iceoryx2.NodeName.new("gui_cmd"))
                .create(iceoryx2.ServiceType.Ipc)
            )
            _cmd_svc = (
                self._cmd_node.service_builder(iceoryx2.ServiceName.new(ServiceName.COMMAND))
                .publish_subscribe(CommandData)
                .open_or_create()
            )
            self._cmd_publisher = _cmd_svc.publisher_builder().create()
            _cmd_evt = (
                self._cmd_node.service_builder(iceoryx2.ServiceName.new(ServiceName.COMMAND))
                .event()
                .open_or_create()
            )
            self._cmd_notifier = _cmd_evt.notifier_builder().create()
            self._cmd_ready_event = iceoryx2.EventId.new(EventId.COMMAND_READY)
            logger.info("Command publisher ready")
        except Exception as e:
            logger.warning(f"Command publisher unavailable: {e}")

        self._cmd_timer = QTimer(self)
        self._cmd_timer.timeout.connect(self._publish_command)
        self._cmd_timer.start(100)

        self.gui_node.status_changed.connect(self.statusBar().showMessage)
        self.gui_node.image_received.connect(self.update_image)
        self.gui_node.start()

        # Status bar
        self.statusBar().showMessage(DroneStatus.INITIALIZING)

        # Dialogs (pre-created to avoid first-open delay)
        self._shortcuts_dialog = ShortcutsDialog(self)

        # Menu bar
        menu_bar = self.menuBar()

        file_menu = menu_bar.addMenu("File")
        exit_action = QAction("Exit", self)
        exit_action.setShortcut("Ctrl+Q")
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        connection_menu = menu_bar.addMenu("Connection")
        self.connect_action = QAction("Connect", self)
        self.connect_action.setEnabled(False)
        self.disconnect_action = QAction("Disconnect", self)
        self.disconnect_action.setEnabled(False)
        connection_menu.addAction(self.connect_action)
        connection_menu.addAction(self.disconnect_action)

        help_menu = menu_bar.addMenu("Help")
        shortcuts_action = QAction("Keyboard Shortcuts", self)
        shortcuts_action.setShortcut("Ctrl+/")
        shortcuts_action.triggered.connect(self._shortcuts_dialog.show)
        help_menu.addAction(shortcuts_action)
        help_menu.addSeparator()
        about_action = QAction("About", self)
        about_action.triggered.connect(self._show_about)
        help_menu.addAction(about_action)

        # Central Widget
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QHBoxLayout(central_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Left Panel (Video)
        video_panel = QFrame()
        video_layout = QVBoxLayout(video_panel)
        video_layout.setContentsMargins(0, 0, 0, 0)
        video_layout.setSpacing(0)
        self.video_label = QLabel("Waiting for video stream...")
        self.video_label.setAlignment(Qt.AlignCenter)
        self.video_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        video_layout.addWidget(self.video_label)
        main_layout.addWidget(video_panel, 1)

        # Right Panel (Telemetry)
        tele_panel = QFrame()
        tele_panel.setFixedWidth(300)
        tele_layout = QVBoxLayout(tele_panel)

        title = QLabel("TELEMETRY")
        title.setFont(QFont("Outfit", 18, QFont.Bold))
        tele_layout.addWidget(title)
        tele_layout.addStretch()
        main_layout.addWidget(tele_panel)

    @Slot(object)
    def update_image(self, pixels: np.ndarray):
        qt_img = QImage(
            pixels.data,
            IMAGE_WIDTH,
            IMAGE_HEIGHT,
            IMAGE_WIDTH,
            QImage.Format.Format_Grayscale8,
        )
        pixmap = QPixmap.fromImage(qt_img).scaledToWidth(
            self.video_label.width(),
            Qt.TransformationMode.SmoothTransformation,
        )
        self.video_label.setPixmap(pixmap)

    def _show_about(self):
        QMessageBox.about(
            self,
            "About Crazyflie GUI",
            "<b>Crazyflie GUI</b><br>"
            "Hand gesture-based UAV control<br><br>"
            "TFM — Jose Angel Sánchez",
        )

    def _publish_command(self):
        if self._cmd_publisher is None:
            return
        try:
            sample = self._cmd_publisher.loan_uninit()
            p = sample.payload().contents
            p.vx        = self._hover["vx"]
            p.vy        = self._hover["vy"]
            p.yawrate   = self._hover["yawrate"]
            p.zdistance = self._hover["zdistance"]
            p.active    = 1 if self._active else 0
            sample = sample.assume_init()
            sample.send()
            self._cmd_notifier.notify_with_custom_event_id(self._cmd_ready_event)
        except Exception as e:
            logger.warning(f"Command publish failed: {e}")

    def keyPressEvent(self, event):
        if event.isAutoRepeat():
            super().keyPressEvent(event)
            return
        key = event.key()
        if key == Qt.Key.Key_Space:
            self._active = True
            logger.debug("Armed")
        elif key == Qt.Key.Key_Escape:
            self._active = False
            self._hover["vx"] = 0.0
            self._hover["vy"] = 0.0
            self._hover["yawrate"] = 0.0
            logger.debug("Disarmed (emergency)")
        elif key == Qt.Key.Key_Up:
            self._hover["vx"] = SPEED_FACTOR
        elif key == Qt.Key.Key_Down:
            self._hover["vx"] = -SPEED_FACTOR
        elif key == Qt.Key.Key_Left:
            self._hover["vy"] = SPEED_FACTOR
        elif key == Qt.Key.Key_Right:
            self._hover["vy"] = -SPEED_FACTOR
        elif key == Qt.Key.Key_A:
            self._hover["yawrate"] = -70.0
        elif key == Qt.Key.Key_D:
            self._hover["yawrate"] = 70.0
        elif key == Qt.Key.Key_Z:
            self._hover["yawrate"] = -200.0
        elif key == Qt.Key.Key_X:
            self._hover["yawrate"] = 200.0
        elif key == Qt.Key.Key_W:
            self._hover["zdistance"] = min(2.0, self._hover["zdistance"] + 0.1)
        elif key == Qt.Key.Key_S:
            self._hover["zdistance"] = max(0.1, self._hover["zdistance"] - 0.1)
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if event.isAutoRepeat():
            super().keyReleaseEvent(event)
            return
        key = event.key()
        if key == Qt.Key.Key_Space:
            self._active = False
            logger.debug("Disarmed")
        elif key in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            self._hover["vx"] = 0.0
        elif key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            self._hover["vy"] = 0.0
        elif key in (Qt.Key.Key_A, Qt.Key.Key_D, Qt.Key.Key_Z, Qt.Key.Key_X):
            self._hover["yawrate"] = 0.0
        super().keyReleaseEvent(event)

    def closeEvent(self, event):
        self._cmd_timer.stop()
        self._active = False
        self._publish_command()  # send one final disarm before shutdown
        self.gui_node.requestInterruption()
        self.gui_node.quit()
        self.gui_node.wait(3000)
        super().closeEvent(event)


@click.command()
@click.option("--sim", is_flag=True, expose_value=False, help="Run in simulation mode")
def main():
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        logger.error("No display available (DISPLAY/WAYLAND_DISPLAY not set)")
        sys.exit(1)

    try:
        app = QApplication(sys.argv[:1])
        window = MainWindow()
        window.show()
        sys.exit(app.exec())
    except KeyboardInterrupt:
        pass
    except BrokenPipeError:
        pass


if __name__ == "__main__":
    main()

