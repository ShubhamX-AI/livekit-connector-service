"""Build the JavaScript payload injected into the Google Meet page."""

import json
from pathlib import Path

ASSET_DIR = Path(__file__).parent / "assets"
ASSET_FILENAMES = (
    "protobuf.min.js",
    "pako.min.js",
    "livekit-client.umd.min.js",
    "livekit-client-adapter.js",
    "shared_chromedriver_payload.js",
    "google_meet_chromedriver_payload.js",
)


def build_initial_script(initial_data: dict) -> str:
    """Combine connector configuration and browser-owned JavaScript assets."""
    script_parts = [
        f"window.initialData = {json.dumps(initial_data)};",
        "window.googleMeetInitialData = {modifyDomForVideoRecording: false, disableIncomingVideo: true};",
    ]
    script_parts.extend(
        (ASSET_DIR / filename).read_text(encoding="utf-8")
        for filename in ASSET_FILENAMES
    )
    return "\n".join(script_parts)
