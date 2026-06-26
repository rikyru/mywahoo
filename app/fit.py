"""FIT file parsing with fitdecode.

Extracts:
- session message  -> refined summary fields on the Workout row
- record messages  -> per-record streams (power/HR/cadence/speed/alt/GPS),
  stored gzip-compressed for the detail charts and the route map.

Also provides downsampling for charts and aggregated stats for the AI prompt.
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import fitdecode
import httpx
from sqlmodel import Session

from .db import Workout, WorkoutStream, engine, pack_streams, unpack_streams

logger = logging.getLogger(__name__)

# FIT stores lat/long as "semicircles": degrees = semicircles * (180 / 2^31)
SEMICIRCLE_TO_DEG = 180.0 / 2 ** 31

# Free DEM lookup for ascent estimation when the FIT has no altitude data
# (e.g. Wahoo phone app without barometer). Public limits: 100 locations
# per request, 1 call/s, 1000 calls/day.
ELEVATION_API = "https://api.opentopodata.org/v1/srtm30m"
ELEVATION_MAX_POINTS = 400  # route samples sent to the DEM per workout
ASCENT_NOISE_M = 2.0  # hysteresis threshold: climbs smaller than this are noise


class FitParseError(Exception):
    pass


def _get(frame, *names):
    """Return the first available (non-None) field among names.
    'enhanced_*' variants take precedence over the 16-bit legacy fields."""
    for name in names:
        if frame.has_field(name):
            value = frame.get_value(name)
            if value is not None:
                return value
    return None


def parse_fit(path: str) -> tuple[dict, dict]:
    """Parse a FIT file. Returns (session_summary, streams).

    streams: {"t": [...], "power": [...], "hr": [...], "cadence": [...],
              "speed": [...], "alt": [...], "latlng": [[lat,lng], ...] | None}
    """
    session_data: dict = {}
    t, power, hr, cadence, speed, alt = [], [], [], [], [], []
    latlng: list = []
    start_ts: Optional[datetime] = None

    try:
        with fitdecode.FitReader(path) as fit:
            for frame in fit:
                if not isinstance(frame, fitdecode.FitDataMessage):
                    continue

                if frame.name == "record":
                    ts = _get(frame, "timestamp")
                    if ts is None:
                        continue
                    if start_ts is None:
                        start_ts = ts
                    t.append(int((ts - start_ts).total_seconds()))
                    power.append(_get(frame, "power"))
                    hr.append(_get(frame, "heart_rate"))
                    cadence.append(_get(frame, "cadence"))
                    speed.append(_get(frame, "enhanced_speed", "speed"))
                    alt.append(_get(frame, "enhanced_altitude", "altitude"))
                    lat = _get(frame, "position_lat")
                    lng = _get(frame, "position_long")
                    if lat is not None and lng is not None:
                        latlng.append([round(lat * SEMICIRCLE_TO_DEG, 6),
                                       round(lng * SEMICIRCLE_TO_DEG, 6)])
                    else:
                        latlng.append(None)

                elif frame.name == "session":
                    session_data = {
                        "sport": str(_get(frame, "sport") or ""),
                        "sub_sport": str(_get(frame, "sub_sport") or ""),
                        "start_time": _get(frame, "start_time"),
                        "total_elapsed_time": _get(frame, "total_elapsed_time"),
                        "total_timer_time": _get(frame, "total_timer_time"),
                        "total_distance": _get(frame, "total_distance"),
                        "total_ascent": _get(frame, "total_ascent"),
                        "avg_speed": _get(frame, "enhanced_avg_speed", "avg_speed"),
                        "max_speed": _get(frame, "enhanced_max_speed", "max_speed"),
                        "avg_heart_rate": _get(frame, "avg_heart_rate"),
                        "max_heart_rate": _get(frame, "max_heart_rate"),
                        "avg_power": _get(frame, "avg_power"),
                        "max_power": _get(frame, "max_power"),
                        "normalized_power": _get(frame, "normalized_power"),
                        "avg_cadence": _get(frame, "avg_cadence"),
                        "total_calories": _get(frame, "total_calories"),
                        "training_stress_score": _get(frame, "training_stress_score"),
                        "intensity_factor": _get(frame, "intensity_factor"),
                    }
    except (fitdecode.FitError, OSError, ValueError) as e:
        raise FitParseError(f"Cannot parse FIT file {path}: {e}") from e

    has_gps = any(p is not None for p in latlng)
    streams = {
        "t": t, "power": power, "hr": hr, "cadence": cadence,
        "speed": speed, "alt": alt, "latlng": latlng if has_gps else None,
    }
    return session_data, streams


def compute_normalized_power(power: list, t: list) -> Optional[float]:
    """Coggan NP: 30s rolling average of power, raised to 4th power, averaged,
    then 4th root. Computed only if the FIT didn't provide it."""
    samples = [(ti, p) for ti, p in zip(t, power) if p is not None]
    if len(samples) < 60:
        return None
    # Resample to 1s grid (simple forward fill) so the 30s window is uniform
    grid: list[float] = []
    idx = 0
    last = 0.0
    for sec in range(samples[0][0], samples[-1][0] + 1):
        while idx < len(samples) and samples[idx][0] <= sec:
            last = samples[idx][1]
            idx += 1
        grid.append(last)
    if len(grid) < 30:
        return None
    window_sum = sum(grid[:30])
    fourth_powers = [(window_sum / 30) ** 4]
    for i in range(30, len(grid)):
        window_sum += grid[i] - grid[i - 30]
        fourth_powers.append((window_sum / 30) ** 4)
    return round((sum(fourth_powers) / len(fourth_powers)) ** 0.25, 1)


