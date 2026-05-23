import logging
import time

import iceoryx2

from client.common.constants import (
    ALTITUDE_STEP,
    ALTITUDE_STEP_FAST,
    DEFAULT_HEIGHT,
    FAST_SPEED_FACTOR,
    IMAGE_HEIGHT,
    IMAGE_WIDTH,
    LAND_CUTOFF,
    LAND_RATE,
    MAX_ALTITUDE,
    MIN_ALTITUDE,
    SPEED_FACTOR,
    TRACKING_ALT_SCALE,
    TRACKING_DEADZONE_PX,
    TRACKING_EMA_ALPHA,
    TRACKING_LOSS_FRAMES,
    TRACKING_SPEED_SCALE,
    YAW_RATE,
    YAW_RATE_FAST,
    ActionSource,
    EventId,
    FlightCommand,
    FlightState,
    KeyCode,
    ServiceName,
)
from client.common.node import Node
from client.common.payloads import ActionData, CommandData, PerceptionData

NODE_NAME = "control_node"


class ControlNode(Node):
    def __init__(self, level=logging.INFO):
        super().__init__(NODE_NAME, level=level)
        self._state = FlightState.IDLE
        self._flight_command = FlightCommand.NONE
        self._last_land_tick: float = 0.0
        self._hover = {
            "vx": 0.0,
            "vy": 0.0,
            "yawrate": 0.0,
            "zdistance": DEFAULT_HEIGHT,
        }

        # EMA state for hand position
        self._ema_x: float | None = None
        self._ema_y: float | None = None

        # Detection-loss debounce
        self._no_hand_frames: int = 0

        # Source of the last landing command (for action logging)
        self._landing_source: ActionSource = ActionSource.KEYBOARD

    def _update_ema(self, x: int, y: int) -> tuple[float, float]:
        """Apply EMA smoothing to raw hand position. Returns filtered (x, y)."""
        if self._ema_x is None:
            # First sample — seed with raw value
            self._ema_x = float(x)
            self._ema_y = float(y)
        else:
            self._ema_x = (
                TRACKING_EMA_ALPHA * x + (1.0 - TRACKING_EMA_ALPHA) * self._ema_x
            )
            self._ema_y = (
                TRACKING_EMA_ALPHA * y + (1.0 - TRACKING_EMA_ALPHA) * self._ema_y
            )
        return self._ema_x, self._ema_y

    def _handle_gesture(self, perception):
        if not perception.hand_detected:
            return

        gesture = perception.gesture_name.rstrip(b"\x00").decode("utf-8")

        if gesture == "Thumb_Down" and self._state in (
            FlightState.AIRBORNE,
            FlightState.TRACKING,
        ):
            self._state = FlightState.LANDING
            self._last_land_tick = time.monotonic()
            self._landing_source = ActionSource.GESTURE
            self._hover["vx"] = self._hover["vy"] = self._hover["yawrate"] = 0.0
            self._publish_action(ActionSource.GESTURE)
            self.logger.info("LAND commanded by gesture")

    def _apply_tracking(self, perception):
        if not perception.hand_detected:
            self._no_hand_frames += 1
            if self._no_hand_frames >= TRACKING_LOSS_FRAMES:
                # Hand truly lost — stop motion and reset EMA
                self._ema_x = None
                self._ema_y = None
                self._hover["vx"] = 0.0
                self._hover["vy"] = 0.0
                self._publish_action(ActionSource.TRACKING)
                self.logger.debug("Tracking: hand lost")
            return

        # Hand present — reset loss counter
        self._no_hand_frames = 0

        filtered_x, filtered_y = self._update_ema(perception.hand_x, perception.hand_y)

        # Error from frame centre (positive = right / below centre)
        error_x = filtered_x - IMAGE_WIDTH / 2
        error_y = filtered_y - IMAGE_HEIGHT / 2

        # Lateral: error_x → vy (strafe)
        # NOTE: flip sign if drone moves wrong way
        vy = (
            max(-SPEED_FACTOR, min(SPEED_FACTOR, -TRACKING_SPEED_SCALE * error_x))
            if abs(error_x) > TRACKING_DEADZONE_PX
            else 0.0
        )

        # Vertical: error_y → zdistance delta (hand above centre → go up)
        if abs(error_y) > TRACKING_DEADZONE_PX:
            new_z = self._hover["zdistance"] - TRACKING_ALT_SCALE * error_y
            self._hover["zdistance"] = max(MIN_ALTITUDE, min(MAX_ALTITUDE, new_z))

        self._hover["vx"] = 0.0
        self._hover["vy"] = vy
        self._publish_action(ActionSource.TRACKING)

        self.logger.debug(
            f"Tracking: raw=({perception.hand_x},{perception.hand_y}) "
            f"ema=({filtered_x:.1f},{filtered_y:.1f}) "
            f"err=({error_x:.1f},{error_y:.1f}) "
            f"vy={vy:.2f} z={self._hover['zdistance']:.2f}"
        )

    def _process_perception(self):
        while True:
            try:
                sample = self.perception_port.subscriber.receive()
            except Exception as e:
                self.logger.warning(f"Perception receive error: {e}")
                break

            if sample is None:
                break

            perception = sample.payload().contents

            # Gestures active regardless of flight state
            self._handle_gesture(perception)

            if self._state == FlightState.TRACKING:
                self._apply_tracking(perception)

            del sample

    def _tick_landing(self):
        now = time.monotonic()
        dt = now - self._last_land_tick
        self._last_land_tick = now
        self._hover["zdistance"] = max(
            LAND_CUTOFF, self._hover["zdistance"] - LAND_RATE * dt
        )
        if self._hover["zdistance"] <= LAND_CUTOFF:
            self._flight_command = FlightCommand.LAND  # signal wifi_node to cut motors
            self._state = FlightState.IDLE
            self.logger.info("Landing complete — cutting motors")
        self._publish_action(self._landing_source)

    def _publish_action(self, source: ActionSource = ActionSource.KEYBOARD):
        try:
            sample = self.action_port.publisher.loan_uninit()
            p = sample.payload().contents
            p.active = self._state != FlightState.IDLE
            p.command = int(self._flight_command)
            p.state = int(self._state)
            p.source = int(source)
            p.vx, p.vy, p.yawrate, p.zdistance = (
                self._hover["vx"],
                self._hover["vy"],
                self._hover["yawrate"],
                self._hover["zdistance"],
            )
            p.ema_x = self._ema_x if self._ema_x is not None else 0.0
            p.ema_y = self._ema_y if self._ema_y is not None else 0.0
            cmd_name = self._flight_command.name
            sample.assume_init().send()
            self._flight_command = (
                FlightCommand.NONE
            )  # one-shot — reset after successful send
            self.action_port.notifier.notify_with_custom_event_id(
                self.action_port.event
            )
            self.logger.debug(
                f"Action: state={self._state.name}, src={source.name}, cmd={cmd_name}, "
                f"vx={p.vx:.2f}, vy={p.vy:.2f}, yaw={p.yawrate:.1f}, z={p.zdistance:.2f}"
            )
        except Exception as e:
            self.logger.warning(f"Action publish failed: {e}")

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
                        self._hover["zdistance"] = DEFAULT_HEIGHT
                        self._state = FlightState.AIRBORNE
                        self._flight_command = FlightCommand.TAKEOFF
                        self.logger.info("TAKEOFF commanded")
                        changed = True
                    elif self._state == FlightState.LANDING:
                        self._state = FlightState.AIRBORNE
                        self._hover["zdistance"] = DEFAULT_HEIGHT
                        self.logger.info(
                            "Re-takeoff: cancelling landing, climbing to DEFAULT_HEIGHT"
                        )
                        changed = True
                    elif airborne:
                        self._state = FlightState.LANDING
                        self._last_land_tick = time.monotonic()
                        self._landing_source = ActionSource.KEYBOARD
                        self._hover["vx"] = self._hover["vy"] = self._hover[
                            "yawrate"
                        ] = 0.0
                        self.logger.info(
                            f"LAND commanded from z={self._hover['zdistance']:.2f}m"
                        )
                        changed = True

                # --- Emergency stop ---
                case (True, KeyCode.ESC | KeyCode.WINDOW_CLOSED):
                    if self._state != FlightState.IDLE:
                        self.logger.warning("EMERGENCY STOP")
                    self._state = FlightState.IDLE
                    self._hover["vx"] = self._hover["vy"] = self._hover["yawrate"] = 0.0
                    self._hover["zdistance"] = DEFAULT_HEIGHT
                    self._flight_command = FlightCommand.EMERGENCY_STOP
                    changed = True

                # --- Toggle tracking ---
                case (True, KeyCode.T):
                    if self._state == FlightState.AIRBORNE:
                        self._state = FlightState.TRACKING
                        self._flight_command = FlightCommand.TOGGLE_TRACKING
                        self._hover["vx"] = 0.0
                        self._hover["vy"] = 0.0
                        self.logger.info("TRACKING mode ON")
                        changed = True
                    elif self._state == FlightState.TRACKING:
                        self._state = FlightState.AIRBORNE
                        self._flight_command = FlightCommand.TOGGLE_TRACKING
                        self._hover["vx"] = 0.0
                        self._hover["vy"] = 0.0
                        self._no_hand_frames = 0
                        self._ema_x = None
                        self._ema_y = None
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
                    self._hover["zdistance"] = min(
                        MAX_ALTITUDE, self._hover["zdistance"] + alt_step
                    )
                    self.logger.info(f"Altitude → {self._hover['zdistance']:.2f}m")
                    changed = True
                case (True, KeyCode.S) if airborne:
                    self._hover["zdistance"] = max(
                        MIN_ALTITUDE, self._hover["zdistance"] - alt_step
                    )
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
                    iceoryx2.Duration.from_millis(50)
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

                if self._state == FlightState.LANDING:
                    self._tick_landing()

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


def main():
    node = ControlNode(level=logging.DEBUG)
    node.run()


if __name__ == "__main__":
    main()
