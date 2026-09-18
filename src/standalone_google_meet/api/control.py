import logging
from typing import Literal

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, HttpUrl

from .client import CoreApiSettings, CoreMeetingClient

logger = logging.getLogger(__name__)

app = FastAPI(title="Google Meet Connector Control")


class JoinMeetingRequest(BaseModel):
    assistant_id: str = Field(min_length=1, max_length=100)
    meeting_url: HttpUrl
    platform: Literal["google_meet"] = "google_meet"
    bot_display_name: str | None = Field(default=None, max_length=100)
    metadata: dict | None = None


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/meetings/join")
async def join_meeting(request: JoinMeetingRequest):
    try:
        client = CoreMeetingClient(CoreApiSettings.from_environment())
    except ValueError as error:
        raise HTTPException(status_code=500, detail=str(error)) from error

    payload = request.model_dump(mode="json", exclude_none=True)
    try:
        response = await client.join_meeting(payload)
    except httpx.HTTPError as error:
        logger.error("Core API meeting join request failed: %s", error)
        raise HTTPException(status_code=502, detail="Core API request failed") from error

    try:
        content = response.json()
    except ValueError:
        content = {"detail": "Core API returned an invalid response"}
    if response.is_error:
        return JSONResponse(status_code=response.status_code, content=content)
    return content
