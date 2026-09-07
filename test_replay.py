"""Tests for replaying a saved hunt back into the tracker.

The hunt is over and the beacon is dead, so the live dashboard now renders an
empty state forever. Replay exists so the saved readings can be put back on
screen exactly as they were captured. It is a documentation and screenshot
tool, which is precisely why it must not invent anything: every point it shows
has to come from the log, wearing the location it was recorded under.
"""

import json

from ble_web import ReadingLog, Tracker, load_saved_readings

SEPARATED = "apple_find_my_separated"


def write_log(tmp_path, rows):
    path = tmp_path / "readings.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


def test_loads_every_reading_from_the_log(tmp_path):
    path = write_log(
        tmp_path,
        [
            {"t": 1.0, "id": "a", "rssi": -70, "location": "desk"},
            {"t": 2.0, "id": "a", "rssi": -65, "location": "bed room"},
        ],
    )
    assert len(load_saved_readings(path)) == 2


def test_a_missing_log_loads_as_empty_rather_than_raising(tmp_path):
    assert load_saved_readings(tmp_path / "nope.jsonl") == []


def test_a_corrupt_line_is_skipped_not_fatal(tmp_path):
    # A JSONL file written by a process that was killed mid-flush can end in a
    # half line. Losing the whole hunt to that would be absurd.
    path = tmp_path / "readings.jsonl"
    path.write_text(
        json.dumps({"t": 1.0, "id": "a", "rssi": -70, "location": "desk"})
        + "\n{ truncated"
    )
    assert len(load_saved_readings(path)) == 1


def test_replay_restores_each_readings_own_location(tmp_path):
    # Not the tracker's current location: a replayed walk must keep the rooms
    # it was actually walked through.
    tracker = Tracker()
    tracker.set_location("somewhere else entirely")
    tracker.replay(
        [
            {"t": 1.0, "id": "a", "rssi": -95, "location": "drawing room"},
            {"t": 2.0, "id": "a", "rssi": -55, "location": "bed room"},
        ]
    )
    rooms = {r["location"]: r for r in tracker.snapshot(3.0)["by_location"]}
    assert set(rooms) == {"drawing room", "bed room"}
    assert rooms["bed room"]["best"] == -55


def test_replay_ranks_rooms_the_same_way_a_live_walk_would(tmp_path):
    tracker = Tracker()
    tracker.replay(
        [
            {"t": 1.0, "id": "a", "rssi": -95, "location": "cold room"},
            {"t": 2.0, "id": "a", "rssi": -55, "location": "hot room"},
        ]
    )
    assert tracker.snapshot(3.0)["by_location"][0]["location"] == "hot room"


def test_replay_never_writes_back_to_the_reading_log(tmp_path):
    # Replaying a log into a tracker that is also logging would double every
    # reading, and doing it twice would quadruple it.
    log_path = tmp_path / "out.jsonl"
    tracker = Tracker(reading_log=ReadingLog(log_path))
    tracker.replay([{"t": 1.0, "id": "a", "rssi": -70, "location": "desk"}])

    assert not log_path.exists() or log_path.read_text() == ""


def test_replayed_readings_are_visible_as_a_target(tmp_path):
    # The screenshot needs a live-looking target, so replay stamps the points
    # as current rather than leaving them decades stale.
    tracker = Tracker()
    tracker.replay([{"t": 1.0, "id": "a", "rssi": -55, "location": "bed room"}])
    snap = tracker.snapshot()
    assert snap["target"] is not None
    assert snap["target"]["id"] == "a"
