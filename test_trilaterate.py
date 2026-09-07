"""Tests for RSSI trilateration.

Synthetic ground truth: pick a true emitter position and a true (tx, n), derive
the exact RSSI each anchor would see, then check the solver recovers the point.
"""

import math

import numpy as np
import pytest

from trilaterate import (
    check_geometry,
    relative_distances,
    solve_once,
    trilaterate,
)


def synth_rssi(anchor, target, tx_power, path_loss_n):
    """Exact RSSI an anchor would measure, inverting the path-loss model."""
    d = max(math.dist(anchor, target), 1e-6)
    return tx_power - 10 * path_loss_n * math.log10(d)


SQUARE_ANCHORS = [(0.0, 0.0), (6.0, 0.0), (6.0, 5.0), (0.0, 5.0)]


def build_readings(target, anchors=SQUARE_ANCHORS, tx_power=-59.0, path_loss_n=2.5):
    return [(a[0], a[1], synth_rssi(a, target, tx_power, path_loss_n)) for a in anchors]


class TestRelativeDistances:
    def test_stronger_signal_gives_smaller_relative_distance(self):
        r = relative_distances(np.array([-50.0, -80.0]), 2.5)
        assert r[0] < r[1]

    def test_scales_by_common_factor_when_all_rssi_shift(self):
        base = relative_distances(np.array([-60.0, -70.0, -80.0]), 2.5)
        shifted = relative_distances(np.array([-70.0, -80.0, -90.0]), 2.5)
        ratios = shifted / base
        assert np.allclose(ratios, ratios[0])


class TestSolveOnce:
    def test_recovers_known_position(self):
        target = (2.0, 3.0)
        readings = build_readings(target)
        anchors = np.array([[r[0], r[1]] for r in readings])
        rssis = np.array([r[2] for r in readings])
        sol = solve_once(anchors, rssis, 2.5)
        assert sol["x"] == pytest.approx(target[0], abs=0.05)
        assert sol["y"] == pytest.approx(target[1], abs=0.05)

    def test_position_is_invariant_to_unknown_tx_power(self):
        """The whole premise: tx power is a common scale and must not shift the fix."""
        target = (4.0, 1.5)
        anchors = np.array(SQUARE_ANCHORS)
        results = []
        for tx in (-45.0, -59.0, -70.0):
            rssis = np.array([synth_rssi(a, target, tx, 2.5) for a in SQUARE_ANCHORS])
            sol = solve_once(anchors, rssis, 2.5)
            results.append((sol["x"], sol["y"]))
        for x, y in results:
            assert x == pytest.approx(target[0], abs=0.05)
            assert y == pytest.approx(target[1], abs=0.05)

    def test_recovers_scale_consistent_with_tx_power(self):
        target = (3.0, 2.0)
        anchors = np.array(SQUARE_ANCHORS)
        rssis = np.array([synth_rssi(a, target, -59.0, 2.5) for a in SQUARE_ANCHORS])
        sol = solve_once(anchors, rssis, 2.5)
        # scale * relative distance must reproduce true geometric distance
        for anchor, dist in zip(SQUARE_ANCHORS, sol["distances"]):
            assert dist == pytest.approx(math.dist(anchor, target), abs=0.1)


class TestCheckGeometry:
    def test_flags_too_few_readings(self):
        warnings = check_geometry(np.array([[0.0, 0.0], [3.0, 0.0]]))
        assert any("at least 3" in w for w in warnings)

    def test_flags_collinear_anchors(self):
        warnings = check_geometry(np.array([[0.0, 0.0], [3.0, 0.0], [6.0, 0.0]]))
        assert any("collinear" in w for w in warnings)

    def test_flags_tightly_clustered_anchors(self):
        warnings = check_geometry(np.array([[0.0, 0.0], [0.4, 0.1], [0.2, 0.4]]))
        assert any("span only" in w for w in warnings)

    def test_good_geometry_has_no_warnings(self):
        assert check_geometry(np.array(SQUARE_ANCHORS)) == []


class TestTrilaterate:
    def test_centre_is_near_truth_on_clean_data(self):
        target = (2.0, 3.0)
        result = trilaterate(build_readings(target), samples=120, rssi_sigma=1.0)
        cx, cy = result["centre"]
        assert math.dist((cx, cy), target) < 1.0

    def test_confidence_radius_grows_with_noise(self):
        target = (2.0, 3.0)
        readings = build_readings(target)
        tight = trilaterate(readings, samples=120, rssi_sigma=0.5, seed=1)
        loose = trilaterate(readings, samples=120, rssi_sigma=6.0, seed=1)
        assert loose["r90"] > tight["r90"]

    def test_r90_is_at_least_r50(self):
        result = trilaterate(build_readings((1.0, 1.0)), samples=120)
        assert result["r90"] >= result["r50"]

    def test_propagates_geometry_warnings(self):
        collinear = [(0.0, 0.0, -60.0), (3.0, 0.0, -66.0), (6.0, 0.0, -72.0)]
        result = trilaterate(collinear, samples=60)
        assert any("collinear" in w for w in result["warnings"])

    def test_rejects_fewer_than_three_readings(self):
        with pytest.raises(ValueError, match="at least 3"):
            trilaterate([(0.0, 0.0, -70.0), (3.0, 0.0, -75.0)], samples=10)

    def test_solves_most_samples(self):
        result = trilaterate(build_readings((2.0, 2.0)), samples=100)
        assert result["samples_solved"] >= 90
