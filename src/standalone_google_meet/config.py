import os
from dataclasses import dataclass
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class ConnectorRuntimeConfig:
    livekit_url: str
    livekit_api_key: str
    livekit_api_secret: str
    sample_rate: int = 48000
    websocket_host: str = "127.0.0.1"
    websocket_port: int = 8765
    chrome_driver_path: str = "/usr/local/bin/chromedriver"
    headless: bool = False
    # "humanized" drives the join UI with real X11 pointer and keyboard input, which is what
    # gets an anonymous bot past Google's "You can't join this video call" screen. "robotic"
    # uses WebDriver clicks and only works for a signed-in browser profile.
    ui_interaction_mode: str = "humanized"
    video_frame_width: int = 1280
    video_frame_height: int = 720
    join_attempts: int = 3
    waiting_room_timeout_seconds: int = 300
    artifact_dir: str = "/app/artifacts"
    max_concurrent_jobs: int = 1

    @classmethod
    def from_environment(cls) -> "ConnectorRuntimeConfig":
        values = {
            "livekit_url": os.environ.get("LIVEKIT_URL"),
            "livekit_api_key": os.environ.get("LIVEKIT_API_KEY"),
            "livekit_api_secret": os.environ.get("LIVEKIT_API_SECRET"),
            "sample_rate": int(os.environ.get("LIVEKIT_AUDIO_SAMPLE_RATE", "48000")),
            "websocket_host": os.environ.get("CONNECTOR_WEBSOCKET_HOST", "127.0.0.1"),
            "websocket_port": int(os.environ.get("CONNECTOR_WEBSOCKET_PORT", "8765")),
            "chrome_driver_path": os.environ.get("CHROMEDRIVER_PATH", "/usr/local/bin/chromedriver"),
            "headless": os.environ.get("CHROME_HEADLESS", "false").lower() == "true",
            "ui_interaction_mode": os.environ.get("UI_INTERACTION_MODE", "humanized"),
            "video_frame_width": int(os.environ.get("VIDEO_FRAME_WIDTH", "1280")),
            "video_frame_height": int(os.environ.get("VIDEO_FRAME_HEIGHT", "720")),
            "join_attempts": int(os.environ.get("JOIN_ATTEMPTS", "3")),
            "waiting_room_timeout_seconds": int(os.environ.get("WAITING_ROOM_TIMEOUT_SECONDS", "300")),
            "artifact_dir": os.environ.get("ARTIFACT_DIR", "/app/artifacts"),
            "max_concurrent_jobs": int(os.environ.get("CONNECTOR_MAX_CONCURRENT_JOBS", "1")),
        }
        missing = [
            name.upper()
            for name in ("livekit_url", "livekit_api_key", "livekit_api_secret")
            if not values[name]
        ]
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")
        if values["ui_interaction_mode"] not in ("humanized", "robotic"):
            raise ValueError("UI_INTERACTION_MODE must be 'humanized' or 'robotic'")
        if values["sample_rate"] <= 0:
            raise ValueError("LIVEKIT_AUDIO_SAMPLE_RATE must be positive")
        if values["max_concurrent_jobs"] <= 0:
            raise ValueError("CONNECTOR_MAX_CONCURRENT_JOBS must be positive")
        return cls(**values)


@dataclass(frozen=True)
class MeetingJobConfig:
    meeting_url: str
    platform: str
    bot_display_name: str

    @classmethod
    def from_metadata(cls, metadata: dict) -> "MeetingJobConfig":
        if not isinstance(metadata, dict):
            raise ValueError("Connector job metadata must be a JSON object")
        if metadata.get("call_type") != "meeting":
            raise ValueError("Connector job metadata must have call_type='meeting'")

        platform = metadata.get("platform") or metadata.get("meeting_platform")
        if platform != "google_meet":
            raise ValueError("Connector only supports platform='google_meet'")

        meeting_url = metadata.get("meeting_url")
        if not isinstance(meeting_url, str) or not meeting_url:
            raise ValueError("Connector job metadata requires meeting_url")
        parsed = urlparse(meeting_url)
        if parsed.scheme != "https" or parsed.netloc != "meet.google.com":
            raise ValueError("meeting_url must be an https://meet.google.com URL")

        bot_display_name = metadata.get("bot_display_name") or "LiveKit Assistant"
        if not isinstance(bot_display_name, str) or not bot_display_name.strip():
            raise ValueError("bot_display_name must be a non-empty string")

        return cls(
            meeting_url=meeting_url,
            platform=platform,
            bot_display_name=bot_display_name.strip(),
        )


@dataclass(frozen=True)
class ConnectorConfig:
    runtime: ConnectorRuntimeConfig
    job: MeetingJobConfig
    room_name: str

    @property
    def meeting_url(self) -> str:
        return self.job.meeting_url

    @property
    def bot_display_name(self) -> str:
        return self.job.bot_display_name

    @property
    def livekit_url(self) -> str:
        return self.runtime.livekit_url

    @property
    def livekit_api_key(self) -> str:
        return self.runtime.livekit_api_key

    @property
    def livekit_api_secret(self) -> str:
        return self.runtime.livekit_api_secret

    @property
    def sample_rate(self) -> int:
        return self.runtime.sample_rate

    @property
    def websocket_host(self) -> str:
        return self.runtime.websocket_host

    @property
    def websocket_port(self) -> int:
        return self.runtime.websocket_port

    @property
    def chrome_driver_path(self) -> str:
        return self.runtime.chrome_driver_path

    @property
    def headless(self) -> bool:
        return self.runtime.headless

    @property
    def ui_interaction_mode(self) -> str:
        return self.runtime.ui_interaction_mode

    @property
    def video_frame_width(self) -> int:
        return self.runtime.video_frame_width

    @property
    def video_frame_height(self) -> int:
        return self.runtime.video_frame_height

    @property
    def join_attempts(self) -> int:
        return self.runtime.join_attempts

    @property
    def waiting_room_timeout_seconds(self) -> int:
        return self.runtime.waiting_room_timeout_seconds

    @property
    def artifact_dir(self) -> str:
        return self.runtime.artifact_dir
