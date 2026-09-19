from livekit.agents import AgentServer, WorkerOptions, cli

from standalone_google_meet.config import ConnectorRuntimeConfig
from standalone_google_meet.livekit.worker import entrypoint

runtime = ConnectorRuntimeConfig.from_environment()


def _worker_load(worker) -> float:
    return min(1.0, len(worker.active_jobs) / runtime.max_concurrent_jobs)


server = AgentServer.from_server_options(
    WorkerOptions(
        api_key=runtime.livekit_api_key,
        api_secret=runtime.livekit_api_secret,
        ws_url=runtime.livekit_url,
        entrypoint_fnc=entrypoint,
        # MUST remain "meet-connector": the LiveKit core engine explicitly dispatches
        # meeting jobs to this exact agent name over WebSocket.
        agent_name="meet-connector",
        num_idle_processes=0,
        load_fnc=_worker_load,
        load_threshold=1.0,
    )
)


if __name__ == "__main__":
    cli.run_app(server)
