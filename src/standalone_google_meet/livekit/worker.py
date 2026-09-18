import asyncio
import json
import logging

from livekit.agents import AutoSubscribe, JobContext

from ..chrome.session import GoogleMeetChromeSession
from ..config import ConnectorConfig, ConnectorRuntimeConfig, MeetingJobConfig
from .audio_sync import LiveKitAudioSync
from .lifecycle import (
    ENDED_EVENT,
    FAILED_EVENT,
    READY_EVENT,
    ConnectorLifecycle,
)

logger = logging.getLogger(__name__)


def _job_metadata(ctx: JobContext) -> dict:
    try:
        value = json.loads(ctx.job.metadata or "{}")
    except json.JSONDecodeError as error:
        raise ValueError("Connector job metadata is not valid JSON") from error
    if not isinstance(value, dict):
        raise ValueError("Connector job metadata must be a JSON object")
    return value


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
    session_task = asyncio.create_task(asyncio.to_thread(session.start))
    terminal_event: str | None = None
    ready = False

    try:
        while not session_task.done():
            status_task = asyncio.create_task(status_queue.get())
            done, _ = await asyncio.wait(
                {session_task, status_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if session_task in done:
                status_task.cancel()
                await asyncio.gather(status_task, return_exceptions=True)
                session_task.result()
                break
            event, detail = status_task.result()
            await lifecycle.publish(event, detail)
            if event == READY_EVENT:
                ready = True
            elif event in {FAILED_EVENT, ENDED_EVENT}:
                terminal_event = event
                break

        while terminal_event is None:
            event, detail = await status_queue.get()
            await lifecycle.publish(event, detail)
            if event == READY_EVENT:
                ready = True
            elif event in {FAILED_EVENT, ENDED_EVENT}:
                terminal_event = event
    except Exception as error:
        if status_task and not status_task.done():
            status_task.cancel()
            await asyncio.gather(status_task, return_exceptions=True)
        if not terminal_event:
            await lifecycle.publish(FAILED_EVENT, str(error))
        raise
    finally:
        if not session_task.done():
            session.stop_requested.set()
            try:
                await asyncio.wait_for(asyncio.shield(session_task), timeout=5)
            except TimeoutError:
                logger.error("Chrome join task did not finish during shutdown")
        if session_task.done():
            await asyncio.to_thread(session.close)
        await audio_sync.close()
        if ready and terminal_event is None:
            await lifecycle.publish(ENDED_EVENT, "worker_shutdown")
