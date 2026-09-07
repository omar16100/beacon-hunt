"""Position a BLE emitter from RSSI readings taken at several known points.

The naive approach converts each RSSI to metres and intersects circles. That
fails here because the conversion needs the emitter's transmit power at 1m,
which the Find My advertisement does not include.

The trick this module uses: for a fixed path-loss exponent n, an unknown
transmit power t enters the distance equation as a pure multiplicative scale.

    d_i = 10**((t - rssi_i) / (10n))
        = 10**(t / (10n)) * 10**(-rssi_i / (10n))
        = s * r_i

So the *shape* of the distance set is known even when the *scale* is not. With
three or more anchors we solve for (x, y, s) jointly and the transmit-power
guess drops out entirely.

The path-loss exponent n does NOT factor out, since the rssi_i differ. That
residual uncertainty is handled by Monte Carlo sampling over a plausible n
range, which yields a position cloud rather than a false-precision point.
"""

import logging
import math

import numpy as np
from scipy.optimize import least_squares

log = logging.getLogger(__name__)

# Typical indoor path-loss exponents: 2.0 is free space, 3.5 is cluttered
# multi-room. Sampling this range is what produces the confidence region.
DEFAULT_N_MIN = 2.0
DEFAULT_N_MAX = 3.5

# Per-anchor variation in the path-loss exponent. Each anchor sees the emitter
# through a different number of walls, so a single shared n structurally cannot
# represent the real model error. Without this the confidence region is far too
# tight.
DEFAULT_N_JITTER = 0.3

# Standard deviation of per-anchor RSSI error in dB.
#
# This is deliberately NOT the packet-to-packet jitter observed at one spot
# (about 2 dB). That jitter averages out in the median we record. What actually
# limits the fix is log-normal shadowing: a roughly fixed offset per measurement
# position, caused by walls, furniture and multipath, typically 4 to 8 dB
# indoors, which does NOT average out and decorrelates between positions.
# Using the 2 dB jitter here produced radii that were measured to contain the
# truth only ~52% of the time at a nominal 90%.
DEFAULT_RSSI_SIGMA = 5.0


def relative_distances(rssis: np.ndarray, path_loss_n: float) -> np.ndarray:
    """Distances up to a common unknown scale factor."""
    return np.power(10.0, -rssis / (10.0 * path_loss_n))


def solve_once(anchors: np.ndarray, rssis: np.ndarray, path_loss_n: float) -> dict:
    """Least-squares solve for (x, y, scale) given one set of assumptions.

    Uses several starting points because the cost surface can have a second
    local minimum (a mirrored solution) when the anchor geometry is poor.
    """
    r = relative_distances(rssis, path_loss_n)

    def residuals(params):
        x, y, s = params
        dist = np.linalg.norm(anchors - np.array([x, y]), axis=1)
        return dist - s * r

    centroid = anchors.mean(axis=0)
    spread = float(np.linalg.norm(anchors - centroid, axis=1).mean()) or 1.0
    scale_guess = spread / (r.mean() or 1.0)

    starts = [
        [centroid[0], centroid[1], scale_guess],
        [centroid[0] + spread, centroid[1], scale_guess],
        [centroid[0] - spread, centroid[1], scale_guess],
        [centroid[0], centroid[1] + spread, scale_guess],
        [centroid[0], centroid[1] - spread, scale_guess],
    ]

    best = None
    solutions = []
    for start in starts:
        try:
            res = least_squares(
                residuals,
                start,
                bounds=([-np.inf, -np.inf, 1e-6], [np.inf, np.inf, np.inf]),
            )
        except Exception as exc:  # pragma: no cover - solver blowup is rare
            log.debug("solver failed from start %s: %s", start, exc)
            continue
        solutions.append((float(res.cost), res.x))
        if best is None or res.cost < best[0]:
            best = (float(res.cost), res.x)

    if best is None:
        raise RuntimeError("all trilateration starts failed")

    cost, params = best
    x, y, s = params

    # Detect a distinct rival minimum: another solution with comparable cost but
    # a meaningfully different position means the geometry is ambiguous.
    ambiguous = any(
        c < cost * 1.5 + 1e-9 and math.dist((x, y), (p[0], p[1])) > 0.5
        for c, p in solutions
    )

    return {
        "x": float(x),
        "y": float(y),
        "scale": float(s),
        "cost": cost,
        "distances": (s * r).tolist(),
        "ambiguous": ambiguous,
    }


def check_geometry(anchors: np.ndarray) -> list[str]:
    """Warn about anchor layouts that cannot pin a position."""
    warnings = []
    if len(anchors) < 3:
        warnings.append(
            f"only {len(anchors)} reading(s): need at least 3 to solve for position "
            "and scale together"
        )
        return warnings

    # Collinearity via the smallest singular value of the centred anchor matrix.
    centred = anchors - anchors.mean(axis=0)
    singular = np.linalg.svd(centred, compute_uv=False)
    if len(singular) >= 2 and singular[1] < 0.15 * singular[0]:
        warnings.append(
            "anchor points are nearly collinear: the solution will be mirrored "
            "about that line and cannot distinguish the two sides"
        )

    spread = float(np.linalg.norm(centred, axis=1).max())
    if spread < 2.0:
        warnings.append(
            f"anchor points span only ~{spread:.1f}m: spread them further apart "
            "for a meaningful fix"
        )
    return warnings


