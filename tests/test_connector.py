import contextlib
import os
import struct
import unittest

from standalone_google_meet.config import ConnectorConfig
from standalone_google_meet.mocap_manager import MocapManager
from standalone_google_meet.protocol import decode_message, decode_participant_audio

REQUIRED_ENVIRONMENT = {
    "MEETING_URL": "https://meet.google.com/test",
    "LIVEKIT_URL": "ws://localhost:7880",
    "LIVEKIT_API_KEY": "key",
    "LIVEKIT_API_SECRET": "secret",
    "LIVEKIT_ROOM": "room",
}


@contextlib.contextmanager
def environment(**values):
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update({key: value for key, value in values.items() if value is not None})
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class ConnectorTests(unittest.TestCase):
    def test_config_rejects_two_source_selectors(self):
        values = {
            "MEETING_URL": "https://meet.google.com/test",
            "LIVEKIT_URL": "ws://localhost:7880",
            "LIVEKIT_API_KEY": "key",
            "LIVEKIT_API_SECRET": "secret",
            "LIVEKIT_ROOM": "room",
            "LIVEKIT_SOURCE_IDENTITY": "assistant",
            "LIVEKIT_SOURCE_PUBLISH_ON_BEHALF": "assistant",
        }
        previous = {key: os.environ.get(key) for key in values}
        try:
            os.environ.update(values)
            with self.assertRaisesRegex(ValueError, "only one"):
                ConnectorConfig.from_environment()
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


    def test_humanized_is_the_default_interaction_mode(self):
        # Robotic WebDriver clicks get anonymous joins blocked by Google, so the humanized
        # X11 input path has to stay the default.
        with environment(**REQUIRED_ENVIRONMENT, UI_INTERACTION_MODE=None):
            os.environ.pop("UI_INTERACTION_MODE", None)
            self.assertEqual(ConnectorConfig.from_environment().ui_interaction_mode, "humanized")

    def test_config_rejects_unknown_interaction_mode(self):
        with environment(**REQUIRED_ENVIRONMENT, UI_INTERACTION_MODE="clicky"):
            with self.assertRaisesRegex(ValueError, "UI_INTERACTION_MODE"):
                ConnectorConfig.from_environment()

    def test_generated_trajectory_lands_inside_the_target_rect(self):
        manager = MocapManager(video_frame_size=(1280, 720))
        sequence = manager.find_random_sequence_landing_in_rect(100, 100, 600, 400, 700, 450)
        self.assertIsNotNone(sequence)
        self.assertTrue(600 <= 100 + sequence.total_dx <= 700)
        self.assertTrue(400 <= 100 + sequence.total_dy <= 450)
        self.assertGreater(len(sequence.movements), 1)

    def test_participant_audio_frame_decodes(self):
        payload = bytes([4]) + b"user" + struct.pack("<ff", 0.5, -0.5)
        audio = decode_participant_audio(payload)
        self.assertEqual(audio.participant_id, "user")
        self.assertEqual(len(audio.pcm_float32), 8)


    def test_short_websocket_frame_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "4-byte"):
            decode_message(b"\x01")


if __name__ == "__main__":
    unittest.main()
