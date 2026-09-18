import asyncio
import contextlib
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from standalone_google_meet.api import control
from standalone_google_meet.browser.protocol import decode_message
from standalone_google_meet.chrome.mocap_manager import MocapManager
from standalone_google_meet.config import (
    ConnectorRuntimeConfig,
    MeetingJobConfig,
)
from standalone_google_meet.livekit.audio_sync import LiveKitAudioSync
from standalone_google_meet.livekit.lifecycle import ConnectorLifecycle

REQUIRED_ENVIRONMENT = {
    "LIVEKIT_URL": "ws://localhost:7880",
    "LIVEKIT_API_KEY": "key",
    "LIVEKIT_API_SECRET": "secret",
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


class FakeParticipant:
    def __init__(self):
        self.messages = []
        self.attributes = {}
        self.published = []
        self.unpublished = []

    async def publish_data(self, payload, *, reliable, topic):
        self.messages.append((payload, reliable, topic))

    async def set_attributes(self, attributes):
        self.attributes.update(attributes)

    async def publish_track(self, track, _options):
        self.published.append(track)
        return SimpleNamespace(sid="track-sid")

    async def unpublish_track(self, track_sid):
        self.unpublished.append(track_sid)


class FakeRoom:
    def __init__(self):
        self.local_participant = FakeParticipant()

class FakeResponse:
    is_error = False

    def json(self):
        return {"success": True, "data": {"room_name": "room"}}


class FakeHttpClient:
    request = None

    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, path, *, json, headers):
        self.request = (path, json, headers)
        FakeHttpClient.request = self.request
        return FakeResponse()


class ConnectorTests(unittest.IsolatedAsyncioTestCase):
    def test_runtime_config_does_not_require_a_meeting_or_room(self):
        with environment(**REQUIRED_ENVIRONMENT):
            self.assertEqual(
                ConnectorRuntimeConfig.from_environment().livekit_url,
                "ws://localhost:7880",
            )

    def test_runtime_config_rejects_invalid_interaction_mode(self):
        with environment(**REQUIRED_ENVIRONMENT, UI_INTERACTION_MODE="clicky"):
            with self.assertRaisesRegex(ValueError, "UI_INTERACTION_MODE"):
                ConnectorRuntimeConfig.from_environment()

    def test_job_metadata_requires_a_google_meet_request(self):
        with self.assertRaisesRegex(ValueError, "call_type"):
            MeetingJobConfig.from_metadata({})

        with self.assertRaisesRegex(ValueError, "google_meet"):
            MeetingJobConfig.from_metadata(
                {
                    "call_type": "meeting",
                    "platform": "zoom",
                    "meeting_url": "https://meet.google.com/test",
                }
            )

    def test_job_metadata_defaults_display_name(self):
        config = MeetingJobConfig.from_metadata(
            {
                "call_type": "meeting",
                "platform": "google_meet",
                "meeting_url": "https://meet.google.com/test",
            }
        )
        self.assertEqual(config.bot_display_name, "LiveKit Assistant")

    def test_worker_entrypoint_imports(self):
        from standalone_google_meet.livekit.worker import entrypoint

        self.assertEqual(entrypoint.__name__, "entrypoint")

    async def test_ready_lifecycle_event_sets_recoverable_attribute(self):
        participant = FakeParticipant()
        lifecycle = ConnectorLifecycle(participant)

        await lifecycle.publish("ready")
        await lifecycle.publish("ready")

        self.assertEqual(participant.attributes, {"lk.meeting_connector_status": "ready"})
        self.assertEqual(len(participant.messages), 1)
        payload, reliable, topic = participant.messages[0]
        self.assertIn('"event": "ready"', payload)
        self.assertTrue(reliable)
        self.assertEqual(topic, "meeting_connector_events")

    async def test_control_backend_forwards_join_to_core(self):
        request = control.JoinMeetingRequest(
            assistant_id="assistant-id",
            meeting_url="https://meet.google.com/test",
        )
        with environment(CORE_API_URL="http://core", CORE_API_KEY="core-key"):
            with patch("standalone_google_meet.api.control.httpx.AsyncClient", FakeHttpClient):
                response = await control.join_meeting(request)

        self.assertEqual(response["data"]["room_name"], "room")
        path, payload, headers = FakeHttpClient.request
        self.assertEqual(path, "/meeting_call/join")
        self.assertEqual(payload["assistant_id"], "assistant-id")
        self.assertEqual(headers["Authorization"], "Bearer " + "core-" + "key")

    async def test_audio_bridge_publishes_one_track_through_worker_room(self):
        room = FakeRoom()
        sync = LiveKitAudioSync(
            room=room,
            loop=asyncio.get_running_loop(),
            url="ws://localhost:7880",
            api_key="key",
            api_secret="secret",
            room_name="room",
        )

        await sync.start()
        await sync.close()

        self.assertEqual(len(room.local_participant.published), 1)
        self.assertEqual(room.local_participant.unpublished, ["track-sid"])

    def test_generated_trajectory_lands_inside_the_target_rect(self):
        manager = MocapManager(video_frame_size=(1280, 720))
        sequence = manager.find_random_sequence_landing_in_rect(100, 100, 600, 400, 700, 450)
        self.assertIsNotNone(sequence)
        self.assertTrue(600 <= 100 + sequence.total_dx <= 700)
        self.assertTrue(400 <= 100 + sequence.total_dy <= 450)
        self.assertGreater(len(sequence.movements), 1)

    def test_short_websocket_frame_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "4-byte"):
            decode_message(b"\x01")


if __name__ == "__main__":
    unittest.main()
