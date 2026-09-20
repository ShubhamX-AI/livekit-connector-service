# Google Meet to LiveKit connector

This repository contains the `meet-connector` LiveKit worker. It joins a Google Meet per
LiveKit job, captures and publishes the meeting's mixed audio into a shared LiveKit room, and routes the
assistant's LiveKit audio back into Google Meet.

---

## System Architecture & Triggering Flow

To trigger meeting calls and send meeting links, clients **must trigger the VoiceKit service**, not the core backend directly.

```text
  Caller / Client App / Wispr API
                │
                ▼
   ┌─────────────────────────────────────────────────────────────┐
   │ VoiceKit Service                                            │
   │  - Manages User Accounts & User IDs                         │
   │  - Validates VoiceKit API Keys                              │
   │  - Resolves Agent Configurations & Details                  │
   └────────────────────────────┬────────────────────────────────┘
                                │ Triggers Meeting Call (HTTP)
                                ▼
   ┌─────────────────────────────────────────────────────────────┐
   │ Core Backend Engine (LiveKit Server)                        │
   │  - Authoritative room creator & call lifecycle manager      │
   │  - Dispatches 'api-agent' (STT/LLM/TTS)                     │
   │  - Dispatches 'meet-connector' job over WebSocket           │
   └────────────────────────────┬────────────────────────────────┘
                                │ WebSocket Dispatch (LIVEKIT_URL)
                                ▼
   ┌─────────────────────────────────────────────────────────────┐
   │ livekit-connector-service (THIS REPO)                       │
   │                                                             │
   │  [MANDATORY] Connector Worker (agent_run.py)                │
   │    • Connected to Core via WebSocket (LIVEKIT_URL)          │
   │    • Listens for and accepts 'meet-connector' jobs          │
   │    • Spawns Selenium/Chrome/Xvfb session                    │
   │    • Bridges mixed audio bidirectionally                    │
   │                                                             │
   │  [OPTIONAL] Control Backend (control_run.py)                │
   │    • Local developer HTTP adapter to trigger core endpoints │
   └────────────────────────────┬────────────────────────────────┘
                                │ Joins & Bridges Audio
                                ▼
                         Google Meet Session
```

### Key Service Roles & Hierarchy

1. **VoiceKit Service (Primary Trigger Entry Point)**:

   - User accounts, user IDs, VoiceKit API keys, and conversational agent configurations all live in VoiceKit.
   - When a meeting link needs to be processed (e.g., via the Wispr API or client integrations), requests must go to **VoiceKit**.
   - VoiceKit resolves the user context, attaches agent settings, and triggers the core backend engine.
2. **Core Backend Service (LiveKit Engine)**:

   - The central engine that orchestrates call records, billing/usage, recording, and the shared LiveKit room.
   - Communicates with agent workers over **LiveKit WebSocket** (`LIVEKIT_URL`).
   - Dispatches jobs to both the conversational `api-agent` and the `meet-connector` worker.
3. **Connector Worker (`meet-connector`) — MANDATORY**:

   - **Running this worker is strictly mandatory.**
   - The worker (`agent_run.py`) must be actively running and connected to the core engine over WebSocket.
   - **Fixed Agent Name**: The worker registers with LiveKit using `agent_name="meet-connector"`. The LiveKit core engine dispatches meeting jobs specifically targeting the `meet-connector` agent name. This exact name is the dispatch target; modifying it will prevent the core engine from routing jobs to this worker.
   - Whenever an external trigger (such as VoiceKit or Wispr API) asks the core service to start a meeting call, the core service dispatches the `meet-connector` agent to this worker over WebSocket.
   - If this worker is not running, the dispatch fails or times out, and the bot cannot join the meeting.
4. **Control Backend (`control_run.py`) — OPTIONAL**:

   - An optional FastAPI adapter exposing `POST /meetings/join`.
   - Used primarily for local development and testing to forward join requests directly to the core API using `VOICEKIT_API_URL` and `VOICEKIT_API_KEY`.
   - In production environments, triggers flow through VoiceKit.

---

## Audio and Lifecycle

```text
Google Meet mixed audio
    -> Chrome WebRTC capture
    -> browser WebSocket (127.0.0.1:8765)
    -> meet-connector worker participant
    -> one mixed LiveKit audio track
    -> api-agent STT/LLM/TTS
    -> assistant LiveKit audio selected by lk.publish_on_behalf
       (every audio track of that participant: speech, background, fillers)
    -> browser LiveKit relay
    -> Web Audio mix down to one track
    -> Google Meet virtual microphone
```

The assistant participant may publish more than one audio track at a time, for
example speech alongside a background bed. The browser relay
(`src/standalone_google_meet/browser/assets/livekit-client-adapter.js`) sums them
in Web Audio into a single track before handing the stream to the virtual
microphone. The merge is required, not cosmetic: the consumer builds one
`MediaStreamAudioSourceNode` from that stream, and such a node reads only one
track, so without the mix all but one audio track would be silent in the meeting.

### Meeting Lifecycle Events & Core Synchronization

The worker and the **LiveKit core engine / `api-agent` share a strict event contract** via the LiveKit room data channel and participant attributes. These events allow the core engine and conversational agent to track real-time meeting progress from this worker:

