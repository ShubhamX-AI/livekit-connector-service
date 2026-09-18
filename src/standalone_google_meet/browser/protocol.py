import json

JSON = 1
MIXED_AUDIO = 3


def decode_message(message: bytes) -> tuple[int, bytes]:
    if len(message) < 4:
        raise ValueError("WebSocket message is shorter than its 4-byte type header")
    return int.from_bytes(message[:4], "little"), message[4:]


def decode_json(payload: bytes) -> dict:
    value = json.loads(payload.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON WebSocket payload must be an object")
    return value
