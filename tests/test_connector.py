import asyncio
import contextlib
import json
import os
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from standalone_google_meet.api import control
from standalone_google_meet.browser.protocol import decode_message
from standalone_google_meet.browser.websocket import BrowserWebSocketServer
from standalone_google_meet.chrome.mocap_manager import MocapManager
from standalone_google_meet.config import (
    ConnectorRuntimeConfig,
    MeetingJobConfig,
)
from standalone_google_meet.events import ENDED_EVENT, FAILED_EVENT, READY_EVENT
from standalone_google_meet.livekit.audio_sync import LiveKitAudioSync
from standalone_google_meet.livekit.lifecycle import ConnectorLifecycle
from standalone_google_meet.livekit.worker import (
    SHUTDOWN,
    build_cleanup,
    run_meeting_session,
)

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

class FakeChromeSession:
    """Stand-in for GoogleMeetChromeSession whose join runs on the worker's thread pool."""

    def __init__(self, loop, status_queue, *, script=(), error=None):
        self.loop = loop
        self.status_queue = status_queue
        self.script = list(script)
        self.error = error
        self.stop_requested = threading.Event()
        self.close_count = 0

    def start(self):
        for event, detail in self.script:
            self.loop.call_soon_threadsafe(self.status_queue.put_nowait, (event, detail))
        if self.error:
            raise self.error

    def close(self):
        self.close_count += 1


class ExplodingLifecycle(ConnectorLifecycle):
    """Lifecycle whose publish fails for one marked event, to exercise the error path."""

    async def publish(self, event, detail=None):
        if detail == "boom":
            raise RuntimeError("publish failed")
        await super().publish(event, detail)


