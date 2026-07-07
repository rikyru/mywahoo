"""FastAPI application: routing, auth guard, webhook ingestion, dashboard, AI."""
import json
import logging
import secrets
import os
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import markdown as md
from fastapi import (BackgroundTasks, Depends, FastAPI, File, Form, HTTPException,
                     Request, UploadFile)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select
from starlette.middleware.sessions import SessionMiddleware

from . import anthropic_client, fit as fitmod, google_health, wahoo
from .config import settings, setup_logging
from .db import (AiAnalysis, IgnoredImport, PeriodSummary, Workout, WorkoutStream,
                 engine, get_setting, init_db, set_setting)

setup_logging()
logger = logging.getLogger(__name__)

app = FastAPI(title="MyWahoo", docs_url=None, redoc_url=None)

# Signed session cookie. HttpOnly is always set by the middleware;
# Secure only when the public URL is HTTPS (so local HTTP testing still works).
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.app_secret_key or "dev-only-insecure",
    session_cookie="mywahoo_session",
    same_site="lax",
    https_only=settings.app_base_url.startswith("https://"),
    max_age=60 * 60 * 24 * 30,
)

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=BASE_DIR / "templates")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

PERIODS = {"week": 7, "month": 30, "year": 365, "all": None}


@app.on_event("startup")
def on_startup() -> None:
    missing = settings.validate()
    if missing:
        logger.warning("Missing required env vars: %s — the app will not work correctly",
                       ", ".join(missing))
    init_db()
    logger.info("MyWahoo started (provider=%s, model=%s, db=%s, fits=%s)",
                settings.ai_provider, settings.ai_model, settings.db_path, settings.fit_dir)


# ---------------------------------------------------------------- helpers

def require_auth(request: Request) -> None:
    if not request.session.get("authed") or not wahoo.is_authenticated():
        raise HTTPException(status_code=307, headers={"Location": "/login"})


def period_start(period: str) -> datetime | None:
    days = PERIODS.get(period)
    return datetime.utcnow() - timedelta(days=days) if days else None


