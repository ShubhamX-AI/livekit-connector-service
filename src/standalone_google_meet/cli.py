import logging
import signal
import threading

from standalone_google_meet.chrome_session import GoogleMeetChromeSession
from standalone_google_meet.config import ConnectorConfig
from standalone_google_meet.livekit_sync import LiveKitAudioSync

logger = logging.getLogger(__name__)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = ConnectorConfig.from_environment()
    sync = LiveKitAudioSync(
        url=config.livekit_url,
        api_key=config.livekit_api_key,
        api_secret=config.livekit_api_secret,
        room_name=config.livekit_room,
        sample_rate=config.sample_rate,
    )
    session = GoogleMeetChromeSession(config, sync)
    stop = threading.Event()

    def shutdown(*_):
        stop.set()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    try:
        session.start()
        logger.info("Connector running. Press Ctrl+C to stop.")
        stop.wait()
    finally:
        session.close()
        sync.close()


if __name__ == "__main__":
    main()