def parse_and_store(workout_id: int, path: str) -> None:
    """Parse the FIT and update the Workout row + WorkoutStream table."""
    session_data, streams = parse_fit(path)

    np_value = session_data.get("normalized_power")
    if np_value is None and any(p is not None for p in streams["power"]):
        np_value = compute_normalized_power(streams["power"], streams["t"])

    with Session(engine) as db:
        workout = db.get(Workout, workout_id)
        if workout is None:
            raise FitParseError(f"Workout {workout_id} not found in DB")

        # Refine summary with authoritative FIT session data (when present)
        def setif(attr, value, cast=float):
            if value is not None:
                try:
                    setattr(workout, attr, cast(value))
                except (TypeError, ValueError):
                    pass

        if session_data.get("sport"):
            workout.sport = session_data["sport"].replace("_", " ").title()
        if session_data.get("sub_sport"):
            workout.sub_sport = session_data["sub_sport"].replace("_", " ").title()
        start = session_data.get("start_time")
        if isinstance(start, datetime):
            workout.start_date = start.astimezone(timezone.utc).replace(tzinfo=None) \
                if start.tzinfo else start
        setif("duration_s", session_data.get("total_elapsed_time"), int)
        setif("moving_s", session_data.get("total_timer_time"), int)
        setif("distance_m", session_data.get("total_distance"))
        setif("ascent_m", session_data.get("total_ascent"))
        setif("avg_speed_ms", session_data.get("avg_speed"))
        setif("max_speed_ms", session_data.get("max_speed"))
        setif("avg_hr", session_data.get("avg_heart_rate"))
        setif("max_hr", session_data.get("max_heart_rate"))
        setif("avg_power", session_data.get("avg_power"))
        setif("max_power", session_data.get("max_power"))
        setif("avg_cadence", session_data.get("avg_cadence"))
        setif("calories", session_data.get("total_calories"))
        setif("tss", session_data.get("training_stress_score"))
        setif("intensity_factor", session_data.get("intensity_factor"))
        if np_value is not None:
            workout.np_power = float(np_value)
        workout.has_fit = True
        workout.fit_path = path
        workout.updated_at = datetime.utcnow()
        db.add(workout)

        existing = db.get(WorkoutStream, workout_id)
        blob = pack_streams(streams)
        if existing:
            existing.data = blob
            existing.n_records = len(streams["t"])
            db.add(existing)
        else:
            db.add(WorkoutStream(workout_id=workout_id, data=blob,
                                 n_records=len(streams["t"])))
        db.commit()
    logger.info("Parsed FIT for workout %s: %s records, NP=%s",
                workout_id, len(streams["t"]), np_value)


# ---------------------------------------------------------------- ascent from DEM

def ascent_from_elevations(elevations: list[float], noise_m: float = ASCENT_NOISE_M) -> float:
    """Total ascent from a terrain elevation profile.

    Light smoothing (3-sample moving average), then positive gains accumulated
    with hysteresis: a climb only counts once it exceeds noise_m over the last
    reference point, so DEM noise on flat roads doesn't inflate the total.
    """
    if len(elevations) < 2:
        return 0.0
    smooth = [sum(elevations[max(0, i - 1):i + 2]) / len(elevations[max(0, i - 1):i + 2])
              for i in range(len(elevations))]
    ascent = 0.0
    ref = smooth[0]
    for e in smooth[1:]:
        if e >= ref + noise_m:
            ascent += e - ref
            ref = e
        elif e < ref:
            ref = e
    return round(ascent)


