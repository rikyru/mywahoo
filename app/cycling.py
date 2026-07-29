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

def detect_climbs(streams: dict, min_gain: float = 30.0, min_grade: float = 0.025,
                  drop_tol: float = 12.0) -> list[dict]:
    """Significant climbs in a ride: sustained ascents of >= min_gain metres at
    >= min_grade average, tolerating small dips (drop_tol) within a climb.

    Returns per climb: distance, elevation gain, avg/max gradient, time, speed,
    VAM (vertical metres climbed per hour), avg HR.
    """
    t = streams.get("t") or []
    alt = _smooth(_fill(streams.get("alt") or []), 7)
    hr = streams.get("hr") or []
    n = len(t)
    if n < 10 or len(alt) < n:
        return []
    dist = _cumulative_distance(streams.get("latlng") or [], _fill(streams.get("speed") or []), t)
    climbs: list[dict] = []
    i = 0
    while i < n - 1:
        peak, peak_k = alt[i], i
        k = i + 1
        while k < n:
            if alt[k] > peak:          # strictly higher: a plateau won't extend the top
                peak, peak_k = alt[k], k
            elif peak - alt[k] > drop_tol:
                break                  # a real descent closes the climb at the peak
            k += 1
        gain = alt[peak_k] - alt[i]
        length = dist[peak_k] - dist[i]
        if gain >= min_gain and length > 0 and (gain / length) >= min_grade:
            secs = (t[peak_k] - t[i]) or 1
            seg_hr = [h for h in hr[i:peak_k + 1] if h]
            # max gradient over ~50 m sub-segments
            max_grade = 0.0
            a = i
            for b in range(i + 1, peak_k + 1):
                if dist[b] - dist[a] >= 50:
                    max_grade = max(max_grade, (alt[b] - alt[a]) / (dist[b] - dist[a]))
                    a = b
            climbs.append({
                "start_km": round(dist[i] / 1000, 1),
                "length_m": round(length),
                "gain_m": round(gain),
                "avg_grade": round(gain / length * 100, 1),
                "max_grade": round(max(max_grade, gain / length) * 100, 1),
                "time_s": int(secs),
                "speed_kmh": round(length / secs * 3.6, 1),
                "vam": round(gain / secs * 3600),   # vertical ascent metres/hour
                "avg_hr": round(sum(seg_hr) / len(seg_hr)) if seg_hr else None,
            })
            i = peak_k
        else:
            i = peak_k if peak_k > i else i + 1
    return climbs
