import os
import sys
import time
import click
import numpy as np
import iceoryx2
import logging
import signal
import tomllib
from pathlib import Path

from client.common.payloads import ImageData, CommandData
from client.common.constants import (
    ServiceName,
    EventId,
    IMAGE_HEIGHT, IMAGE_WIDTH,
    SPEED_FACTOR, DEFAULT_HEIGHT,
)
from client.common.utils import setup_logging
from client.common.node import Node
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

ROOT_DIR = Path(__file__).resolve().parents[2]
NODE_NAME = "gui_node"
logger = setup_logging(NODE_NAME, level=logging.DEBUG)

_SHORTCUTS = {
    "Ctrl+Q":   "Exit",
    "Ctrl+/":   "Keyboard Shortcuts",
    "Space":    "Arm / Disarm",
    "Esc":      "Emergency stop",
    "↑ / ↓":    "Forward / Backward",
    "← / →":    "Strafe left / right",
    "A / D":    "Yaw left / right",
    "Z / X":    "Fast yaw left / right",
    "W / S":    "Altitude up / down",
}

class DroneStatus(StrEnum):
    INITIALIZING = "Initializing..."
    WAITING = "Waiting for services..."
    CONNECTED = "Connected — receiving frames"
    DISCONNECTED = "Disconnected"