async def estimate_ascent_from_gps(latlng: list) -> Optional[float]:
    """Estimate total ascent by sampling the GPS track against a free DEM
    (OpenTopoData, SRTM 30m). Returns None when the route is too short or
    the elevation service is unavailable — callers just keep ascent as-is."""
    pts = [p for p in latlng or [] if p]
    if len(pts) < 10:
        return None
    step = max(len(pts) // ELEVATION_MAX_POINTS, 1)
    pts = pts[::step]

    elevations: list[float] = []
    async with httpx.AsyncClient(timeout=30) as client:
        for i in range(0, len(pts), 100):
            chunk = pts[i:i + 100]
            locations = "|".join(f"{lat},{lng}" for lat, lng in chunk)
            try:
                resp = await client.post(ELEVATION_API, json={"locations": locations})
            except httpx.HTTPError as e:
                logger.warning("Elevation API unreachable: %s", e)
                return None
            if resp.status_code != 200:
                logger.warning("Elevation API HTTP %s: %s", resp.status_code, resp.text[:200])
                return None
            batch = [r.get("elevation") for r in resp.json().get("results", [])]
            if any(e is None for e in batch):
                logger.warning("Elevation API returned gaps in the profile")
                return None
            elevations.extend(batch)
            if i + 100 < len(pts):
                await asyncio.sleep(1.1)  # public API: max 1 call/s
    return ascent_from_elevations(elevations)


async def enrich_ascent(workout_id: int) -> None:
    """Fill ascent_m from the DEM when the FIT had no altitude data.
    Best-effort: any failure leaves the workout untouched."""
    with Session(engine) as db:
        workout = db.get(Workout, workout_id)
    if workout is None or workout.ascent_m:
        return
    streams = load_streams(workout_id)
    if not streams or not streams.get("latlng"):
        return
    ascent = await estimate_ascent_from_gps(streams["latlng"])
    if ascent is None:
        return
    with Session(engine) as db:
        workout = db.get(Workout, workout_id)
        workout.ascent_m = float(ascent)
        workout.updated_at = datetime.utcnow()
        db.add(workout)
        db.commit()
    logger.info("Estimated ascent for workout %s from DEM: %sm", workout_id, ascent)


# ---------------------------------------------------------------- chart/AI helpers

def downsample(streams: dict, max_points: int = 500) -> dict:
    """Reduce streams to ~max_points by bucket-averaging (charts stay light)."""
    n = len(streams["t"])
    if n <= max_points:
        return streams
    step = n / max_points

    def avg_bucket(arr, i0, i1):
        vals = [v for v in arr[i0:i1] if v is not None]
        return round(sum(vals) / len(vals), 1) if vals else None

    out = {"t": [], "power": [], "hr": [], "cadence": [], "speed": [], "alt": [],
           "latlng": None}
    for b in range(max_points):
        i0, i1 = int(b * step), max(int((b + 1) * step), int(b * step) + 1)
        out["t"].append(streams["t"][min(i0, n - 1)])
        for key in ("power", "hr", "cadence", "speed", "alt"):
            out[key].append(avg_bucket(streams[key], i0, i1))
    if streams.get("latlng"):
        # Route map only needs the shape: take every k-th valid point
        pts = [p for p in streams["latlng"] if p]
        k = max(len(pts) // 1000, 1)
        out["latlng"] = pts[::k]
    return out


def ai_stats(streams: dict) -> dict:
    """Compact, aggregated view of the streams for the AI prompt (never the
    raw arrays — a 3h ride would be tens of thousands of samples).

    Produces per-decile averages and cardiac-drift indicators.
    """
    n = len(streams["t"])
    if n == 0:
        return {}

    def clean(key):
        return [(t, v) for t, v in zip(streams["t"], streams[key]) if v is not None]

    out: dict = {"n_records": n, "duration_s": streams["t"][-1] if streams["t"] else 0}

    for key, label in (("power", "potenza_w"), ("hr", "fc_bpm"),
                       ("cadence", "cadenza"), ("speed", "velocita_ms")):
        data = clean(key)
        if not data:
            continue
        vals = [v for _, v in data]
        deciles = []
        step = max(len(vals) // 10, 1)
        for i in range(0, len(vals), step):
            chunk = vals[i:i + step]
            deciles.append(round(sum(chunk) / len(chunk), 1))
        out[label] = {
            "media": round(sum(vals) / len(vals), 1),
            "max": round(max(vals), 1),
            "per_decimi_di_sessione": deciles[:10],
        }

    # Cardiac drift: HR/power ratio first half vs second half (aerobic decoupling).
    # A ratio increase > ~5% suggests fatigue/dehydration/heat stress.
    hr_data = clean("hr")
    pw_data = clean("power")
    if len(hr_data) > 60 and len(pw_data) > 60:
        half_t = streams["t"][-1] / 2

        def ratio(data_hr, data_pw, first: bool):
            hrs = [v for t, v in data_hr if (t < half_t) == first]
            pws = [v for t, v in data_pw if (t < half_t) == first and v > 0]
            if not hrs or not pws:
                return None
            return (sum(hrs) / len(hrs)) / (sum(pws) / len(pws))

        r1, r2 = ratio(hr_data, pw_data, True), ratio(hr_data, pw_data, False)
        if r1 and r2:
            out["drift_cardiaco_pct"] = round((r2 - r1) / r1 * 100, 1)

    return out


def load_streams(workout_id: int) -> Optional[dict]:
    with Session(engine) as db:
        row = db.get(WorkoutStream, workout_id)
    return unpack_streams(row.data) if row else None
