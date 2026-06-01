"""Flight state machine that translates keyboard commands and perception data into ActionData setpoints."""

import logging
import time

import iceoryx2

from client.common.blackboards import CONFIG
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
    MOTOR_TEST_DURATION,
    MOTOR_TEST_THRUST,
    SPEED_FACTOR,
    TRACKING_ALT_SCALE,
    TRACKING_DEADZONE_PX,
    TRACKING_DISTANCE,
    TRACKING_DISTANCE_SCALE,
    TRACKING_EMA_ALPHA,
    TRACKING_HAND_SPAN_AT_1M,
    TRACKING_LOSS_FRAMES,
    TRACKING_MAX_SPEED,
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
from client.common.utils import EMAFilter

NODE_NAME = "control_node"


class ControlNode(Node):
    """ROS-style node that owns the flight state machine and publishes ActionData each control cycle."""

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

        # EMA (Exponential Moving Average) — smooths jittery hand pixel coords before mapping to velocity
        self._ema_x = EMAFilter(TRACKING_EMA_ALPHA)
        self._ema_y = EMAFilter(TRACKING_EMA_ALPHA)

        # Detection-loss debounce
        self._no_hand_frames: int = 0

        # Source of the last landing command (for action logging)
        self._landing_source: ActionSource = ActionSource.KEYBOARD

        # Motor test state
        self._motor_test_end: float = 0.0
        self._thrust: int = 0

        self._estimated_distance: float = 0.0
        self._last_gesture: str = "NONE"

        # True while the corresponding arrow key is held — blocks tracking from overriding that axis
        self._key_override_vx: bool = False
        self._key_override_vy: bool = False

        self.blackboard_reader = None

    def _bb(self, key: str, fallback):
        """Read a tunable parameter from the shared blackboard, returning fallback if unavailable."""
        if self.blackboard_reader is None:
            return fallback
        return self.blackboard_read(self.blackboard_reader, key)

    def _handle_gesture(self, perception):
        """Dispatch a gesture command on its rising edge, ignoring repeated frames of the same gesture."""
        if not perception.hand_detected:
            self._last_gesture = "NONE"
            return

        gesture = perception.gesture_name.rstrip(b"\x00").decode("utf-8")

        if gesture == self._last_gesture:
            return  # rising-edge only — don't re-fire while same gesture held
        self._last_gesture = gesture

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

        elif gesture == "Thumb_Up" and self._state == FlightState.IDLE:
            self._hover["zdistance"] = DEFAULT_HEIGHT
            self._state = FlightState.TRACKING
            self._flight_command = FlightCommand.TAKEOFF
            self._publish_action(ActionSource.GESTURE)
            self.logger.info("TAKEOFF commanded by gesture")

        elif gesture == "Victory":
            if self._state == FlightState.AIRBORNE:
                self._state = FlightState.TRACKING
                self._flight_command = FlightCommand.TOGGLE_TRACKING
                self._hover["vx"] = self._hover["vy"] = 0.0
                self._publish_action(ActionSource.GESTURE)
                self.logger.info("TRACKING mode ON by gesture")
            elif self._state == FlightState.TRACKING:
                self._state = FlightState.AIRBORNE
                self._flight_command = FlightCommand.TOGGLE_TRACKING
                self._hover["vx"] = self._hover["vy"] = 0.0
                self._no_hand_frames = 0
                self._ema_x.reset()
                self._ema_y.reset()
                self._publish_action(ActionSource.GESTURE)
                self.logger.info("TRACKING mode OFF by gesture")

    def _apply_tracking(self, perception):
        """P-controller that maps EMA-smoothed hand position and span to lateral, altitude, and forward velocity."""
        if not self._bb("process_images", False):
            # process_images turned off while airborne in TRACKING — fall back to AIRBORNE
            self._state = FlightState.AIRBORNE
            self._hover["vx"] = self._hover["vy"] = self._hover["yawrate"] = 0.0
            self._flight_command = FlightCommand.TOGGLE_TRACKING
            self._publish_action()
            self.logger.info("process_images disabled — exited tracking mode")
            return

        if not perception.hand_detected:
            self._no_hand_frames += 1
            if self._no_hand_frames >= TRACKING_LOSS_FRAMES:
                # Hand truly lost — stop motion and reset EMA
                self._ema_x.reset()
                self._ema_y.reset()
                if not self._key_override_vx:
                    self._hover["vx"] = 0.0
                if not self._key_override_vy:
                    self._hover["vy"] = 0.0
                self._publish_action(ActionSource.TRACKING)
                self.logger.debug("Tracking: hand lost")
            return

        # Hand present — reset loss counter
        self._no_hand_frames = 0

        filtered_x = self._ema_x.update(perception.hand_x)
        filtered_y = self._ema_y.update(perception.hand_y)

        # Error from frame centre (positive = right / below centre)
        error_x = filtered_x - IMAGE_WIDTH / 2
        error_y = filtered_y - IMAGE_HEIGHT / 2

        speed_cap = self._bb("tracking_max_speed", TRACKING_MAX_SPEED)
        tracking_speed = self._bb("tracking_speed_scale", TRACKING_SPEED_SCALE)
        tracking_alt = self._bb("tracking_alt_scale", TRACKING_ALT_SCALE)
        max_alt = self._bb("max_altitude", MAX_ALTITUDE)
        min_alt = self._bb("min_altitude", MIN_ALTITUDE)

        # Lateral: error_x → vy (strafe)
        # NOTE: flip sign if drone moves wrong way
        vy = (
            max(-speed_cap, min(speed_cap, -tracking_speed * error_x))
            if abs(error_x) > TRACKING_DEADZONE_PX
            else 0.0
        )

        # Vertical: error_y → zdistance delta (hand above centre → go up)
        if abs(error_y) > TRACKING_DEADZONE_PX:
            new_alt = self._hover["zdistance"] - tracking_alt * error_y
            self._hover["zdistance"] = max(min_alt, min(max_alt, new_alt))

        tracking_distance = self._bb("tracking_distance", TRACKING_DISTANCE)
        if tracking_distance > 0 and perception.hand_span > 0:
            tracking_dist_scale = self._bb("tracking_distance_scale", TRACKING_DISTANCE_SCALE)
            hand_span_at_1m = self._bb("tracking_hand_span_at_1m", TRACKING_HAND_SPAN_AT_1M)
            self._estimated_distance = hand_span_at_1m / perception.hand_span
            error_d = self._estimated_distance - tracking_distance
            vx = max(-speed_cap, min(speed_cap, tracking_dist_scale * error_d))
        else:
            self._estimated_distance = 0.0
            vx = 0.0

        if not self._key_override_vx:
            self._hover["vx"] = vx
        if not self._key_override_vy:
            self._hover["vy"] = vy
        self._publish_action(ActionSource.TRACKING)

        self.logger.debug(
            f"Tracking: raw=({perception.hand_x},{perception.hand_y}) "
            f"ema=({filtered_x:.1f},{filtered_y:.1f}) "
            f"err=({error_x:.1f},{error_y:.1f}) "
            f"vy={vy:.2f} z={self._hover['zdistance']:.2f}"
        )

    def _process_perception(self):
        """Drain all pending perception samples and dispatch gesture handling and tracking updates."""
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
        """Decrement altitude at LAND_RATE each tick until LAND_CUTOFF is reached, then cut motors."""
        now = time.monotonic()
        dt = now - self._last_land_tick
        self._last_land_tick = now
        self._hover["zdistance"] = max(LAND_CUTOFF, self._hover["zdistance"] - LAND_RATE * dt)
        if self._hover["zdistance"] <= LAND_CUTOFF:
            self._flight_command = FlightCommand.LAND  # signal wifi_node to cut motors
            self._state = FlightState.IDLE
            self.logger.info("Landing complete — cutting motors")
        self._publish_action(self._landing_source)

    def _tick_motor_test(self):
        """End the motor-test sequence once its fixed duration has elapsed and return to IDLE."""
        if time.monotonic() >= self._motor_test_end:
            # Test done — one final publish to push state=IDLE back to wifi_node
            self._motor_test_end = 0.0
            self._thrust = 0
            self._state = FlightState.IDLE
            self._flight_command = FlightCommand.NONE
            self._publish_action()
            self.logger.info("Motor test complete")
        # else: still active — wifi_node spins motors based on flight_state=MOTOR_TESTING;
        # no repeated notifications needed (avoids flooding iceoryx2 listener queue)

    def _publish_action(self, source: ActionSource = ActionSource.KEYBOARD):
        """Build an ActionData payload from current hover state and publish it to the action service."""
        try:
            sample = self.action_port.publisher.loan_uninit()
            payload = sample.payload().contents
            payload.active = self._state in (
                FlightState.AIRBORNE,
                FlightState.TRACKING,
                FlightState.LANDING,
            )
            payload.command = int(self._flight_command)
            payload.state = int(self._state)
            payload.source = int(source)
            payload.vx, payload.vy, payload.yawrate, payload.zdistance = (
                self._hover["vx"],
                self._hover["vy"],
                self._hover["yawrate"],
                self._hover["zdistance"],
            )
            payload.ema_x = self._ema_x.value or 0.0
            payload.ema_y = self._ema_y.value or 0.0
            payload.estimated_distance = self._estimated_distance
            payload.thrust = self._thrust
            command_name = self._flight_command.name
            sample.assume_init().send()
            self._flight_command = FlightCommand.NONE  # one-shot — reset after successful send
            self.action_port.notifier.notify_with_custom_event_id(self.action_port.event)
            self.logger.debug(
                f"Action: state={self._state.name}, source={source.name}, command={command_name}, "
                f"vx={payload.vx:.2f}, vy={payload.vy:.2f}, yaw={payload.yawrate:.1f}, z={payload.zdistance:.2f}"
            )
        except Exception as e:
            self.logger.warning(f"Action publish failed: {e}")

    def _process_command(self):
        """Drain all pending keyboard commands, advance the flight state machine, and publish if anything changed."""
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

            speed_factor = self._bb("speed_factor", SPEED_FACTOR)
            fast_speed_factor = self._bb("fast_speed_factor", FAST_SPEED_FACTOR)
            yaw_rate = self._bb("yaw_rate", YAW_RATE)
            yaw_rate_fast = self._bb("yaw_rate_fast", YAW_RATE_FAST)
            max_alt = self._bb("max_altitude", MAX_ALTITUDE)
            min_alt = self._bb("min_altitude", MIN_ALTITUDE)
            speed = fast_speed_factor if shift else speed_factor
            yaw = yaw_rate_fast if shift else yaw_rate
            altitude_step = ALTITUDE_STEP_FAST if shift else ALTITUDE_STEP
            airborne = self._state in (FlightState.AIRBORNE, FlightState.TRACKING)

            match (is_pressed, key):
                # --- Takeoff / Land ---
                case (True, KeyCode.SPACE):
                    if self._state == FlightState.IDLE:
                        self._hover["zdistance"] = DEFAULT_HEIGHT
                        self._state = FlightState.TRACKING if self._bb("process_images", False) else FlightState.AIRBORNE
                        self._flight_command = FlightCommand.TAKEOFF
                        self.logger.info(f"TAKEOFF commanded → {self._state.name}")
                        changed = True
                    elif self._state == FlightState.LANDING:
                        self._state = FlightState.TRACKING
                        self._hover["zdistance"] = DEFAULT_HEIGHT
                        self.logger.info("Re-takeoff: cancelling landing, climbing to DEFAULT_HEIGHT")
                        changed = True
                    elif airborne:
                        self._state = FlightState.LANDING
                        self._last_land_tick = time.monotonic()
                        self._landing_source = ActionSource.KEYBOARD
                        self._hover["vx"] = self._hover["vy"] = self._hover["yawrate"] = 0.0
                        self.logger.info(f"LAND commanded from z={self._hover['zdistance']:.2f}m")
                        changed = True

                # --- Emergency stop ---
                case (True, KeyCode.ESC | KeyCode.WINDOW_CLOSED):
                    if self._state != FlightState.IDLE:
                        self.logger.warning("EMERGENCY STOP")
                    self._state = FlightState.IDLE
                    self._hover["vx"] = self._hover["vy"] = self._hover["yawrate"] = 0.0
                    self._hover["zdistance"] = DEFAULT_HEIGHT
                    self._flight_command = FlightCommand.EMERGENCY_STOP
                    self._key_override_vx = False
                    self._key_override_vy = False
                    changed = True

                # --- Toggle tracking ---
                case (True, KeyCode.T):
                    if self._state == FlightState.AIRBORNE and self._bb("process_images", False):
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
                        self._ema_x.reset()
                        self._ema_y.reset()
                        self.logger.info("TRACKING mode OFF")
                        changed = True

                # --- Movement (only when airborne) ---
                case (True, KeyCode.UP) if airborne:
                    self._key_override_vx = True
                    self._hover["vx"] = speed
                    changed = True
                case (True, KeyCode.DOWN) if airborne:
                    self._key_override_vx = True
                    self._hover["vx"] = -speed
                    changed = True
                case (True, KeyCode.LEFT) if airborne:
                    self._key_override_vy = True
                    self._hover["vy"] = speed
                    changed = True
                case (True, KeyCode.RIGHT) if airborne:
                    self._key_override_vy = True
                    self._hover["vy"] = -speed
                    changed = True
                case (True, KeyCode.A) if airborne:
                    self._hover["yawrate"] = yaw
                    changed = True
                case (True, KeyCode.D) if airborne:
                    self._hover["yawrate"] = -yaw
                    changed = True
                case (True, KeyCode.W) if airborne:
                    self._hover["zdistance"] = min(max_alt, self._hover["zdistance"] + altitude_step)
                    self.logger.info(f"Altitude → {self._hover['zdistance']:.2f}m")
                    changed = True
                case (True, KeyCode.S) if airborne:
                    self._hover["zdistance"] = max(min_alt, self._hover["zdistance"] - altitude_step)
                    self.logger.info(f"Altitude → {self._hover['zdistance']:.2f}m")
                    changed = True

                # --- Stabilise ---
                case (True, KeyCode.C) if airborne:
                    self._hover["vx"] = self._hover["vy"] = self._hover["yawrate"] = 0.0
                    self._key_override_vx = False
                    self._key_override_vy = False
                    self.logger.info("Stabilised")
                    changed = True

                # --- Motor test (ground only) ---
                case (True, KeyCode.M) if self._state == FlightState.IDLE:
                    self._state = FlightState.MOTOR_TESTING
                    self._motor_test_end = time.monotonic() + MOTOR_TEST_DURATION
                    self._thrust = MOTOR_TEST_THRUST
                    self._flight_command = FlightCommand.MOTOR_TEST
                    self.logger.info("MOTOR TEST commanded")
                    changed = True

                # --- Zero on release ---
                case (False, KeyCode.UP | KeyCode.DOWN):
                    self._key_override_vx = False
                    self._hover["vx"] = 0.0
                    changed = True
                case (False, KeyCode.LEFT | KeyCode.RIGHT):
                    self._key_override_vy = False
                    self._hover["vy"] = 0.0
                    changed = True
                case (False, KeyCode.A | KeyCode.D):
                    self._hover["yawrate"] = 0.0
                    changed = True

        if changed:
            self._publish_action()

    def run(self):
        self.logger.info(f"{NODE_NAME} running")

        self.command_port = self.create_subscriber(ServiceName.COMMAND, CommandData, EventId.COMMAND_READY)
        self.perception_port = self.create_subscriber(ServiceName.PERCEPTION, PerceptionData, EventId.PERCEPTION_READY)
        self.action_port = self.create_publisher(ServiceName.ACTION, ActionData, EventId.ACTION_READY)

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

        self.blackboard_reader = self.create_blackboard_reader("/config", CONFIG)

        # TODO: Replace timed_wait_one workaround with WaitSet once the
        # iceoryx2 spinning bug is fixed (see GitHub issue in thesis/Iceoryx2.md).
        # Intended WaitSet code (2 attachments — command, perception):
        #
        #   waitset = iceoryx2.WaitSetBuilder.new().create(iceoryx2.ServiceType.Ipc)
        #   command_guard    = waitset.attach_notification(self.command_port.listener)
        #   perception_guard = waitset.attach_notification(self.perception_port.listener)
        #   while self.running:
        #       ids, result = waitset.wait_and_process_with_timeout(Duration.from_millis(50))
        #       for event_id in ids:
        #           if event_id.has_event_from(command_guard): self._process_command()
        #           elif event_id.has_event_from(perception_guard): self._process_perception()
        #       if self._state == FlightState.LANDING: self._tick_landing()
        #   finally: command_guard.delete(); perception_guard.delete(); waitset.delete()
        #
        # Workaround: WaitSet spins at 100% CPU after any listener receives its first
        # notification — even with 2 attachments and a 50ms timeout (confirmed v0.9.0–v0.9.1).
        try:
            while self.running:
                # Block up to 50ms waiting for a perception event
                event_id = self.perception_port.listener.timed_wait_one(iceoryx2.Duration.from_millis(50))
                if event_id == self.perception_port.event:
                    self._process_perception()

                # Non-blocking drain of command subscriber each iteration
                self._process_command()

                if self._state == FlightState.LANDING:
                    self._tick_landing()
                elif self._state == FlightState.MOTOR_TESTING:
                    self._tick_motor_test()

        except KeyboardInterrupt:
            pass
        except (iceoryx2.NodeWaitFailure, iceoryx2.ListenerWaitError) as e:
            self.logger.warning(f"iceoryx2 wait interrupted: {e}")
        except Exception as e:
            self.logger.error(f"ControlNode error: {e}", exc_info=True)
        finally:
            self.stop()
            self.logger.info(f"{NODE_NAME} shut down")


def main():
    node = ControlNode(level=logging.DEBUG)
    node.run()


if __name__ == "__main__":
    main()
