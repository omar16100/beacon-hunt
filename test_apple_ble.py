"""Tests for the Apple BLE payload decoder."""

import pytest

from apple_ble import (
    decode_apple_payload,
    decode_find_my,
    estimate_distance_m,
    is_valid_rssi,
    parse_tlv,
)


def build_tlv(msg_type: int, value: bytes) -> bytes:
    return bytes([msg_type, len(value)]) + value


# A separated Find My beacon: status byte, 22 pubkey bytes, pubkey bits, hint.
FIND_MY_SEPARATED = build_tlv(0x12, bytes([0xC0]) + bytes(range(22)) + bytes([0x03, 0x00]))
FIND_MY_NEARBY = build_tlv(0x12, bytes([0x00, 0x00]))
NEARBY_INFO = build_tlv(0x10, bytes([0x05, 0x1C, 0x9B, 0x2A, 0x7F]))


class TestParseTlv:
    def test_parses_single_entry(self):
        entries = parse_tlv(NEARBY_INFO)
        assert len(entries) == 1
        assert entries[0]["type"] == 0x10
        assert entries[0]["name"] == "nearby_info"
        assert entries[0]["length"] == 5

    def test_parses_chained_entries(self):
        entries = parse_tlv(NEARBY_INFO + FIND_MY_SEPARATED)
        assert [e["name"] for e in entries] == ["nearby_info", "find_my"]

    def test_labels_unknown_type(self):
        entries = parse_tlv(build_tlv(0xAB, b"\x01\x02"))
        assert entries[0]["name"] == "unknown_0xab"

    def test_empty_payload_yields_nothing(self):
        assert parse_tlv(b"") == []

    def test_truncated_value_does_not_raise(self):
        # Declares 10 bytes but supplies 2.
        entries = parse_tlv(bytes([0x12, 0x0A, 0x01, 0x02]))
        assert len(entries) == 1
        assert entries[0]["length"] == 10
        assert len(entries[0]["value"]) == 2


class TestDecodeFindMy:
    def test_separated_state_extracts_pubkey(self):
        out = decode_find_my(bytes([0xC0]) + bytes(range(22)) + bytes([0x03, 0x00]))
        assert out["state"] == "separated"
        assert len(out["pubkey_partial"]) == 44  # 22 bytes as hex
        assert out["pubkey_bits"] == 0x03
        assert out["hint"] == 0x00

    def test_nearby_owner_state(self):
        out = decode_find_my(bytes([0x00, 0x00]))
        assert out["state"] == "nearby_owner"
        assert "pubkey_partial" not in out

    def test_battery_bits_from_status_high_bits(self):
        assert decode_find_my(bytes([0b1100_0000, 0x00]))["battery_bits"] == 3
        assert decode_find_my(bytes([0b0100_0000, 0x00]))["battery_bits"] == 1
        assert decode_find_my(bytes([0b0000_0000, 0x00]))["battery_bits"] == 0

    def test_empty_value(self):
        assert decode_find_my(b"")["state"] == "empty"


class TestDecodeApplePayload:
    def test_flags_find_my(self):
        out = decode_apple_payload(FIND_MY_SEPARATED)
        assert out["is_find_my"] is True
        assert out["find_my"]["state"] == "separated"

    def test_does_not_flag_plain_nearby_info(self):
        out = decode_apple_payload(NEARBY_INFO)
        assert out["is_find_my"] is False
        assert out["find_my"] is None
        assert out["messages"][0]["name"] == "nearby_info"

    def test_finds_find_my_when_chained_after_other_message(self):
        out = decode_apple_payload(NEARBY_INFO + FIND_MY_NEARBY)
        assert out["is_find_my"] is True
        assert out["find_my"]["state"] == "nearby_owner"

    def test_preserves_raw_hex(self):
        out = decode_apple_payload(NEARBY_INFO)
        assert out["raw"] == NEARBY_INFO.hex()


class TestIsValidRssi:
    def test_rejects_hci_unavailable_sentinel(self):
        assert is_valid_rssi(127) is False

    def test_rejects_zero_and_positive(self):
        assert is_valid_rssi(0) is False
        assert is_valid_rssi(20) is False

    def test_rejects_none(self):
        assert is_valid_rssi(None) is False

    def test_accepts_normal_range(self):
        assert is_valid_rssi(-40) is True
        assert is_valid_rssi(-99) is True

    def test_rejects_out_of_range_floor(self):
        assert is_valid_rssi(-128) is False


class TestEstimateDistance:
    def test_reference_power_is_about_one_metre(self):
        assert estimate_distance_m(-59) == pytest.approx(1.0, abs=0.01)

    def test_weaker_signal_is_further(self):
        assert estimate_distance_m(-90) > estimate_distance_m(-60)

    def test_stronger_signal_is_closer(self):
        assert estimate_distance_m(-40) < 1.0
