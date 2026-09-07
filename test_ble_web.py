"""Tests for the auto-reacquiring tracker behind the web dashboard.

The point of this module over `watch` mode: `watch` locks onto one
CoreBluetooth id, and a Find My key rotation retires that id silently, so the
meter dies without saying so. Tracking by *state* survives rotation, because a
rotated beacon is still a separated beacon.
"""

from ble_web import Tracker, pick_target, signal_band, signal_percent

NOW = 1000.0
SEPARATED = "apple_find_my_separated"


def cand(device_id, kind, rssi, last_seen):
    return {"id": device_id, "kind": kind, "rssi": rssi, "last_seen": last_seen}


# MARK: - pick_target


def test_no_candidates_gives_no_target():
    assert pick_target([], NOW) is None


def test_a_non_separated_beacon_is_never_the_target():
    # nearby_owner means the thing is sitting with its owner. Chasing one is the
    # most common way to waste an evening, so strength must not rescue it.
    loud = [cand("a", "apple_find_my_nearby_owner", -40, NOW)]
    assert pick_target(loud, NOW) is None


def test_the_strongest_separated_beacon_wins():
    picked = pick_target(
        [
            cand("far", SEPARATED, -95, NOW),
            cand("near", SEPARATED, -67, NOW),
        ],
        NOW,
    )
    assert picked["id"] == "near"


def test_a_stale_beacon_is_ignored_however_strong():
    # The key-rotation case: the retired id must not keep winning on the
    # strength of a reading taken five minutes ago.
    picked = pick_target(
        [
            cand("old", SEPARATED, -60, NOW - 400),
            cand("new", SEPARATED, -80, NOW),
        ],
        NOW,
        stale_after=180.0,
    )
    assert picked["id"] == "new"


def test_everything_stale_gives_no_target():
    stale = [cand("old", SEPARATED, -60, NOW - 400)]
    assert pick_target(stale, NOW, stale_after=180.0) is None


def test_a_truncated_separated_frame_still_counts_as_a_target():
    # Python's decoder keeps `separated_truncated` distinct. Dropping it here
    # would hide the very beacon the truncation warning is about.
    picked = pick_target([cand("t", "apple_find_my_separated_truncated", -70, NOW)], NOW)
    assert picked["id"] == "t"


# MARK: - bands


def test_signal_bands_run_from_close_to_far():
    assert signal_band(-45)[0] == "Very close"
    assert signal_band(-67)[0] == "Warm"
    assert signal_band(-80)[0] == "Cold"
    assert signal_band(-95)[0] == "Very cold"


def test_every_band_ships_a_label_alongside_its_colour_role():
    # Colour never carries meaning alone, so each band must name itself.
    for rssi in (-30, -60, -75, -90, -120):
        label, role = signal_band(rssi)
        assert label
        assert role in {"good", "warning", "serious", "critical"}


def test_meter_fill_is_clamped_to_the_track():
    assert signal_percent(-120) == 0
    assert signal_percent(-20) == 100
    assert 0 < signal_percent(-70) < 100


def test_a_stronger_signal_fills_more_of_the_meter():
    assert signal_percent(-60) > signal_percent(-80)


# MARK: - Tracker


def test_tracker_follows_a_rotation_without_being_told():
    tracker = Tracker(stale_after=180.0)
    tracker.record("old", -67, SEPARATED, now=NOW)
    assert tracker.snapshot(NOW)["target"]["id"] == "old"

    # Key rotates: the old id goes silent, a new separated beacon appears.
    tracker.record("new", -70, SEPARATED, now=NOW + 200)
    assert tracker.snapshot(NOW + 200)["target"]["id"] == "new"


def test_tracker_reports_no_target_when_the_beacon_goes_quiet():
    # An empty meter must be distinguishable from a meter reading zero.
    tracker = Tracker(stale_after=180.0)
    tracker.record("a", -67, SEPARATED, now=NOW)
    assert tracker.snapshot(NOW + 400)["target"] is None


def test_tracker_keeps_the_best_reading_seen_for_a_beacon():
    tracker = Tracker()
    for rssi in (-80, -64, -77):
        tracker.record("a", rssi, SEPARATED, now=NOW)
    target = tracker.snapshot(NOW)["target"]
    assert target["best"] == -64
    assert target["rssi"] == -77  # latest packet, not the best one
    assert target["packets"] == 3


def test_history_only_records_separated_traffic():
    # With duplicates enabled a busy room emits hundreds of packets a minute.
    # Keeping all of them would swamp the chart with beacons we are not hunting.
    tracker = Tracker()
    tracker.record("chatty", -50, "apple_active_device", now=NOW)
    tracker.record("target", -70, SEPARATED, now=NOW)
    history = tracker.snapshot(NOW)["history"]
    assert [point["id"] for point in history] == ["target"]
