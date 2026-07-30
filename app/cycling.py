"""Estimated cycling power (no power meter) and climb detection from GPS streams.

Power is modelled from physics — gravity + rolling + aerodynamic drag — using the
rider's mass, the road gradient (from GPS + elevation) and speed. Wind, drafting
and position aren't known, so it's an ESTIMATE: good on climbs (gravity dominates),
rough on the flat/descents (aero dominates). The FTP proxy is the best rolling
20-minute estimated power × 0.95 over recent rides — trackable, not lab-accurate.
"""
import math
from datetime import datetime

from .fit import compute_normalized_power
from .gpx import _haversine

G = 9.81
BIKE_KG = 9.0          # bike + kit, added to rider weight
DEFAULT_RIDER_KG = 75.0
CRR = 0.005            # rolling resistance, road tyre on asphalt
CDA = 0.32             # drag area, amateur on the hoods
RHO = 1.225            # air density at sea level


def _fill(xs: list) -> list[float]:
    """Forward/backward-fill None samples (GPS/altitude gaps); leftover -> 0."""
    out = list(xs)
    last = None
    for i, v in enumerate(out):
        if v is None:
            out[i] = last
        else:
            last = v
    nxt = None
    for i in range(len(out) - 1, -1, -1):
        if out[i] is None:
            out[i] = nxt
        else:
            nxt = out[i]
    return [float(v) if v is not None else 0.0 for v in out]


def _smooth(xs: list[float], win: int = 5) -> list[float]:
    n = len(xs)
    if n < win:
        return list(xs)
    half = win // 2
    out = []
    for i in range(n):
        lo, hi = max(0, i - half), min(n, i + half + 1)
        out.append(sum(xs[lo:hi]) / (hi - lo))
    return out


def _cumulative_distance(latlng: list, speed: list, t: list) -> list[float]:
    """Cumulative distance (m). Prefer GPS (haversine); fall back to speed·dt."""
    n = len(t)
    cum = [0.0] * n
    if latlng and any(p for p in latlng):
        for i in range(1, n):
            a, b = latlng[i - 1], latlng[i]
            step = _haversine(a[0], a[1], b[0], b[1]) if a and b else 0.0
            cum[i] = cum[i - 1] + step
        return cum
    for i in range(1, n):
        dt = (t[i] - t[i - 1]) or 0
        v = speed[i] or 0
        cum[i] = cum[i - 1] + v * dt
    return cum


def estimate_power_series(streams: dict, mass_kg: float,
                          crr: float = CRR, cda: float = CDA, rho: float = RHO) -> list:
    """Per-sample estimated power (W), aligned with streams['t']. Coasting/braking
    and stopped samples read 0 (no pedalling)."""
    t = streams.get("t") or []
    speed = _fill(streams.get("speed") or [])
    alt = _smooth(_fill(streams.get("alt") or []), 7)
    if len(t) < 10 or len(speed) < 10 or len(alt) < 10:
        return []
    dist = _cumulative_distance(streams.get("latlng") or [], speed, t)
    out = [0.0] * len(t)
    for i in range(1, len(t)):
        v = speed[i] or 0
        if v < 0.8:                      # essentially stopped
            continue
        dd = dist[i] - dist[i - 1]
        grade = ((alt[i] - alt[i - 1]) / dd) if dd > 0.3 else 0.0
        grade = max(-0.25, min(0.25, grade))
        theta = math.atan(grade)
        p = (mass_kg * G * math.sin(theta) * v          # gravity
             + mass_kg * G * crr * math.cos(theta) * v  # rolling
             + 0.5 * rho * cda * v ** 3)                # aero
        out[i] = max(0.0, p)
    return out


def best_rolling_avg(t: list, values: list, window_s: float) -> float | None:
    """Best average of `values` over any `window_s`-long time window (two-pointer)."""
    n = len(t)
    if n < 2:
        return None
    best, i, run = None, 0, 0.0
    # simple time-weighted mean over [i, j]
    for j in range(n):
        while t[j] - t[i] > window_s and i < j:
            i += 1
        if t[j] - t[i] >= window_s * 0.9:
            seg = values[i:j + 1]
            if seg:
                avg = sum(seg) / len(seg)
                best = avg if best is None else max(best, avg)
    return best


