"""Tests for tagging readings with the room they were taken in.

A room-to-room hunt is a comparison, and a comparison needs labels. Without
them the history chart is one undifferentiated line and the walk has to be
reconstructed from memory afterwards, which is how the first four rooms tonight
nearly got lost.
"""

import json

from ble_web import ReadingLog, Tracker, summarise_by_location

NOW = 1000.0
SEPARATED = "apple_find_my_separated"


def test_readings_are_tagged_with_the_current_location():
    tracker = Tracker()
    tracker.set_location("desk")
    tracker.record("a", -70, SEPARATED, now=NOW)

    assert tracker.snapshot(NOW)["history"][0]["location"] == "desk"


def test_a_location_change_never_retro_labels_earlier_readings():
    # The whole point is comparing rooms. Relabelling the drawing room's
    # readings as "guest room" on arrival would silently fake the comparison.
    tracker = Tracker()
    tracker.set_location("drawing room")
    tracker.record("a", -95, SEPARATED, now=NOW)
    tracker.set_location("guest room")
    tracker.record("a", -70, SEPARATED, now=NOW + 1)

    history = tracker.snapshot(NOW + 1)["history"]
    assert [p["location"] for p in history] == ["drawing room", "guest room"]


def test_readings_taken_before_any_location_is_set_are_kept_not_dropped():
    tracker = Tracker()
    tracker.record("a", -70, SEPARATED, now=NOW)

    point = tracker.snapshot(NOW)["history"][0]
    assert point["location"] == "unknown"


def test_the_current_location_is_reported_in_the_snapshot():
    tracker = Tracker()
    tracker.set_location("kitchen")
    assert tracker.snapshot(NOW)["location"] == "kitchen"


def test_blank_input_does_not_wipe_the_current_location():
    # A stray Enter on an empty box mid-walk must not silently unlabel the room.
    tracker = Tracker()
    tracker.set_location("attic")
    tracker.set_location("   ")
    assert tracker.snapshot(NOW)["location"] == "attic"


def test_location_is_written_to_the_reading_log(tmp_path):
    path = tmp_path / "readings.jsonl"
    tracker = Tracker(reading_log=ReadingLog(path))
    tracker.set_location("desk")
    tracker.record("a", -70, SEPARATED, now=NOW)

    assert json.loads(path.read_text().strip())["location"] == "desk"


# MARK: - summary


def points(*rows):
    return [{"t": NOW, "id": "a", "rssi": r, "location": loc} for loc, r in rows]


def test_summary_groups_readings_by_room():
    summary = summarise_by_location(points(("desk", -75), ("desk", -71), ("hall", -90)))
    rooms = {row["location"]: row for row in summary}

    assert rooms["desk"]["packets"] == 2
    assert rooms["desk"]["best"] == -71
    assert rooms["hall"]["packets"] == 1


def test_summary_reports_a_median_not_just_a_best():
    # One lucky packet at -60 should not promote a room whose typical read is -80.
    summary = summarise_by_location(points(("desk", -60), ("desk", -80), ("desk", -80)))

    assert summary[0]["best"] == -60
    assert summary[0]["median"] == -80


def test_summary_ranks_the_strongest_room_first():
    # The top row is the answer to "where do I search", so it must lead.
    summary = summarise_by_location(
        points(("cold", -95), ("hot", -65), ("middling", -80))
    )
    assert [row["location"] for row in summary] == ["hot", "middling", "cold"]


def test_summary_of_nothing_is_empty_rather_than_an_error():
    assert summarise_by_location([]) == []
