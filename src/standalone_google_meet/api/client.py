"""Client adapter for the core meeting-call API."""

import os
from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class CoreApiSettings:
    base_url: str
    api_key: str
    timeout_seconds: float = 30

    @classmethod
    def from_environment(cls) -> "CoreApiSettings":
        base_url = os.environ.get("CORE_API_URL")
        api_key = os.environ.get("CORE_API_KEY")
        if not base_url or not api_key:
            raise ValueError("CORE_API_URL and CORE_API_KEY are required")
        return cls(base_url=base_url.rstrip("/"), api_key=api_key)


class CoreMeetingClient:
    """Forward meeting-call requests to the core API."""

    def __init__(self, settings: CoreApiSettings):
        self.settings = settings

    async def join_meeting(self, payload: dict) -> httpx.Response:
        async with httpx.AsyncClient(
            base_url=self.settings.base_url,
            timeout=self.settings.timeout_seconds,
        ) as client:
            return await client.post(
                "/meeting_call/join",
                json=payload,
                headers={"Authorization": f"Bearer {self.settings.api_key}"},
            )
