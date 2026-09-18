# Google Meet to LiveKit connector

This repository contains the `meet-connector` LiveKit worker. It joins one Google Meet per
LiveKit job, publishes the meeting's mixed audio into the shared LiveKit room, and routes the
assistant's selected LiveKit audio back into Google Meet.

The core API remains authoritative for meeting calls. It creates the room, dispatches both
`api-agent` and `meet-connector`, owns the `CallRecord`, recording, usage, webhook, and teardown,
and receives connector lifecycle events from the shared room.

## Runtime roles

```text
caller -> control backend -> core POST /meeting_call/join
                         -> api-agent
                         -> meet-connector -> Chrome/Xvfb -> Google Meet
```

- **Worker**: `agent_run.py` registers `meet-connector` with LiveKit. Every job reads its
  `meeting_url`, `platform`, and `bot_display_name` from LiveKit job metadata.
- **Control backend**: `control_run.py` exposes `POST /meetings/join`. It validates the request,
  calls the core API with a bearer token, and returns the core response. It is a request adapter,
  not a dispatcher and not a second source of call state.

There is no fixed meeting URL or room in the worker environment. The only deployment-time values
are LiveKit credentials, browser/runtime settings, worker capacity, and core API credentials for
the optional control backend.

## Audio and lifecycle

```text
Google Meet mixed audio
    -> Chrome WebRTC capture
    -> browser WebSocket
    -> meet-connector worker participant
    -> one mixed LiveKit audio track
    -> api-agent STT/LLM/TTS
    -> assistant LiveKit audio selected by lk.publish_on_behalf
    -> browser LiveKit relay
    -> Google Meet virtual microphone
```

The connector publishes lifecycle data on `meeting_connector_events`:

- `waiting`: Google is waiting for admission.
- `ready`: the bot is admitted, the mixed track is published, and media sending is enabled.
- `failed`: metadata, browser, join, or runtime failure.
- `ended`: Google Meet ended or the connector job is shutting down.

On `ready`, the worker also sets `lk.meeting_connector_status=ready` on its LiveKit participant.
The core assistant uses this attribute to recover readiness if the data packet arrived before it
was listening.

## Start locally

Copy `.env.example` to `.env` and fill in deployment credentials. Do not commit `.env`.

Start the worker:

```bash
uv sync
uv run python -m livekit.agents start agent_run.py
```

Start the optional control backend in a second process:

```bash
uv run python control_run.py
```

Submit a meeting request:

```bash
curl -X POST http://127.0.0.1:8080/meetings/join \
  -H 'Content-Type: application/json' \
  -d '{
    "assistant_id": "assistant-id",
    "meeting_url": "https://meet.google.com/abc-defg-hij",
    "platform": "google_meet",
    "bot_display_name": "Sales Assistant",
    "metadata": {"source": "local-control"}
  }'
```

The control backend uses `CORE_API_URL` and `CORE_API_KEY` to call the core API. The core API key
must be valid for the assistant owner.

## Docker

The worker and control backend use the same image but run as separate roles:

```bash
docker compose up --build worker
docker compose up --build control
```

Chrome uses Xvfb and needs the configured shared-memory allocation. Failed joins write screenshots,
HTML, and URL artifacts under `./artifacts`.

## Project structure

```text
.
├── agent_run.py                         # LiveKit worker registration
├── control_run.py                       # Control backend startup
├── CONTEXT.md                           # Connector domain language
├── src/standalone_google_meet/
│   ├── api/                             # Control backend and core API adapter
│   │   ├── client.py                    # Core meeting-call client
│   │   └── control.py                   # FastAPI transport
│   ├── browser/                         # Browser transport and JavaScript
│   │   ├── assets/                      # Injected Google Meet and LiveKit scripts
│   │   ├── payload.py                   # Browser payload assembly
│   │   ├── protocol.py                  # WebSocket message framing
│   │   └── websocket.py                 # Browser socket and LiveKit relay
│   ├── chrome/                          # Google Meet session and X11 input
│   │   ├── session.py                   # Selenium/Xvfb meeting session
│   │   ├── humanized_input.py           # Human-like pointer and keyboard actions
│   │   ├── mocap_manager.py             # Pointer trajectory planning
│   │   └── x11_input.py                 # X11 input adapter
│   ├── livekit/                         # LiveKit worker and media
│   │   ├── worker.py                    # Per-job worker entry point
│   │   ├── audio_sync.py                # Mixed audio publisher and browser token relay
│   │   └── lifecycle.py                 # Meeting connector event publisher
│   ├── config.py                        # Deployment and job metadata configuration
│   └── events.py                        # Shared meeting connector events
├── tests/                               # Unit tests at the module seams
├── Dockerfile
├── docker-compose.yaml
├── .env.example
└── pyproject.toml
```

The role-first package paths are the canonical imports:

```python
from standalone_google_meet.api.control import app
from standalone_google_meet.browser.protocol import decode_message
from standalone_google_meet.chrome.session import GoogleMeetChromeSession
from standalone_google_meet.livekit.worker import entrypoint
```

## Verification

```bash
uv run python -m unittest discover -s tests -p 'test_*.py' -v
uv run ruff check src tests agent_run.py control_run.py
docker build .
```

Real integration verification requires a running core API, LiveKit server, `api-agent` worker,
and a Google Meet that permits the bot to join.
