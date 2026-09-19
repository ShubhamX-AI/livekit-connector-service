# Google Meet calls — fix status

Tracking document for the work that makes a Google Meet call reach `answered`, converse, and
end cleanly. It records what has landed in this repository and what is still outstanding,
including the parts that belong to the core repository (`lvk_agents/api_livekit`) and cannot
be fixed from here.

## Symptoms this work addresses

A meeting call joined the meeting, the operator heard only the assistant's background
ambience, the assistant never replied to anything said to it, and about sixty seconds later
everything went silent while tokens continued to be consumed.

Five defects in this repository produce that behaviour, together with the two failures that
only became visible once the first of them was fixed. All five are fixed here. Four further
items belong to the core repository and are listed at the end; one of them is blocking, and it
is the reason the assistant is still silent.

## Done — this repository

### 1. The `ready` lifecycle event was destroyed by a race in the worker loop

`src/standalone_google_meet/livekit/worker.py`

The old loop selected over two futures: the join thread's task and a freshly created
`status_queue.get()` task. `GoogleMeetChromeSession.start()` emits `ready` as the last thing it
does before returning (`chrome/session.py:199-201`), so the event and the join completion
landed in the same event-loop wakeup. The select checked the join task first, cancelled a
`get()` task that had *already removed the item from the queue*, never read its result, and
broke. `ready` was gone. The loop then blocked forever on an empty queue and the SDK had to
cancel the job.

The select is gone. There is now one queue and one consumer, and every producer pushes onto it:

- the browser status callback pushes `(event, detail)` as before;
- the join thread's completion pushes a `JOIN_FINISHED` sentinel — a successful join is *not*
  the end of the job, because the connector stays alive until the browser reports
  `meeting_ended` (`browser/websocket.py:105-109`), so the loop continues;
- a join that raises pushes `failed` with the exception text instead of propagating, because
  the core acts on `failed` by marking the call failed and tearing down, whereas a raised
  exception gives it nothing;
- the room's `disconnected` event pushes a `SHUTDOWN` sentinel. The core deletes the room when
  the call finishes, so the room going away is the normal end of a meeting job. Without this
  the loop would wait on a queue nobody writes to again.

Nothing is cancelled any more, so nothing can be dropped, and the outcome no longer depends on
which future happens to win.

The loop was also extracted into a module-level `run_meeting_session(...)` coroutine that takes
no `JobContext`, which is what makes it testable. `entrypoint` keeps configuration parsing,
`ctx.connect()`, the audio bridge and the `room.on("disconnected")` wiring.

A `NameError` on the old `except` path (it referenced `status_task`, which could be unbound and
would have masked the real error) disappeared with the select.

### 2. Cleanup was not guaranteed

`src/standalone_google_meet/livekit/worker.py`

The old `finally` only called `session.close()` when the join task had already finished, so a
join thread that outlived the five-second shutdown shield leaked Chrome, the virtual display
and the WebSocket server for the lifetime of the process.

`build_cleanup(...)` now returns one idempotent teardown covering Chrome, the WebSocket server,
Xvfb and the audio bridge. It is registered both in the entrypoint's `finally` and through
`ctx.add_shutdown_callback`, because neither path alone covers every exit: a cancelled job
never reaches the `finally`, and a worker drain never disconnects the room. Running it twice is
a no-op.

A shutdown callback cannot unblock the loop — the SDK awaits the shutdown future, gives the
entrypoint 15 seconds, cancels it, and only then runs the callbacks — so the `disconnected`
sentinel above is what makes the exit prompt. The callback is the safety net, not the trigger.

### 3. The bot was deaf to anyone who joined after it

`src/standalone_google_meet/browser/assets/google_meet_chromedriver_payload.js`

`StyleManager.startSilenceDetection` mapped `this.audioTracks` into Web Audio source nodes
exactly once and connected that snapshot to the mix destination. It runs from
`enableMediaSending()`, which `chrome/session.py:199` fires the instant the bot is admitted —
and Google Meet delivers most participants' audio tracks to the `track` listener *after*
admission. Every track pushed into `addAudioTrack` after that instant was never connected to
the destination, so the mix was empty or near-empty and the assistant heard nothing.

The graph is now incremental. `StyleManager` keeps the destination on `this.mixDestination` and
a map of already-connected tracks to their source nodes. `addAudioTrack` connects each track as it arrives;
`startSilenceDetection` creates the destination and then connects whatever had been buffered
before it ran. A track that arrives before the AudioContext exists stays in `this.audioTracks`
and is picked up when the context is created.