def ride_power_stats(streams: dict, mass_kg: float) -> dict | None:
    """Estimated power summary for one ride: avg (moving), NP, best 20'/5'/1'."""
    power = estimate_power_series(streams, mass_kg)
    if not power or not any(power):
        return None
    t = streams["t"]
    moving = [p for p in power if p > 0]
    stats = {
        "avg": round(sum(moving) / len(moving)) if moving else 0,
        "np": round(compute_normalized_power(power, t) or 0) or None,
        "best_20min": best_rolling_avg(t, power, 1200),
        "best_5min": best_rolling_avg(t, power, 300),
        "best_1min": best_rolling_avg(t, power, 60),
    }
    for k in ("best_20min", "best_5min", "best_1min"):
        stats[k] = round(stats[k]) if stats[k] else None
    return stats


def rider_mass(weight_kg: float | None) -> float:
    return (weight_kg or DEFAULT_RIDER_KG) + BIKE_KG


def estimate_ftp(best_20min_watts: list[float]) -> int | None:
    """FTP proxy = 0.95 × the best 20-minute estimated power across recent rides."""
    vals = [w for w in best_20min_watts if w]
    return round(max(vals) * 0.95) if vals else None


# --------------------------------------------------------------- climb detection

def _resample(dist: list[float], alt: list[float], step: float = 25.0) -> tuple[list, list]:
    """Elevation resampled to a fixed distance grid (linear interpolation), so
    gradient is measured over distance, not over time (speed varies a lot)."""
    rd, ra = [dist[0]], [alt[0]]
    target, i = dist[0] + step, 1
    while i < len(dist):
        if dist[i] >= target:
            d0, d1 = dist[i - 1], dist[i]
            f = (target - d0) / (d1 - d0) if d1 > d0 else 0.0
            rd.append(target)
            ra.append(alt[i - 1] + f * (alt[i] - alt[i - 1]))
            target += step
        else:
            i += 1
    return rd, ra


def _nearest_idx(dist: list[float], target: float) -> int:
    """Index of the original sample closest to a cumulative distance (bisect)."""
    import bisect
    j = bisect.bisect_left(dist, target)
    if j <= 0:
        return 0
    if j >= len(dist):
        return len(dist) - 1
    return j if (dist[j] - target) < (target - dist[j - 1]) else j - 1


def _run_segments(grade, rd, sign, min_grade, merge_gap):
    """(k_start, end) index runs where sign*grade stays >= min_grade, bridging
    brief sub-threshold gaps up to merge_gap metres."""
    m = len(rd)
    out, k = [], 1
    while k < m:
        if sign * grade[k] < min_grade:
            k += 1
            continue
        j, gap = k, 0.0
        while j < m:
            if sign * grade[j] >= min_grade:
                gap = 0.0
            else:
                gap += rd[j] - rd[j - 1]
                if gap > merge_gap:
                    break
            j += 1
        end = j - 1
        while end > k and sign * grade[end] < min_grade:   # trim trailing false-flat
            end -= 1
        out.append((k, end))
        k = j + 1
    return out


