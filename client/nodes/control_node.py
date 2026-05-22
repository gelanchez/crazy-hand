import logging

import iceoryx2

from client.common.constants import (
    ALTITUDE_STEP,
    ALTITUDE_STEP_FAST,
    DEFAULT_HEIGHT,
    FAST_SPEED_FACTOR,
    SPEED_FACTOR,
    YAW_RATE,
    YAW_RATE_FAST,
    EventId,
    FlightCommand,
    FlightState,
    KeyCode,
    ServiceName,
)
from client.common.node import Node
from client.common.payloads import ActionData, CommandData, PerceptionData


class ControlNode(Node):
    def __init__(self, level=logging.INFO):
        super().__init__("control_node", level=level)
        self._state = FlightState.IDLE
        self._flight_command = FlightCommand.NONE
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

            if self._state == FlightState.TRACKING:
                self._apply_tracking(sample.payload().contents)

            del sample

    def _apply_tracking(self, perception):
        # Placeholder — tracking logic goes here
        pass

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
            key = int(command.key)
            is_pressed = bool(command.is_pressed)
            shift = bool(command.shift)
            del command, sample

            speed = FAST_SPEED_FACTOR if shift else SPEED_FACTOR
            yaw = YAW_RATE_FAST if shift else YAW_RATE
            alt_step = ALTITUDE_STEP_FAST if shift else ALTITUDE_STEP
            airborne = self._state in (FlightState.AIRBORNE, FlightState.TRACKING)

            match (is_pressed, key):
                # --- Takeoff / Land ---
                case (True, KeyCode.SPACE):
                    if self._state == FlightState.IDLE:
                        self._state = FlightState.AIRBORNE
                        self._flight_command = FlightCommand.TAKEOFF
                        self.logger.info("TAKEOFF commanded")
                        changed = True
                    elif airborne:
                        self._state = FlightState.IDLE
                        self._hover["vx"] = self._hover["vy"] = self._hover["yawrate"] = 0.0
                        self._flight_command = FlightCommand.LAND
                        self.logger.info("LAND commanded")
                        changed = True

                # --- Emergency stop ---
                case (True, KeyCode.ESC | KeyCode.WINDOW_CLOSED):
                    if self._state != FlightState.IDLE:
                        self.logger.warning("EMERGENCY STOP")
                    self._state = FlightState.IDLE
                    self._hover["vx"] = self._hover["vy"] = self._hover["yawrate"] = 0.0
                    self._flight_command = FlightCommand.EMERGENCY_STOP
                    changed = True

                # --- Toggle tracking ---
                case (True, KeyCode.T):
                    if self._state == FlightState.AIRBORNE:
                        self._state = FlightState.TRACKING
                        self._flight_command = FlightCommand.TOGGLE_TRACKING
                        self.logger.info("TRACKING mode ON")
                        changed = True
                    elif self._state == FlightState.TRACKING:
                        self._state = FlightState.AIRBORNE
                        self._flight_command = FlightCommand.TOGGLE_TRACKING
                        self.logger.info("TRACKING mode OFF")
                        changed = True

                # --- Movement (only when airborne) ---
                case (True, KeyCode.UP) if airborne:
                    self._hover["vx"] = speed
                    changed = True
                case (True, KeyCode.DOWN) if airborne:
                    self._hover["vx"] = -speed
                    changed = True
                case (True, KeyCode.LEFT) if airborne:
                    self._hover["vy"] = speed
                    changed = True
                case (True, KeyCode.RIGHT) if airborne:
                    self._hover["vy"] = -speed
                    changed = True
                case (True, KeyCode.A) if airborne:
                    self._hover["yawrate"] = -yaw
                    changed = True
                case (True, KeyCode.D) if airborne:
                    self._hover["yawrate"] = yaw
                    changed = True
                case (True, KeyCode.W) if airborne:
                    self._hover["zdistance"] = min(2.0, self._hover["zdistance"] + alt_step)
                    self.logger.info(f"Altitude → {self._hover['zdistance']:.2f}m")
                    changed = True
                case (True, KeyCode.S) if airborne:
                    self._hover["zdistance"] = max(0.1, self._hover["zdistance"] - alt_step)
                    self.logger.info(f"Altitude → {self._hover['zdistance']:.2f}m")
                    changed = True

                # --- Stabilise ---
                case (True, KeyCode.C) if airborne:
                    self._hover["vx"] = self._hover["vy"] = self._hover["yawrate"] = 0.0
                    self.logger.info("Stabilised")
                    changed = True

                # --- Zero on release ---
                case (False, KeyCode.UP | KeyCode.DOWN):
                    self._hover["vx"] = 0.0
                    changed = True
                case (False, KeyCode.LEFT | KeyCode.RIGHT):
                    self._hover["vy"] = 0.0
                    changed = True
                case (False, KeyCode.A | KeyCode.D):
                    self._hover["yawrate"] = 0.0
                    changed = True

        if changed:
            self._publish_action()

    def _publish_action(self):
        try:
            sample = self.action_port.publisher.loan_uninit()
            p = sample.payload().contents
            p.active = self._state != FlightState.IDLE
            p.command = int(self._flight_command)
            p.vx, p.vy, p.yawrate, p.zdistance = (
                self._hover["vx"],
                self._hover["vy"],
                self._hover["yawrate"],
                self._hover["zdistance"],
            )
            sample.assume_init().send()
            self.action_port.notifier.notify_with_custom_event_id(self.action_port.event)
            self.logger.debug(
                f"Action: state={self._state.name}, cmd={self._flight_command.name}, "
                f"vx={p.vx:.2f}, vy={p.vy:.2f}, yaw={p.yawrate:.1f}, z={p.zdistance:.2f}"
            )
            # Command is one-shot — reset after publish
            self._flight_command = FlightCommand.NONE
        except Exception as e:
            self.logger.warning(f"Action publish failed: {e}")


def main():
    node = ControlNode(level=logging.DEBUG)
    node.run()


if __name__ == "__main__":
    main()
