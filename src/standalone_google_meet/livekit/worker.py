import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

from livekit.agents import AutoSubscribe, JobContext

from ..chrome.session import GoogleMeetChromeSession
from ..config import ConnectorConfig, ConnectorRuntimeConfig, MeetingJobConfig
from ..events import ENDED_EVENT, FAILED_EVENT, READY_EVENT
from .audio_sync import LiveKitAudioSync
from .lifecycle import ConnectorLifecycle

logger = logging.getLogger(__name__)

# Internal queue sentinels. They never reach the core engine, which only sees the
# events declared in ..events.
JOIN_FINISHED = "__join_finished__"
SHUTDOWN = "__shutdown__"

JOIN_SHUTDOWN_GRACE_SECONDS = 5


def _job_metadata(ctx: JobContext) -> dict:
    try:
        value = json.loads(ctx.job.metadata or "{}")
    except json.JSONDecodeError as error:
        raise ValueError("Connector job metadata is not valid JSON") from error
    if not isinstance(value, dict):
        raise ValueError("Connector job metadata must be a JSON object")
    return value


def build_cleanup(
    *,
    session: GoogleMeetChromeSession,
    audio_sync: LiveKitAudioSync,
) -> Callable[[], Awaitable[None]]:
    """Return an idempotent teardown for Chrome, the WebSocket server, Xvfb and the audio bridge.

    The same callable is registered as the job's shutdown callback and awaited from the
    entrypoint's `finally`, because neither path alone covers every exit: a cancelled job
    never reaches the `finally`, and a worker drain never disconnects the room. Running it
    twice is a no-op.
    """
    finished = False

    async def cleanup() -> None:
        nonlocal finished
        if finished:
            return
        finished = True
        try:
            # `GoogleMeetChromeSession.close` null-guards the driver, the WebSocket server
            # and the virtual display, so this is safe even when the join never started.
            await asyncio.to_thread(session.close)
        except Exception:
            logger.exception("Failed to close the Google Meet Chrome session")
        try:
            await audio_sync.close()
        except Exception:
            logger.exception("Failed to close the LiveKit audio bridge")

    return cleanup


async def run_meeting_session(
    *,
    session: GoogleMeetChromeSession,
    lifecycle: ConnectorLifecycle,
    status_queue: "asyncio.Queue[tuple[str, str | None]]",
) -> str | None:
    """Publish connector lifecycle events until the meeting reaches a terminal state.

    One queue and one consumer. The browser status callback, the join thread and the room
    disconnect handler all push onto the same queue, and nothing is ever cancelled, so no
    event can be lost to a race between two futures. Returns the terminal event that was
    published, or None when the job was shut down before one arrived.
    """
    join_task = asyncio.create_task(asyncio.to_thread(session.start))

    def on_join_finished(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        error = task.exception()
        status_queue.put_nowait((JOIN_FINISHED, str(error) if error else None))

    join_task.add_done_callback(on_join_finished)

    ready = False
    terminal_event: str | None = None
    try:
        while terminal_event is None:
            event, detail = await status_queue.get()
            if event == SHUTDOWN:
                break
            if event == JOIN_FINISHED:
                if detail is None:
                    # A successful join is not the end of the job. The connector stays
                    # alive until the browser reports that the meeting ended.
                    continue
                # A failed join is published rather than raised: the core engine acts on
                # `failed` by marking the call failed and tearing down, and a raised
                # exception gives it nothing.
                event, detail = FAILED_EVENT, detail
            await lifecycle.publish(event, detail)
            if event == READY_EVENT:
                ready = True
            elif event in {FAILED_EVENT, ENDED_EVENT}:
                terminal_event = event
    except Exception as error:
        if terminal_event is None:
            terminal_event = FAILED_EVENT
            await lifecycle.publish(FAILED_EVENT, str(error))
        raise
    finally:
        if not join_task.done():
            session.stop_requested.set()
            try:
                await asyncio.wait_for(
                    asyncio.shield(join_task), timeout=JOIN_SHUTDOWN_GRACE_SECONDS
                )
            except TimeoutError:
                logger.error("Chrome join task did not finish during shutdown")
            except Exception:
                logger.exception("Chrome join task failed during shutdown")
        if ready and terminal_event is None:
            await lifecycle.publish(ENDED_EVENT, "worker_shutdown")

    return terminal_event


async def entrypoint(ctx: JobContext) -> None:
    runtime = ConnectorRuntimeConfig.from_environment()
    job = MeetingJobConfig.from_metadata(_job_metadata(ctx))

    await ctx.connect(auto_subscribe=AutoSubscribe.SUBSCRIBE_NONE)
    config = ConnectorConfig(runtime=runtime, job=job, room_name=ctx.room.name)
    lifecycle = ConnectorLifecycle(ctx.room.local_participant)
    audio_sync = LiveKitAudioSync(
        room=ctx.room,
        loop=asyncio.get_running_loop(),
        url=config.livekit_url,
        api_key=config.livekit_api_key,
        api_secret=config.livekit_api_secret,
        room_name=config.room_name,
        sample_rate=config.sample_rate,
    )
    await audio_sync.start()

    status_queue: asyncio.Queue[tuple[str, str | None]] = asyncio.Queue()
    worker_loop = asyncio.get_running_loop()

    def on_status(event: str, detail: str | None) -> None:
        worker_loop.call_soon_threadsafe(
            status_queue.put_nowait,
            (event, detail),
        )

    session = GoogleMeetChromeSession(config, audio_sync, on_status=on_status)
    cleanup = build_cleanup(session=session, audio_sync=audio_sync)
    ctx.add_shutdown_callback(cleanup)

    @ctx.room.on("disconnected")
    def on_room_disconnected(*_args) -> None:
        # The core engine deletes the room once the call is finished, so the room going
        # away is the normal end of a meeting job. A shutdown callback cannot wake the
        # loop — the SDK runs those only after it has already cancelled the entrypoint —
        # so this sentinel is what makes the exit prompt.
        status_queue.put_nowait((SHUTDOWN, None))

    try:
        await run_meeting_session(
            session=session,
            lifecycle=lifecycle,
            status_queue=status_queue,
        )
    finally:
        await cleanup()
