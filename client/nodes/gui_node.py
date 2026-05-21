import logging
import os
import signal
import sys
import time
import tomllib

import click
import iceoryx2
import numpy as np

from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QFont, QImage, QPixmap
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

from client.common.blackboards import CONFIG
from client.common.constants import (
    AppStatus,
    EventId,
    IMAGE_HEIGHT,
    IMAGE_SCALING_FACTOR,
    IMAGE_WIDTH,
    KeyCode,
    ServiceName,
)
from client.common.node import Node
from client.common.payloads import CommandData, ImageData, TelemetryData
from client.common.utils import setup_logging

ROOT_DIR = Path(__file__).resolve().parents[2]
NODE_NAME = "gui_node"

logger = setup_logging(NODE_NAME, level=logging.DEBUG)

_SHORTCUTS = {
    "Space":    "Arm / Disarm",
    "Esc":      "Emergency stop",
    "↑ / ↓":    "Forward / Backward",
    "← / →":    "Strafe left / right",
    "A / D":    "Yaw left / right",
    "Z / X":    "Fast yaw left / right",
    "W / S":    "Altitude up / down",
    "Ctrl+Q":   "Exit",
    "Ctrl+P":   "Process images",
    "Ctrl+S":   "Save images",
    "Ctrl+/":   "Keyboard shortcuts",
}

APP_STATUS_TEXT = {
    AppStatus.CONNECTED: "Connected — receiving frames",
    AppStatus.DISCONNECTED: "Disconnected",
    AppStatus.SIMULATING: "Connected (Simulating)",
}

class ImageReceiverThreadNode(Node, QThread):
    status_changed = Signal(str)
    image_received = Signal(object)

    def __init__(self, parent=None):
        QThread.__init__(self, parent)
        Node.__init__(self, NODE_NAME, level=logging.DEBUG, handle_signals=False)

    def run(self):
        self.logger.info(f"{NODE_NAME} running")
        self.status_changed.emit("Waiting for services...")

        self.image_port = self.create_subscriber(ServiceName.IMAGE, ImageData, EventId.IMAGE_READY,
                                  check_interruption=self.isInterruptionRequested)
        self.telemetry_port = self.create_subscriber(ServiceName.TELEMETRY, TelemetryData, EventId.TELEMETRY_READY,
                                  check_interruption=self.isInterruptionRequested)

        if self.image_port.subscriber is None or self.telemetry_port.subscriber is None:
            self.status_changed.emit(APP_STATUS_TEXT[AppStatus.DISCONNECTED])
            return

        # Status remains WAITING until first telemetry payload arrives

        waitset = iceoryx2.WaitSetBuilder.new().create(iceoryx2.ServiceType.Ipc)
        image_guard = waitset.attach_notification(self.image_port.listener)
        telemetry_guard = waitset.attach_notification(self.telemetry_port.listener)

        try:
            while not self.isInterruptionRequested() and self.running:
                ids, result = waitset.wait_and_process_with_timeout(
                    iceoryx2.Duration.from_millis(10)
                )
                
                # Explicitly yield the GIL so the Qt main thread can process
                # key/mouse events without waiting for iceoryx2's blocking call.
                time.sleep(0)
                
                for event_id in ids:
                    if event_id.has_event_from(image_guard):
                        sample = self.image_port.subscriber.receive()
                        if sample is not None:
                            data = sample.payload()
                            pixels = np.ctypeslib.as_array(data.contents.pixels).copy()
                            del data, sample
                            self.image_received.emit(pixels)

                    elif event_id.has_event_from(telemetry_guard):
                        sample = self.telemetry_port.subscriber.receive()
                        if sample is not None:
                            data = sample.payload()
                            status_val = AppStatus(data.contents.status)
                            self.status_changed.emit(APP_STATUS_TEXT.get(status_val, "Unknown State"))
                            del data, sample

        except (iceoryx2.NodeWaitFailure, iceoryx2.ListenerWaitError, KeyboardInterrupt):
            pass
        except Exception as e:
            self.logger.error(f"{NODE_NAME} run error: {e}", exc_info=True)
        finally:
            if 'image_guard' in locals(): image_guard.delete()
            if 'telemetry_guard' in locals(): telemetry_guard.delete()
            if 'waitset' in locals(): waitset.delete()

        self.status_changed.emit(APP_STATUS_TEXT[AppStatus.DISCONNECTED])

class ShortcutsDialog(QDialog):
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

