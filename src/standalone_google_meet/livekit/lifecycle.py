import json
import logging

from livekit import rtc

from ..events import EVENTS_TOPIC, READY_EVENT, STATUS_ATTRIBUTE

logger = logging.getLogger(__name__)

class ConnectorLifecycle:
    """Publish connector state through the worker's LiveKit participant."""

    def __init__(self, participant: rtc.LocalParticipant):
        self.participant = participant
        self._published: set[str] = set()

    async def publish(self, event: str, detail: str | None = None) -> None:
        """Publish an idempotent lifecycle event and persist readiness when applicable."""
        if event in self._published:
            return
        payload = {"event": event}
        if detail:
            payload["detail"] = detail
        await self.participant.publish_data(
            json.dumps(payload),
            reliable=True,
            topic=EVENTS_TOPIC,
        )
        if event == READY_EVENT:
            await self.participant.set_attributes({STATUS_ATTRIBUTE: READY_EVENT})
        self._published.add(event)
        logger.info("Published connector lifecycle event: %s", payload)
