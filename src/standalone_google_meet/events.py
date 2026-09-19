"""Connector events shared by browser, worker, and the LiveKit core engine.

CONTRACT WITH LIVEKIT CORE:
The event strings, topic, and status attribute defined here represent a shared contract
with the LiveKit core backend and conversational agent (e.g. `api-agent`).
The core engine subscribes to `EVENTS_TOPIC` (and inspects `STATUS_ATTRIBUTE`) to receive
real-time meeting status updates from this worker:

- WAITING_EVENT ("waiting"):
    The bot has navigated to the meeting URL and is currently in the Google Meet
    waiting/knock room awaiting admission by the meeting host. The core engine knows
    the bot successfully reached the meeting.
- READY_EVENT ("ready"):
    The bot was admitted by the host, mixed meeting audio capture is active, and media
    tracks are published. The core assistant agent uses this update to begin conversational
    turn-taking (STT/LLM/TTS).
- FAILED_EVENT ("failed"):
    The join or browser session encountered an unrecoverable failure (e.g., meeting URL invalid,
    admission denied, timeout). Includes error description in the `detail` payload so the core
    engine can record failure status, notify webhooks, or perform teardown.
- ENDED_EVENT ("ended"):
    The meeting has concluded (e.g., host ended meeting, all participants left, or worker shut down).
    The core engine uses this update to initiate room teardown, finalize call records, and generate
    recordings/transcripts.
"""

EVENTS_TOPIC = "meeting_connector_events"
STATUS_ATTRIBUTE = "lk.meeting_connector_status"
WAITING_EVENT = "waiting"
READY_EVENT = "ready"
FAILED_EVENT = "failed"
ENDED_EVENT = "ended"