- **Data Topic**: `meeting_connector_events` (reliable data packets: `{"event": "<name>", "detail": "..."}`)
- **Readiness Participant Attribute**: `lk.meeting_connector_status` (set to `ready` on successful admission)

```text
 Connector Worker (This Service)                  LiveKit Core Engine / api-agent
─────────────────────────────────                ─────────────────────────────────
 Bot enters waiting room
       │
       ├──── publish_data("waiting") ───────────► Core notes bot is at meeting,
       │                                          awaiting host knock admission
 Bot admitted & audio track live
       │
       ├──── publish_data("ready") ─────────────► Core api-agent begins conversational
       ├──── set_attributes("ready") ───────────► turn-taking (STT/LLM/TTS)
       │
 Join failure / admission timeout
       │
       └──── publish_data("failed", detail) ────► Core triggers teardown, logs reason,
                                                  and fires failure webhooks
 Meeting ended / worker stopping
       │
       └──── publish_data("ended") ─────────────► Core finalizes recording, usage,
                                                  and cleans up room
```

| Event Name  | Worker State Update                                                                                      | LiveKit Core Engine & Agent Action                                                                                                                                                    |
| :---------- | :------------------------------------------------------------------------------------------------------- | :------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `waiting` | Bot reached the Google Meet URL and is in the knock/waiting room awaiting host admission.                | Core tracks that navigation succeeded and the bot is waiting for admission.                                                                                                           |
| `ready`   | Host admitted bot; mixed audio track is published to the room; virtual microphone relay is ready.        | Core`api-agent` initiates speech/greeting, listening to meeting participants. Fallback attribute `lk.meeting_connector_status=ready` ensures late-joining agents catch readiness. |
| `failed`  | Failure during launch, navigation, knock rejection, or audio pipeline error (error passed in`detail`). | Core marks the call record failed, dispatches failure webhooks to callers, and halts assistant processes.                                                                             |
| `ended`   | Meeting completed, host ended call, participants departed, or worker shut down.                          | Core triggers call termination, billing calculation, room cleanup, and post-call webhook dispatches.                                                                                  |

> [!NOTE]
> These event names (`waiting`, `ready`, `failed`, `ended`) and topic (`meeting_connector_events`) must match identically between this connector repository and the LiveKit core service so the core engine can properly interpret meeting lifecycle signals.

---

## Running the Service

### 1. Environment Configuration

Copy `.env.example` to `.env` and configure credentials:

```bash
cp .env.example .env
```

Key environment variables:

- `LIVEKIT_URL`: WebSocket URL to the core LiveKit engine (e.g. `ws://127.0.0.1:7880` or `wss://livekit.example.com`).
- `LIVEKIT_API_KEY` & `LIVEKIT_API_SECRET`: LiveKit credentials used by the worker to connect and register.
- `CONNECTOR_MAX_CONCURRENT_JOBS`: Maximum concurrent Google Meet browser sessions per worker (default: `1`).
- `VOICEKIT_API_URL` & `VOICEKIT_API_KEY`: Required only if using the optional control backend adapter or testing core API dispatch.

### 2. Start the Worker (MANDATORY)

The worker process connects to LiveKit via WebSocket and waits for job dispatches:

```bash
uv sync
uv run python -m livekit.agents start agent_run.py
```

> [!IMPORTANT]
> - **Must Remain Running**: This worker process must remain running. When VoiceKit or Wispr API triggers a meeting call, the core engine dispatches the job to this active worker over WebSocket.
> - **Fixed Agent Name (`meet-connector`)**: The worker registers with `WorkerOptions(agent_name="meet-connector")`. The core engine explicitly dispatches to this agent name. Do not change this identifier, or dispatches will fail.

### 3. Start the Control Backend (OPTIONAL)

For local testing without routing through the full VoiceKit stack, run the control backend in a separate terminal:

```bash
uv run python control_run.py
```

Submit a test meeting request:

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

---

## Docker

The worker and control backend use the same container image:

```bash
# MANDATORY: Start the worker to process meeting jobs
docker compose up --build worker

# OPTIONAL: Start the control backend adapter if needed
docker compose up --build control
```

The control backend is published on `http://127.0.0.1:8080` (health endpoint: `/health`).
Docker containers reach LiveKit and the core API on the host via `host.docker.internal`.
Override `DOCKER_LIVEKIT_URL` or `DOCKER_VOICEKIT_API_URL` if these services run at different hostnames/ports.

Chrome runs inside Xvfb and requires shared memory (`shm_size: 2gb`). Failed join diagnostics (screenshots, HTML source, and final URLs) are written to `./artifacts`.

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
│   ├── test_connector.py                # Worker, transport, and lifecycle seams
│   └── browser/                         # Headless-Chrome checks for injected scripts
│       ├── mix-audio.html               # Two-tone audio mixing harness
│       └── run_mix_audio.py             # Harness runner and verdict
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
uv run python tests/browser/run_mix_audio.py
docker build .
```

`run_mix_audio.py` drives the real LiveKit adapter in headless Chrome against a
stubbed LiveKit SDK that publishes two tones at once, and fails if either tone is
missing from the mixed output. It needs a Chrome or Chromium binary; set
`CHROME_BINARY` if it is not on the usual paths.

Real integration verification requires a running core API, LiveKit server, `api-agent` worker,
and a Google Meet that permits the bot to join.
