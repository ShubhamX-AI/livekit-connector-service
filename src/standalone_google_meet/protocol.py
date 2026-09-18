import json
from dataclasses import dataclass

JSON = 1
MIXED_AUDIO = 3
PER_PARTICIPANT_AUDIO = 5


@dataclass(frozen=True)
class ParticipantAudio:
    participant_id: str
    pcm_float32: bytes


def decode_message(message: bytes) -> tuple[int, bytes]:
    if len(message) < 4:
        raise ValueError("WebSocket message is shorter than its 4-byte type header")
    return int.from_bytes(message[:4], "little"), message[4:]


def decode_json(payload: bytes) -> dict:
    value = json.loads(payload.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON WebSocket payload must be an object")
    return value


def decode_participant_audio(payload: bytes) -> ParticipantAudio:
    if len(payload) < 1:
        raise ValueError("Participant audio payload has no participant-id length")
    participant_id_length = payload[0]
    end = 1 + participant_id_length
    if len(payload) <= end:
        raise ValueError("Participant audio payload has no audio data")
    return ParticipantAudio(payload[1:end].decode("utf-8"), payload[end:])
