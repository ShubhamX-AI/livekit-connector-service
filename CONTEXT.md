# Google Meet connector context

This context names the runtime roles and meeting concepts shared by the connector. It keeps
VoiceKit, core backend, LiveKit, Chrome, and browser transport modules aligned on the same language.

## Runtime roles

**VoiceKit service**:
The authoritative platform service holding user accounts, user IDs, VoiceKit API keys, and
agent configurations. Meeting triggers (e.g. from callers or Wispr API) MUST pass through
VoiceKit, which triggers the core backend engine.
_Avoid_: client-direct core caller, headless auth bypass

**Core LiveKit engine / Core backend**:
The central orchestration engine. It creates the LiveKit room, manages call records, and
dispatches agent jobs (`api-agent` and `meet-connector`) to workers over LiveKit WebSocket.
_Avoid_: worker manager, frontend router

**Connector worker** (MANDATORY RUNTIME):
The LiveKit worker in this service (`agent_run.py`) connected to the core engine over WebSocket.
It registers with the fixed agent name `agent_name="meet-connector"`, which is the exact target
name the core engine uses for job dispatches. It joins the Google Meet session and bridges mixed
audio into the shared room. It must be continuously running to accept dispatched jobs.
_Avoid_: bot process, meeting service

**Control backend** (OPTIONAL ADAPTER):
An optional local HTTP adapter (`control_run.py`) that forwards meeting join requests to
the core API using VoiceKit credentials. Used primarily for local development/testing.
_Avoid_: dispatcher, call state manager

**Core meeting call**:
The authoritative call record and orchestration owned by the core API.
_Avoid_: connector session, room session

**Google Meet session**:
The browser interaction that joins, remains in, and leaves one Google Meet.
_Avoid_: Chrome job, browser task

**Meeting connector event**:
A lifecycle signal published over the LiveKit data channel (`meeting_connector_events`)
and participant attribute (`lk.meeting_connector_status`) informing the LiveKit core engine
and conversational agent whether the meeting session is `waiting` (in knock room),
`ready` (admitted and audio streaming), `failed` (join or runtime error), or `ended` (meeting concluded).
This forms a shared, mirrored contract that must be identically defined in the LiveKit core service.
_Avoid_: worker status, browser event


