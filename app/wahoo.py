"""Wahoo Cloud API client: OAuth flow, transparent token refresh, workout list,
FIT download, sync with exponential backoff.

Base URL: https://api.wahooligan.com — live docs: https://cloud-api.wahooligan.com
"""
import asyncio
import json
import logging
import os
import time
from datetime import datetime
from typing import Optional

import httpx
from sqlmodel import Session, select

from .config import settings
from .db import WahooToken, Workout, engine

logger = logging.getLogger(__name__)

BASE_URL = "https://api.wahooligan.com"
AUTH_URL = f"{BASE_URL}/oauth/authorize"
TOKEN_URL = f"{BASE_URL}/oauth/token"

# Minimal read scopes for profile + workouts + FIT download and refresh tokens.
# TODO: verificare su cloud-api.wahooligan.com l'elenco esatto degli scope
# (attesi: user_read, workouts_read, offline_data; power_zones_read se servono le zone)
SCOPES = "user_read workouts_read offline_data"

MAX_RETRIES = 4

# Mapping workout_type_id -> human label, used as fallback when the FIT
# session message is not available (webhook-only workouts).
# TODO: verificare su cloud-api.wahooligan.com la tabella completa dei workout_type_id
WORKOUT_TYPES = {
    0: "Biking", 1: "Running", 2: "FE", 3: "Running (track)", 4: "Running (trail)",
    5: "Running (treadmill)", 6: "Walking", 7: "Walking (speed)", 8: "Walking (nordic)",
    9: "Hiking", 10: "Mountaineering", 11: "Biking (cyclecross)", 12: "Biking (indoor)",
    13: "Biking (mountain)", 14: "Biking (recumbent)", 15: "Biking (road)",
    16: "Biking (track)", 17: "Biking (motocycling)", 18: "FE (general)",
    19: "FE (treadmill)", 20: "FE (elliptical)", 21: "FE (bike)", 22: "FE (rower)",
    23: "FE (climber)", 25: "Swimming (lap)", 26: "Swimming (open water)",
    27: "Snowboarding", 28: "Skiing", 29: "Skiing (downhill)", 30: "Skiing (cross country)",
    31: "Skating", 32: "Skating (ice)", 33: "Skating (inline)", 34: "Long Board",
    35: "Sailing", 36: "Windsurfing", 37: "Canoeing", 38: "Kayaking", 39: "Rowing",
    40: "Kiteboarding", 41: "Stand Up Paddle Board", 42: "Workout", 43: "Cardio Class",
    44: "Stair Climber", 45: "Wheelchair", 46: "Golfing", 47: "Other",
    49: "Biking (indoor cycling class)", 56: "Walking (treadmill)",
    61: "Biking (indoor trainer)", 62: "Multisport", 63: "Transition",
    64: "Ebiking", 65: "Tickr offline", 66: "Yoga", 67: "Running (indoor)",
    68: "Strength training", 255: "Unknown",
}


class WahooError(Exception):
    pass


class NotAuthenticatedError(WahooError):
    pass


# ---------------------------------------------------------------- OAuth