class MainWindow(QMainWindow):
    PANEL_WIDTH = 300
    WINDOW_WIDTH = IMAGE_WIDTH * IMAGE_SCALING_FACTOR + PANEL_WIDTH
    WINDOW_HEIGHT = IMAGE_HEIGHT * IMAGE_SCALING_FACTOR
    
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Crazyflie GUI")

        with open(ROOT_DIR / "pyproject.toml", "rb") as f:
            self.project_config = tomllib.load(f)

        self.resize(MainWindow.WINDOW_WIDTH, MainWindow.WINDOW_HEIGHT)

        self.resize(MainWindow.WINDOW_WIDTH, MainWindow.WINDOW_HEIGHT)

        # Command Node (Main Thread): Publishes commands instantly on UI events
        self.command_node = Node("gui_cmd", level=logging.DEBUG, handle_signals=False)
        
        # Receiver Thread (Background): Blocks while waiting for high-frequency images
        self.image_receiver = ImageReceiverThreadNode()

        # Writer first, then reader
        self.blackboard_writer = self.image_receiver.create_blackboard_writer("/config", CONFIG)
        self.blackboard_reader = self.image_receiver.create_blackboard_reader("/config", CONFIG)

        # Command publisher setup
        self.command_port = self.command_node.create_publisher(ServiceName.COMMAND, CommandData, EventId.COMMAND_READY)

        self.image_receiver.status_changed.connect(self.statusBar().showMessage)
        self.image_receiver.image_received.connect(self.update_image)
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

        process_images_enabled = self.image_receiver.blackboard_read(self.blackboard_reader, "process_images")
        self.process_images_action = QAction("Process images", self)
        self.process_images_action.setCheckable(True)
        self.process_images_action.setShortcut("Ctrl+P")
        self.process_images_action.setChecked(process_images_enabled)
        self.process_images_action.toggled.connect(self._on_process_images_toggled)
        settings_menu.addAction(self.process_images_action)

        save_images_enabled = self.image_receiver.blackboard_read(self.blackboard_reader, "save_images")
        self.save_images_action = QAction("Save images", self)
        self.save_images_action.setCheckable(True)
        self.save_images_action.setShortcut("Ctrl+S")
        self.save_images_action.setChecked(save_images_enabled)
        self.save_images_action.toggled.connect(self._on_save_images_toggled)
        settings_menu.addAction(self.save_images_action)

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
        tele_panel.setFixedWidth(MainWindow.PANEL_WIDTH)
        tele_layout = QVBoxLayout(tele_panel)
        title = QLabel("TELEMETRY")
        title.setFont(QFont("Outfit", 18, QFont.Bold))
        tele_layout.addWidget(title)
        tele_layout.addStretch()
        main_layout.addWidget(tele_panel)

    @Slot(object)
    def update_image(self, pixels: np.ndarray):
        # logger.debug(f"Frame pixel sum: {pixels.sum()}") # Uncomment to verify if drone is sending identical frames
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

    def _publish_command(self, key: KeyCode, is_pressed: bool):
        if self.command_port is None: return
        try:
            sample = self.command_port.publisher.loan_uninit()
            p = sample.payload().contents
            p.key = key
            p.is_pressed = is_pressed
            sample.assume_init().send()
            self.command_port.notifier.notify_with_custom_event_id(self.command_port.event)
        except Exception as e:
            logger.warning(f"Command publish failed: {e}")
    
    def _on_save_images_toggled(self, checked: bool):
        self.image_receiver.blackboard_write(self.blackboard_writer, "save_images", checked)
        logger.info(f"Save images set to {checked}")
    
    def _on_process_images_toggled(self, checked: bool):
        self.image_receiver.blackboard_write(self.blackboard_writer, "process_images", checked)
        logger.info(f"Process images set to {checked}")

    def keyPressEvent(self, event):
        if event.isAutoRepeat():
            super().keyPressEvent(event)
            return
        key = event.key()
        match key:
            case Qt.Key.Key_Space:  self._publish_command(KeyCode.SPACE, True)
            case Qt.Key.Key_Escape: self._publish_command(KeyCode.ESC, True)
            case Qt.Key.Key_Up:     self._publish_command(KeyCode.UP, True)
            case Qt.Key.Key_Down:   self._publish_command(KeyCode.DOWN, True)
            case Qt.Key.Key_Left:   self._publish_command(KeyCode.LEFT, True)
            case Qt.Key.Key_Right:  self._publish_command(KeyCode.RIGHT, True)
            case Qt.Key.Key_Q:      self._publish_command(KeyCode.Q, True)
            case Qt.Key.Key_W:      self._publish_command(KeyCode.W, True)
            case Qt.Key.Key_E:      self._publish_command(KeyCode.E, True)
            case Qt.Key.Key_A:      self._publish_command(KeyCode.A, True)
            case Qt.Key.Key_S:      self._publish_command(KeyCode.S, True)
            case Qt.Key.Key_D:      self._publish_command(KeyCode.D, True)
            case Qt.Key.Key_Z:      self._publish_command(KeyCode.Z, True)
            case Qt.Key.Key_X:      self._publish_command(KeyCode.X, True)
            case Qt.Key.Key_C:      self._publish_command(KeyCode.C, True)

        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if event.isAutoRepeat():
            super().keyReleaseEvent(event)
            return
        key = event.key()
        match key:
            case Qt.Key.Key_Space:  self._publish_command(KeyCode.SPACE, False)
            case Qt.Key.Key_Escape: self._publish_command(KeyCode.ESC, False)
            case Qt.Key.Key_Up:     self._publish_command(KeyCode.UP, False)
            case Qt.Key.Key_Down:   self._publish_command(KeyCode.DOWN, False)
            case Qt.Key.Key_Left:   self._publish_command(KeyCode.LEFT, False)
            case Qt.Key.Key_Right:  self._publish_command(KeyCode.RIGHT, False)
            case Qt.Key.Key_Q:      self._publish_command(KeyCode.Q, False)
            case Qt.Key.Key_W:      self._publish_command(KeyCode.W, False)
            case Qt.Key.Key_E:      self._publish_command(KeyCode.E, False)
            case Qt.Key.Key_A:      self._publish_command(KeyCode.A, False)
            case Qt.Key.Key_S:      self._publish_command(KeyCode.S, False)
            case Qt.Key.Key_D:      self._publish_command(KeyCode.D, False)
            case Qt.Key.Key_Z:      self._publish_command(KeyCode.Z, False)
            case Qt.Key.Key_X:      self._publish_command(KeyCode.X, False)
            case Qt.Key.Key_C:      self._publish_command(KeyCode.C, False)

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
