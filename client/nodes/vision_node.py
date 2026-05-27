import ctypes
import logging
import time
from pathlib import Path

import cv2
import iceoryx2
import mediapipe as mp
import numpy as np
from mediapipe.tasks import python
from mediapipe.tasks.python import vision

from client.common.blackboards import CONFIG
from client.common.constants import (
    GESTURE_DEBOUNCE_MS,
    GESTURE_HYSTERESIS_MS,
    GESTURE_MIN_CONFIDENCE,
    GESTURE_THRESHOLD,
    IMAGE_HEIGHT,
    IMAGE_SIZE,
    IMAGE_WIDTH,
    EventId,
    ServiceName,
)
from client.common.node import Node
from client.common.payloads import ImageData, PerceptionData

NODE_NAME = "vision_node"
MODEL_PATH = Path("./data/gesture_recognizer.task")


def to_c_char_array(value: str, size: int = 32) -> bytes:
    """
    Convert Python string to fixed-size null-padded bytes
    suitable for ctypes.c_char * size fields.
    """
    encoded = value.encode("utf-8")[: size - 1]
    return encoded + b"\x00" * (size - len(encoded))


class VisionNode(Node):
    def __init__(self, level=logging.DEBUG):
        super().__init__(NODE_NAME, level=level)

        # --- MediaPipe config ---
        base_options = python.BaseOptions(model_asset_path=str(MODEL_PATH))

        options = vision.GestureRecognizerOptions(
            base_options=base_options,
            running_mode=vision.RunningMode.VIDEO,
            num_hands=1,
            min_hand_detection_confidence=GESTURE_MIN_CONFIDENCE,
            min_hand_presence_confidence=GESTURE_MIN_CONFIDENCE,
            min_tracking_confidence=GESTURE_MIN_CONFIDENCE,
        )

        self.recognizer = vision.GestureRecognizer.create_from_options(options)
        self._last_timestamp_ms: int = -1  # guard for VIDEO mode monotonic requirement

        # --- Gesture filtering parameters ---
        self.confidence_threshold = GESTURE_THRESHOLD
        self.debounce_ms = GESTURE_DEBOUNCE_MS
        self.hysteresis_ms = GESTURE_HYSTERESIS_MS

        # state tracking
        self.current_candidate = None
        self.candidate_start_time = 0

        self.confirmed_gesture = "NONE"
        self.last_confirm_time = 0

    # --- Stable gesture resolution ---
    def _update_gesture(self, candidate, confidence):
        now = time.time() * 1000  # ms

        # --- Confidence filter ---
        if confidence is None or confidence < self.confidence_threshold:
            candidate = "NONE"

        # --- New candidate ---
        if candidate != self.current_candidate:
            self.current_candidate = candidate
            self.candidate_start_time = now

        elapsed = now - self.candidate_start_time

        # --- Debounce (stability check) ---
        if elapsed >= self.debounce_ms:
            # --- Hysteresis check ---
            if self.confirmed_gesture != candidate:
                if (now - self.last_confirm_time) < self.hysteresis_ms:
                    return self.confirmed_gesture  # block fast switching

                self.confirmed_gesture = candidate
                self.last_confirm_time = now

                self.logger.info(
                    f"[CONFIRMED] Gesture: {candidate} "
                    f"({confidence if confidence is not None else 0.0:.2f}) "
                    f"stable for {int(elapsed)}ms"
                )

        return self.confirmed_gesture

    def run(self):
        self.image_port = self.create_subscriber(
            ServiceName.IMAGE,
            ImageData,
            EventId.IMAGE_READY,
        )

        self.perception_port = self.create_publisher(
            ServiceName.PERCEPTION,
            PerceptionData,
            EventId.PERCEPTION_READY,
        )

        if self.image_port.subscriber is None:
            return

        self.blackboard_reader = self.create_blackboard_reader("/config", CONFIG)
        if self.blackboard_reader is None:
            return

        try:
            while self.running:
                event_id = self.image_port.listener.timed_wait_one(
                    iceoryx2.Duration.from_millis(500)
                )

                if event_id != self.image_port.event:
                    continue

                # Drain the queue: discard stale frames, keep only the latest.
                # This prevents compounding lag when inference is slower than camera FPS.
                sample = None
                while True:
                    new_sample = self.image_port.subscriber.receive()
                    if new_sample is None:
                        break
                    if sample is not None:
                        del sample  # discard stale frame
                    sample = new_sample

                if sample is None:
                    continue

                try:
                    if not self.blackboard_read(
                        self.blackboard_reader, "process_images"
                    ):
                        continue

                    # --- Iceoryx safe copy ---
                    payload = sample.payload()
                    raw_ptr = payload.contents.pixels
                    img_id = payload.contents.id
                    img_timestamp = payload.contents.timestamp

                    pixels = np.frombuffer(
                        np.ctypeslib.as_array(raw_ptr),
                        dtype=np.uint8,
                        count=IMAGE_SIZE,
                    ).reshape(IMAGE_HEIGHT, IMAGE_WIDTH).copy()

                finally:
                    del sample

                # --- Image prep ---
                # cv2.COLOR_GRAY2RGB: single C++ call, output already contiguous
                pixels = cv2.cvtColor(pixels, cv2.COLOR_GRAY2RGB)

                mp_image = mp.Image(
                    image_format=mp.ImageFormat.SRGB,
                    data=pixels,
                )

                # --- Inference ---
                # VIDEO mode: guard against non-monotonic timestamps
                if img_timestamp <= self._last_timestamp_ms:
                    self.logger.debug(
                        f"Non-monotonic timestamp {img_timestamp} <= "
                        f"{self._last_timestamp_ms}, skipping frame"
                    )
                    continue
                self._last_timestamp_ms = img_timestamp
                results = self.recognizer.recognize_for_video(mp_image, img_timestamp)

                # --- Hand detection ---
                pixel_x, pixel_y = 0, 0
                if results.hand_landmarks:
                    landmarks = results.hand_landmarks[0]

                    coords = np.array([(lm.x, lm.y) for lm in landmarks])
                    cx, cy = coords.mean(axis=0)

                    pixel_x = int(cx * IMAGE_WIDTH)
                    pixel_y = int(cy * IMAGE_HEIGHT)

                # --- Gesture extraction ---
                gesture_name = None
                confidence = None

                if results.gestures:
                    top = results.gestures[0][0]
                    # Open_Palm detection is unreliable — ignore it
                    if top.category_name != "Open_Palm":
                        gesture_name = top.category_name
                        confidence = float(top.score)

                # --- Stable gesture ---
                stable_gesture = self._update_gesture(
                    gesture_name,
                    confidence,
                )

                # --- Logging and overlay ---
                if results.hand_landmarks:
                    self.logger.debug(
                        f"Hand @ ({pixel_x},{pixel_y}) | "
                        f"Raw: {gesture_name} ({confidence}) | "
                        f"Stable: {stable_gesture}"
                    )

                    # Draw on the RGB pixels array.
                    for lm in landmarks:
                        px = int(lm.x * IMAGE_WIDTH)
                        py = int(lm.y * IMAGE_HEIGHT)
                        cv2.circle(
                            pixels, (px, py), 2, (255, 0, 0), -1
                        )  # Red dots for joints

                    cv2.circle(
                        pixels, (pixel_x, pixel_y), 5, (0, 255, 0), -1
                    )  # Green dot for center

                    label = (
                        stable_gesture
                        if stable_gesture != "NONE"
                        else (gesture_name or "")
                    )
                    if label and label != "NONE":
                        text = f"{label} ({confidence or 0.0:.2f})"
                        cv2.putText(
                            pixels,
                            text,
                            (10, 30),
                            cv2.FONT_HERSHEY_SIMPLEX,
                            0.7,
                            (0, 255, 0),  # Green text
                            2,
                            cv2.LINE_AA,
                        )
                else:
                    self.logger.debug("No hand detected")
                    cv2.putText(
                        pixels,
                        "No hand detected",
                        (10, 30),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.7,
                        (0, 0, 255),  # Red text
                        2,
                        cv2.LINE_AA,
                    )

                # --- Publish perception data ---
                perc_sample = self.perception_port.publisher.loan_uninit()
                if perc_sample is not None:
                    data = perc_sample.payload()
                    data.contents.id = img_id
                    data.contents.timestamp = img_timestamp

                    if results.hand_landmarks:
                        data.contents.hand_detected = True
                        data.contents.hand_x = pixel_x
                        data.contents.hand_y = pixel_y
                        data.contents.gesture_name = to_c_char_array(stable_gesture)
                        data.contents.gesture_confidence = (
                            confidence if confidence else 0.0
                        )
                    else:
                        data.contents.hand_detected = False
                        data.contents.hand_x = 0
                        data.contents.hand_y = 0
                        data.contents.gesture_name = to_c_char_array("NONE")
                        data.contents.gesture_confidence = 0.0

                    processed_flat = pixels.flatten()
                    ctypes.memmove(
                        data.contents.processed_pixels,
                        processed_flat.ctypes.data_as(ctypes.POINTER(ctypes.c_ubyte)),
                        len(processed_flat),
                    )

                    perc_sample.assume_init().send()
                    self.perception_port.notifier.notify_with_custom_event_id(
                        self.perception_port.event
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