class FakeAudioSync:
    def __init__(self):
        self.close_count = 0

    async def close(self):
        self.close_count += 1


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
        with environment(VOICEKIT_API_URL="http://core", VOICEKIT_API_KEY="core-key"):
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

    def test_bot_alone_in_the_meeting_ends_the_call(self):
        """Meet reports `meeting_ended` minutes late, so a lone bot has to end the call itself."""
        reported = []
        ended = threading.Event()

        def on_status(event, detail):
            reported.append((event, detail))
            ended.set()

        server = BrowserWebSocketServer(
            host="127.0.0.1",
            port=0,
            livekit_sync=None,
            upstream_livekit_url="ws://localhost:7880",
            on_status=on_status,
            alone_in_meeting_timeout_seconds=0,
        )
        server._handle_json(
            {
                "type": "UsersUpdate",
                "newUsers": [
                    {"deviceId": "bot", "humanized_status": "in_meeting"},
                    {"deviceId": "human", "humanized_status": "in_meeting"},
                ],
            }
        )
        self.assertEqual(reported, [])

        server._handle_json({"type": "UsersUpdate", "removedUsers": [{"deviceId": "human"}]})

        self.assertTrue(ended.wait(2), "the connector never reported that the bot was alone")
        self.assertEqual(reported, [("ended", "alone_in_meeting")])
        server.close()

    def test_short_websocket_frame_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "4-byte"):
            decode_message(b"\x01")

    # --- Meeting session loop ---------------------------------------------------------

    @staticmethod
    def _published_events(participant):
        return [json.loads(payload)["event"] for payload, _reliable, _topic in participant.messages]

    async def _wait_for_event(self, participant, event, timeout=2.0):
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if event in self._published_events(participant):
                return
            await asyncio.sleep(0.01)
        raise AssertionError(f"{event!r} was never published")

    def _session(self, status_queue, **kwargs):
        return FakeChromeSession(asyncio.get_running_loop(), status_queue, **kwargs)

    async def test_ready_survives_a_join_that_returns_immediately(self):
        """The join thread finishing must not swallow the `ready` it emitted on its way out."""
        participant = FakeParticipant()
        status_queue = asyncio.Queue()
        session = self._session(status_queue, script=[(READY_EVENT, None)])

        task = asyncio.create_task(
            run_meeting_session(
                session=session,
                lifecycle=ConnectorLifecycle(participant),
                status_queue=status_queue,
            )
        )
        await self._wait_for_event(participant, READY_EVENT)
        status_queue.put_nowait((ENDED_EVENT, "meeting_ended"))
        terminal = await asyncio.wait_for(task, 2)

        self.assertEqual(terminal, ENDED_EVENT)
        self.assertEqual(self._published_events(participant), [READY_EVENT, ENDED_EVENT])
        self.assertEqual(participant.attributes, {"lk.meeting_connector_status": "ready"})

    async def test_loop_exits_on_room_disconnect(self):
        participant = FakeParticipant()
        status_queue = asyncio.Queue()
        session = self._session(status_queue)

        task = asyncio.create_task(
            run_meeting_session(
                session=session,
                lifecycle=ConnectorLifecycle(participant),
                status_queue=status_queue,
            )
        )
        status_queue.put_nowait((SHUTDOWN, None))
        terminal = await asyncio.wait_for(task, 2)

        self.assertIsNone(terminal)
        self.assertEqual(self._published_events(participant), [])

    async def test_room_disconnect_after_ready_publishes_ended(self):
        participant = FakeParticipant()
        status_queue = asyncio.Queue()
        session = self._session(status_queue, script=[(READY_EVENT, None)])

        task = asyncio.create_task(
            run_meeting_session(
                session=session,
                lifecycle=ConnectorLifecycle(participant),
                status_queue=status_queue,
            )
        )
        await self._wait_for_event(participant, READY_EVENT)
        status_queue.put_nowait((SHUTDOWN, None))
        terminal = await asyncio.wait_for(task, 2)

        self.assertIsNone(terminal)
        self.assertEqual(self._published_events(participant), [READY_EVENT, ENDED_EVENT])

    async def test_meeting_ended_is_terminal(self):
        participant = FakeParticipant()
        status_queue = asyncio.Queue()
        session = self._session(status_queue, script=[(READY_EVENT, None)])

        task = asyncio.create_task(
            run_meeting_session(
                session=session,
                lifecycle=ConnectorLifecycle(participant),
                status_queue=status_queue,
            )
        )
        await self._wait_for_event(participant, READY_EVENT)
        status_queue.put_nowait((ENDED_EVENT, "meeting_ended"))
        status_queue.put_nowait((ENDED_EVENT, "meeting_ended"))
        terminal = await asyncio.wait_for(task, 2)

        self.assertEqual(terminal, ENDED_EVENT)
        self.assertEqual(self._published_events(participant), [READY_EVENT, ENDED_EVENT])

    async def test_join_failure_publishes_failed(self):
        participant = FakeParticipant()
        status_queue = asyncio.Queue()
        session = self._session(status_queue, error=RuntimeError("Not admitted within 300s"))

        terminal = await asyncio.wait_for(
            run_meeting_session(
                session=session,
                lifecycle=ConnectorLifecycle(participant),
                status_queue=status_queue,
            ),
            2,
        )

        self.assertEqual(terminal, FAILED_EVENT)
        payload = json.loads(participant.messages[0][0])
        self.assertEqual(payload["event"], FAILED_EVENT)
        self.assertEqual(payload["detail"], "Not admitted within 300s")

    async def test_publish_failure_after_ready_does_not_also_publish_ended(self):
        """A failure is terminal: the shutdown path must not add `ended` on top of `failed`."""
        participant = FakeParticipant()
        status_queue = asyncio.Queue()
        session = self._session(status_queue, script=[(READY_EVENT, None)])

        task = asyncio.create_task(
            run_meeting_session(
                session=session,
                lifecycle=ExplodingLifecycle(participant),
                status_queue=status_queue,
            )
        )
        await self._wait_for_event(participant, READY_EVENT)
        status_queue.put_nowait((ENDED_EVENT, "boom"))

        with self.assertRaisesRegex(RuntimeError, "publish failed"):
            await asyncio.wait_for(task, 2)

        self.assertEqual(self._published_events(participant), [READY_EVENT, FAILED_EVENT])

    async def test_cleanup_runs_exactly_once(self):
        status_queue = asyncio.Queue()
        session = self._session(status_queue)
        audio_sync = FakeAudioSync()
        cleanup = build_cleanup(session=session, audio_sync=audio_sync)

        await cleanup()
        await cleanup()

        self.assertEqual(session.close_count, 1)
        self.assertEqual(audio_sync.close_count, 1)


if __name__ == "__main__":
    unittest.main()