def detect_segments(streams: dict, min_gain: float = 25.0, min_grade: float = 0.015,
                    merge_gap: float = 300.0, step: float = 25.0,
                    kinds: tuple = ("climb", "descent")) -> list[dict]:
    """Significant climbs AND descents in a ride, by GRADIENT over distance. A
    segment is a run where the smoothed grade stays past +/-min_grade (brief
    sub-threshold gaps up to merge_gap m are bridged), with elevation change
    >= min_gain. Gentle rolling terrain has long shallow segments, so the floor
    is low. Each segment carries a downsampled GPS path for a map and matching.
    """
    t = streams.get("t") or []
    alt_s = _smooth(_fill(streams.get("alt") or []), 9)
    hr = streams.get("hr") or []
    n = len(t)
    if n < 10 or len(alt_s) < n:
        return []
    dist = _cumulative_distance(streams.get("latlng") or [], _fill(streams.get("speed") or []), t)
    latlng = streams.get("latlng") or []
    rd, ra = _resample(dist, alt_s, step)
    if len(rd) < 3:
        return []
    grade = _smooth([0.0] + [(ra[k] - ra[k - 1]) / (rd[k] - rd[k - 1] or step)
                             for k in range(1, len(rd))], 7)

    segs: list[dict] = []
    for kind in kinds:
        sign = 1 if kind == "climb" else -1
        for k, end in _run_segments(grade, rd, sign, min_grade, merge_gap):
            drop = abs(ra[end] - ra[k - 1])
            length = rd[end] - rd[k - 1]
            if drop < min_gain or length <= 0:
                continue
            i0, i1 = _nearest_idx(dist, rd[k - 1]), _nearest_idx(dist, rd[end])
            secs = (t[i1] - t[i0]) or 1
            seg_hr = [h for h in hr[i0:i1 + 1] if h]
            peak_grade = max((sign * grade[x] for x in range(k, end + 1)),
                             default=drop / length)
            segs.append({
                "kind": kind,
                "start_km": round(rd[k - 1] / 1000, 1),
                "length_m": round(length),
                "gain_m": round(drop),                      # magnitude (up or down)
                "avg_grade": round(sign * drop / length * 100, 1),
                "max_grade": round(sign * peak_grade * 100, 1),
                "time_s": int(secs),
                "speed_kmh": round(length / secs * 3.6, 1),
                "vam": round(drop / secs * 3600),           # vertical metres/hour
                "avg_hr": round(sum(seg_hr) / len(seg_hr)) if seg_hr else None,
                "start_ll": _latlng_at(latlng, i0),         # endpoints: match the same
                "top_ll": _latlng_at(latlng, i1),           # segment across rides
                "path": _segment_path(latlng, i0, i1),      # polyline for the mini-map
            })
    segs.sort(key=lambda s: s["start_km"])
    return segs


def detect_climbs(streams: dict, **kw) -> list[dict]:
    """Climbs only (kept for callers that don't want descents)."""
    return detect_segments(streams, kinds=("climb",), **kw)


def _segment_path(latlng: list, i0: int, i1: int, max_pts: int = 40) -> list:
    """Downsampled [[lat,lng],...] between two sample indices, for a small map."""
    pts = [p for p in latlng[i0:i1 + 1] if p and p[0] is not None]
    if len(pts) <= max_pts:
        return [[round(p[0], 5), round(p[1], 5)] for p in pts]
    stepn = len(pts) / max_pts
    return [[round(pts[int(i * stepn)][0], 5), round(pts[int(i * stepn)][1], 5)]
            for i in range(max_pts)]


def _latlng_at(latlng: list, idx: int, span: int = 40):
    """Nearest valid [lat, lng] around idx, or None (GPS can have gaps)."""
    n = len(latlng)
    if not n:
        return None
    for off in range(span):
        for i in (idx + off, idx - off):
            if 0 <= i < n and latlng[i] and latlng[i][0] is not None:
                return [round(latlng[i][0], 6), round(latlng[i][1], 6)]
    return None


# Two climbs are the same segment if foot AND top are within this distance —
# tolerant to where detection places the exact boundaries ride to ride.
SEGMENT_TOL_M = 250.0


def same_segment(a: dict, b: dict) -> bool:
    """True if two efforts are the same real segment: same kind, and both
    endpoints within SEGMENT_TOL_M (matched by GPS)."""
    if a.get("kind", "climb") != b.get("kind", "climb"):
        return False
    for key in ("start_ll", "top_ll"):
        pa, pb = a.get(key), b.get(key)
        if not pa or not pb or _haversine(pa[0], pa[1], pb[0], pb[1]) > SEGMENT_TOL_M:
            return False
    return True


