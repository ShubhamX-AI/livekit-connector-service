# Standalone Google Meet to LiveKit Connector

Audio-only local connector. It joins one Google Meet session, publishes Meet participant audio into LiveKit, and routes one selected LiveKit participant into the Meet microphone.

## Run

Use UV from this directory. Chrome, ChromeDriver, Xvfb, and PulseAudio are required.

```bash
export MEETING_URL='https://meet.google.com/abc-defg-hij'
export BOT_DISPLAY_NAME='LiveKit Assistant'
export LIVEKIT_URL='ws://127.0.0.1:7880'
export LIVEKIT_API_KEY='your-key'
export LIVEKIT_API_SECRET='your-secret'
export LIVEKIT_ROOM='assistant-room'
export LIVEKIT_SOURCE_IDENTITY='assistant'

uv run python -m standalone_google_meet
```

Use `LIVEKIT_SOURCE_PUBLISH_ON_BEHALF` instead of `LIVEKIT_SOURCE_IDENTITY` when the LiveKit agent publishes with the `lk.publish_on_behalf` participant attribute.

## Current scope

- Anonymous/name-based Google Meet joining.
- One meeting per process.
- Bidirectional audio.
- Meet participant audio is published as individual LiveKit participants.
- One selected LiveKit participant is routed into Meet.

Signed-in Google Workspace login, video, captions, chat, recording, transcription, and application database state are intentionally excluded from this first milestone.

## Runtime notes

The browser payload uses Google Meet's private WebRTC/data-channel structures and may need updates when Google changes Meet. Pin browser versions and test against a real meeting. The LiveKit API secret stays in the Python process; the browser receives only a short-lived subscribe-only token.

## Python and JavaScript

Python is the controller. It validates environment variables, starts Chrome, serves the local WebSocket endpoint, creates LiveKit tokens, publishes Meet participant audio, and manages shutdown.

JavaScript runs inside the Meet Chrome page. The injected scripts observe Meet's WebRTC receivers and internal participant events. They send binary audio and JSON lifecycle messages to Python over `ws://127.0.0.1:8765`.

For assistant audio, Python places a short-lived LiveKit subscriber token in the injected configuration. The browser-side LiveKit adapter connects through the local `/rtc` WebSocket relay, receives the selected LiveKit audio track, and routes it into the virtual microphone exposed to Google Meet. Media stays in the browser; Python handles orchestration and Meet audio publishing.

## Project commands

```bash
uv sync
uv run python -m unittest discover -s tests -p 'test_*.py' -v
uv run ruff check src tests
uv run python -m standalone_google_meet
```
