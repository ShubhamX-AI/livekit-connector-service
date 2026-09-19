import logging
import threading
from collections.abc import Callable
from urllib.parse import urlparse

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect
from websockets.sync.server import serve

from ..events import ENDED_EVENT
from .protocol import JSON, MIXED_AUDIO, decode_json, decode_message

logger = logging.getLogger(__name__)

# Browser-side messages worth surfacing in the connector log. They are the only evidence of
# what the page's LiveKit adapter did with the assistant's audio track.
BROWSER_DIAGNOSTIC_MESSAGES = frozenset(
    {
        "LiveKitTrackAdded",
        "LiveKitTrackNotAccepted",
        "LiveKitConnectionFailed",
        "LiveKitVideoTrackNotAvailable",
        "Error",
    }
)


class BrowserWebSocketServer:
    def __init__(
        self,
        *,
        host,
        port,
        livekit_sync,
        upstream_livekit_url,
        on_status: Callable[[str, str | None], None] | None = None,
        alone_in_meeting_timeout_seconds: int = 30,
    ):
        self.host = host
        self.port = port
        self.livekit_sync = livekit_sync
        self.upstream_livekit_url = upstream_livekit_url
        self.on_status = on_status
        self.alone_in_meeting_timeout_seconds = alone_in_meeting_timeout_seconds
        self.server = None
        self.thread = None
        self.started = threading.Event()
        self._participants_in_meeting: set[str] = set()
        self._alone_lock = threading.Lock()
        self._alone_timer: threading.Timer | None = None

    def start(self):
        self.thread = threading.Thread(target=self._run, name="meet-browser-websocket", daemon=True)
        self.thread.start()
        if not self.started.wait(10):
            raise RuntimeError("Browser WebSocket server did not start")

    def _run(self):
        with serve(self._handle, self.host, self.port, compression=None, max_size=None) as server:
            self.server = server
            self.started.set()
            server.serve_forever()

    def _handle(self, websocket):
        path = self._request_path(websocket)
        if path == "/rtc" or path.startswith("/rtc/"):
            self._relay_livekit(websocket, path)
            return
        for message in websocket:
            try:
                message_type, payload = decode_message(message)
                if message_type == JSON:
                    self._handle_json(decode_json(payload))
                elif message_type == MIXED_AUDIO:
                    # Google Meet's own mix of everyone in the call. The payload is bare float32
                    # with no participant id, because there is no single speaker to name.
                    self.livekit_sync.send_mixed_audio(self._float32_to_pcm16(payload))
            except Exception:
                logger.exception("Failed to process browser WebSocket message")

    @staticmethod
    def _request_path(websocket):
        request = getattr(websocket, "request", None)
        return getattr(request, "path", None) or getattr(websocket, "path", "/")

    def _handle_json(self, message: dict):
        message_type = message.get("type")
        if message_type == "UsersUpdate":
            for participant in message.get("newUsers", []) + message.get("updatedUsers", []):
                device_id = participant.get("deviceId")
                if participant.get("humanized_status") == "in_meeting":
                    if device_id and device_id not in self._participants_in_meeting:
                        logger.info(
                            "Meet participant joined: %s (%s)",
                            participant.get("fullName") or device_id,
                            device_id,
                        )
                    self._participants_in_meeting.add(device_id)
                else:
                    self._participants_in_meeting.discard(device_id)
            for participant in message.get("removedUsers", []):
                logger.info("Meet participant left: %s", participant.get("deviceId"))
                self._participants_in_meeting.discard(participant.get("deviceId"))
            self._reschedule_alone_check()
        elif message_type == "MeetingStatusChange":
            change = message.get("change")
            logger.info("Google Meet status changed: %s", change)
            if change in {"meeting_ended", "removed_from_meeting"} and self.on_status:
                self.on_status(ENDED_EVENT, change)
        elif message_type in BROWSER_DIAGNOSTIC_MESSAGES:
            # The browser's LiveKit adapter reports here whether it accepted the assistant's
            # track. Without these lines a rejected track is silent on both sides, and the
            # meeting simply never hears the assistant.
            logger.info("Browser reported %s: %s", message_type, message)

    def _bot_is_alone(self) -> bool:
        # The bot counts as a participant in Meet's own list, so one entry means
        # every human has left.
        return len(self._participants_in_meeting) <= 1

    def _reschedule_alone_check(self) -> None:
        """Arm or cancel the timer that ends the call once every human has left.

        Google Meet keeps a lone bot in the call for minutes before it reports
        `meeting_ended`, and the core engine will not tear the call down until this
        connector says the meeting is over, so the two would wait for each other.
        """
        with self._alone_lock:
            if self._bot_is_alone():
                if self._alone_timer is not None:
                    return
                self._alone_timer = threading.Timer(
                    self.alone_in_meeting_timeout_seconds, self._report_alone_in_meeting
                )
                self._alone_timer.daemon = True
                self._alone_timer.start()
                return
            if self._alone_timer is not None:
                self._alone_timer.cancel()
                self._alone_timer = None

    def _report_alone_in_meeting(self) -> None:
        with self._alone_lock:
            self._alone_timer = None
            if not self._bot_is_alone():
                return
        logger.info(
            "Every other participant left the meeting %ss ago; ending the call",
            self.alone_in_meeting_timeout_seconds,
        )
        if self.on_status:
            self.on_status(ENDED_EVENT, "alone_in_meeting")

    @staticmethod
    def _float32_to_pcm16(raw: bytes) -> bytes:
        import numpy as np

        values = np.frombuffer(raw, dtype=np.float32)
        return (np.clip(values, -1.0, 1.0) * 32767).astype(np.int16).tobytes()

    def _relay_livekit(self, browser_websocket, path):
        parsed = urlparse(self.upstream_livekit_url)
        if parsed.scheme not in ("ws", "wss") or not parsed.netloc:
            raise ValueError("LIVEKIT_URL must be a ws:// or wss:// base URL")
        upstream_url = f"{parsed.scheme}://{parsed.netloc}{path}"
        upstream = connect(upstream_url, compression=None, max_size=None)
        reverse = threading.Thread(target=self._relay_reverse, args=(browser_websocket, upstream), daemon=True)
        reverse.start()
        try:
            for message in browser_websocket:
                upstream.send(message)
        except ConnectionClosed:
            pass
        finally:
            upstream.close()

    @staticmethod
    def _relay_reverse(browser_websocket, upstream):
        try:
            for message in upstream:
                browser_websocket.send(message)
        except Exception:
            pass

    def close(self):
        with self._alone_lock:
            if self._alone_timer is not None:
                self._alone_timer.cancel()
                self._alone_timer = None
        if self.server:
            self.server.shutdown()
        if self.thread:
            self.thread.join(timeout=5)