def segment_from_km(streams: dict, start_km: float, end_km: float) -> dict | None:
    """Define a custom segment from a ride's track between two distances: its
    GPS path plus start/mid/end anchor points and length."""
    t = streams.get("t") or []
    latlng = streams.get("latlng") or []
    if len(t) < 3 or not latlng:
        return None
    dist = _cumulative_distance(latlng, _fill(streams.get("speed") or []), t)
    i0, i1 = _nearest_idx(dist, start_km * 1000), _nearest_idx(dist, end_km * 1000)
    if i1 <= i0:
        return None
    start_ll, end_ll = _latlng_at(latlng, i0), _latlng_at(latlng, i1)
    mid_ll = _latlng_at(latlng, (i0 + i1) // 2)
    path = _segment_path(latlng, i0, i1, 60)
    if not start_ll or not end_ll or not mid_ll or len(path) < 2:
        return None
    return {"start_ll": start_ll, "mid_ll": mid_ll, "end_ll": end_ll,
            "path": path, "length_m": round(dist[i1] - dist[i0])}


def segment_from_points(streams: dict, start_ll: list, end_ll: list) -> dict | None:
    """Define a custom segment from two clicked points (snapped to the nearest
    track samples). Order is fixed so start comes before end along the ride."""
    t = streams.get("t") or []
    latlng = streams.get("latlng") or []
    if len(t) < 3 or not latlng or not start_ll or not end_ll:
        return None
    i0, _ = _nearest_on_track(latlng, start_ll)
    i1, _ = _nearest_on_track(latlng, end_ll)
    if i0 < 0 or i1 < 0 or i0 == i1:
        return None
    if i1 < i0:
        i0, i1 = i1, i0
    s_ll, e_ll = _latlng_at(latlng, i0), _latlng_at(latlng, i1)
    mid_ll = _latlng_at(latlng, (i0 + i1) // 2)
    path = _segment_path(latlng, i0, i1, 60)
    if not s_ll or not e_ll or not mid_ll or len(path) < 2:
        return None
    dist = _cumulative_distance(latlng, _fill(streams.get("speed") or []), t)
    return {"start_ll": s_ll, "mid_ll": mid_ll, "end_ll": e_ll,
            "path": path, "length_m": round(dist[i1] - dist[i0])}


def _nearest_on_track(latlng: list, ll: list, lo: int = 0, hi: int | None = None):
    """(index, distance_m) of the track point closest to ll, in [lo, hi)."""
    hi = len(latlng) if hi is None else hi
    best, bi = 1e18, -1
    for i in range(max(0, lo), min(len(latlng), hi)):
        p = latlng[i]
        if p and p[0] is not None:
            d = _haversine(p[0], p[1], ll[0], ll[1])
            if d < best:
                best, bi = d, i
    return bi, best


def match_segment(streams: dict, start_ll: list, mid_ll: list, end_ll: list,
                  tol: float = 60.0) -> dict | None:
    """Time a ride over a custom segment: find where the track passes closest to
    the segment's start then (later) its end, within tol; a midpoint check rejects
    a different road that merely shares endpoints. Returns the effort, or None."""
    t = streams.get("t") or []
    latlng = streams.get("latlng") or []
    if len(t) < 3 or not latlng:
        return None
    i_s, ds = _nearest_on_track(latlng, start_ll)
    if i_s < 0 or ds > tol:
        return None
    i_e, de = _nearest_on_track(latlng, end_ll, i_s + 1)
    if i_e < 0 or de > tol:
        return None
    _, dm = _nearest_on_track(latlng, mid_ll, i_s, i_e + 1)
    if dm > tol * 3:                       # went a different way between the anchors
        return None
    dist = _cumulative_distance(latlng, _fill(streams.get("speed") or []), t)
    secs = (t[i_e] - t[i_s]) or 1
    length = dist[i_e] - dist[i_s]
    hr = [h for h in (streams.get("hr") or [])[i_s:i_e + 1] if h]
    return {"time_s": int(secs), "distance_m": round(length),
            "speed_kmh": round(length / secs * 3.6, 1),
            "avg_hr": round(sum(hr) / len(hr)) if hr else None}


def cluster_segments(efforts: list[dict]) -> list[list[dict]]:
    """Group climb efforts (each a climb dict + at least start_ll/top_ll) into
    segments of the same climb. Greedy: each effort joins the first segment whose
    representative it matches, else starts a new one."""
    segments: list[list[dict]] = []
    for e in efforts:
        if not e.get("start_ll") or not e.get("top_ll"):
            continue
        for seg in segments:
            if same_segment(seg[0], e):
                seg.append(e)
                break
        else:
            segments.append([e])
    return segments
