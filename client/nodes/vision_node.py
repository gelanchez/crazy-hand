import logging
import time

import iceoryx2
import numpy as np
from pathlib import Path

import mediapipe as mp
from mediapipe.tasks import python
from mediapipe.tasks.python import vision

from client.common.blackboards import CONFIG
from client.common.constants import (
    EventId,
    IMAGE_HEIGHT,
    IMAGE_WIDTH,
    ServiceName,
)
from client.common.node import Node
from client.common.payloads import ImageData


NODE_NAME = "vision_node"
MODEL_PATH = Path("./data/gesture_recognizer.task")


class VisionNode(Node):
    def __init__(self, level=logging.DEBUG):
        super().__init__(NODE_NAME, level=level)

        # =========================================================
        # MEDIAPIPE CONFIG
        # =========================================================
        base_options = python.BaseOptions(model_asset_path=str(MODEL_PATH))

        options = vision.GestureRecognizerOptions(
            base_options=base_options,
            running_mode=vision.RunningMode.IMAGE,
            num_hands=1,
            min_hand_detection_confidence=0.5,
            min_hand_presence_confidence=0.5,
            min_tracking_confidence=0.5,
        )

        self.recognizer = vision.GestureRecognizer.create_from_options(options)

        # =========================================================
        # GESTURE FILTERING PARAMETERS
        # =========================================================
        self.confidence_threshold = 0.65          # ignore weak predictions
        self.debounce_ms = 300                    # stable time required
        self.hysteresis_ms = 200                  # prevents fast switching back

        # state tracking
        self.current_candidate = None
        self.candidate_start_time = 0

        self.confirmed_gesture = None
        self.last_confirm_time = 0

        # optional smoothing history
        self.gesture_history = []

        # image size
        self.img_size = IMAGE_HEIGHT * IMAGE_WIDTH

    # =========================================================
    # STABLE GESTURE RESOLUTION
    # =========================================================
    def _update_gesture(self, candidate, confidence):
        now = time.time() * 1000  # ms

        # -------------------------
        # CONFIDENCE FILTER
        # -------------------------
        if confidence is None or confidence < self.confidence_threshold:
            candidate = "NONE"

        # -------------------------
        # NEW CANDIDATE
        # -------------------------
        if candidate != self.current_candidate:
            self.current_candidate = candidate
            self.candidate_start_time = now

        elapsed = now - self.candidate_start_time

        # -------------------------
        # DEBOUNCE (STABILITY CHECK)
        # -------------------------
        if elapsed >= self.debounce_ms:

            # -------------------------
            # HYSTERESIS CHECK
            # -------------------------
            if self.confirmed_gesture != candidate:
                if (now - self.last_confirm_time) < self.hysteresis_ms:
                    return self.confirmed_gesture  # block fast switching

                self.confirmed_gesture = candidate
                self.last_confirm_time = now

                self.logger.info(
                    f"[CONFIRMED] Gesture: {candidate} "
                    f"({confidence:.2f if confidence else 0.0}) "
                    f"stable for {int(elapsed)}ms"
                )

        return self.confirmed_gesture

    # =========================================================
    # MAIN LOOP
    # =========================================================
    def run(self):
        self.image_port = self.create_subscriber(
            ServiceName.IMAGE,
            ImageData,
            EventId.IMAGE_READY,
        )

        if self.image_port.subscriber is None:
            return

        self.blackboard_reader = self.create_blackboard_reader("/config", CONFIG)
        if self.blackboard_reader is None:
            return

        try:
            while self.running:
                event_id = self.image_port.listener.timed_wait_one(iceoryx2.Duration.from_millis(500))

                if event_id != self.image_port.event:
                    continue

                sample = self.image_port.subscriber.receive()
                if sample is None:
                    continue

                try:
                    if not self.blackboard_read(self.blackboard_reader, "process_images"):
                        continue

                    # ======================================================
                    # ICEORYX SAFE COPY
                    # ======================================================
                    payload = sample.payload()
                    raw_ptr = payload.contents.pixels

                    pixels = np.frombuffer(
                        np.ctypeslib.as_array(raw_ptr),
                        dtype=np.uint8,
                        count=self.img_size,
                    ).copy()

                finally:
                    del sample

                # ======================================================
                # IMAGE PREP
                # ======================================================
                pixels = pixels.reshape((IMAGE_HEIGHT, IMAGE_WIDTH))

                pixels = np.stack((pixels,) * 3, axis=-1)

                mp_image = mp.Image(
                    image_format=mp.ImageFormat.SRGB,
                    data=pixels.astype(np.uint8),
                )

                # ======================================================
                # INFERENCE
                # ======================================================
                results = self.recognizer.recognize(mp_image)

                # ======================================================
                # HAND CHECK
                # ======================================================
                if not results.hand_landmarks:
                    self.logger.info("No hand detected")
                    continue

                landmarks = results.hand_landmarks[0]

                xs = [lm.x for lm in landmarks]
                ys = [lm.y for lm in landmarks]

                cx = sum(xs) / len(xs)
                cy = sum(ys) / len(ys)

                pixel_x = int(cx * IMAGE_WIDTH)
                pixel_y = int(cy * IMAGE_HEIGHT)

                # ======================================================
                # GESTURE EXTRACTION
                # ======================================================
                gesture_name = None
                confidence = None

                if results.gestures:
                    top = results.gestures[0][0]
                    gesture_name = top.category_name
                    confidence = float(top.score)

                # ======================================================
                # FILTERED + STABLE GESTURE
                # ======================================================
                stable_gesture = self._update_gesture(
                    gesture_name,
                    confidence,
                )

                # ======================================================
                # LOGGING
                # ======================================================
                self.logger.info(
                    f"Hand @ ({pixel_x},{pixel_y}) | "
                    f"Raw: {gesture_name} ({confidence}) | "
                    f"Stable: {stable_gesture}"
                )

        except (iceoryx2.NodeWaitFailure, iceoryx2.ListenerWaitError):
            pass

        finally:
            self.logger.info(f"{NODE_NAME} shut down")


def main():
    node = VisionNode()
    node.run()


if __name__ == "__main__":
    main()