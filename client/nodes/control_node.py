import logging

import iceoryx2

from client.common.constants import (
    DEFAULT_HEIGHT,
    EventId,
    KeyCode,
    ServiceName,
    SPEED_FACTOR,
)
from client.common.node import Node
from client.common.payloads import ActionData, CommandData, PerceptionData


class ControlNode(Node):
    def __init__(self, level=logging.INFO):
        super().__init__("control_node", level=level)
        self._active = False
        self._hover = {
            "vx": 0.0,
            "vy": 0.0,
            "yawrate": 0.0,
            "zdistance": DEFAULT_HEIGHT,
        }

    def run(self):
        self.logger.info(f"{self.name} running")

        self.command_port = self.create_subscriber(
            ServiceName.COMMAND, CommandData, EventId.COMMAND_READY
        )
        self.perception_port = self.create_subscriber(
            ServiceName.PERCEPTION, PerceptionData, EventId.PERCEPTION_READY
        )
        self.action_port = self.create_publisher(
            ServiceName.ACTION, ActionData, EventId.ACTION_READY
        )

        if (
            self.command_port is None
            or self.command_port.subscriber is None
            or self.perception_port is None
            or self.perception_port.subscriber is None
            or self.action_port is None
            or self.action_port.publisher is None
        ):
            self.logger.error("Failed to create ports")
            return

        waitset = iceoryx2.WaitSetBuilder.new().create(iceoryx2.ServiceType.Ipc)
        command_guard = waitset.attach_notification(self.command_port.listener)
        perception_guard = waitset.attach_notification(self.perception_port.listener)

        try:
            while self.running:
                ids, result = waitset.wait_and_process_with_timeout(
                    iceoryx2.Duration.from_millis(100)
                )

                if result in (
                    iceoryx2.WaitSetRunResult.Interrupt,
                    iceoryx2.WaitSetRunResult.TerminationRequest,
                ):
                    self.running = False
                    break

                for event_id in ids:
                    if event_id.has_event_from(command_guard):
                        self._process_command()
                    elif event_id.has_event_from(perception_guard):
                        self._process_perception()
        except (
            iceoryx2.NodeWaitFailure,
            iceoryx2.ListenerWaitError,
            KeyboardInterrupt,
        ):
            pass
        except Exception as e:
            self.logger.error(f"ControlNode error: {e}", exc_info=True)
        finally:
            command_guard.delete()
            perception_guard.delete()
            waitset.delete()
            self.logger.info(f"{self.name} shut down")

    def _process_perception(self):
        while True:
            try:
                sample = self.perception_port.subscriber.receive()
            except Exception as e:
                self.logger.warning(f"Perception receive error: {e}")
                break

            if sample is None:
                break

            # Consume and discard for now # TODO
            del sample

    def _process_command(self):
        changed = False
        while True:
            try:
                sample = self.command_port.subscriber.receive()
            except Exception as e:
                self.logger.warning(f"Command receive error: {e}")
                break

            if sample is None:
                break

            command = sample.payload().contents
            key = command.key
            is_pressed = command.is_pressed
            del command, sample

            changed = True

            match (is_pressed, key):
                case (True, KeyCode.SPACE):
                    self._active = True
                case (True, KeyCode.ESC | KeyCode.WINDOW_CLOSED):
                    self._active = False
                    self._hover["vx"] = self._hover["vy"] = self._hover["yawrate"] = 0.0
                case (True, KeyCode.UP):
                    self._hover["vx"] = SPEED_FACTOR
                case (True, KeyCode.DOWN):
                    self._hover["vx"] = -SPEED_FACTOR
                case (True, KeyCode.LEFT):
                    self._hover["vy"] = SPEED_FACTOR
                case (True, KeyCode.RIGHT):
                    self._hover["vy"] = -SPEED_FACTOR
                case (True, KeyCode.A):
                    self._hover["yawrate"] = -70.0
                case (True, KeyCode.D):
                    self._hover["yawrate"] = 70.0
                case (True, KeyCode.Z):
                    self._hover["yawrate"] = -200.0
                case (True, KeyCode.X):
                    self._hover["yawrate"] = 200.0
                case (True, KeyCode.W):
                    self._hover["zdistance"] = min(2.0, self._hover["zdistance"] + 0.1)
                case (True, KeyCode.S):
                    self._hover["zdistance"] = max(0.1, self._hover["zdistance"] - 0.1)
                case (False, KeyCode.UP | KeyCode.DOWN):
                    self._hover["vx"] = 0.0
                case (False, KeyCode.LEFT | KeyCode.RIGHT):
                    self._hover["vy"] = 0.0
                case (False, KeyCode.A | KeyCode.D | KeyCode.Z | KeyCode.X):
                    self._hover["yawrate"] = 0.0

        if changed:
            self._publish_action()

    def _publish_action(self):
        try:
            sample = self.action_port.publisher.loan_uninit()
            p = sample.payload().contents
            p.vx, p.vy, p.yawrate, p.zdistance = (
                self._hover["vx"],
                self._hover["vy"],
                self._hover["yawrate"],
                self._hover["zdistance"],
            )
            p.active = self._active
            sample.assume_init().send()
            self.action_port.notifier.notify_with_custom_event_id(
                self.action_port.event
            )
            self.logger.debug(
                f"Action published: {p.vx}, {p.vy}, {p.yawrate}, {p.zdistance}, active={p.active}"
            )
        except Exception as e:
            self.logger.warning(f"Action publish failed: {e}")


def main():
    node = ControlNode(level=logging.DEBUG)
    node.run()


if __name__ == "__main__":
    main()
