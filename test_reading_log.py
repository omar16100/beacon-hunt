"""Tests for the append-only reading log.

The hunt produces data that cannot be reproduced: a beacon key rotates every
few minutes, so a reading taken at 21:26 can never be taken again. Losing it to
a process restart is losing it permanently, which is why this is append-only and
flushed per line rather than buffered.
"""

import json

from ble_web import ReadingLog


def test_appends_one_json_object_per_line(tmp_path):
    path = tmp_path / "readings.jsonl"
    log = ReadingLog(path)
    log.append({"t": 1.0, "id": "a", "rssi": -70})
    log.append({"t": 2.0, "id": "a", "rssi": -68})

    lines = path.read_text().strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["rssi"] == -70
    assert json.loads(lines[1])["rssi"] == -68


def test_reopening_appends_rather_than_truncating(tmp_path):
    # A restart mid-hunt must not wipe the trail already walked.
    path = tmp_path / "readings.jsonl"
    ReadingLog(path).append({"t": 1.0, "id": "a", "rssi": -70})
    ReadingLog(path).append({"t": 2.0, "id": "a", "rssi": -68})

    assert len(path.read_text().strip().splitlines()) == 2


def test_each_line_is_flushed_immediately(tmp_path):
    # Not buffered: if the process is killed the readings so far must survive.
    path = tmp_path / "readings.jsonl"
    log = ReadingLog(path)
    log.append({"t": 1.0, "id": "a", "rssi": -70})

    assert path.read_text().strip()  # readable without closing the log


def test_creates_missing_parent_directories(tmp_path):
    path = tmp_path / "nested" / "dir" / "readings.jsonl"
    ReadingLog(path).append({"t": 1.0, "id": "a", "rssi": -70})

    assert path.exists()


def test_a_disabled_log_accepts_appends_and_writes_nothing(tmp_path):
    # `--no-log` must not force every call site into an `if log is not None`.
    log = ReadingLog(None)
    log.append({"t": 1.0, "id": "a", "rssi": -70})  # must not raise
    assert log.path is None


def test_tracker_writes_every_separated_packet_to_the_log(tmp_path):
    from ble_web import Tracker

    path = tmp_path / "readings.jsonl"
    tracker = Tracker(reading_log=ReadingLog(path))
    tracker.record("chatty", -50, "apple_active_device", now=1.0)
    tracker.record("target", -70, "apple_find_my_separated", now=2.0)

    lines = path.read_text().strip().splitlines()
    assert len(lines) == 1, "only separated traffic belongs in the hunt trail"
    assert json.loads(lines[0])["id"] == "target"
