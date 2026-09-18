import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class ConnectorConfig:
    meeting_url: str
    bot_display_name: str
    livekit_url: str
    livekit_api_key: str
    livekit_api_secret: str
    livekit_room: str
    source_identity: str | None = None
    source_publish_on_behalf: str | None = None
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

    @classmethod
    def from_environment(cls) -> "ConnectorConfig":
        values = {
            "meeting_url": os.environ.get("MEETING_URL"),
            "bot_display_name": os.environ.get("BOT_DISPLAY_NAME", "LiveKit Assistant"),
            "livekit_url": os.environ.get("LIVEKIT_URL"),
            "livekit_api_key": os.environ.get("LIVEKIT_API_KEY"),
            "livekit_api_secret": os.environ.get("LIVEKIT_API_SECRET"),
            "livekit_room": os.environ.get("LIVEKIT_ROOM"),
            "source_identity": os.environ.get("LIVEKIT_SOURCE_IDENTITY"),
            "source_publish_on_behalf": os.environ.get("LIVEKIT_SOURCE_PUBLISH_ON_BEHALF"),
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
        }
        missing = [name.upper() for name in ("meeting_url", "livekit_url", "livekit_api_key", "livekit_api_secret", "livekit_room") if not values[name]]
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")
        if values["ui_interaction_mode"] not in ("humanized", "robotic"):
            raise ValueError("UI_INTERACTION_MODE must be 'humanized' or 'robotic'")
        if values["source_identity"] and values["source_publish_on_behalf"]:
            raise ValueError("Set only one of LIVEKIT_SOURCE_IDENTITY or LIVEKIT_SOURCE_PUBLISH_ON_BEHALF")
        if values["sample_rate"] <= 0:
            raise ValueError("LIVEKIT_AUDIO_SAMPLE_RATE must be positive")
        return cls(**values)

    @property
    def source_selector(self) -> dict[str, str] | None:
        if self.source_identity:
            return {"identity": self.source_identity}
        if self.source_publish_on_behalf:
            return {"publish_on_behalf": self.source_publish_on_behalf}
        return None