def build_authorize_url(state: str) -> str:
    from urllib.parse import urlencode
    params = {
        "client_id": settings.wahoo_client_id,
        "redirect_uri": settings.wahoo_redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def _store_token_response(data: dict) -> None:
    """Persist access/refresh token. Wahoo returns expires_in (seconds), we
    store an absolute expires_at so the refresh check is trivial."""
    expires_at = int(time.time()) + int(data.get("expires_in", 7200))
    with Session(engine) as session:
        token = session.get(WahooToken, 1)
        if token:
            token.access_token = data["access_token"]
            # Wahoo rotates the refresh token on every refresh: always keep the latest
            token.refresh_token = data.get("refresh_token", token.refresh_token)
            token.expires_at = expires_at
            session.add(token)
        else:
            session.add(WahooToken(
                id=1,
                access_token=data["access_token"],
                refresh_token=data["refresh_token"],
                expires_at=expires_at,
            ))
        session.commit()


async def exchange_code(code: str) -> None:
    """Exchange the OAuth code for tokens, persist them, fetch the user profile."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(TOKEN_URL, data={
            "client_id": settings.wahoo_client_id,
            "client_secret": settings.wahoo_client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": settings.wahoo_redirect_uri,
        })
    if resp.status_code != 200:
        logger.error("OAuth code exchange failed: %s %s", resp.status_code, resp.text[:200])
        raise WahooError(f"Token exchange failed (HTTP {resp.status_code})")
    _store_token_response(resp.json())

    # Best-effort: store user name/id for the UI greeting
    try:
        user = await api_get("/v1/user")
        with Session(engine) as session:
            token = session.get(WahooToken, 1)
            token.user_id = user.get("id", 0)
            token.user_name = f"{user.get('first', '')} {user.get('last', '')}".strip()
            session.add(token)
            session.commit()
        logger.info("Stored Wahoo tokens for user %s", user.get("id"))
    except WahooError:
        logger.warning("Could not fetch user profile after OAuth (non-fatal)")


async def get_valid_access_token() -> str:
    """Return a valid access token, refreshing it first if it expires soon.

    Transparent to callers: they never deal with token expiry themselves.
    """
    with Session(engine) as session:
        token = session.get(WahooToken, 1)
    if token is None:
        raise NotAuthenticatedError("No Wahoo token stored — complete the OAuth login first")

    margin = settings.token_refresh_margin_min * 60
    if token.expires_at - time.time() > margin:
        return token.access_token

    logger.info("Access token expiring soon, refreshing")
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(TOKEN_URL, data={
            "client_id": settings.wahoo_client_id,
            "client_secret": settings.wahoo_client_secret,
            "grant_type": "refresh_token",
            "refresh_token": token.refresh_token,
        })
    if resp.status_code != 200:
        logger.error("Token refresh failed: %s %s", resp.status_code, resp.text[:200])
        raise WahooError(f"Token refresh failed (HTTP {resp.status_code})")
    _store_token_response(resp.json())
    with Session(engine) as session:
        return session.get(WahooToken, 1).access_token


def is_authenticated() -> bool:
    with Session(engine) as session:
        return session.get(WahooToken, 1) is not None


def get_user_name() -> str:
    with Session(engine) as session:
        token = session.get(WahooToken, 1)
        return token.user_name if token else ""


# ---------------------------------------------------------------- API client

async def _request_with_backoff(client: httpx.AsyncClient, method: str, url: str,
                                **kwargs) -> httpx.Response:
    """Authenticated request with exponential backoff on 429/5xx."""
    for attempt in range(MAX_RETRIES + 1):
        token = await get_valid_access_token()
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {token}"
        resp = await client.request(method, url, headers=headers, **kwargs)
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt == MAX_RETRIES:
                raise WahooError(f"Wahoo API HTTP {resp.status_code} after {MAX_RETRIES} retries")
            delay = min(2 ** attempt * 10, 120)  # 10s, 20s, 40s, 80s
            logger.warning("Wahoo HTTP %s on %s — backing off %ss (attempt %s/%s)",
                           resp.status_code, url, delay, attempt + 1, MAX_RETRIES)
            await asyncio.sleep(delay)
            continue
        if resp.status_code == 401:
            raise NotAuthenticatedError("Wahoo rejected the token (401) — re-authenticate")
        if resp.status_code >= 400:
            raise WahooError(f"Wahoo API HTTP {resp.status_code}: {resp.text[:200]}")
        return resp
    raise WahooError("unreachable")


async def api_get(path: str, params: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await _request_with_backoff(client, "GET", f"{BASE_URL}{path}",
                                           params=params or {})
        return resp.json()


# ---------------------------------------------------------------- workouts

def _parse_iso(value: str | None) -> datetime:
    if not value:
        return datetime.utcnow()
    return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)


def workout_from_payload(w: dict, summary: dict | None = None) -> Workout:
    """Build a Workout row from the Wahoo workout object (+ optional summary).

    Field names per cloud-api.wahooligan.com; summaries report accumulated
    values as strings (e.g. "12345.0").
    # TODO: verificare su cloud-api.wahooligan.com i nomi esatti dei campi summary
    """
    s = summary or w.get("workout_summary") or {}

    def num(key: str) -> Optional[float]:
        v = s.get(key)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    distance = num("distance_accum") or 0.0
    duration = num("duration_total_accum") or (w.get("minutes") or 0) * 60
    moving = num("duration_active_accum") or duration

    return Workout(
        id=w["id"],
        name=w.get("name") or f"Workout {w['id']}",
        sport=WORKOUT_TYPES.get(w.get("workout_type_id", 255), "Unknown"),
        start_date=_parse_iso(w.get("starts")),
        duration_s=int(duration),
        moving_s=int(moving),
        distance_m=distance,
        ascent_m=num("ascent_accum") or 0.0,
        avg_speed_ms=num("speed_avg") or 0.0,
        avg_hr=num("heart_rate_avg"),
        avg_power=num("power_avg"),
        avg_cadence=num("cadence_avg"),
        calories=num("calories_accum"),
        raw_summary=json.dumps({"workout": w, "summary": s}, ensure_ascii=False),
        updated_at=datetime.utcnow(),
    )


def upsert_workout(workout: Workout) -> None:
    """Idempotent insert/update on workout id (duplicate webhooks are harmless).

    FIT-derived fields (has_fit, fit_path, refined metrics) are preserved if
    already set, so a late webhook can't downgrade a fully parsed workout.

    expire_on_commit=False: callers keep using the passed instance (e.g. its id)
    after this session is gone.
    """
    with Session(engine, expire_on_commit=False) as session:
        existing = session.get(Workout, workout.id)
        if existing:
            if existing.has_fit:
                # Only refresh the lightweight metadata, keep FIT-derived fields
                existing.name = workout.name
                existing.raw_summary = workout.raw_summary
                existing.updated_at = datetime.utcnow()
            else:
                for field, value in workout.model_dump().items():
                    setattr(existing, field, value)
            session.add(existing)
        else:
            session.add(workout)
        session.commit()


def _fit_url_from_payload(w: dict, summary: dict | None = None) -> Optional[str]:
    """Extract the FIT file URL from a workout/summary payload.

    # TODO: verificare su cloud-api.wahooligan.com: il summary contiene
    # "file": {"url": "https://...fit"}
    """
    s = summary or w.get("workout_summary") or {}
    f = s.get("file") or {}
    return f.get("url")


async def download_fit(workout_id: int, fit_url: str) -> str:
    """Download the FIT file to the persistent volume. Returns the local path.

    FIT URLs are typically pre-signed (no Authorization header needed); we
    follow redirects and fall back gracefully if auth is required.
    """
    path = os.path.join(settings.fit_dir, f"{workout_id}.fit")
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        resp = await client.get(fit_url)
        if resp.status_code == 401 or resp.status_code == 403:
            # Some deployments serve the file behind the API auth instead
            token = await get_valid_access_token()
            resp = await client.get(fit_url, headers={"Authorization": f"Bearer {token}"})
        if resp.status_code != 200:
            raise WahooError(f"FIT download failed (HTTP {resp.status_code})")
        with open(path, "wb") as fh:
            fh.write(resp.content)
    logger.info("Downloaded FIT for workout %s (%s bytes)", workout_id, len(resp.content))
    return path


async def fetch_workout_summary(workout_id: int) -> dict:
    # TODO: verificare su cloud-api.wahooligan.com il path esatto
    return await api_get(f"/v1/workouts/{workout_id}/workout_summary")


async def list_workouts(page: int = 1, per_page: int = 50) -> list[dict]:
    data = await api_get("/v1/workouts", {"page": page, "per_page": per_page})
    # The list endpoint wraps results: {"workouts": [...], "total": N, ...}
    if isinstance(data, dict):
        return data.get("workouts", [])
    return data


# ---------------------------------------------------------------- ingest & sync

async def ingest_workout(w: dict, summary: dict | None = None) -> None:
    """Full pipeline for one workout: upsert summary row -> download FIT ->
    parse -> refine the row with FIT data and store the streams.

    Used both by the webhook background task and by the manual sync.
    Failures in the FIT stage leave a valid summary-only row (has_fit=False).
    """
    from . import fit as fitmod  # local import to avoid cycles

    workout = workout_from_payload(w, summary)
    upsert_workout(workout)

    with Session(engine) as session:
        existing = session.get(Workout, workout.id)
        if existing and existing.has_fit:
            logger.info("Workout %s already has FIT, skipping download", workout.id)
            return

    fit_url = _fit_url_from_payload(w, summary)
    if not fit_url:
        # Webhook payloads should include it; fall back to the summary endpoint
        try:
            summary = await fetch_workout_summary(workout.id)
            fit_url = _fit_url_from_payload(w, summary)
        except WahooError as e:
            logger.warning("No summary for workout %s: %s", workout.id, e)
    if not fit_url:
        logger.info("Workout %s has no FIT file (summary only)", workout.id)
        return

    try:
        path = await download_fit(workout.id, fit_url)
        fitmod.parse_and_store(workout.id, path)
    except (WahooError, fitmod.FitParseError) as e:
        logger.error("FIT pipeline failed for workout %s: %s", workout.id, e)
        return
    # Wahoo phone-app FITs carry no altitude: estimate ascent from GPS + DEM
    await fitmod.enrich_ascent(workout.id)


async def sync_workouts(full: bool = False) -> int:
    """Manual fallback sync: list workouts (paginated) and ingest missing ones.

    Incremental: skip every workout already in the DB (no re-fetch of their
    summary — that hammered the API and rate-limited on the no-FIT third-party
    workouts). Stop paging once a full page is all already-known. Full: re-ingest
    rows without FIT to retry a FIT that may have appeared, but never re-download
    parsed FITs.
    """
    from .db import IgnoredImport
    with Session(engine) as session:
        ignored = {r.id for r in session.exec(select(IgnoredImport))} if not full else set()

    count = 0
    page = 1
    while True:
        batch = await list_workouts(page=page, per_page=50)
        if not batch:
            break
        known = 0
        for w in batch:
            if w["id"] in ignored:
                known += 1  # deleted by the user; don't re-create it (incremental)
                continue
            with Session(engine) as session:
                existing = session.get(Workout, w["id"])
            if existing:
                known += 1
                if not full or existing.has_fit:
                    continue  # incremental: skip; full: keep parsed FITs as-is
            await ingest_workout(w)
            count += 1
        logger.info("Sync page %s: %s ingested (%s già presenti)", page, count, known)
        if not full and known == len(batch):
            break  # whole page already known -> stop early
        if len(batch) < 50:
            break
        page += 1
    logger.info("Sync complete: %s workouts ingested %s", count, "(full)" if full else "")
    return count
