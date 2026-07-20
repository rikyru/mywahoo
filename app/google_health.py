"""Google Health API (successor of the Fitbit Web API): OAuth + exercise reads.

Used to enrich workouts that reach Wahoo without data (e.g. swims/walks synced
through Strava). Raw httpx, same style as wahoo.py. Note: while the OAuth app
is in "Testing" status Google expires the refresh token after 7 days — the UI
must offer a quick re-connect.
"""
import logging
import statistics
import time
from urllib.parse import urlencode

import httpx
from sqlmodel import Session

from .config import settings
from .db import GoogleToken, engine

logger = logging.getLogger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
BASE_URL = "https://health.googleapis.com/v4"
_SCOPE = "https://www.googleapis.com/auth/googlehealth.{}.readonly"
SCOPES = " ".join(_SCOPE.format(s) for s in (
    "activity_and_fitness",
    "location",
    "health_metrics_and_measurements",
    "sleep",
    "ecg",
    "irn",
    "nutrition",
    "profile",
))


class GoogleHealthError(Exception):
    pass


class GoogleNotAuthenticatedError(GoogleHealthError):
    pass


class GoogleScopeMissingError(GoogleHealthError):
    """The OAuth grant lacks the scope for this data type (HTTP 403)."""


def redirect_uri() -> str:
    return settings.app_base_url.rstrip("/") + "/oauth/google/callback"


def build_authorize_url(state: str) -> str:
    # access_type=offline + prompt=consent: always get a refresh token
    return AUTH_URL + "?" + urlencode({
        "client_id": settings.google_client_id,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    })


def _store_token_response(data: dict) -> None:
    expires_at = int(time.time()) + int(data.get("expires_in", 3600))
    with Session(engine) as session:
        token = session.get(GoogleToken, 1)
        if token:
            token.access_token = data["access_token"]
            # Refresh responses may omit the refresh token: keep the current one
            token.refresh_token = data.get("refresh_token") or token.refresh_token
            token.expires_at = expires_at
            session.add(token)
        else:
            session.add(GoogleToken(id=1, access_token=data["access_token"],
                                    refresh_token=data.get("refresh_token", ""),
                                    expires_at=expires_at))
        session.commit()


async def exchange_code(code: str) -> None:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(TOKEN_URL, data={
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri(),
        })
    if resp.status_code != 200:
        logger.error("Google code exchange failed: %s %s", resp.status_code, resp.text[:200])
        raise GoogleHealthError(f"Google token exchange failed (HTTP {resp.status_code})")
    _store_token_response(resp.json())
    logger.info("Google Health connected")


def is_authenticated() -> bool:
    with Session(engine) as session:
        return session.get(GoogleToken, 1) is not None


async def get_valid_access_token() -> str:
    with Session(engine) as session:
        token = session.get(GoogleToken, 1)
    if token is None:
        raise GoogleNotAuthenticatedError("Google Health non collegato")

    margin = settings.token_refresh_margin_min * 60
    if token.expires_at - time.time() > margin:
        return token.access_token

    logger.info("Google access token expiring, refreshing")
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(TOKEN_URL, data={
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "grant_type": "refresh_token",
            "refresh_token": token.refresh_token,
        })
    if resp.status_code != 200:
        # Typical when the 7-day testing-mode refresh token expired
        logger.error("Google token refresh failed: %s %s", resp.status_code, resp.text[:200])
        raise GoogleNotAuthenticatedError(
            "Token Google scaduto — ricollega Google Health dal menu")
    _store_token_response(resp.json())
    with Session(engine) as session:
        return session.get(GoogleToken, 1).access_token