### 4. The assistant's own audio was being mixed back into the meeting audio

`src/standalone_google_meet/browser/assets/google_meet_chromedriver_payload.js`,
`src/standalone_google_meet/browser/assets/livekit-client-adapter.js`

Found by the first real call after fix 3, which produced severe echo on the recording, no
transcription and no reply from the assistant.

`RTCInterceptor` replaces `window.RTCPeerConnection` globally, so the LiveKit room the adapter
opens to receive the assistant's audio is built through the same wrapper and its remote tracks
reach the `track` listener that feeds `addAudioTrack`. Before fix 3 that was harmless: the mix
snapshot was taken in `styleManager.start()`, and the adapter connects afterwards, so the
assistant's track was excluded by accident. Once the graph became incremental, the assistant's
own voice was mixed into the "meeting audio" sent back to LiveKit — a feedback loop that grows
on every round trip, which is what the echo was, and which swamped the assistant's own input so
it never replied.

Two guards, because one alone is not enough:

- Peer connections created while the adapter is connecting are tagged
  `excludeFromMeetingAudioMix`, and the `track` listener skips their audio.
- `StyleManager.removeAudioTrack` takes a track back out of the mix, and the adapter calls it
  for every remote audio track it receives. This catches a LiveKit reconnect that builds a
  fresh peer connection outside the tagging window.

`connectedAudioTracks` changed from a `Set` to a `Map` of track to source node so a track can be
disconnected again.

### 5. The bot stayed in the meeting after everyone left

`src/standalone_google_meet/browser/websocket.py`, `src/standalone_google_meet/config.py`

Also found by the first real call. Google Meet only reports `meeting_ended` minutes after the
last human leaves, and the core engine will not tear the call down until this connector says the
meeting is over — so the two waited for each other. It was previously hidden by the core killing
the job after sixty seconds.

The WebSocket server now tracks which participants Meet says are in the call. When the bot is
the only one left for `ALONE_IN_MEETING_TIMEOUT_SECONDS` (default 30), it emits
`ended` with detail `alone_in_meeting`. A participant rejoining inside that window cancels the
timer.

### 6. An amplitude probe on the mixed audio

`src/standalone_google_meet/livekit/audio_sync.py`

`_capture_audio` now logs the peak sample of each mixed frame at roughly 1 Hz. A flat zero
while somebody in the meeting is speaking means the mix is empty, which is a different fault
from the assistant hearing silence. This is permanent instrumentation, not a scratch patch.

### 7. The browser adapter's verdict on the assistant's track is now logged

`src/standalone_google_meet/browser/websocket.py`

The page's LiveKit adapter already reported whether it accepted the assistant's audio track —
`LiveKitTrackAdded` or `LiveKitTrackNotAccepted` — but `_handle_json` dropped every message
type it did not recognise, so a rejected track was silent on both sides and the meeting simply
never heard the assistant. Those messages, plus `LiveKitConnectionFailed`,
`LiveKitVideoTrackNotAvailable` and the page's own `Error`, are now logged.

This is what distinguishes "the assistant's track never arrived" from "it arrived and the
adapter rejected it", which is the open question in core item A below.

### 8. Regression tests

`tests/test_connector.py`, using the existing `FakeParticipant` plus a new `FakeChromeSession`
whose join runs on the worker's thread pool:

| Test | Asserts |
|---|---|
| `test_ready_survives_a_join_that_returns_immediately` | A join that emits `ready` and returns still gets `ready` published, and the status attribute set. This is the regression guard for defect 1. |
| `test_loop_exits_on_room_disconnect` | The `SHUTDOWN` sentinel ends the loop without publishing anything. |
| `test_room_disconnect_after_ready_publishes_ended` | A disconnect after `ready` publishes `ended` with reason `worker_shutdown`. |
| `test_meeting_ended_is_terminal` | `ended` after `ready` stops the loop and is published once, not twice. |
| `test_join_failure_publishes_failed` | A join that raises publishes `failed` carrying the exception text, and does not propagate. |
| `test_publish_failure_after_ready_does_not_also_publish_ended` | A failure is terminal. The shutdown path must not publish `ended` on top of `failed` for the same job. |
| `test_cleanup_runs_exactly_once` | The `finally` and the shutdown callback together close Chrome and the audio bridge once. |
| `test_bot_alone_in_the_meeting_ends_the_call` | Every human leaving arms the timer and publishes `ended` with `alone_in_meeting`; a second participant present does not. |

