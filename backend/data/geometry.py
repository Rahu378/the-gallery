"""Circuit geometry: build a centerline and project car positions onto it.

Everything downstream (race order, gaps, battle detection) is expressed as
arc-length along the centerline, so it works identically for real FastF1
position telemetry and for the synthetic fallback.
"""
from __future__ import annotations

import numpy as np


def resample_closed(xy: np.ndarray, n: int = 900) -> np.ndarray:
    """Resample a closed polyline to `n` evenly spaced points."""
    pts = np.asarray(xy, dtype=float)
    if not np.allclose(pts[0], pts[-1]):
        pts = np.vstack([pts, pts[0]])

    seg = np.linalg.norm(np.diff(pts, axis=0), axis=1)
    s = np.concatenate([[0.0], np.cumsum(seg)])
    total = s[-1]
    if total <= 0:
        raise ValueError("degenerate centerline")

    targets = np.linspace(0.0, total, n, endpoint=False)
    out = np.empty((n, 2))
    out[:, 0] = np.interp(targets, s, pts[:, 0])
    out[:, 1] = np.interp(targets, s, pts[:, 1])
    return out


def smooth_closed(xy: np.ndarray, window: int = 21) -> np.ndarray:
    """Circular moving-average smoothing — removes GPS jitter from the trace."""
    if window < 3:
        return xy
    if window % 2 == 0:
        window += 1
    k = np.ones(window) / window
    pad = window // 2
    out = np.empty_like(xy)
    for d in range(2):
        col = np.concatenate([xy[-pad:, d], xy[:, d], xy[:pad, d]])
        out[:, d] = np.convolve(col, k, mode="valid")
    return out


class Centerline:
    """A closed circuit centerline supporting arc-length projection."""

    def __init__(self, xy: np.ndarray, n: int = 900, rotation: float = 0.0):
        # Resample first, smooth second. A raw lap arrives at ~320 samples, and
        # smoothing that with a wide window rounds the chicanes off the circuit
        # before there are enough points to preserve them. Upsample to `n`, then
        # smooth over ~1% of the lap — enough to kill GPS jitter, not corners.
        self.pts = smooth_closed(resample_closed(np.asarray(xy, float), n),
                                 window=max(3, n // 110))
        d = np.linalg.norm(np.diff(np.vstack([self.pts, self.pts[0]]), axis=0), axis=1)
        self.seg_len = d
        self.cum = np.concatenate([[0.0], np.cumsum(d)])
        self.length = float(self.cum[-1])

        # Circuits are published at a conventional orientation; FastF1 carries
        # the angle. Applying it also tends to lay the long axis horizontally,
        # which is what the wide stage has room for.
        pts = self.pts
        if rotation:
            th = np.deg2rad(rotation)
            c, s = np.cos(th), np.sin(th)
            centre = (pts.min(axis=0) + pts.max(axis=0)) / 2.0
            rel = pts - centre
            pts = np.column_stack([rel[:, 0] * c - rel[:, 1] * s,
                                   rel[:, 0] * s + rel[:, 1] * c]) + centre

        # Normalised for the frontend with aspect preserved. `bounds` is the
        # box actually occupied — fitting a long thin circuit into a square
        # wastes most of the stage, so the client fits this box instead.
        lo, hi = pts.min(axis=0), pts.max(axis=0)
        span = float((hi - lo).max())
        centred = pts - (lo + hi) / 2.0
        self.norm = centred / span + 0.5
        nlo, nhi = self.norm.min(axis=0), self.norm.max(axis=0)
        self.bounds = [float(nlo[0]), float(nlo[1]), float(nhi[0]), float(nhi[1])]

    def project(self, x: np.ndarray, y: np.ndarray) -> np.ndarray:
        """Arc-length (metres) of the nearest centerline point for each (x, y)."""
        px = np.asarray(x, float).reshape(-1, 1)
        py = np.asarray(y, float).reshape(-1, 1)
        dx = px - self.pts[:, 0].reshape(1, -1)
        dy = py - self.pts[:, 1].reshape(1, -1)
        idx = np.argmin(dx * dx + dy * dy, axis=1)
        return self.cum[idx]

    def point_at(self, s: float) -> tuple[float, float]:
        """Normalised (x, y) in [0,1]^2 at arc-length `s`."""
        s = s % self.length
        i = int(np.searchsorted(self.cum, s, side="right") - 1)
        i = max(0, min(i, len(self.pts) - 1))
        prev = self.cum[i]
        frac = (s - prev) / self.seg_len[i] if self.seg_len[i] > 0 else 0.0
        a = self.norm[i]
        b = self.norm[(i + 1) % len(self.norm)]
        return float(a[0] + (b[0] - a[0]) * frac), float(a[1] + (b[1] - a[1]) * frac)

    def outline(self) -> list[list[float]]:
        return [[round(float(p[0]), 5), round(float(p[1]), 5)] for p in self.norm]


def unwrap_progress(s: np.ndarray, length: float) -> np.ndarray:
    """Turn wrapping arc-length into monotonic progress (laps completed + fraction).

    A backwards jump larger than half the lap is a start/finish crossing.
    """
    s = np.asarray(s, float)
    laps = np.zeros(len(s))
    n = 0
    for i in range(1, len(s)):
        if s[i] - s[i - 1] < -length / 2.0:
            n += 1
        laps[i] = n
    return laps + s / length


def synthetic_circuit(n: int = 900) -> np.ndarray:
    """A plausible closed circuit — long straight, hairpin, esses, fast sweepers.

    Used when FastF1 telemetry is unavailable so the pipeline always runs.
    """
    t = np.linspace(0, 2 * np.pi, n, endpoint=False)
    r = (
        1.0
        + 0.34 * np.sin(2 * t + 0.6)
        + 0.17 * np.sin(3 * t - 1.1)
        + 0.09 * np.sin(5 * t + 2.2)
        + 0.05 * np.sin(7 * t)
    )
    return np.column_stack([r * np.cos(t) * 1.45, r * np.sin(t)]) * 1000.0