def fmt_duration(seconds: int) -> str:
    h, rem = divmod(int(seconds or 0), 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s"


def fmt_speed(w: Workout) -> str:
    """Pace (min/km) for running/walking, km/h otherwise."""
    if not w.avg_speed_ms or w.avg_speed_ms <= 0:
        return "-"
    if "run" in w.sport.lower() or "walk" in w.sport.lower() or "hik" in w.sport.lower():
        sec_per_km = 1000 / w.avg_speed_ms
        return f"{int(sec_per_km // 60)}:{int(sec_per_km % 60):02d} /km"
    return f"{w.avg_speed_ms * 3.6:.1f} km/h"


templates.env.filters["duration"] = fmt_duration
templates.env.globals["fmt_speed"] = fmt_speed


def query_workouts(session: Session, period: str, sport: str | None) -> list[Workout]:
    stmt = select(Workout)
    start = period_start(period)
    if start:
        stmt = stmt.where(Workout.start_date >= start)
    if sport:
        stmt = stmt.where(Workout.sport == sport)
    return list(session.exec(stmt.order_by(Workout.start_date.desc())))


# Movable analysis window for the dashboard
WINDOWS = {"7": "1 settimana", "14": "2 settimane", "30": "1 mese"}


def _parse_date(s: str | None) -> date | None:
    try:
        return date.fromisoformat(s) if s else None
    except ValueError:
        return None


def resolve_window(win: str, end_s: str, from_s: str, to_s: str) -> dict:
    """Resolve the dashboard time window from query params into a date range
    plus the metadata (label, prev/next ends) the template needs to navigate."""
    today = date.today()
    if win == "custom":
        f = _parse_date(from_s) or today - timedelta(days=29)
        t = _parse_date(to_s) or today
        if f > t:
            f, t = t, f
        return {"win": "custom", "from": f.isoformat(), "to": t.isoformat(),
                "start": datetime.combine(f, time.min), "end": datetime.combine(t, time.max),
                "label": f"{f.strftime('%d/%m/%Y')} – {t.strftime('%d/%m/%Y')}",
                "prev_end": None, "next_end": None, "is_current": True}
    days = int(win) if win in WINDOWS else 30
    win = str(days)
    end_d = _parse_date(end_s) or today
    if end_d > today:
        end_d = today
    start_d = end_d - timedelta(days=days - 1)
    is_current = end_d >= today
    return {"win": win, "from": "", "to": "",
            "start": datetime.combine(start_d, time.min),
            "end": datetime.combine(end_d, time.max),
            "end_d": end_d.isoformat(),
            "label": f"{start_d.strftime('%d/%m')} – {end_d.strftime('%d/%m/%Y')}",
            "prev_end": (start_d - timedelta(days=1)).isoformat(),
            "next_end": None if is_current else min(today, end_d + timedelta(days=days)).isoformat(),
            "is_current": is_current}


def query_range(session: Session, start: datetime, end: datetime,
                sport: str | None) -> list[Workout]:
    stmt = select(Workout).where(Workout.start_date >= start, Workout.start_date <= end)
    if sport:
        stmt = stmt.where(Workout.sport == sport)
    return list(session.exec(stmt.order_by(Workout.start_date.desc())))


def build_chart_data(workouts: list[Workout]) -> dict:
    """Server-side aggregation for Chart.js."""
    week_km: dict[str, float] = defaultdict(float)
    type_count: dict[str, int] = defaultdict(int)
    metric_points = []   # avg power if available, else speed km/h
    hr_buckets: dict[str, int] = defaultdict(int)
    pw_buckets: dict[str, int] = defaultdict(int)

    for w in workouts:
        iso = w.start_date.isocalendar()
        week_km[f"{iso[0]}-W{iso[1]:02d}"] += w.distance_m / 1000
        type_count[w.sport or "Altro"] += 1
        if w.avg_power:
            metric_points.append({"x": w.start_date.strftime("%Y-%m-%d"),
                                  "y": round(w.avg_power, 0), "kind": "power"})
        elif w.avg_speed_ms:
            metric_points.append({"x": w.start_date.strftime("%Y-%m-%d"),
                                  "y": round(w.avg_speed_ms * 3.6, 1), "kind": "speed"})
        if w.avg_hr:
            b = int(w.avg_hr // 10) * 10
            hr_buckets[f"{b}-{b + 9}"] += 1
        if w.avg_power:
            b = int(w.avg_power // 25) * 25
            pw_buckets[f"{b}-{b + 24}"] += 1

    weeks = sorted(week_km)[-26:]
    power_mode = any(p["kind"] == "power" for p in metric_points)
    points = [p for p in metric_points if p["kind"] == ("power" if power_mode else "speed")]
    return {
        "weekly": {"labels": weeks, "values": [round(week_km[k], 1) for k in weeks]},
        "types": {"labels": list(type_count), "values": list(type_count.values())},
        "metric": {"label": "Potenza media (W)" if power_mode else "Velocità media (km/h)",
                   "points": sorted(points, key=lambda p: p["x"])},
        "hr": {"labels": sorted(hr_buckets), "values": [hr_buckets[k] for k in sorted(hr_buckets)]},
        "power": {"labels": sorted(pw_buckets, key=lambda k: int(k.split("-")[0])),
                  "values": [pw_buckets[k] for k in sorted(pw_buckets, key=lambda k: int(k.split("-")[0]))]},
    }


# ---------------------------------------------------------------- auth

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if request.session.get("authed") and wahoo.is_authenticated():
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html",
                                      {"error": request.query_params.get("error")})


@app.get("/login/wahoo")
def login_wahoo(request: Request):
    # Random state stored in the session, validated in the callback (CSRF protection)
    state = secrets.token_urlsafe(24)
    request.session["oauth_state"] = state
    return RedirectResponse(wahoo.build_authorize_url(state))


@app.get("/oauth/callback")
async def oauth_callback(request: Request, code: str | None = None,
                         state: str | None = None, error: str | None = None):
    if error:
        return RedirectResponse(f"/login?error={error}", status_code=303)
    expected = request.session.pop("oauth_state", None)
    if not state or not expected or not secrets.compare_digest(state, expected):
        logger.warning("OAuth callback with invalid state")
        return RedirectResponse("/login?error=invalid_state", status_code=303)
    if not code:
        return RedirectResponse("/login?error=missing_code", status_code=303)
    try:
        await wahoo.exchange_code(code)
    except wahoo.WahooError:
        return RedirectResponse("/login?error=exchange_failed", status_code=303)
    request.session["authed"] = True
    return RedirectResponse("/", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ------------------------------------------------- Google Health (ex Fitbit)

@app.get("/login/google", dependencies=[Depends(require_auth)])
def login_google(request: Request):
    state = secrets.token_urlsafe(24)
    request.session["google_oauth_state"] = state
    return RedirectResponse(google_health.build_authorize_url(state), status_code=303)


@app.get("/oauth/google/callback")
async def google_oauth_callback(request: Request, code: str | None = None,
                                state: str | None = None, error: str | None = None):
    if error:
        return RedirectResponse(f"/?google_error={error}", status_code=303)
    expected = request.session.pop("google_oauth_state", None)
    if not state or not expected or not secrets.compare_digest(state, expected):
        logger.warning("Google OAuth callback with invalid state")
        return RedirectResponse("/?google_error=invalid_state", status_code=303)
    if not code:
        return RedirectResponse("/?google_error=missing_code", status_code=303)
    try:
        await google_health.exchange_code(code)
    except google_health.GoogleHealthError:
        return RedirectResponse("/?google_error=exchange_failed", status_code=303)
    return RedirectResponse("/?google=connected", status_code=303)


def _health_key(window: dict) -> str:
    return f"health:{window['start'].date().isoformat()}:{window['end'].date().isoformat()}"


def _health_qs(window: dict, sport: str = "") -> str:
    """Query string that reproduces the current health window."""
    if window["win"] == "custom":
        return f"win=custom&from={window['from']}&to={window['to']}"
    if not window["is_current"]:
        return f"win={window['win']}&end={window['end_d']}"
    return f"win={window['win']}"


@app.get("/health", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def health_page(request: Request, win: str = "30", end: str = ""):
    window = resolve_window(win, end, request.query_params.get("from", ""),
                            request.query_params.get("to", ""))
    if not google_health.is_authenticated():
        return templates.TemplateResponse(request, "health.html",
                                          {"connected": False, "data": None, "window": window})
    try:
        data = await google_health.fetch_health_overview(window["start"].date(),
                                                         window["end"].date())
    except google_health.GoogleNotAuthenticatedError:
        return templates.TemplateResponse(request, "health.html",
                                          {"connected": False, "expired": True,
                                           "data": None, "window": window})
    except google_health.GoogleHealthError as e:
        return templates.TemplateResponse(request, "health.html",
                                          {"connected": True, "data": None,
                                           "error": str(e), "window": window})
    with Session(engine) as session:
        insight = session.get(PeriodSummary, _health_key(window))
    return templates.TemplateResponse(request, "health.html", {
        "connected": True, "data": data, "window": window, "windows": WINDOWS,
        "insight_html": md.markdown(insight.content, extensions=["tables"]) if insight else None,
        "insight_date": insight.created_at if insight else None,
        "error": request.query_params.get("error"),
    })


@app.post("/health/insight", dependencies=[Depends(require_auth)])
async def health_insight(request: Request, regenerate: str = Form(default=""),
                         win: str = Form("30"), end: str = Form(""),
                         from_: str = Form("", alias="from"), to: str = Form("")):
    window = resolve_window(win, end, from_, to)
    key = _health_key(window)
    redirect = f"/health?{_health_qs(window)}"
    with Session(engine) as session:
        cached = session.get(PeriodSummary, key)
    if cached and not regenerate:
        return RedirectResponse(redirect, status_code=303)
    with Session(engine) as session:
        workouts = [w.model_dump(exclude={"raw_summary", "fit_path", "updated_at"})
                    for w in query_range(session, window["start"], window["end"], None)]
    try:
        data = await google_health.fetch_health_overview(window["start"].date(),
                                                         window["end"].date())
        content = await anthropic_client.summarize_health(data, workouts)
    except google_health.GoogleHealthError as e:
        return RedirectResponse(f"/health?{_health_qs(window)}&{urlencode({'error': str(e)})}",
                                status_code=303)
    except anthropic_client.AnthropicError as e:
        return RedirectResponse(f"/health?{_health_qs(window)}&{urlencode({'error': str(e)})}",
                                status_code=303)
    with Session(engine) as session:
        existing = session.get(PeriodSummary, key)
        if existing:
            existing.content = content
            existing.model = anthropic_client.effective_model()
            existing.created_at = datetime.utcnow()
            session.add(existing)
        else:
            session.add(PeriodSummary(key=key, content=content, model=anthropic_client.effective_model()))
        session.commit()
    return RedirectResponse(redirect, status_code=303)


@app.post("/health/chat", dependencies=[Depends(require_auth)])
async def health_chat(request: Request):
    """Grounded chat: answers follow-up questions using the window's health data
    and activities. Stateless — the client sends the whole short history."""
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "richiesta non valida"}, status_code=400)
    history = body.get("messages") or []
    # keep it small and well-formed
    history = [{"role": m.get("role"), "content": str(m.get("content", ""))[:2000]}
               for m in history if m.get("role") in ("user", "assistant") and m.get("content")][-12:]
    if not history or history[-1]["role"] != "user":
        return JSONResponse({"error": "nessuna domanda"}, status_code=400)

    window = resolve_window(body.get("win", "30"), body.get("end", ""),
                            body.get("from", ""), body.get("to", ""))
    try:
        data = await google_health.fetch_health_overview(window["start"].date(),
                                                         window["end"].date())
    except google_health.GoogleHealthError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    with Session(engine) as session:
        workouts = [w.model_dump(exclude={"raw_summary", "fit_path", "updated_at"})
                    for w in query_range(session, window["start"], window["end"], None)]
    try:
        reply = await anthropic_client.chat_health(data, workouts, history)
    except anthropic_client.AnthropicError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    return JSONResponse({"reply": reply})


@app.get("/settings", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def settings_page(request: Request):
    provider = anthropic_client.effective_provider()
    openai_models = await anthropic_client.list_openai_models() if settings.openai_api_key else []
    return templates.TemplateResponse(request, "settings.html", {
        "provider": provider,
        "model": anthropic_client.effective_model(),
        "openai_models": openai_models,
        "has_openai": bool(settings.openai_api_key),
        "has_anthropic": bool(settings.anthropic_api_key),
        "message": request.query_params.get("msg"),
    })


@app.post("/settings", dependencies=[Depends(require_auth)])
async def settings_save(provider: str = Form("openai"), model: str = Form("")):
    provider = provider if provider in ("openai", "anthropic") else "openai"
    set_setting("ai_provider", provider)
    set_setting("ai_model", model.strip())
    logger.info("AI settings updated: provider=%s model=%s", provider, model.strip() or "(default)")
    return RedirectResponse(f"/settings?{urlencode({'msg': 'Impostazioni salvate'})}",
                            status_code=303)


@app.get("/google/probe", dependencies=[Depends(require_auth)])
async def google_probe(page_token: str = ""):
    """Diagnostic: dump recent Google Health exercises to decide what we can enrich."""
    try:
        data = await google_health.list_exercises(page_token=page_token or None)
    except google_health.GoogleNotAuthenticatedError as e:
        return JSONResponse({"error": str(e), "connect": "/login/google"}, status_code=401)
    except google_health.GoogleHealthError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    return JSONResponse(data)


# ---------------------------------------------------------------- webhook

async def _process_webhook(payload: dict) -> None:
    """Background processing: upsert -> FIT download -> parse. Never raises."""
    try:
        # TODO: verificare su cloud-api.wahooligan.com la struttura esatta del
        # payload webhook workout_summary (atteso: {"event_type": "workout_summary",
        # "workout_summary": {..., "workout": {...}, "file": {"url": ...}}})
        summary = payload.get("workout_summary") or {}
        workout = summary.get("workout") or payload.get("workout") or {}
        if not workout.get("id"):
            logger.warning("Webhook payload without workout id, ignoring")
            return
        await wahoo.ingest_workout(workout, summary)
        if google_health.is_authenticated():
            try:
                await google_health.enrich_workouts(max_pages=1)
            except google_health.GoogleHealthError as e:
                logger.warning("Google Health enrichment skipped: %s", e)
    except Exception:
        logger.exception("Webhook background processing failed")


@app.post("/webhook/wahoo")
async def webhook_wahoo(request: Request, background: BackgroundTasks):
    """Receives workout_summary notifications. Validates the shared token,
    answers 200 immediately and processes in background (webhooks have short
    delivery timeouts on Wahoo's side)."""
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid JSON")

    # Authenticity check: shared token. Wahoo includes the webhook_token in the
    # payload body; we also accept it as a header for flexibility.
    # TODO: verificare su cloud-api.wahooligan.com il meccanismo esatto di firma
    received = payload.get("webhook_token") or request.headers.get("x-webhook-token", "")
    if not settings.wahoo_webhook_token or \
            not secrets.compare_digest(str(received), settings.wahoo_webhook_token):
        logger.warning("Webhook with invalid token rejected")
        raise HTTPException(401, "Invalid webhook token")

    event_type = payload.get("event_type", "")
    logger.info("Webhook received: event_type=%s", event_type)
    if event_type == "workout_summary" or "workout_summary" in payload:
        background.add_task(_process_webhook, payload)
    return JSONResponse({"status": "accepted"})


# ---------------------------------------------------------------- pages

@app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def dashboard(request: Request, win: str = "30", end: str = "",
              sport: str = "", sort: str = "date", order: str = "desc"):
    window = resolve_window(win, end, request.query_params.get("from", ""),
                            request.query_params.get("to", ""))
    with Session(engine) as session:
        workouts = query_range(session, window["start"], window["end"], sport or None)
        all_sports = sorted({s for s in session.exec(
            select(Workout.sport).distinct()) if s})

    total_km = sum(w.distance_m for w in workouts) / 1000
    total_time = sum(w.moving_s for w in workouts)
    total_elev = sum(w.ascent_m for w in workouts)
    powers = [w.avg_power for w in workouts if w.avg_power]
    hrs = [w.avg_hr for w in workouts if w.avg_hr]

    key = {"date": lambda w: w.start_date,
           "distance": lambda w: w.distance_m,
           "duration": lambda w: w.moving_s}.get(sort, lambda w: w.start_date)
    table = sorted(workouts, key=key, reverse=(order != "asc"))[:200]

    return templates.TemplateResponse(request, "dashboard.html", {
        "user": wahoo.get_user_name(),
        "window": window, "windows": WINDOWS,
        "sport": sport, "sort": sort, "order": order,
        "all_sports": all_sports,
        "kpi": {
            "count": len(workouts),
            "distance_km": f"{total_km:,.0f}",
            "time": fmt_duration(total_time),
            "elevation_m": f"{total_elev:,.0f}",
            "avg_power": f"{sum(powers) / len(powers):.0f}" if powers else "-",
            "avg_hr": f"{sum(hrs) / len(hrs):.0f}" if hrs else "-",
        },
        "charts_json": json.dumps(build_chart_data(workouts)),
        "workouts": table,
        "message": request.query_params.get("msg"),
        "error": request.query_params.get("error"),
    })


# ---------------------------------------------------------------- cleanup

def _is_google_import(w: Workout) -> bool:
    return '"source": "google_health"' in (w.raw_summary or "")


@app.get("/duplicates", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def duplicates(request: Request):
    """Coppie di attività che iniziano a meno di 30 min l'una dall'altra —
    candidate doppioni da rivedere a mano."""
    with Session(engine) as session:
        ws = list(session.exec(select(Workout).order_by(Workout.start_date)))
    pairs = []
    for a, b in zip(ws, ws[1:]):
        gap = abs((b.start_date - a.start_date).total_seconds())
        if gap <= 30 * 60:
            pairs.append({"a": a, "b": b, "gap_min": round(gap / 60)})
    return templates.TemplateResponse(request, "duplicates.html",
                                      {"pairs": pairs, "n": len(pairs)})


@app.post("/workout/{workout_id}/delete", dependencies=[Depends(require_auth)])
def workout_delete(workout_id: int):
    with Session(engine) as session:
        w = session.get(Workout, workout_id)
        if not w:
            raise HTTPException(404, "Attività non trovata")
        google_import = _is_google_import(w)
        # Drop stream + cached analysis, then the workout itself
        stream = session.get(WorkoutStream, workout_id)
        if stream:
            session.delete(stream)
        analysis = session.get(AiAnalysis, workout_id)
        if analysis:
            session.delete(analysis)
        # A Google-only import would re-appear at the next sync: blacklist its uid
        if google_import and not session.get(IgnoredImport, workout_id):
            session.add(IgnoredImport(id=workout_id))
        session.delete(w)
        session.commit()
    return RedirectResponse(f"/?{urlencode({'msg': 'Attività eliminata'})}", status_code=303)


@app.get("/workout/{workout_id}/edit", response_class=HTMLResponse,
         dependencies=[Depends(require_auth)])
def workout_edit_form(request: Request, workout_id: int):
    with Session(engine) as session:
        w = session.get(Workout, workout_id)
        if not w:
            raise HTTPException(404, "Attività non trovata")
        sports = sorted({s for s in session.exec(select(Workout.sport).distinct()) if s})
    return templates.TemplateResponse(request, "workout_edit.html", {"w": w, "sports": sports})


@app.post("/workout/{workout_id}/edit", dependencies=[Depends(require_auth)])
def workout_edit(workout_id: int, name: str = Form(""), sport: str = Form(""),
                 distance_km: str = Form(""), ascent_m: str = Form(""),
                 moving_min: str = Form(""), avg_hr: str = Form(""),
                 avg_power: str = Form(""), calories: str = Form("")):
    def num(s):
        try:
            return float(s) if s.strip() != "" else None
        except ValueError:
            return None

    with Session(engine) as session:
        w = session.get(Workout, workout_id)
        if not w:
            raise HTTPException(404, "Attività non trovata")
        w.name = name.strip() or w.name
        w.sport = sport.strip() or w.sport
        if (km := num(distance_km)) is not None:
            w.distance_m = km * 1000
        if (asc := num(ascent_m)) is not None:
            w.ascent_m = asc
        if (mins := num(moving_min)) is not None:
            w.moving_s = int(mins * 60)
            if w.distance_m and w.moving_s:
                w.avg_speed_ms = w.distance_m / w.moving_s
        w.avg_hr = num(avg_hr)
        w.avg_power = num(avg_power)
        w.calories = num(calories)
        w.updated_at = datetime.utcnow()
        session.add(w)
        session.commit()
    return RedirectResponse(f"/workout/{workout_id}?{urlencode({'msg': 'Modifiche salvate'})}",
                            status_code=303)


@app.get("/workout/{workout_id}", response_class=HTMLResponse,
         dependencies=[Depends(require_auth)])
def workout_detail(request: Request, workout_id: int):
    with Session(engine) as session:
        w = session.get(Workout, workout_id)
        if not w:
            raise HTTPException(404, "Attività non trovata")
        analysis = session.get(AiAnalysis, workout_id)

    streams_json = "null"
    streams = fitmod.load_streams(workout_id)
    if streams:
        streams_json = json.dumps(fitmod.downsample(streams))

    return templates.TemplateResponse(request, "workout.html", {
        "w": w,
        "streams_json": streams_json,
        "analysis_html": md.markdown(analysis.content, extensions=["tables"]) if analysis else None,
        "analysis_date": analysis.created_at if analysis else None,
        "message": request.query_params.get("msg"),
        "error": request.query_params.get("error"),
    })


# ---------------------------------------------------------------- actions

@app.post("/sync", dependencies=[Depends(require_auth)])
async def sync(full: str = Form(default="")):
    try:
        n = await wahoo.sync_workouts(full=bool(full))
    except wahoo.NotAuthenticatedError:
        return RedirectResponse("/login", status_code=303)
    except wahoo.WahooError as e:
        logger.error("Sync failed: %s", e)
        return RedirectResponse(f"/?{urlencode({'error': str(e)})}", status_code=303)
    msg = f"Sync completato: {n} workout ingeriti"
    if google_health.is_authenticated():
        try:
            enriched = await google_health.enrich_workouts()
            if enriched:
                msg += f", {enriched} arricchiti da Google Health"
        except google_health.GoogleHealthError as e:
            logger.warning("Google Health enrichment skipped: %s", e)
            msg += " (arricchimento Google saltato: ricollega da /login/google)"
    return RedirectResponse(f"/?{urlencode({'msg': msg})}", status_code=303)


@app.post("/upload/fit", dependencies=[Depends(require_auth)])
async def upload_fit(file: UploadFile = File(...)):
    """Ingest a manually exported FIT (e.g. a full swim from swim.com). Matched
    by start time to an existing activity (which it upgrades to has_fit and full
    data), or added as a new one. The FIT is the authoritative source."""
    data = await file.read()
    if not data:
        return RedirectResponse(f"/?{urlencode({'error': 'File vuoto'})}", status_code=303)
    tmp = os.path.join(settings.fit_dir, f"_upload_{secrets.token_hex(8)}.fit")
    with open(tmp, "wb") as fh:
        fh.write(data)
    try:
        session_data, _ = fitmod.parse_fit(tmp)
    except fitmod.FitParseError:
        os.remove(tmp)
        return RedirectResponse(f"/?{urlencode({'error': 'FIT non valido o illeggibile'})}",
                                status_code=303)

    start = session_data.get("start_time")
    if isinstance(start, datetime):
        start = start.astimezone(timezone.utc).replace(tzinfo=None) if start.tzinfo else start
    else:
        os.remove(tmp)
        return RedirectResponse(f"/?{urlencode({'error': 'FIT senza orario di inizio'})}",
                                status_code=303)

    lo, hi = start - timedelta(minutes=25), start + timedelta(minutes=25)
    with Session(engine) as session:
        near = list(session.exec(select(Workout).where(
            Workout.start_date >= lo, Workout.start_date <= hi)))
        # prefer a data-less stub over an existing FIT-backed activity
        near.sort(key=lambda w: (w.has_fit, abs((w.start_date - start).total_seconds())))
        match = near[0] if near else None
        wid = match.id if match else int(start.timestamp())
        if match is None:
            session.add(Workout(id=wid, name=os.path.splitext(file.filename or "")[0] or "Attività",
                                sport="", start_date=start))
            session.commit()

    fit_path = os.path.join(settings.fit_dir, f"{wid}.fit")
    os.replace(tmp, fit_path)
    try:
        fitmod.parse_and_store(wid, fit_path)  # sets has_fit + FIT-authoritative fields
    except fitmod.FitParseError as e:
        return RedirectResponse(f"/?{urlencode({'error': f'FIT non elaborabile: {e}'})}",
                                status_code=303)
    logger.info("Ingested uploaded FIT -> workout %s (%s)", wid,
                "match" if match else "new")
    return RedirectResponse(
        f"/workout/{wid}?{urlencode({'msg': 'FIT caricato: dati completi importati'})}",
        status_code=303)


@app.post("/workout/{workout_id}/analyze", dependencies=[Depends(require_auth)])
async def analyze(workout_id: int, regenerate: str = Form(default="")):
    with Session(engine) as session:
        w = session.get(Workout, workout_id)
        if not w:
            raise HTTPException(404)
        cached = session.get(AiAnalysis, workout_id)

    # Cache hit: don't spend tokens unless the user asked to regenerate
    if cached and not regenerate:
        return RedirectResponse(f"/workout/{workout_id}", status_code=303)

    row = w.model_dump(exclude={"raw_summary", "fit_path", "updated_at"})
    stats = {}
    streams = fitmod.load_streams(workout_id)
    if streams:
        stats = fitmod.ai_stats(streams)

    try:
        content = await anthropic_client.analyze_workout(row, stats)
    except anthropic_client.AnthropicError as e:
        return RedirectResponse(
            f"/workout/{workout_id}?{urlencode({'error': str(e)})}", status_code=303)

    with Session(engine) as session:
        existing = session.get(AiAnalysis, workout_id)
        if existing:
            existing.content = content
            existing.model = anthropic_client.effective_model()
            existing.created_at = datetime.utcnow()
            session.add(existing)
        else:
            session.add(AiAnalysis(workout_id=workout_id, content=content,
                                   model=anthropic_client.effective_model()))
        session.commit()
    return RedirectResponse(f"/workout/{workout_id}", status_code=303)


def _summary_key(period: str) -> str:
    return f"{period}:{period_start(period).strftime('%Y-%m-%d')}"


@app.get("/summary/{period}", response_class=HTMLResponse,
         dependencies=[Depends(require_auth)])
def summary_page(request: Request, period: str):
    if period not in ("week", "month"):
        raise HTTPException(404)
    with Session(engine) as session:
        cached = session.get(PeriodSummary, _summary_key(period))
    return templates.TemplateResponse(request, "summary.html", {
        "period": period,
        "summary_html": md.markdown(cached.content, extensions=["tables"]) if cached else None,
        "summary_date": cached.created_at if cached else None,
        "error": request.query_params.get("error"),
    })


@app.post("/summary/{period}/generate", dependencies=[Depends(require_auth)])
async def summary_generate(period: str, regenerate: str = Form(default="")):
    if period not in ("week", "month"):
        raise HTTPException(404)
    key = _summary_key(period)
    with Session(engine) as session:
        cached = session.get(PeriodSummary, key)
        if cached and not regenerate:
            return RedirectResponse(f"/summary/{period}", status_code=303)
        workouts = query_workouts(session, period, None)

    if not workouts:
        return RedirectResponse(
            f"/summary/{period}?{urlencode({'error': 'Nessuna attività nel periodo'})}",
            status_code=303)

    rows = [w.model_dump(exclude={"raw_summary", "fit_path", "updated_at"}) for w in workouts]
    label = "ultima settimana" if period == "week" else "ultimo mese"
    try:
        content = await anthropic_client.summarize_period(label, rows)
    except anthropic_client.AnthropicError as e:
        return RedirectResponse(
            f"/summary/{period}?{urlencode({'error': str(e)})}", status_code=303)

    with Session(engine) as session:
        existing = session.get(PeriodSummary, key)
        if existing:
            existing.content = content
            existing.model = anthropic_client.effective_model()
            existing.created_at = datetime.utcnow()
            session.add(existing)
        else:
            session.add(PeriodSummary(key=key, content=content, model=anthropic_client.effective_model()))
        session.commit()
    return RedirectResponse(f"/summary/{period}", status_code=303)


# ---------------------------------------------------------------- health

@app.get("/healthz")
def healthz():
    return {"status": "ok"}
