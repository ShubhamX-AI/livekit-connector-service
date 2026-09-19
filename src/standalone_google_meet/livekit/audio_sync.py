import asyncio
import logging
import time
import uuid

import jwt
import numpy as np
from livekit import rtc

logger = logging.getLogger(__name__)


class LiveKitAudioSync:
    """Bridge the browser's mixed Google Meet audio into the worker's LiveKit room."""

    TOKEN_TTL_SECONDS = 6 * 60 * 60
    TRACK_NAME = "meet-audio-mixed"
    AMPLITUDE_LOG_INTERVAL_SECONDS = 1.0

    def __init__(
        self,
        *,
        room: rtc.Room,
        loop: asyncio.AbstractEventLoop,
        url: str,
        api_key: str,
        api_secret: str,
        room_name: str,
        sample_rate: int = 48000,
    ):
        self.room = room
        self.loop = loop
        self.url = url
        self.api_key = api_key
        self.api_secret = api_secret
        self.room_name = room_name
        self.sample_rate = sample_rate
        self.source: rtc.AudioSource | None = None
        self.publication: rtc.LocalTrackPublication | None = None
        self.audio_queue: asyncio.Queue[bytes] | None = None
        self.capture_task: asyncio.Task | None = None
        self.closed = False
        self._last_amplitude_log = 0.0

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

    def source_browser_config(self) -> dict:
        """Return the browser relay configuration for the assistant's audio."""
        subscriber_identity = f"meet-source-{uuid.uuid4().hex[:10]}"
        return {
            "livekit": {
                "room_name": self.room_name,
                "url": self.url,
                "token": self._token(
                    subscriber_identity,
                    subscriber_identity,
                    {"canPublish": False, "canSubscribe": True, "hidden": True},
                ),
                "publish_on_behalf": self.room_name,
            }
        }

    async def start(self) -> None:
        """Publish the single mixed track before the browser is allowed to send audio."""
        if self.source is not None:
            return
        self.source = rtc.AudioSource(self.sample_rate, 1)
        self.audio_queue = asyncio.Queue(maxsize=32)
        self.capture_task = asyncio.create_task(self._capture_worker())
        track = rtc.LocalAudioTrack.create_audio_track(self.TRACK_NAME, self.source)
        try:
            self.publication = await self.room.local_participant.publish_track(
                track,
                rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE),
            )
        except Exception:
            self.source = None
            self.audio_queue = None
            if self.capture_task:
                self.capture_task.cancel()
                await asyncio.gather(self.capture_task, return_exceptions=True)
                self.capture_task = None
            raise
        logger.info("Published mixed Google Meet audio in room %s", self.room_name)

    def send_mixed_audio(self, pcm_s16le: bytes) -> None:
        """Schedule one browser frame on the worker event loop."""
        if self.closed or self.source is None:
            return
        self.loop.call_soon_threadsafe(self._enqueue_audio, pcm_s16le)

    def _enqueue_audio(self, pcm_s16le: bytes) -> None:
        if self.closed or self.audio_queue is None:
            return
        if self.audio_queue.full():
            self.audio_queue.get_nowait()
        self.audio_queue.put_nowait(pcm_s16le)

    async def _capture_worker(self) -> None:
        while True:
            pcm_s16le = await self.audio_queue.get()
            try:
                await self._capture_audio(pcm_s16le)
            except Exception:
                logger.exception("Failed to publish a mixed Google Meet audio frame")

    def _log_amplitude(self, pcm_s16le: bytes) -> None:
        """Log the peak sample of the mixed meeting audio about once a second.

        The browser builds the mix from whatever audio tracks it has connected to its
        AudioContext destination. A flat zero here while somebody in the meeting is
        speaking means the mix is empty and the assistant is deaf, which is a different
        fault from the assistant hearing silence.
        """
        now = time.monotonic()
        if now - self._last_amplitude_log < self.AMPLITUDE_LOG_INTERVAL_SECONDS:
            return
        self._last_amplitude_log = now
        peak = int(np.abs(np.frombuffer(pcm_s16le, dtype=np.int16)).max())
        logger.info("Mixed Google Meet audio peak amplitude: %s", peak)

    async def _capture_audio(self, pcm_s16le: bytes) -> None:
        if self.source is None or not pcm_s16le:
            return
        samples = len(pcm_s16le) // 2
        if samples == 0:
            return
        self._log_amplitude(pcm_s16le)
        await self.source.capture_frame(
            rtc.AudioFrame(
                data=pcm_s16le,
                sample_rate=self.sample_rate,
                num_channels=1,
                samples_per_channel=samples,
            )
        )

    async def close(self) -> None:
        """Stop publishing without disconnecting the worker-owned room."""
        if self.closed:
            return
        self.closed = True
        if self.capture_task:
            self.capture_task.cancel()
            await asyncio.gather(self.capture_task, return_exceptions=True)
            self.capture_task = None
        self.audio_queue = None
        publication = self.publication
        self.publication = None
        self.source = None
        if publication:
            await self.room.local_participant.unpublish_track(publication.sid)
