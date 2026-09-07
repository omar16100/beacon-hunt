"""Decoder for Apple's BLE manufacturer-data payloads (Continuity + Find My).

Apple advertises under company ID 0x004C. The payload is a TLV chain:
    [type:1][length:1][value:length] ... repeated

Only a subset of types matter for locating a lost device, so unknown types are
preserved as raw hex rather than dropped.
"""

import logging

log = logging.getLogger(__name__)

APPLE_COMPANY_ID = 0x004C

# Continuity message types. Names follow the widely documented reverse
# engineering of Apple's Continuity protocol; Apple publishes no spec.
CONTINUITY_TYPES = {
    0x02: "ibeacon",
    0x05: "airdrop",
    0x06: "homekit",
    0x07: "proximity_pairing",
    0x08: "hey_siri",
    0x09: "airplay_target",
    0x0A: "airplay_source",
    0x0B: "magic_switch",
    0x0C: "handoff",
    0x0D: "tethering_target",
    0x0E: "tethering_source",
    0x0F: "nearby_action",
    0x10: "nearby_info",
    0x12: "find_my",
}

# Find My payload lengths. A separated beacon carries a truncated rotating
# public key; a nearby-owner beacon carries almost nothing.
FIND_MY_LEN_SEPARATED = 0x19
FIND_MY_LEN_NEARBY = 0x02


def parse_tlv(payload: bytes) -> list[dict]:
    """Split an Apple manufacturer-data blob into its TLV entries."""
    entries = []
    i = 0
    while i + 1 < len(payload):
        msg_type = payload[i]
        length = payload[i + 1]
        value = payload[i + 2 : i + 2 + length]
        if len(value) != length:
            log.debug(
                "truncated TLV: type=0x%02x declared_len=%d actual_len=%d",
                msg_type,
                length,
                len(value),
            )
        entries.append(
            {
                "type": msg_type,
                "name": CONTINUITY_TYPES.get(msg_type, f"unknown_0x{msg_type:02x}"),
                "length": length,
                "value": value,
            }
        )
        i += 2 + length
    if i == len(payload) - 1:
        log.debug("orphan trailing byte 0x%02x with no length byte", payload[i])
    return entries


def decode_find_my(value: bytes, declared_len: int | None = None) -> dict:
    """Interpret a Find My (offline finding) payload.

    The rotating public key is NOT resolvable without the owner's private key
    held in the Secure Enclave, so this reports structure only, never identity.

    State is decided from the TLV's DECLARED length, not from how many bytes
    actually arrived. A separated frame that gets truncated in transit still
    declares 0x19, and must never be reported as `nearby_owner`: that would tell
    someone hunting a lost device "this beacon is with its owner, ignore it",
    which is the most harmful mislabel this decoder can produce.
    """
    if not value:
        return {"state": "empty", "truncated": False}

    status = value[0]
    declared = declared_len if declared_len is not None else len(value)
    truncated = len(value) < declared
    out = {
        "status_byte": status,
        # Bits 6-7 of the status byte are widely reported to encode a coarse
        # battery level. Reported as raw so it is not over-trusted.
        "battery_bits": (status >> 6) & 0x03,
        "declared_length": declared,
        "actual_length": len(value),
        "truncated": truncated,
    }

    if declared >= FIND_MY_LEN_SEPARATED:
        out["state"] = "separated_truncated" if truncated else "separated"
        out["pubkey_partial"] = value[1:23].hex()
        out["pubkey_bits"] = value[23] if len(value) > 23 else None
        out["hint"] = value[24] if len(value) > 24 else None
    elif declared <= FIND_MY_LEN_NEARBY:
        out["state"] = "nearby_owner"
    else:
        # Real frames are 2 bytes (nearby) or 25 (separated). Anything between
        # is unrecognised, and must not be collapsed into either bucket.
        out["state"] = "unknown_length"
    return out


def decode_apple_payload(payload: bytes) -> dict:
    """Decode a full Apple manufacturer-data blob into a summary dict."""
    entries = parse_tlv(payload)
    summary = {
        "raw": payload.hex(),
        "messages": [],
        "is_find_my": False,
        "find_my": None,
    }
    for entry in entries:
        msg = {
            "type": entry["type"],
            "name": entry["name"],
            "hex": entry["value"].hex(),
        }
        if entry["type"] == 0x12:
            summary["is_find_my"] = True
            summary["find_my"] = decode_find_my(entry["value"], entry["length"])
            msg["decoded"] = summary["find_my"]
        summary["messages"].append(msg)
    return summary


def is_valid_rssi(rssi) -> bool:
    """Reject the HCI 'RSSI not available' sentinel and other impossible values.

    127 is the documented unavailable marker. Real BLE RSSI is negative; anything
    at or above 0 dBm from a passive scan is not a genuine reading.
    """
    return rssi is not None and rssi < 0 and rssi > -128


def estimate_distance_m(rssi: int, tx_power: int = -59, path_loss_n: float = 2.5) -> float:
    """Very rough log-distance path-loss estimate, in metres.

    Indoor multipath makes this unreliable in absolute terms. It is useful only
    for relative comparison as you walk around, which is the whole hot/cold idea.
    """
    return 10 ** ((tx_power - rssi) / (10 * path_loss_n))


# Retention priority for a device's label across many packets. Apple devices
# alternate frame types and some CoreBluetooth events carry no manufacturer data
# at all, so the LAST packet is not representative. A device that ever emitted a
# separated Find My frame must keep that label.
KIND_PRIORITY = {
    "apple_find_my_separated": 100,
    "apple_find_my_separated_truncated": 95,
    "apple_find_my_unknown_length": 90,
    "apple_find_my_nearby_owner": 80,
    "apple_find_my_empty": 75,
    "apple_proximity_pairing": 60,
    "apple_active_device": 50,
    "apple_other": 40,
    "non_apple": 10,
    "unknown": 0,
}


def kind_priority(kind: str) -> int:
    """Rank a classification label. Unknown labels sort above non_apple."""
    return KIND_PRIORITY.get(kind, 30)


def merge_kind(existing: str, incoming: str) -> str:
    """Keep whichever label is more informative across a device's packets."""
    if existing is None:
        return incoming
    return incoming if kind_priority(incoming) > kind_priority(existing) else existing


def classify_device(adv_data) -> str:
    """Best-effort label for a scanned advertisement."""
    apple = adv_data.manufacturer_data.get(APPLE_COMPANY_ID)
    if apple is None:
        return "non_apple"
    decoded = decode_apple_payload(apple)
    if decoded["is_find_my"]:
        return f"apple_find_my_{decoded['find_my']['state']}"
    names = [m["name"] for m in decoded["messages"]]
    if "nearby_info" in names:
        return "apple_active_device"
    if "proximity_pairing" in names:
        return "apple_proximity_pairing"
    return "apple_other"