class GuiNode(Node, QThread):
    status_changed = Signal(str)
    image_received = Signal(object)

    def __init__(self, parent=None):
        QThread.__init__(self, parent)
        Node.__init__(self, NODE_NAME, level=logging.DEBUG, handle_signals=False)

    def run(self):
        self.logger.info(f"{NODE_NAME} running")
        self.status_changed.emit(DroneStatus.WAITING)

        self.image_port = self.create_subscriber(ServiceName.IMAGE, ImageData, EventId.IMAGE_READY,
                                  check_interruption=self.isInterruptionRequested)

        if self.image_port.subscriber is None:
            self.status_changed.emit(DroneStatus.DISCONNECTED)
            return

        self.status_changed.emit(DroneStatus.CONNECTED)

        try:
            while not self.isInterruptionRequested() and self.running:
                event_id = self.image_port.listener.timed_wait_one(
                    iceoryx2.Duration.from_millis(10)
                )
                # Explicitly yield the GIL so the Qt main thread can process
                # key/mouse events without waiting for iceoryx2's blocking call.
                time.sleep(0)
                
                if event_id != self.image_port.event:
                    continue
                
                sample = self.image_port.subscriber.receive()
                if sample is not None:
                    data = sample.payload()
                    pixels = np.ctypeslib.as_array(data.contents.pixels).copy()
                    del data, sample
                    self.image_received.emit(pixels)
        except (iceoryx2.NodeWaitFailure, iceoryx2.ListenerWaitError, KeyboardInterrupt):
            pass
        except Exception as e:
            self.logger.error(f"{NODE_NAME} run error: {e}", exc_info=True)

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

        with open(ROOT_DIR / "pyproject.toml", "rb") as f:
            self.project_config = tomllib.load(f)

        video_w = IMAGE_WIDTH * 2
        self.resize(video_w + 300, IMAGE_HEIGHT * 2)

        # Flight state
        self._active = False
        self._hover = {"vx": 0.0, "vy": 0.0, "yawrate": 0.0, "zdistance": DEFAULT_HEIGHT}

        # Shared node for UI commands
        # TODO Why two nodes?
        self.ui_node = Node("gui_cmd", level=logging.DEBUG, handle_signals=False)
        self.gui_node = GuiNode()

        # Command publisher setup
        self._cmd_port = self.ui_node.create_publisher(ServiceName.COMMAND, CommandData, EventId.COMMAND_READY)

        self.gui_node.status_changed.connect(self.statusBar().showMessage)
        self.gui_node.image_received.connect(self.update_image)
        self.gui_node.start()

        self.statusBar().showMessage(DroneStatus.INITIALIZING)
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
        self.video_label = QLabel("Waiting for video stream...")
        self.video_label.setAlignment(Qt.AlignCenter)
        self.video_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        video_layout.addWidget(self.video_label)
        main_layout.addWidget(video_panel, 1)

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
        qt_img = QImage(pixels.data, IMAGE_WIDTH, IMAGE_HEIGHT, IMAGE_WIDTH, QImage.Format.Format_Grayscale8)
        pixmap = QPixmap.fromImage(qt_img).scaledToWidth(self.video_label.width(), Qt.TransformationMode.SmoothTransformation)
        self.video_label.setPixmap(pixmap)

    def _show_about(self):
        project = self.project_config["project"]

        name = project["name"]
        version = project["version"]
        description = project["description"]
        author = project["authors"][0]["name"]
        homepage = project["urls"]["Homepage"]

        QMessageBox.about(
            self,
            f"About {name}",
            f"""
            <h2 align="center">{name}</h2>

            <p align="center">
                <b>Version {version}</b>
            </p>

            <p align="center">
                {description}
            </p>

            <p align="center">
                <a href="{homepage}">{homepage}</a>
            </p>

            <hr>

            <p align="center">
                <small>
                    Created by {author}
                </small>
            </p>
            """,
        )

    def _publish_command(self):
        if self._cmd_port is None: return
        logger.debug(f"Publishing command: active={self._active}, hover={self._hover}")
        try:
            sample = self._cmd_port.publisher.loan_uninit()
            p = sample.payload().contents
            p.vx, p.vy, p.yawrate, p.zdistance = self._hover["vx"], self._hover["vy"], self._hover["yawrate"], self._hover["zdistance"]
            p.active = 1 if self._active else 0
            sample.assume_init().send()
            self._cmd_port.notifier.notify_with_custom_event_id(self._cmd_port.event)
        except Exception as e:
            logger.warning(f"Command publish failed: {e}")


    def keyPressEvent(self, event):
        if event.isAutoRepeat():
            super().keyPressEvent(event)
            return
        key = event.key()
        match key:
            case Qt.Key.Key_Space:
                # Toggle signal: wifi_node handles arming state
                self._active = True
                self._publish_command()
                super().keyPressEvent(event)
                return
            case Qt.Key.Key_Escape:
                self._active = False
                self._hover["vx"] = self._hover["vy"] = self._hover["yawrate"] = 0.0
            case Qt.Key.Key_Up:
                self._hover["vx"] = SPEED_FACTOR
            case Qt.Key.Key_Down:
                self._hover["vx"] = -SPEED_FACTOR
            case Qt.Key.Key_Left:
                self._hover["vy"] = SPEED_FACTOR
            case Qt.Key.Key_Right:
                self._hover["vy"] = -SPEED_FACTOR
            case Qt.Key.Key_A:
                self._hover["yawrate"] = -70.0
            case Qt.Key.Key_D:
                self._hover["yawrate"] = 70.0
            case Qt.Key.Key_Z:
                self._hover["yawrate"] = -200.0
            case Qt.Key.Key_X:
                self._hover["yawrate"] = 200.0
            case Qt.Key.Key_W:
                self._hover["zdistance"] = min(2.0, self._hover["zdistance"] + 0.1)
            case Qt.Key.Key_S:
                self._hover["zdistance"] = max(0.1, self._hover["zdistance"] - 0.1)

        self._publish_command()
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if event.isAutoRepeat():
            super().keyReleaseEvent(event)
            return
        key = event.key()
        match key:
            case Qt.Key.Key_Space:
                # Don't publish on Space release — toggle is handled by wifi_node
                super().keyReleaseEvent(event)
                return
            case Qt.Key.Key_Up | Qt.Key.Key_Down:
                self._hover["vx"] = 0.0
            case Qt.Key.Key_Left | Qt.Key.Key_Right:
                self._hover["vy"] = 0.0
            case Qt.Key.Key_A | Qt.Key.Key_D | Qt.Key.Key_Z | Qt.Key.Key_X:
                self._hover["yawrate"] = 0.0

        self._publish_command()
        super().keyReleaseEvent(event)

    def closeEvent(self, event):
        self._active = False
        self._publish_command()
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
