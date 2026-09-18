import asyncio
import logging
import threading
import time
import uuid

import jwt
from livekit import rtc

logger = logging.getLogger(__name__)


class LiveKitAudioSync:
    TOKEN_TTL_SECONDS = 6 * 60 * 60

    def __init__(self, *, url: str, api_key: str, api_secret: str, room_name: str, sample_rate: int = 48000):
        self.url = url
        self.api_key = api_key
        self.api_secret = api_secret
        self.room_name = room_name
        self.sample_rate = sample_rate
        self._rooms: dict[str, rtc.Room] = {}
        self._sources: dict[str, rtc.AudioSource] = {}
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, name="livekit-audio-sync", daemon=True)
        self._thread.start()

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _submit(self, coroutine):
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop)

    def _token(self, identity: str, name: str, grants: dict) -> str:
        now = int(time.time())
        return jwt.encode(
            {
                "iss": self.api_key,
                "sub": identity,
                "name": name,
                "nbf": now,
                "exp": now + self.TOKEN_TTL_SECONDS,
                "video": {"roomJoin": True, "room": self.room_name, **grants},
            },
            self.api_secret,
            algorithm="HS256",
        )

    def source_browser_config(self, source_selector: dict[str, str] | None) -> dict | None:
        if not source_selector:
            return None
        identity = source_selector.get("identity")
        publish_on_behalf = source_selector.get("publish_on_behalf")
        subscriber_identity = f"meet-source-{uuid.uuid4().hex[:10]}"
        return {
            "livekit": {
                "room_name": self.room_name,
                "url": self.url,
                "token": self._token(subscriber_identity, subscriber_identity, {"canPublish": False, "canSubscribe": True, "hidden": True}),
                "identity": identity,
                "publish_on_behalf": publish_on_behalf,
            }
        }

    def participant_joined(self, participant_id: str, name: str | None = None):
        self._submit(self._add_participant(participant_id, name or participant_id))

    def participant_left(self, participant_id: str):
        self._submit(self._remove_participant(participant_id))

    def send_audio(self, participant_id: str, pcm_s16le: bytes):
        self._submit(self._capture_audio(participant_id, pcm_s16le))

    async def _add_participant(self, participant_id: str, name: str):
        if participant_id in self._rooms:
            return
        room = rtc.Room()
        try:
            await room.connect(self.url, self._token(participant_id, name, {"canPublish": True, "canPublishData": True, "canSubscribe": False}), options=rtc.RoomOptions(auto_subscribe=False))
            source = rtc.AudioSource(self.sample_rate, 1)
            track = rtc.LocalAudioTrack.create_audio_track(f"meet-audio-{participant_id}", source)
            await room.local_participant.publish_track(track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
        except Exception:
            logger.exception("Failed to publish LiveKit audio for Meet participant %s", participant_id)
            await room.disconnect()
            return
        self._rooms[participant_id] = room
        self._sources[participant_id] = source
        logger.info("Published Meet participant %s into LiveKit room %s", participant_id, self.room_name)

    async def _remove_participant(self, participant_id: str):
        self._sources.pop(participant_id, None)
        room = self._rooms.pop(participant_id, None)
        if room:
            await room.disconnect()

    async def _capture_audio(self, participant_id: str, pcm_s16le: bytes):
        source = self._sources.get(participant_id)
        if not source:
            return
        samples = len(pcm_s16le) // 2
        if samples == 0:
            return
        await source.capture_frame(rtc.AudioFrame(data=pcm_s16le, sample_rate=self.sample_rate, num_channels=1, samples_per_channel=samples))

    async def _close(self):
        for room in list(self._rooms.values()):
            await room.disconnect()
        self._rooms.clear()
        self._sources.clear()

    def close(self):
        future = self._submit(self._close())
        future.result(timeout=10)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=10)
