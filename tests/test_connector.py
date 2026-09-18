import os
import struct
import unittest

from standalone_google_meet.config import ConnectorConfig
from standalone_google_meet.protocol import decode_message, decode_participant_audio


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