def trilaterate(
    readings: list[tuple[float, float, float]],
    samples: int = 500,
    n_min: float = DEFAULT_N_MIN,
    n_max: float = DEFAULT_N_MAX,
    rssi_sigma: float = DEFAULT_RSSI_SIGMA,
    n_jitter: float = DEFAULT_N_JITTER,
    seed: int = 0,
) -> dict:
    """Monte Carlo position estimate from (x, y, rssi) readings.

    Each trial resamples the path-loss exponent and perturbs the RSSI values,
    then solves. The spread of results is the honest confidence region.
    """
    if len(readings) < 3:
        raise ValueError(
            f"need at least 3 readings to solve for position and scale, got "
            f"{len(readings)}"
        )

    anchors = np.array([[r[0], r[1]] for r in readings], dtype=float)
    rssis = np.array([r[2] for r in readings], dtype=float)

    warnings = check_geometry(anchors)
    for w in warnings:
        log.warning("geometry: %s", w)

    rng = np.random.default_rng(seed)
    points = []
    scales = []
    ambiguous_count = 0

    for _ in range(samples):
        # Base exponent for the environment, then per-anchor jitter: each anchor
        # sees the emitter through a different number of walls.
        base_n = rng.uniform(n_min, n_max)
        n = np.clip(
            base_n + rng.normal(0.0, n_jitter, size=rssis.shape), 1.5, 5.0
        )
        noisy = rssis + rng.normal(0.0, rssi_sigma, size=rssis.shape)
        try:
            sol = solve_once(anchors, noisy, n)
        except RuntimeError:
            continue
        points.append((sol["x"], sol["y"]))
        scales.append(sol["scale"])
        ambiguous_count += bool(sol["ambiguous"])

    if not points:
        raise RuntimeError("trilateration produced no solutions")

    cloud = np.array(points)
    centre = cloud.mean(axis=0)
    radial = np.linalg.norm(cloud - centre, axis=1)

    nominal = solve_once(anchors, rssis, (n_min + n_max) / 2)

    return {
        "centre": (float(centre[0]), float(centre[1])),
        "nominal": (nominal["x"], nominal["y"]),
        "r50": float(np.percentile(radial, 50)),
        "r90": float(np.percentile(radial, 90)),
        "cloud": cloud,
        "anchor_distances": nominal["distances"],
        "scale_median": float(np.median(scales)),
        "ambiguous_fraction": ambiguous_count / len(points),
        "warnings": warnings,
        "samples_solved": len(points),
    }


def render_map(result: dict, readings: list[tuple[float, float, float]], width: int = 61) -> str:
    """ASCII plan view of anchors (A/B/C...) and the solution cloud."""
    cloud = result["cloud"]
    anchors = np.array([[r[0], r[1]] for r in readings], dtype=float)

    all_pts = np.vstack([cloud, anchors])
    min_xy = all_pts.min(axis=0)
    max_xy = all_pts.max(axis=0)
    span = np.maximum(max_xy - min_xy, 1e-6)
    pad = span * 0.1
    min_xy -= pad
    max_xy += pad
    span = max_xy - min_xy

    height = max(9, int(width * span[1] / span[0] / 2.2))
    height = min(height, 25)
    grid = [[" "] * width for _ in range(height)]

    def to_cell(x, y):
        cx = int((x - min_xy[0]) / span[0] * (width - 1))
        cy = int((1 - (y - min_xy[1]) / span[1]) * (height - 1))
        return max(0, min(height - 1, cy)), max(0, min(width - 1, cx))

    density = {}
    for x, y in cloud:
        density[to_cell(x, y)] = density.get(to_cell(x, y), 0) + 1
    if density:
        peak = max(density.values())
        ramp = ".:-=+*#%@"
        for (cy, cx), count in density.items():
            idx = min(len(ramp) - 1, int(count / peak * (len(ramp) - 1)))
            grid[cy][cx] = ramp[idx]

    for i, (ax, ay) in enumerate(anchors):
        cy, cx = to_cell(ax, ay)
        grid[cy][cx] = chr(ord("A") + i)

    cy, cx = to_cell(*result["centre"])
    grid[cy][cx] = "X"

    body = "\n".join("  |" + "".join(row) + "|" for row in grid)
    border = "  +" + "-" * width + "+"
    scale_note = f"  plan view, {span[0]:.1f}m wide x {span[1]:.1f}m tall"
    legend = "  A,B,C.. = your reading positions   X = best estimate   shading = likelihood"
    return f"{border}\n{body}\n{border}\n{scale_note}\n{legend}"