Every test is bounded by `asyncio.wait_for(..., 2)`, so a regression is a failure rather than a
hung suite.

Honest note: these tests exercise the extracted `run_meeting_session` seam, which did not exist
before this change, so they cannot be run against the pre-fix code to watch them go red. They
guard the behaviour from here on.

### Verification run

```bash
uv run python -m unittest discover -s tests -v   # 18 tests, all passing
uvx ruff check src tests                          # clean
node --check src/standalone_google_meet/browser/assets/google_meet_chromedriver_payload.js
node --check src/standalone_google_meet/browser/assets/livekit-client-adapter.js
```

### What the real calls proved

Both runs were against an unchanged core.

**First run, 18:58 on 2026-09-19** — fixes 1 to 3 and 6 in place:

- `Published connector lifecycle event: {'event': 'ready'}` — fix 1 works, the event is no
  longer destroyed;
- `Mixed Google Meet audio peak amplitude:` reported real values (hundreds to twenty thousand)
  — fix 3 works, the bot hears the meeting;
- the job stayed alive instead of being cancelled at sixty seconds;
- the recording echoed badly, there was no transcript and the assistant never spoke. The echo
  is what led to fixes 4 and 5.

**Second run** — fixes 4 and 5 in place:

- the echo is gone and the recording carries the human voice clearly, so fix 4 works;
- the realtime model is still being billed;
- there is still no assistant audio in the meeting and still no transcript.

That remaining failure is core item A below. Fix 7 was added so the next run reports, from this
side, which half of it is true.

## Not done — core repository (`/home/shubham_halder/CODE/lvk_agents/api_livekit`)

**The connector fixes alone do not make the feature work.** Item A is why the assistant is
still silent. Item B is why a slow admission still kills a healthy connector at sixty seconds.
They are independent of each other and both are blocking.

### A. Confirm `is_meeting_call` is actually true for the agent job — blocking, check first

Symptom after fixes 1 to 6: the recording contains the human's voice clearly, the realtime
model is being billed, and yet there is no assistant audio in the meeting and no transcript.

One condition explains all three at once. In `src/core/agents/session.py:325`,
`is_meeting_call = job_metadata.get("call_type") == CALL_TYPE_MEETING`. Every meeting-specific
branch hangs off it:

- `room_options.participant_kinds` (`:1088`) is only widened to include
  `PARTICIPANT_KIND_AGENT` for meeting calls. The connector joins the room as an agent-kind
  participant, and RoomIO ignores agent participants by default — so without this the realtime
  model receives no audio at all, which is exactly "no transcript".
- `lk.publish_on_behalf` (`:1275-1277`) is only set for meeting calls. The browser adapter
  matches the assistant on that attribute (`livekit-client-adapter.js:96-117`) and rejects any
  publisher without it — so without this the meeting never hears the assistant.
- the `meeting_connector_events` data handler (`:1204`) is gated on it too, so `ready` would be
  ignored even though the connector published it.

Billing continues either way, because `session.start()` runs regardless.

Check it before changing anything else: the core log line
`Announced agent to meeting connector via lk.publish_on_behalf=<room>` appears only when
`is_meeting_call` is true. If that line is missing from the agent's log, the agent job's
metadata is not carrying `call_type: "meeting"` and that is the whole fault. `lk room
participants list --room <room>` shows the same thing from outside: the agent participant
should carry a `lk.publish_on_behalf` attribute.

The connector now logs the browser adapter's own verdict — `LiveKitTrackAdded` versus
`LiveKitTrackNotAccepted` — so the next run says from this side whether the assistant's track
arrived and was rejected, or never arrived at all.

### B. Split the connector deadline into two settings — blocking

`src/core/config.py:117-119` defines a single `MEETING_CONNECTOR_READY_TIMEOUT_SECONDS`,
defaulting to `60`, and uses it for two unrelated things: waiting for the connector participant
to appear in the room (`src/core/agents/session.py:1341-1346`) and waiting for the connector to
report `ready` (`session.py:674-681`). Both timeout paths reach `_flush_and_end_call`, which
deletes the room (`session.py:670`).

This connector gives a human 300 seconds to admit the bot
(`src/standalone_google_meet/config.py:27`, `WAITING_ROOM_TIMEOUT_SECONDS`). Sixty seconds is
therefore guaranteed to kill a connector that is still legitimately waiting — and it is exactly
the "everything went silent about sixty seconds later" in the original report.

Two questions, two settings:

| Setting | Default | What it bounds |
|---|---|---|
| `MEETING_CONNECTOR_JOIN_TIMEOUT_SECONDS` | `120` | The connector participant appearing in the room: container start, process init, `audio_sync.start()`. Observed at 3 s. Use at `session.py:1341-1346`. |
| `MEETING_CONNECTOR_READY_TIMEOUT_SECONDS` | `360` | The connector reporting `ready`. Must exceed this connector's 300 s waiting-room budget, so that the connector's own `failed` arrives first and carries a real reason. Use at `session.py:674-681`. |

Document the coupling where the default is defined: 360 is only correct while this repository's
`WAITING_ROOM_TIMEOUT_SECONDS` is 300. Pin it with a test in `tests/test_meeting_calls.py`
asserting `MEETING_CONNECTOR_READY_TIMEOUT_SECONDS >= 360` and that the join timeout is the
smaller of the two — that test is the only place the cross-repository contract can be recorded.

Accepted regression: a dead connector holds a `MEETING` capacity slot for six minutes rather
than one (`MAX_CONCURRENT_MEETING_CALLS`, default 4).

### C. Set `lk.publish_on_behalf` before `session.start()` — recommended

`session.py:1275-1277` sets the attribute *after* `session.start()` at `:1263` has already
published the agent's track. The browser-side adapter matches the agent only inside
`addRemoteTrack` (`browser/assets/livekit-client-adapter.js:96-134`) and has no
`ParticipantAttributesChanged` retry, so if the browser subscribes before the attribute lands
it emits `LiveKitTrackNotAccepted` and the assistant is permanently inaudible in the meeting.

In practice the browser only connects at admission, which is usually seconds later, so this
rarely fires — but it is a two-line hardening: move the `set_attributes` call above
`session.start()`.

### D. Stop burning tokens before the connector is ready — after A to C are verified

Chosen form (the cheap one): for `is_meeting_call` only, set `speech_gate.muted = True` before
`session.start()` and clear it once `_wait_for_meeting_connector()` returns True. This is the
same lever `InputGuardController` uses at `session.py:1517`. Realtime input-audio tokens are
the bulk of the burn and silence keeps the stream flowing, which is why `muted` exists rather
than detaching the input. About five lines. It removes the per-second cost, not the
session-open cost.

Also move the `recorder.start_once()` at `session.py:431-432` behind `ready` for meeting calls,
or the recording is minutes of silence. `RecordingManager.start_once()` is already idempotent,
so this is a one-line move.

Gate every line on `is_meeting_call`: this is the hottest function in that repository and every
call type flows through it.

## Open and deliberately not built

- **`waiting` restarting the readiness clock.** `_handle_meeting_connector_event` already
  receives `waiting`, so it is a small change, but the fixed numbers in B already cover the
  case. Build it only if real admissions start running past six minutes.
- **`num_idle_processes=0` in `agent_run.py:22`.** Costs about 2.2 s of cold start per job.
  Setting it to `1` buys that back at the price of one idle process. Left alone.
- **`CONNECTOR_MAX_CONCURRENT_JOBS` defaults to `1`.** One meeting per replica, `load=1.0`
  immediately afterwards. Working as designed; scale by replicas.
- **Audio queue overflow.** `audio_sync.py` drops the oldest frame when its 32-frame queue is
  full. That causes glitching under backpressure, never silence. Not worth changing without
  evidence it happens.
- **`publisher data channel '_data_track' closed unexpectedly`.** Log noise. `publish_data`
  uses the `_lossy` or `_reliable` channel depending on the packet kind and never `_data_track`,
  and the handler only logs. Filter that logger to `WARNING` if it is irritating.

## End-to-end check, still outstanding

Run one real meeting (`POST /meeting_call/join`, admit the bot by hand) **after** core changes
A and B have landed, and confirm:

- the connector log shows `Published connector lifecycle event: {'event': 'ready'}`;
- the core log shows the call moving from `initiated` to `answered`;
- `Mixed Google Meet audio peak amplitude:` reports a non-zero value while a human speaks;
- the assistant greets, and answers when spoken to;
- `lk room participants list --room <room>` shows both the agent and the connector;
- leaving the meeting produces `ended`, a finalised `CallRecord`, a `UsageRecord` and exactly
  one end-of-call webhook;
- the room recording contains both voices.

Then delay admission past 60 seconds deliberately, to confirm the room survives, and past 300
seconds, to confirm this connector's own timeout produces a failed call with a real reason.
