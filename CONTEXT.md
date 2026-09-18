# Google Meet connector context

This context names the runtime roles and meeting concepts shared by the connector. It keeps
control, LiveKit, Chrome, and browser transport modules aligned on the same language.

## Runtime roles

**Connector worker**:
The LiveKit worker that joins one Google Meet and bridges its mixed audio into the shared room.
_Avoid_: bot process, meeting service

**Control backend**:
The request-facing entry point that asks the core API to create a meeting call.
_Avoid_: dispatcher, call state manager

**Core meeting call**:
The authoritative call record and orchestration owned by the core API.
_Avoid_: connector session, room session

**Google Meet session**:
The browser interaction that joins, remains in, and leaves one Google Meet.
_Avoid_: Chrome job, browser task

**Meeting connector event**:
A lifecycle signal describing whether the Google Meet session is waiting, ready, failed, or ended.
_Avoid_: worker status, browser event
