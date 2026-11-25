"""Pure Python feed decoder; shared verbatim with the desktop plugin."""
import json

SLOT_SIZE = 4096
FEED_SIZE = SLOT_SIZE * 2
FEED_SCHEMA = "magpie-usage-feed-v1"
MAX_SEQUENCE = 9007199254740991


def checksum(sequence, payload):
    value = 2166136261
    for char in str(sequence) + "|" + payload:
        value = ((value ^ ord(char)) * 16777619) & 0xffffffff
    return "{:08x}".format(value)


def decode_slots(data):
    result = []
    if not isinstance(data, str) or len(data) != FEED_SIZE:
        return result
    for index in range(2):
        try:
            slot = json.loads(data[index * SLOT_SIZE:(index + 1) * SLOT_SIZE])
            sequence, payload = slot["sequence"], slot["payload"]
            if (type(sequence) is not int or not 0 <= sequence <= MAX_SEQUENCE
                    or not isinstance(payload, str)
                    or slot["checksum"] != checksum(sequence, payload)):
                continue
            value = json.loads(payload)
            if (not isinstance(value, dict) or value.get("schema") != FEED_SCHEMA
                    or type(value.get("failed")) is not bool
                    or (value.get("snapshot") is not None and not isinstance(value["snapshot"], dict))):
                continue
            result.append((sequence, index, value))
        except (ValueError, TypeError, KeyError):
            pass
    return result


def latest(data):
    slots = decode_slots(data)
    return max(slots, key=lambda slot: slot[0]) if slots else None