async def api_get(path: str, params: dict | None = None) -> dict:
    token = await get_valid_access_token()
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(f"{BASE_URL}{path}", params=params or {},
                                headers={"Authorization": f"Bearer {token}"})
    if resp.status_code == 401:
        raise GoogleNotAuthenticatedError("Google ha rifiutato il token — ricollega")
    if resp.status_code == 403:
        raise GoogleScopeMissingError(f"Scope mancante: {resp.text[:200]}")
    if resp.status_code >= 400:
        raise GoogleHealthError(f"Google Health HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.json()


async def list_exercises(page_size: int = 25, page_token: str | None = None,
                         filter_: str | None = None) -> dict:
    """List exercise data points (pageSize max 25 for this data type)."""
    params: dict = {"pageSize": page_size}
    if page_token:
        params["pageToken"] = page_token
    if filter_:
        params["filter"] = filter_
    return await api_get("/users/me/dataTypes/exercise/dataPoints", params)


# --------------------------------------------------------------- enrichment

# Strava->Wahoo and Google/Fitbit record the same activity with start times that
# can drift ~20 min, so match generously (the sport-family guard prevents merging
# genuinely different sports that happen to be close in time).
MATCH_TOLERANCE_S = 25 * 60
# Import label per sport family (workouts Google has but Wahoo never delivered)
FAMILY_LABEL = {"swim": "Swimming", "bike": "Cycling", "run": "Running", "walk": "Walking"}


def _sport_family(label: str) -> str | None:
    """Canonical sport family from any Wahoo/Google label or exercise type, or
    None if unknown. Works on variants too (SWIMMING_POOL, MOUNTAIN_BIKING…)."""
    s = (label or "").lower()
    if any(k in s for k in ("swim", "nuot")):
        return "swim"
    if any(k in s for k in ("bik", "cycl", "ciclis", "spin")):
        return "bike"
    if any(k in s for k in ("run", "cors")):
        return "run"
    if any(k in s for k in ("walk", "hik", "cammin", "escurs", "trek")):
        return "walk"
    return None


def _same_sport(a: str, b: str) -> bool:
    """True unless both labels are confidently different sports — so dedup/import
    never merge a swim with a bike that merely started at the same time."""
    fa, fb = _sport_family(a), _sport_family(b)
    if fa is None or fb is None:
        return True
    return fa == fb


def _sport_label(ex_type: str) -> str:
    """Human label for a Google exercise type, via its family."""
    return FAMILY_LABEL.get(_sport_family(ex_type)) or ex_type.replace("_", " ").title() or "Altro"


def _matches_sport(wahoo_label: str, ex_type: str) -> bool:
    """True only if both map to the SAME known sport family (positive match for
    enrichment — unlike _same_sport which is permissive on unknowns)."""
    fe = _sport_family(ex_type)
    return fe is not None and _sport_family(wahoo_label) == fe


def _overlap_s(w_start, w_dur_s: int, e_start, e_end) -> float:
    """Seconds of overlap between a workout [start, start+dur] and an exercise
    interval [e_start, e_end]. A zero-duration workout overlaps if its start
    falls inside the exercise."""
    from datetime import timedelta
    w_end = w_start + timedelta(seconds=w_dur_s or 0)
    if not w_dur_s:
        return 60.0 if e_start <= w_start <= e_end else 0.0
    lo, hi = max(w_start, e_start), min(w_end, e_end)
    return max(0.0, (hi - lo).total_seconds())
# Don't import a Google exercise younger than this: the Strava->Wahoo sync can
# lag hours, and the Wahoo version (when it comes) is the preferred base row.
IMPORT_GRACE_S = 12 * 3600


def _exercise_fields(point: dict) -> dict | None:
    """Flatten a Google exercise data point into Workout-shaped fields."""
    from datetime import datetime as dt

    ex = point.get("exercise") or {}
    interval = ex.get("interval") or {}
    start_raw = interval.get("startTime")
    if not start_raw:
        return None
    start = dt.fromisoformat(start_raw.replace("Z", "+00:00")).replace(tzinfo=None)
    end_raw = interval.get("endTime")
    end = dt.fromisoformat(end_raw.replace("Z", "+00:00")).replace(tzinfo=None) \
        if end_raw else None
    m = ex.get("metricsSummary") or {}

    def num(v):
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    active_s = num(str(ex.get("activeDuration", "")).rstrip("s"))
    distance_m = num(m.get("distanceMillimeters"))
    distance_m = distance_m / 1000 if distance_m else None
    ascent_m = num(m.get("elevationGainMillimeters"))
    ascent_m = ascent_m / 1000 if ascent_m else None
    try:
        uid = int(str(point.get("name", "")).rsplit("/", 1)[-1])
    except ValueError:
        uid = None
    if uid is not None and uid >= 2 ** 63:  # keep within SQLite signed int64
        uid = uid % 10 ** 18
    return {
        "uid": uid,
        "name": ex.get("displayName", ""),
        "type": ex.get("exerciseType", ""),
        "start": start,
        "end": end,
        "duration_s": (end - start).total_seconds() if end else None,
        "moving_s": active_s,
        "distance_m": distance_m,
        "avg_hr": num(m.get("averageHeartRateBeatsPerMinute")),
        "calories": num(m.get("caloriesKcal")),
        "ascent_m": ascent_m,
        "avg_speed_ms": (distance_m / active_s) if distance_m and active_s else None,
    }


def _is_imported(w) -> bool:
    return '"source": "google_health"' in (w.raw_summary or "")


async def fetch_hr_stream(start_utc, end_utc, max_pages: int = 6) -> dict | None:
    """Intraday heart-rate samples over [start,end] as a FIT-shaped streams dict.

    This is the data the bike FITs already give us; here we pull it for
    swims/walks/runs that reach the system as a bare summary, so their detail
    page gets the same HR-over-time chart. Returns None when too few samples.
    """
    from datetime import datetime as dt

    flt = ('heart_rate.sample_time.physical_time >= '
           f'"{start_utc.strftime("%Y-%m-%dT%H:%M:%S")}Z" AND '
           'heart_rate.sample_time.physical_time < '
           f'"{end_utc.strftime("%Y-%m-%dT%H:%M:%S")}Z"')
    samples: list[tuple] = []
    page_token = None
    for _ in range(max_pages):
        params = {"pageSize": 10000, "filter": flt}
        if page_token:
            params["pageToken"] = page_token
        data = await api_get("/users/me/dataTypes/heart-rate/dataPoints", params)
        for p in data.get("dataPoints", []):
            hr = p.get("heartRate") or {}
            ts_raw = (hr.get("sampleTime") or {}).get("physicalTime")
            bpm = hr.get("beatsPerMinute")
            if not ts_raw or bpm is None:
                continue
            try:
                ts = dt.fromisoformat(ts_raw.replace("Z", "+00:00")).replace(tzinfo=None)
                samples.append((ts, int(bpm)))
            except (TypeError, ValueError):
                pass
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    if len(samples) < 10:
        return None
    samples.sort(key=lambda x: x[0])
    t0 = samples[0][0]
    t = [int((ts - t0).total_seconds()) for ts, _ in samples]
    hr = [v for _, v in samples]
    n = len(t)
    return {"t": t, "hr": hr, "power": [None] * n, "cadence": [None] * n,
            "speed": [None] * n, "alt": [None] * n, "latlng": None}


async def _attach_hr_stream(workout_id: int, start_utc, end_utc) -> bool:
    """Idempotently fetch + store the intraday HR stream for one workout."""
    from .db import Workout, WorkoutStream, pack_streams

    if start_utc is None or end_utc is None:
        return False
    with Session(engine) as session:
        if session.get(WorkoutStream, workout_id):
            return False  # already attached on a previous run
    streams = await fetch_hr_stream(start_utc, end_utc)
    if not streams:
        return False
    hrs = [v for v in streams["hr"] if v]
    with Session(engine) as session:
        session.add(WorkoutStream(workout_id=workout_id,
                                  data=pack_streams(streams), n_records=len(streams["t"])))
        w = session.get(Workout, workout_id)
        if w and hrs:
            # the intraday stream is the authoritative HR source for the window
            w.avg_hr = round(sum(hrs) / len(hrs), 1)
            w.max_hr = float(max(hrs))
            session.add(w)
        session.commit()
    logger.info("Attached HR stream to workout %s (%s samples)", workout_id, len(streams["t"]))
    return True


async def enrich_workouts(max_pages: int = 8) -> int:
    """Merge Google Health exercises into the workout table. Three phases:

    1. dedupe: a Google-imported row whose Wahoo counterpart arrived late is
       deleted (the Wahoo row is the preferred base and gets the data in 2.)
    2. fill: data-less Wahoo workouts (no FIT: swims/walks/runs synced through
       third parties) get empty fields filled from the matching exercise.
       Coarse durations (whole minutes on Wahoo) are always refined.
       Workouts with a parsed FIT (rides) are never touched.
    3. import: exercises with no Wahoo workout anywhere near them (e.g. rides
       Wahoo never received) become new rows, after IMPORT_GRACE_S so a
       late-syncing Wahoo version can still win.

    Returns the number of created/updated workouts.
    """
    import json as jsonlib
    from datetime import datetime as dt, timedelta

    from sqlmodel import select

    from .db import IgnoredImport, Workout

    with Session(engine) as session:
        all_workouts = list(session.exec(select(Workout)))
        ignored = {r.id for r in session.exec(select(IgnoredImport))}
    candidates = [w for w in all_workouts if not w.has_fit]
    oldest_needed = (min((w.start_date for w in candidates), default=dt.utcnow())
                     - timedelta(hours=1))

    exercises: list[dict] = []
    page_token = None
    for _ in range(max_pages):
        data = await list_exercises(page_token=page_token)
        batch = [f for p in data.get("dataPoints", []) if (f := _exercise_fields(p))]
        exercises.extend(batch)
        page_token = data.get("nextPageToken")
        # Results are newest-first: stop paging once we are past the oldest candidate
        if not page_token or (batch and batch[-1]["start"] < oldest_needed):
            break

    def near(a: dt, b: dt) -> bool:
        return abs((a - b).total_seconds()) <= MATCH_TOLERANCE_S

    changed_total = 0

    # 0. Reconcile data-less Wahoo stubs: Wahoo can push a third-party activity
    # with the WRONG sport and no data (e.g. a swim from Strava arrives as an
    # empty "Biking"). Its label is unreliable, so trust Google by time overlap:
    # adopt the overlapping exercise's sport + data. The duplicate Google import
    # (if any) then collapses in the dedupe phase below.
    for w in candidates:
        if w.has_fit or w.distance_m or w.avg_hr:
            continue
        w_dur = max(w.duration_s or 0, w.moving_s or 0)
        best, best_ov = None, 0.0
        for e in exercises:
            ov = _overlap_s(w.start_date, w_dur, e["start"], e["end"])
            if ov > best_ov:
                best, best_ov = e, ov
        if best is None or best_ov < 60:
            continue
        new_sport = _sport_label(best["type"])
        with Session(engine) as session:
            wk = session.get(Workout, w.id)
            wk.sport = new_sport
            for field in ("distance_m", "avg_hr", "calories", "ascent_m", "avg_speed_ms"):
                if best[field]:
                    setattr(wk, field, best[field])
            if best["moving_s"]:
                wk.moving_s = int(best["moving_s"])
            if best["duration_s"]:
                wk.duration_s = int(best["duration_s"])
            wk.updated_at = dt.utcnow()
            session.add(wk)
            session.commit()
        w.sport = new_sport  # keep the in-memory copy in sync for the phases below
        dur = max(w.duration_s or 0, w.moving_s or 0) or 3600
        await _attach_hr_stream(w.id, w.start_date - timedelta(minutes=10),
                                w.start_date + timedelta(seconds=dur) + timedelta(minutes=10))
        changed_total += 1
        logger.info("Reconciled empty stub %s -> %s from Google (overlap %ss)",
                    w.id, new_sport, int(best_ov))

    # 1. Dedupe: imported row + a real Wahoo row for the same activity
    imported = [w for w in all_workouts if _is_imported(w)]
    for imp in imported:
        twin = next((w for w in all_workouts
                     if w.id != imp.id and not _is_imported(w)
                     and near(w.start_date, imp.start_date)
                     and _same_sport(imp.sport, w.sport)), None)
        if twin:
            merge_fields = ("distance_m", "ascent_m", "avg_hr", "max_hr", "avg_power",
                            "calories", "avg_speed_ms")
            with Session(engine) as session:
                row = session.get(Workout, imp.id)
                keep = session.get(Workout, twin.id)
                if keep and row:
                    # keep the Wahoo row but don't lose data the import had
                    for f in merge_fields:
                        if not getattr(keep, f) and getattr(row, f):
                            setattr(keep, f, getattr(row, f))
                            setattr(twin, f, getattr(row, f))  # keep in-memory in sync
                    keep.updated_at = dt.utcnow()
                    session.add(keep)
                if row:
                    session.delete(row)
                session.commit()
            all_workouts = [w for w in all_workouts if w.id != imp.id]
            candidates = [w for w in candidates if w.id != imp.id]
            logger.info("Merged+removed Google import %s into Wahoo %s", imp.id, twin.id)

    # 2. Field-by-field fill of data-less Wahoo workouts
    for w in candidates:
        match = next(
            (e for e in exercises
             if _matches_sport(w.sport, e["type"]) and near(e["start"], w.start_date)),
            None)
        if match is None:
            continue
        with Session(engine) as session:
            workout = session.get(Workout, w.id)
            changed = False
            for field in ("distance_m", "avg_hr", "calories", "ascent_m", "avg_speed_ms"):
                if not getattr(workout, field) and match[field]:
                    setattr(workout, field, match[field])
                    changed = True
            # Wahoo only has whole minutes here; Google times are exact
            if match["moving_s"] and workout.moving_s == workout.duration_s \
                    and int(match["moving_s"]) != workout.moving_s:
                workout.moving_s = int(match["moving_s"])
                changed = True
            if match["duration_s"] and int(match["duration_s"]) != workout.duration_s:
                workout.duration_s = int(match["duration_s"])
                changed = True
            if changed:
                workout.updated_at = dt.utcnow()
                session.add(workout)
                session.commit()
                logger.info("Enriched workout %s (%s) from Google Health %s",
                            w.id, w.sport, match["type"])
        # HR-over-time chart from intraday samples, over the workout's own window
        # (not the Google exercise's, which may be a fragment). Idempotent.
        dur = max(w.duration_s or 0, w.moving_s or 0) or 3600
        attached = await _attach_hr_stream(
            w.id, w.start_date - timedelta(minutes=10),
            w.start_date + timedelta(seconds=dur) + timedelta(minutes=10))
        if changed or attached:
            changed_total += 1

    # 3. Import exercises Wahoo never delivered (any sport, e.g. watch-only rides)
    grace_cutoff = dt.utcnow() - timedelta(seconds=IMPORT_GRACE_S)
    for e in exercises:
        if e["uid"] is None or e["uid"] in ignored or e["start"] > grace_cutoff:
            continue
        if _sport_family(e["type"]) == "walk":
            continue  # camminate/escursioni: solo arricchimento, mai import
        sport = _sport_label(e["type"])
        # Same start time AND same sport -> already seen by both sources, skip.
        # Different sport at a close time (e.g. walk then ride) is kept.
        if any(near(e["start"], w.start_date) and _same_sport(sport, w.sport)
               for w in all_workouts):
            continue
        new = Workout(
            id=e["uid"],
            name=e["name"] or sport,
            sport=sport,
            start_date=e["start"],
            duration_s=int(e["duration_s"] or e["moving_s"] or 0),
            moving_s=int(e["moving_s"] or e["duration_s"] or 0),
            distance_m=e["distance_m"] or 0.0,
            ascent_m=e["ascent_m"] or 0.0,
            avg_speed_ms=e["avg_speed_ms"] or 0.0,
            avg_hr=e["avg_hr"],
            calories=e["calories"],
            raw_summary=jsonlib.dumps({"source": "google_health",
                                       "exercise_type": e["type"]}),
        )
        with Session(engine, expire_on_commit=False) as session:
            if session.get(Workout, new.id):  # already imported on a previous run
                continue
            session.add(new)
            session.commit()
        all_workouts.append(new)
        changed_total += 1
        logger.info("Imported workout %s (%s, %s) from Google Health",
                    new.id, new.sport, new.start_date)
        await _attach_hr_stream(new.id, e["start"], e["end"])
    return changed_total


# --------------------------------------------------------------- health metrics

# key -> (dataType id, JSON wrapper field, value field, label, unit, decimals)
DAILY_METRICS = {
    "resting_hr": ("daily-resting-heart-rate", "dailyRestingHeartRate",
                   "beatsPerMinute", "FC a riposo", "bpm", 0),
    "hrv": ("daily-heart-rate-variability", "dailyHeartRateVariability",
            "averageHeartRateVariabilityMilliseconds", "Variabilità FC (HRV)", "ms", 0),
    "spo2": ("daily-oxygen-saturation", "dailyOxygenSaturation",
             "averagePercentage", "Saturazione O₂", "%", 1),
    "respiratory": ("daily-respiratory-rate", "dailyRespiratoryRate",
                    "breathsPerMinute", "Freq. respiratoria", "/min", 1),
    "skin_temp": ("daily-sleep-temperature-derivations", "dailySleepTemperatureDerivations",
                  "nightlyTemperatureCelsius", "Temp. cutanea (notte)", "°C", 1),
}


def _date_str(d: dict | None) -> str | None:
    if not d:
        return None
    return f"{d['year']:04d}-{d['month']:02d}-{d['day']:02d}"


async def _daily_series(dtype: str, wrapper: str, value_key: str,
                        decimals: int, days: int) -> list[dict]:
    """Return [{date, value}, ...] ascending for a daily-rollup data type."""
    data = await api_get(f"/users/me/dataTypes/{dtype}/dataPoints", {"pageSize": days})
    series = []
    for p in data.get("dataPoints", []):
        sub = p.get(wrapper) or {}
        val, date = sub.get(value_key), _date_str(sub.get("date"))
        if val is None or date is None:
            continue
        try:
            series.append({"date": date, "value": round(float(val), decimals)})
        except (TypeError, ValueError):
            continue
    series.sort(key=lambda x: x["date"])
    return series


async def _body_series(dtype: str, wrapper: str, value_key: str,
                       scale: float, decimals: int, days: int) -> list[dict]:
    """Body measurements are timestamped samples, not daily rollups."""
    from datetime import datetime as dt

    data = await api_get(f"/users/me/dataTypes/{dtype}/dataPoints", {"pageSize": days})
    series = []
    for p in data.get("dataPoints", []):
        sub = p.get(wrapper) or {}
        val = sub.get(value_key)
        ts = (sub.get("sampleTime") or {}).get("physicalTime")
        if val is None or not ts:
            continue
        try:
            date = dt.fromisoformat(ts.replace("Z", "+00:00")).strftime("%Y-%m-%d")
            series.append({"date": date, "value": round(float(val) * scale, decimals)})
        except (TypeError, ValueError):
            continue
    series.sort(key=lambda x: x["date"])
    return series


_SLEEP_STAGE_LABELS = {
    "DEEP": "profondo", "LIGHT": "leggero", "REM": "REM",
    "AWAKE": "sveglio", "ASLEEP": "dormito", "RESTLESS": "agitato",
}


# A sleep whose LOCAL start hour is in [NAP_START_FROM, NAP_START_TO) is a daytime
# nap, not a night — even a long siesta. Real nights start in the evening or after
# midnight, well outside this window; the range is wide enough to survive a couple
# of hours of UTC/local offset without misclassifying either.
NAP_START_FROM, NAP_START_TO = 9, 19
NIGHT_MIN_MINUTES = 90  # a night-window episode shorter than this is a fragment
NAP_MIN_MINUTES = 20    # a daytime episode shorter than this is noise


def _parse_sleep_point(p: dict) -> dict | None:
    """One sleep dataPoint -> episode dict tagged with "kind" ("night"|"nap"), or
    None if it's noise (missing interval, a night-window fragment, or a micro-nap).

    A night starts in the evening/night and lasts >= 90 min; a daytime episode
    (start 09:00-19:00) is a nap — kept for recovery, but never a night — as long
    as it's >= 20 min. This keeps a siesta from taking a night's place while still
    counting it toward daily rest.
    """
    from datetime import datetime as dt

    s = p.get("sleep") or {}
    interval = s.get("interval") or {}
    start, end = interval.get("startTime"), interval.get("endTime")
    if not start or not end:
        return None
    try:
        a = dt.fromisoformat(start.replace("Z", "+00:00"))
        b = dt.fromisoformat(end.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    total_min = round((b - a).total_seconds() / 60)
    if NAP_START_FROM <= a.hour < NAP_START_TO:
        if total_min < NAP_MIN_MINUTES:
            return None  # micro daytime episode, noise
        kind = "nap"
    else:
        if total_min < NIGHT_MIN_MINUTES:
            return None  # night-window fragment, not a night
        kind = "night"

    stages_min: dict[str, float] = {}
    for st in s.get("stages") or []:
        try:
            sa = dt.fromisoformat(st["startTime"].replace("Z", "+00:00"))
            sb = dt.fromisoformat(st["endTime"].replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError):
            continue
        stages_min[st.get("type", "")] = stages_min.get(st.get("type", ""), 0) \
            + (sb - sa).total_seconds() / 60

    summary = s.get("summary") or {}
    asleep = summary.get("minutesAsleep")
    if asleep is None:
        asleep = sum(stages_min.get(k, 0) for k in ("LIGHT", "REM", "DEEP", "ASLEEP"))
    asleep = round(float(asleep))
    return {
        "kind": kind,
        "date": a.strftime("%Y-%m-%d"),
        "bedtime": a.strftime("%H:%M"),
        "wake": b.strftime("%H:%M"),
        "total_min": total_min,
        "asleep_min": asleep,
        "efficiency": round(asleep / total_min * 100) if total_min else None,
        "stages": {_SLEEP_STAGE_LABELS.get(k, k.lower()): round(v)
                   for k, v in stages_min.items() if v},
    }


def _split_sleep(episodes: list[dict], days: int) -> tuple[list[dict], list[dict]]:
    """Split raw episodes into (nights, naps). At most one night per date — the
    longest — so a fragmented night can't double-count. Naps are summed per date
    (a day can have several) and kept for the recovery analysis."""
    by_date: dict[str, dict] = {}
    naps_by_date: dict[str, dict] = {}
    for e in episodes:
        if e["kind"] == "night":
            prev = by_date.get(e["date"])
            if prev is None or e["asleep_min"] > prev["asleep_min"]:
                by_date[e["date"]] = e
        else:
            nap = naps_by_date.setdefault(e["date"], {"date": e["date"], "asleep_min": 0,
                                                      "episodi": 0})
            nap["asleep_min"] += e["asleep_min"]
            nap["episodi"] += 1
    nights = sorted(by_date.values(), key=lambda n: n["date"])[-days:]
    kept = {n["date"] for n in nights}
    # only surface naps for the days whose night we actually kept, so the window
    # of naps matches the window of nights
    naps = sorted((n for d, n in naps_by_date.items() if d in kept),
                  key=lambda n: n["date"])
    for n in nights:  # drop the internal tag before returning
        n.pop("kind", None)
    return nights, naps


async def _sleep_episodes(days: int, max_pages: int = 4) -> tuple[list[dict], list[dict]]:
    """(nights, naps) over the window. Paginates because the API caps sleep at 25
    points/page — see _parse_sleep_point / _split_sleep."""
    episodes: list[dict] = []
    n_nights = 0
    page_token = None
    for _ in range(max_pages):
        params: dict = {"pageSize": 25}
        if page_token:
            params["pageToken"] = page_token
        data = await api_get("/users/me/dataTypes/sleep/dataPoints", params)
        points = data.get("dataPoints", [])
        for p in points:
            if (e := _parse_sleep_point(p)) is not None:
                episodes.append(e)
                n_nights += e["kind"] == "night"
        page_token = data.get("nextPageToken")
        if not page_token or not points or n_nights >= days:
            break
    return _split_sleep(episodes, days)


# Direction that counts as "better" per metric (None = neutral, no good/bad colour)
_HIGHER_BETTER = {"resting_hr": False, "hrv": True, "spo2": True,
                  "respiratory": None, "skin_temp": None,
                  "weight": None, "body_fat": False}


def _annotate_trend(metric: dict, key: str) -> None:
    """Add latest-vs-recent-baseline delta + direction/tone to a metric dict."""
    series = metric.get("series") or []
    metric["delta"], metric["dir"], metric["tone"] = None, "flat", "neutral"
    if len(series) < 3:
        return
    latest = series[-1]["value"]
    base_vals = [p["value"] for p in series[:-1][-7:]]
    base = sum(base_vals) / len(base_vals)
    delta = round(latest - base, 1)
    metric["delta"] = abs(delta)
    if abs(delta) <= abs(base) * 0.01:  # within 1% -> flat
        return
    metric["dir"] = "up" if delta > 0 else "down"
    hib = _HIGHER_BETTER.get(key)
    if hib is not None:
        metric["tone"] = "good" if (delta > 0) == hib else "bad"


def _score_one(series: list, higher_better: bool) -> float | None:
    """Latest value mapped to 0-100 vs the person's own recent baseline."""
    vals = [p["value"] for p in series]
    if len(vals) < 5:
        return None
    base = vals[:-1]
    mean, sd = statistics.mean(base), statistics.pstdev(base)
    if sd == 0:
        return 50.0
    z = (vals[-1] - mean) / sd
    if not higher_better:
        z = -z
    return max(0.0, min(100.0, 50 + z * 15))


def _wellness(metrics: dict, sleep: list) -> dict | None:
    """Our own readiness-style index (Fitbit's proprietary scores aren't in the
    API). Weighted blend of HRV, resting HR, sleep and SpO2 vs recent baseline."""
    sources = []
    if "hrv" in metrics:
        sources.append((metrics["hrv"]["series"], True, 0.35))
    if "resting_hr" in metrics:
        sources.append((metrics["resting_hr"]["series"], False, 0.30))
    if sleep and len(sleep) >= 5:
        sources.append(([{"value": n["asleep_min"]} for n in sleep], True, 0.25))
    if "spo2" in metrics:
        sources.append((metrics["spo2"]["series"], True, 0.10))

    scored = [(s, w) for series, hib, w in sources
              if (s := _score_one(series, hib)) is not None]
    if not scored:
        return None
    total_w = sum(w for _, w in scored)
    score = round(sum(s * w for s, w in scored) / total_w)
    label = ("Buono" if score >= 67 else "Nella media" if score >= 40 else "Sotto la media")
    return {"score": score, "label": label, "n_inputs": len(scored)}


async def fetch_health_overview(start_date=None, end_date=None) -> dict:
    """Aggregate the daily health metrics, body composition and sleep for the
    window [start_date, end_date] (dates; default last 30 days). Missing
    scopes/data are reported, not fatal."""
    from datetime import date, timedelta

    end_date = end_date or date.today()
    start_date = start_date or (end_date - timedelta(days=29))
    start_iso, end_iso = start_date.isoformat(), end_date.isoformat()
    # how far back to fetch (newest-first), capped
    span = min((date.today() - start_date).days + 2, 200)

    def in_window(d):
        return start_iso <= d <= end_iso

    out: dict = {"metrics": {}, "body": {}, "sleep": [], "naps": [],
                 "missing": [], "score": None}

    for key, (dtype, wrapper, vkey, label, unit, dec) in DAILY_METRICS.items():
        try:
            series = await _daily_series(dtype, wrapper, vkey, dec, span)
        except GoogleScopeMissingError:
            out["missing"].append(label)
            continue
        series = [p for p in series if in_window(p["date"])]
        if series:
            out["metrics"][key] = {"label": label, "unit": unit,
                                   "latest": series[-1]["value"], "series": series}

    for key, (dtype, wrapper, vkey, scale, unit, dec, label) in {
        "weight": ("weight", "weight", "weightGrams", 0.001, "kg", 1, "Peso"),
        "body_fat": ("body-fat", "bodyFat", "percentage", 1.0, "%", 1, "Massa grassa"),
    }.items():
        try:
            series = await _body_series(dtype, wrapper, vkey, scale, dec, span * 4)
        except GoogleScopeMissingError:
            out["missing"].append(label)
            continue
        series = [p for p in series if in_window(p["date"])]
        if series:
            out["body"][key] = {"label": label, "unit": unit,
                                "latest": series[-1]["value"], "series": series}

    try:
        nights, naps = await _sleep_episodes(span)
        out["sleep"] = [n for n in nights if in_window(n["date"])]
        out["naps"] = [n for n in naps if in_window(n["date"])]
    except GoogleScopeMissingError:
        out["missing"].append("Sonno")

    for key, metric in out["metrics"].items():
        _annotate_trend(metric, key)
    for key, metric in out["body"].items():
        _annotate_trend(metric, key)
    out["score"] = _wellness(out["metrics"], out["sleep"])

    return out
