"""FastAPI application: routing, auth guard, webhook ingestion, dashboard, AI."""
import json
import logging
import secrets
import calendar as pycal
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
from sqlalchemy import func
from sqlmodel import Session, select
from starlette.middleware.sessions import SessionMiddleware

from . import (anthropic_client, fit as fitmod, form as formmod, google_health,
               gpx as gpxmod, nutrition, profile as profilemod, wahoo)
from .config import settings, setup_logging
from .db import (AiAnalysis, ChatMessage, Conversation, IgnoredImport, PeriodSummary,
                 PlanSession, RouteAssessment, TrainingPlan, Workout, WorkoutStream,
                 engine, get_setting, init_db, set_setting)

setup_logging()
logger = logging.getLogger(__name__)

app = FastAPI(title=settings.app_name, docs_url=None, redoc_url=None)

# Signed session cookie. HttpOnly is always set by the middleware;
# Secure only when the public URL is HTTPS (so local HTTP testing still works).
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.app_secret_key or "dev-only-insecure",
    session_cookie="ofit_session",
    same_site="lax",
    https_only=settings.app_base_url.startswith("https://"),
    max_age=60 * 60 * 24 * 30,
)

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=BASE_DIR / "templates")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

@app.on_event("startup")
def on_startup() -> None:
    missing = settings.validate()
    if missing:
        logger.warning("Missing required env vars: %s — the app will not work correctly",
                       ", ".join(missing))
    init_db()
    logger.info("%s started (provider=%s, model=%s, db=%s, fits=%s)",
                settings.app_name, settings.ai_provider, settings.ai_model,
                settings.db_path, settings.fit_dir)


# ---------------------------------------------------------------- helpers

def require_auth(request: Request) -> None:
    # App session only — Wahoo/Google are data sources, not the login gate
    if not request.session.get("authed"):
        raise HTTPException(status_code=307, headers={"Location": "/login"})


def app_password() -> str:
    """Login password: DB override (set from Settings) else env APP_PASSWORD."""
    return get_setting("app_password") or settings.app_password


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


IT_DAYS = ["Lun", "Mar", "Mer", "Gio", "Ven", "Sab", "Dom"]


def fmt_day(d) -> str:
    """"Gio 16/07" — strftime("%a") would follow the server locale (English in
    the container), and the rest of the UI is Italian."""
    return f"{IT_DAYS[d.weekday()]} {d.strftime('%d/%m')}" if d else ""


templates.env.filters["duration"] = fmt_duration
templates.env.filters["it_day"] = fmt_day
def sport_icon(sport: str) -> str:
    s = (sport or "").lower()
    if any(k in s for k in ("swim", "nuot")):
        return "🏊"
    if any(k in s for k in ("bik", "cycl", "cicl", "bici")):
        return "🚴"
    if any(k in s for k in ("run", "cors")):
        return "🏃"
    if any(k in s for k in ("walk", "hik", "cammin", "escurs", "trek")):
        return "🥾"
    if any(k in s for k in ("forza", "strength", "corpo", "pesi", "hiit", "gym", "palestra", "wod")):
        return "🏋️"
    if any(k in s for k in ("yoga", "stretch", "mobilit", "pilates")):
        return "🧘"
    if any(k in s for k in ("riposo", "rest", "recupero")):
        return "😴"
    return "🔵"


templates.env.globals["fmt_speed"] = fmt_speed
templates.env.globals["app_name"] = settings.app_name
templates.env.globals["sport_icon"] = sport_icon
def asset(name: str) -> str:
    """/static URL stamped with the file's mtime.

    Cloudflare/edge caches /static aggressively, so the URL has to change when
    the file does. Per-file (not one global version) or touching any asset would
    have to invalidate all the others to take effect.
    """
    try:
        v = int((BASE_DIR / "static" / name).stat().st_mtime)
    except OSError:
        v = 1
    return f"/static/{name}?v={v}"


templates.env.globals["asset"] = asset


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
    if request.session.get("authed"):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {
        "error": request.query_params.get("error"),
        "has_password": bool(app_password()),
    })


@app.post("/login")
def login_submit(request: Request, password: str = Form("")):
    pw = app_password()
    if pw and secrets.compare_digest(password, pw):
        request.session["authed"] = True
        return RedirectResponse("/", status_code=303)
    logger.warning("Failed app-password login")
    return RedirectResponse("/login?error=bad_password", status_code=303)


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


def _window_key(prefix: str, window: dict) -> str:
    return f"{prefix}:{window['start'].date().isoformat()}:{window['end'].date().isoformat()}"


def _health_key(window: dict) -> str:
    return _window_key("health", window)


def _window_qs(window: dict) -> str:
    """Query string that reproduces the current window (win/end/from/to)."""
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
    nutri = await nutrition.fetch_nutrition(window["start"].date(), window["end"].date())
    with Session(engine) as session:
        insight = session.get(PeriodSummary, _health_key(window))
    return templates.TemplateResponse(request, "health.html", {
        "connected": True, "data": data, "window": window, "windows": WINDOWS,
        "nutrition": nutri,
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
    redirect = f"/health?{_window_qs(window)}"
    with Session(engine) as session:
        cached = session.get(PeriodSummary, key)
    if cached and not regenerate:
        return RedirectResponse(redirect, status_code=303)
    with Session(engine) as session:
        workouts = [w.model_dump(exclude={"raw_summary", "fit_path", "updated_at"})
                    for w in query_range(session, window["start"], window["end"], None)]
    nutri = await nutrition.fetch_nutrition(window["start"].date(), window["end"].date())
    try:
        data = await google_health.fetch_health_overview(window["start"].date(),
                                                         window["end"].date())
        content = await anthropic_client.summarize_health(data, workouts, nutri)
    except google_health.GoogleHealthError as e:
        return RedirectResponse(f"/health?{_window_qs(window)}&{urlencode({'error': str(e)})}",
                                status_code=303)
    except anthropic_client.AnthropicError as e:
        return RedirectResponse(f"/health?{_window_qs(window)}&{urlencode({'error': str(e)})}",
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
    """Grounded assistant, persisted: answers using the window's health data,
    activities and nutrition. The thread is saved (Conversation) to review later.
    Client sends {message, conversation_id?, win/end/from/to}."""
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "richiesta non valida"}, status_code=400)
    msg = str(body.get("message", "")).strip()[:2000]
    if not msg:
        return JSONResponse({"error": "nessuna domanda"}, status_code=400)

    # load or create the conversation, gather prior turns as context
    conv_id = body.get("conversation_id")
    with Session(engine) as session:
        conv = session.get(Conversation, conv_id) if conv_id else None
        if conv is None:
            conv = Conversation(title=msg[:60])
            session.add(conv)
            session.commit()
            session.refresh(conv)
        conv_id = conv.id
        prior = list(session.exec(select(ChatMessage)
                                  .where(ChatMessage.conversation_id == conv_id)
                                  .order_by(ChatMessage.created_at)))
    history = ([{"role": m.role, "content": m.content} for m in prior]
               + [{"role": "user", "content": msg}])[-12:]

    window = resolve_window(body.get("win", "30"), body.get("end", ""),
                            body.get("from", ""), body.get("to", ""))
    try:  # health data is best-effort: the assistant still works without it
        data = await google_health.fetch_health_overview(window["start"].date(),
                                                         window["end"].date())
    except google_health.GoogleHealthError:
        data = {"metrics": {}, "body": {}, "sleep": [], "score": None}
    with Session(engine) as session:
        workouts = [w.model_dump(exclude={"raw_summary", "fit_path", "updated_at"})
                    for w in query_range(session, window["start"], window["end"], None)]
    nutri = await nutrition.fetch_nutrition(window["start"].date(), window["end"].date())
    try:
        reply = await anthropic_client.chat_health(data, workouts, history, nutri)
    except anthropic_client.AnthropicError as e:
        return JSONResponse({"error": str(e)}, status_code=502)

    with Session(engine) as session:
        session.add(ChatMessage(conversation_id=conv_id, role="user", content=msg))
        session.add(ChatMessage(conversation_id=conv_id, role="assistant", content=reply))
        c = session.get(Conversation, conv_id)
        c.updated_at = datetime.utcnow()
        session.add(c)
        session.commit()
    return JSONResponse({"reply": reply, "conversation_id": conv_id})


@app.get("/conversations", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def conversations_list(request: Request):
    with Session(engine) as session:
        convs = list(session.exec(select(Conversation)
                                  .order_by(Conversation.updated_at.desc())))
        counts = {c.id: len(list(session.exec(select(ChatMessage)
                  .where(ChatMessage.conversation_id == c.id)))) for c in convs}
    return templates.TemplateResponse(request, "conversations.html",
                                      {"convs": convs, "counts": counts})


@app.get("/conversations/{cid}", response_class=HTMLResponse,
         dependencies=[Depends(require_auth)])
def conversation_view(request: Request, cid: int):
    with Session(engine) as session:
        conv = session.get(Conversation, cid)
        if not conv:
            raise HTTPException(404)
        msgs = list(session.exec(select(ChatMessage)
                    .where(ChatMessage.conversation_id == cid)
                    .order_by(ChatMessage.created_at)))
    rendered = [{"role": m.role, "created_at": m.created_at,
                 "html": md.markdown(m.content, extensions=["tables"]) if m.role == "assistant"
                 else None, "content": m.content} for m in msgs]
    return templates.TemplateResponse(request, "conversation.html",
                                      {"conv": conv, "messages": rendered})


@app.post("/conversations/{cid}/delete", dependencies=[Depends(require_auth)])
def conversation_delete(cid: int):
    with Session(engine) as session:
        for m in session.exec(select(ChatMessage).where(ChatMessage.conversation_id == cid)):
            session.delete(m)
        conv = session.get(Conversation, cid)
        if conv:
            session.delete(conv)
        session.commit()
    return RedirectResponse("/conversations", status_code=303)


# ---------------------------------------------------------------- routes (GPX)

# sport chosen at upload -> (label shown, sport family, min km to exclude noise)
SPORT_CHOICES = {"bici": ("Bici", "bike", 15.0),
                 "escursione": ("Escursione", "walk", 3.0),
                 "corsa": ("Corsa", "run", 3.0)}
SPORT_FAMILY = {label: fam for label, fam, _ in SPORT_CHOICES.values()}


def _activity_history(family: str) -> dict:
    """Athlete's envelope (typical + max) in a sport family, for feasibility.
    Short activities are excluded so commutes/strolls don't drag the typical."""
    min_m = next((mn for _, fam, mn in SPORT_CHOICES.values() if fam == family), 5.0) * 1000
    with Session(engine) as session:
        acts = [w for w in session.exec(select(Workout))
                if google_health._sport_family(w.sport or "") == family
                and (w.distance_m or 0) >= min_m]
    if not acts:
        return {}

    def med(xs):
        xs = sorted(xs)
        return xs[len(xs) // 2] if xs else None

    dist = [w.distance_m / 1000 for w in acts]
    asc = [w.ascent_m or 0 for w in acts]
    dur = [w.moving_s / 60 for w in acts if w.moving_s]
    apk = [(w.ascent_m or 0) / (w.distance_m / 1000) for w in acts]
    powers = [w.avg_power for w in acts if w.avg_power]
    hrs = [w.avg_hr for w in acts if w.avg_hr]
    out = {
        "attivita_analizzate": len(acts),
        "distanza_tipica_km": round(med(dist), 1), "distanza_max_km": round(max(dist), 1),
        "dislivello_tipico_m": round(med(asc)), "dislivello_max_m": round(max(asc)),
        "dislivello_per_km_tipico": round(med(apk), 1) if apk else None,
        "durata_tipica_min": round(med(dur)) if dur else None,
        "fc_media_tipica": round(med(hrs)) if hrs else None,
    }
    if powers:
        out["potenza_media_tipica_w"] = round(med(powers))
    return out


@app.get("/routes", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def routes_list(request: Request):
    with Session(engine) as session:
        routes = list(session.exec(select(RouteAssessment)
                                   .order_by(RouteAssessment.created_at.desc())))
    return templates.TemplateResponse(request, "routes.html", {
        "routes": routes, "error": request.query_params.get("error"),
    })


async def _current_form():
    """Current readiness index (best-effort; needs Google Health)."""
    if google_health.is_authenticated():
        try:
            overview = await google_health.fetch_health_overview()
            return overview.get("score")
        except google_health.GoogleHealthError:
            return None
    return None


@app.post("/routes/assess", dependencies=[Depends(require_auth)])
async def route_assess(file: UploadFile = File(...), name: str = Form(""),
                       sport: str = Form("bici")):
    data = await file.read()
    if not data:
        return RedirectResponse(f"/routes?{urlencode({'error': 'File vuoto'})}", status_code=303)
    try:
        route = gpxmod.parse_gpx(data)
    except gpxmod.GpxError as e:
        return RedirectResponse(f"/routes?{urlencode({'error': str(e)})}", status_code=303)

    label, family, _ = SPORT_CHOICES.get(sport, SPORT_CHOICES["bici"])
    history = _activity_history(family)
    form = await _current_form()
    try:
        verdict = await anthropic_client.assess_route(route, history, form, label)
    except anthropic_client.AnthropicError as e:
        return RedirectResponse(f"/routes?{urlencode({'error': str(e)})}", status_code=303)

    with Session(engine) as session:
        ra = RouteAssessment(
            name=name.strip() or os.path.splitext(file.filename or "")[0] or "Percorso",
            sport=label,
            distance_km=route["distance_km"], ascent_m=route.get("ascent_m") or 0,
            max_gradient=route.get("max_gradient_pct"), content=verdict,
            profile_json=json.dumps(route.get("profile") or []),
            route_json=json.dumps(route))
        session.add(ra)
        session.commit()
        session.refresh(ra)
        rid = ra.id
    return RedirectResponse(f"/routes/{rid}", status_code=303)


@app.post("/routes/{rid}/regenerate", dependencies=[Depends(require_auth)])
async def route_regenerate(rid: int):
    """Re-assess the same route against the CURRENT form/history (days later)."""
    with Session(engine) as session:
        ra = session.get(RouteAssessment, rid)
        if not ra:
            raise HTTPException(404)
        route = json.loads(ra.route_json or "{}")
    if not route:
        return RedirectResponse(f"/routes/{rid}?{urlencode({'error': 'Dati percorso non disponibili (ricarica il GPX)'})}",
                                status_code=303)
    family = SPORT_FAMILY.get(ra.sport, "bike")
    try:
        verdict = await anthropic_client.assess_route(
            route, _activity_history(family), await _current_form(), ra.sport)
    except anthropic_client.AnthropicError as e:
        return RedirectResponse(f"/routes/{rid}?{urlencode({'error': str(e)})}", status_code=303)
    with Session(engine) as session:
        ra = session.get(RouteAssessment, rid)
        ra.content = verdict
        ra.created_at = datetime.utcnow()  # "valutato il" = ultima valutazione
        session.add(ra)
        session.commit()
    return RedirectResponse(f"/routes/{rid}", status_code=303)


@app.get("/routes/{rid}", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def route_view(request: Request, rid: int):
    with Session(engine) as session:
        ra = session.get(RouteAssessment, rid)
        if not ra:
            raise HTTPException(404)
    return templates.TemplateResponse(request, "route.html", {
        "route": ra,
        "verdict_html": md.markdown(ra.content, extensions=["tables"]),
        "profile_json": ra.profile_json,
        "error": request.query_params.get("error"),
    })


@app.post("/routes/{rid}/delete", dependencies=[Depends(require_auth)])
def route_delete(rid: int):
    with Session(engine) as session:
        ra = session.get(RouteAssessment, rid)
        if ra:
            session.delete(ra)
            session.commit()
    return RedirectResponse("/routes", status_code=303)


# ---------------------------------------------------------------- form timeline

FORM_MONTHS = {"3": 3, "6": 6, "12": 12, "all": None}


def _hr_anchors() -> tuple[float, float, str]:
    """(rest_hr, max_hr, sex) for the load model, from the profile + measured max."""
    with Session(engine) as session:
        measured_max = session.exec(select(func.max(Workout.max_hr))).one()
    return profilemod.hr_anchors(measured_max=measured_max)


def _fitness_series() -> tuple[list, dict]:
    """Full CTL/ATL/TSB series over all history + current-state summary."""
    with Session(engine) as session:
        ws = list(session.exec(select(Workout).order_by(Workout.start_date)))
    measured_max = max([w.max_hr for w in ws if w.max_hr] + [0]) or None
    rest_hr, max_hr, sex = profilemod.hr_anchors(measured_max=measured_max)
    items = [(w.start_date.date(), w.avg_hr, (w.moving_s or 0) / 60, w.sport, w.rpe)
             for w in ws]
    series = formmod.fitness_series(items, rest_hr=rest_hr, max_hr=max_hr, sex=sex)
    return series, formmod.summarize(series)


@app.get("/form", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def form_page(request: Request, months: str = "6"):
    if months not in FORM_MONTHS:
        months = "6"
    series, summary = _fitness_series()
    shown = series
    if FORM_MONTHS[months] and series:
        cutoff = (date.today() - timedelta(days=FORM_MONTHS[months] * 30)).isoformat()
        shown = [p for p in series if p["date"] >= cutoff]
    with Session(engine) as session:
        insight = session.get(PeriodSummary, f"form:{date.today().isoformat()}")
    return templates.TemplateResponse(request, "form.html", {
        "months": months, "windows": FORM_MONTHS, "summary": summary,
        "series_json": json.dumps(shown),
        "insight_html": md.markdown(insight.content, extensions=["tables"]) if insight else None,
        "insight_date": insight.created_at if insight else None,
        "error": request.query_params.get("error"),
    })


@app.post("/form/insight", dependencies=[Depends(require_auth)])
async def form_insight(regenerate: str = Form(default="")):
    key = f"form:{date.today().isoformat()}"
    with Session(engine) as session:
        cached = session.get(PeriodSummary, key)
    if cached and not regenerate:
        return RedirectResponse("/form", status_code=303)
    series, summary = _fitness_series()
    if not summary:
        return RedirectResponse(f"/form?{urlencode({'error': 'Non ci sono abbastanza attività'})}",
                                status_code=303)
    # weekly-sampled last ~12 weeks for the AI (compact)
    recent = series[-84:][::7]
    try:
        content = await anthropic_client.summarize_form(summary, recent)
    except anthropic_client.AnthropicError as e:
        return RedirectResponse(f"/form?{urlencode({'error': str(e)})}", status_code=303)
    with Session(engine) as session:
        existing = session.get(PeriodSummary, key)
        if existing:
            existing.content = content
            existing.model = anthropic_client.effective_model()
            existing.created_at = datetime.utcnow()
            session.add(existing)
        else:
            session.add(PeriodSummary(key=key, content=content,
                                      model=anthropic_client.effective_model()))
        session.commit()
    return RedirectResponse("/form", status_code=303)


# ------------------------------------------------ calendar & manual workouts

@app.get("/calendar", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def calendar_page(request: Request, month: str = ""):
    today = date.today()
    try:
        y, m = (int(x) for x in month.split("-"))
        first = date(y, m, 1)
    except (ValueError, TypeError):
        first = date(today.year, today.month, 1)
    ndays = pycal.monthrange(first.year, first.month)[1]
    last = date(first.year, first.month, ndays)
    with Session(engine) as session:
        ws = query_range(session, datetime.combine(first, time.min),
                         datetime.combine(last, time.max), None)
        planned = session.exec(
            select(PlanSession).where(
                PlanSession.done == False,  # noqa: E712
                PlanSession.date >= datetime.combine(first, time.min),
                PlanSession.date <= datetime.combine(last, time.max))).all()
    byday: dict = defaultdict(list)
    for w in ws:
        byday[w.start_date.day].append(w)
    planned_byday: dict = defaultdict(list)
    for ps in planned:
        planned_byday[ps.date.day].append(ps)
    cells = [None] * first.weekday() + list(range(1, ndays + 1))
    while len(cells) % 7:
        cells.append(None)
    weeks = [cells[i:i + 7] for i in range(0, len(cells), 7)]
    IT_MONTHS = ["", "Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno",
                 "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"]
    return templates.TemplateResponse(request, "calendar.html", {
        "weeks": weeks, "days": {d: byday.get(d, []) for d in range(1, ndays + 1)},
        "planned": {d: planned_byday.get(d, []) for d in range(1, ndays + 1)},
        "month_label": f"{IT_MONTHS[first.month]} {first.year}",
        "prev": (first - timedelta(days=1)).strftime("%Y-%m"),
        "next": (last + timedelta(days=1)).strftime("%Y-%m"),
        "is_current": first.year == today.year and first.month == today.month,
        "today_day": today.day if (first.year, first.month) == (today.year, today.month) else 0,
    })


@app.get("/workout/new", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def workout_new(request: Request):
    with Session(engine) as session:
        sports = sorted({s for s in session.exec(select(Workout.sport).distinct()) if s})
    return templates.TemplateResponse(request, "workout_new.html", {
        "sports": sports, "today": date.today().isoformat()})


@app.post("/workout/parse", dependencies=[Depends(require_auth)])
async def workout_parse(request: Request):
    """AI-structure a free-text workout description into editable fields."""
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "richiesta non valida"}, status_code=400)
    desc = str(body.get("description", "")).strip()[:2000]
    if not desc:
        return JSONResponse({"error": "descrizione vuota"}, status_code=400)
    try:
        fields = await anthropic_client.structure_workout(desc)
    except anthropic_client.AnthropicError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    return JSONResponse(fields or {})


def _create_manual_workout(name, sport, start, duration_min, notes="",
                           distance_km=None, calories=None, avg_hr=None,
                           rpe=None) -> int:
    """Create a manual Workout and return its id."""
    wid = int(datetime.utcnow().timestamp() * 1000)
    mins = duration_min or 0
    with Session(engine) as session:
        w = Workout(id=wid, name=(name or "").strip() or "Allenamento",
                    sport=(sport or "").strip() or "Allenamento",
                    start_date=start, manual=True, notes=(notes or "").strip(),
                    duration_s=int(mins * 60), moving_s=int(mins * 60),
                    distance_m=(distance_km * 1000) if distance_km else 0.0,
                    avg_hr=avg_hr, calories=calories, rpe=rpe)
        if w.distance_m and w.moving_s:
            w.avg_speed_ms = w.distance_m / w.moving_s
        session.add(w)
        session.commit()
    return wid


@app.post("/workout/manual", dependencies=[Depends(require_auth)])
def workout_manual(name: str = Form(""), sport: str = Form(""), date_: str = Form("", alias="date"),
                   durata_min: str = Form(""), distanza_km: str = Form(""),
                   calorie: str = Form(""), fc_media: str = Form(""),
                   note: str = Form(""), rpe: str = Form("")):
    def num(s):
        try:
            return float(s) if str(s).strip() != "" else None
        except ValueError:
            return None

    start = datetime.utcnow()
    d = _parse_date(date_)
    if d:
        start = datetime.combine(d, datetime.utcnow().time())
    wid = _create_manual_workout(name, sport, start, num(durata_min) or 0, note,
                                 num(distanza_km), num(calorie), num(fc_media),
                                 rpe=num(rpe))
    return RedirectResponse(f"/workout/{wid}?{urlencode({'msg': 'Allenamento aggiunto'})}",
                            status_code=303)


# ---- Piani di allenamento ------------------------------------------------

@app.get("/plans", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def plans_page(request: Request):
    with Session(engine) as session:
        plans = session.exec(select(TrainingPlan).order_by(
            TrainingPlan.created_at.desc())).all()
        counts = {}
        for p in plans:
            sess = session.exec(select(PlanSession).where(
                PlanSession.plan_id == p.id)).all()
            counts[p.id] = (sum(1 for s in sess if s.done), len(sess))
    return templates.TemplateResponse(request, "plans.html", {
        "plans": plans, "counts": counts,
        "today": date.today().isoformat(),
        "message": request.query_params.get("msg"),
        "error": request.query_params.get("err")})


@app.post("/plans/generate", dependencies=[Depends(require_auth)])
async def plans_generate(goal: str = Form(""), n_days: str = Form("7"),
                         start: str = Form("")):
    goal = goal.strip()
    if not goal:
        return RedirectResponse(f"/plans?{urlencode({'err': 'Descrivi un obiettivo'})}",
                                status_code=303)
    try:
        n = max(1, min(60, int(n_days)))
    except ValueError:
        n = 7
    start_d = _parse_date(start) or date.today()
    try:
        data = await anthropic_client.generate_plan(goal, n, start_d.isoformat())
    except anthropic_client.AnthropicError as e:
        return RedirectResponse(f"/plans?{urlencode({'err': str(e)})}", status_code=303)
    sessions = data.get("sessions") or []
    if not sessions:
        return RedirectResponse(
            f"/plans?{urlencode({'err': 'AI non ha restituito un piano valido'})}",
            status_code=303)
    with Session(engine) as session:
        plan = TrainingPlan(title=(data.get("title") or goal)[:200], goal=goal)
        session.add(plan)
        session.commit()
        session.refresh(plan)
        for i, s in enumerate(sessions):
            sd = _parse_date(str(s.get("date", "")))
            session.add(PlanSession(
                plan_id=plan.id, order=i, day_label=str(s.get("day", ""))[:40],
                date=datetime.combine(sd, time(12, 0)) if sd else None,
                title=str(s.get("title", ""))[:200], sport=str(s.get("sport", ""))[:60],
                duration_min=int(s.get("durata_min") or 0),
                description=str(s.get("description", ""))))
        session.commit()
        pid = plan.id
    return RedirectResponse(f"/plans/{pid}", status_code=303)


@app.get("/plans/{plan_id}", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def plan_detail(request: Request, plan_id: int):
    with Session(engine) as session:
        plan = session.get(TrainingPlan, plan_id)
        if not plan:
            return RedirectResponse("/plans", status_code=303)
        sessions = session.exec(select(PlanSession).where(
            PlanSession.plan_id == plan_id).order_by(PlanSession.order)).all()
        # Persisted AI threads, so reopening the page keeps the conversation
        chats = {s.id: [{"role": m.role, "content": m.content}
                        for m in session.exec(
                            select(ChatMessage)
                            .where(ChatMessage.conversation_id == s.conversation_id)
                            .order_by(ChatMessage.created_at))]
                 for s in sessions if s.conversation_id}
    done = sum(1 for s in sessions if s.done)
    loads = {s.id: round(_session_load(s.sport, s.duration_min)) for s in sessions}
    return templates.TemplateResponse(request, "plan.html", {
        "plan": plan, "sessions": sessions, "done": done, "total": len(sessions),
        "loads": loads, "chats_json": json.dumps(chats),
        "now": datetime.utcnow(), "message": request.query_params.get("msg")})


@app.post("/plans/{plan_id}/delete", dependencies=[Depends(require_auth)])
def plan_delete(plan_id: int):
    with Session(engine) as session:
        for s in session.exec(select(PlanSession).where(
                PlanSession.plan_id == plan_id)).all():
            session.delete(s)
        plan = session.get(TrainingPlan, plan_id)
        if plan:
            session.delete(plan)
        session.commit()
    return RedirectResponse(f"/plans?{urlencode({'msg': 'Piano eliminato'})}",
                            status_code=303)


@app.post("/plans/{plan_id}/session/{sid}/done", dependencies=[Depends(require_auth)])
async def plan_session_done(plan_id: int, sid: int, done_notes: str = Form(""),
                            done_min: str = Form(""), done_date: str = Form("")):
    """Mark a session done, recording what was *actually* done (defaults to the
    planned session) as the workout notes, so the AI can judge the real load."""
    with Session(engine) as session:
        ps = session.get(PlanSession, sid)
        if not ps or ps.plan_id != plan_id or ps.done:
            return RedirectResponse(f"/plans/{plan_id}", status_code=303)
        title, sport, planned_min = ps.title, ps.sport, ps.duration_min
        notes = done_notes.strip() or ps.description
    try:
        mins = int(done_min) if done_min.strip() else planned_min
    except ValueError:
        mins = planned_min
    d = _parse_date(done_date)
    # Home sessions carry no heart rate: ask the AI to rate the effort from the
    # description so the load model sees more than a per-sport average. Best
    # effort — a failure here must not lose the fact that the session was done.
    rpe = None
    if notes:
        try:
            rpe = await anthropic_client.estimate_rpe(
                title, sport, mins, notes, profilemod.ai_context())
        except anthropic_client.AnthropicError as e:
            logger.warning("RPE estimate failed for plan session %s: %s", sid, e)
    with Session(engine) as session:
        ps = session.get(PlanSession, sid)
        if not ps or ps.done:
            return RedirectResponse(f"/plans/{plan_id}", status_code=303)
        start = (datetime.combine(d, time(12, 0)) if d
                 else (ps.date or datetime.utcnow()))
        wid = _create_manual_workout(title, sport, start, mins, notes, rpe=rpe)
        ps.done = True
        ps.workout_id = wid
        session.add(ps)
        session.commit()
    return RedirectResponse(
        f"/plans/{plan_id}?{urlencode({'msg': 'Sessione segnata come fatta'})}",
        status_code=303)


@app.post("/plans/{plan_id}/session/{sid}/undo", dependencies=[Depends(require_auth)])
def plan_session_undo(plan_id: int, sid: int):
    with Session(engine) as session:
        ps = session.get(PlanSession, sid)
        if ps and ps.plan_id == plan_id and ps.done:
            if ps.workout_id:
                w = session.get(Workout, ps.workout_id)
                if w and w.manual:
                    session.delete(w)
            ps.done = False
            ps.workout_id = None
            session.add(ps)
            session.commit()
    return RedirectResponse(f"/plans/{plan_id}", status_code=303)


def _session_load(sport: str, minutes: float, rpe: float | None = None) -> float:
    """Estimated TRIMP load of a planned/proposed session (no HR by definition)."""
    rest_hr, max_hr, sex = _hr_anchors()
    return formmod.activity_load(None, minutes, sport, rpe, rest_hr, max_hr, sex)


@app.post("/plans/{plan_id}/session/{sid}/chat", dependencies=[Depends(require_auth)])
async def plan_session_chat(plan_id: int, sid: int, request: Request):
    """Talk to the AI about adapting one planned session ("today it rains, give
    me an equivalent session at home"). Returns a reply plus, when the AI is
    proposing a concrete swap, a structured proposal the UI can apply.

    The thread is persisted (Conversation) and hangs off the PlanSession.
    """
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "richiesta non valida"}, status_code=400)
    msg = str(body.get("message", "")).strip()[:2000]
    if not msg:
        return JSONResponse({"error": "nessuna domanda"}, status_code=400)

    with Session(engine) as session:
        ps = session.get(PlanSession, sid)
        if not ps or ps.plan_id != plan_id:
            return JSONResponse({"error": "sessione non trovata"}, status_code=404)
        plan = session.get(TrainingPlan, plan_id)
        conv = session.get(Conversation, ps.conversation_id) if ps.conversation_id else None
        if conv is None:
            conv = Conversation(title=f"Piano · {ps.title}"[:60])
            session.add(conv)
            session.commit()
            session.refresh(conv)
            ps.conversation_id = conv.id
            session.add(ps)
            session.commit()
        conv_id = conv.id
        planned = {"title": ps.title, "sport": ps.sport, "durata_min": ps.duration_min,
                   "descrizione": ps.description,
                   "carico_trimp": round(_session_load(ps.sport, ps.duration_min))}
        goal = plan.goal if plan else ""
        prior = list(session.exec(select(ChatMessage)
                                  .where(ChatMessage.conversation_id == conv_id)
                                  .order_by(ChatMessage.created_at)))
    history = ([{"role": m.role, "content": m.content} for m in prior]
               + [{"role": "user", "content": msg}])[-12:]

    _, form_summary = _fitness_series()
    ctx = {"sessione_pianificata": planned, "obiettivo_del_piano": goal,
           "atleta": profilemod.ai_context(), "forma_attuale": form_summary}
    try:
        out = await anthropic_client.chat_plan_session(ctx, history)
    except anthropic_client.AnthropicError as e:
        return JSONResponse({"error": str(e)}, status_code=502)

    reply, proposal = out.get("risposta") or "", out.get("proposta")
    if proposal:
        # Quantify the swap: the AI aims for an equivalent load, so show what it
        # actually landed on rather than taking its word for it.
        try:
            mins = int(proposal.get("durata_min") or 0)
            proposal["durata_min"] = mins
            proposal["carico_trimp"] = round(_session_load(
                str(proposal.get("sport") or ""), mins,
                float(proposal["rpe"]) if proposal.get("rpe") else None))
        except (TypeError, ValueError):
            proposal = None
    # Store the turn as prose, not raw JSON: it has to stay readable on the
    # Conversations page while still telling the AI what it already proposed.
    stored = reply
    if proposal:
        stored += (f"\n\n[Proposta] {proposal.get('title')} · {proposal.get('sport')} · "
                   f"{proposal['durata_min']} min · RPE {proposal.get('rpe')} · "
                   f"carico ~{proposal['carico_trimp']}\n{proposal.get('description')}")
    with Session(engine) as session:
        session.add(ChatMessage(conversation_id=conv_id, role="user", content=msg))
        session.add(ChatMessage(conversation_id=conv_id, role="assistant", content=stored))
        c = session.get(Conversation, conv_id)
        c.updated_at = datetime.utcnow()
        session.add(c)
        session.commit()
    return JSONResponse({"reply": reply, "proposal": proposal,
                         "planned_load": planned["carico_trimp"],
                         "conversation_id": conv_id})


@app.post("/plans/{plan_id}/session/{sid}/edit", dependencies=[Depends(require_auth)])
def plan_session_edit(plan_id: int, sid: int, title: str = Form(""),
                      sport: str = Form(""), durata_min: str = Form("0"),
                      date_: str = Form("", alias="date"), description: str = Form("")):
    with Session(engine) as session:
        ps = session.get(PlanSession, sid)
        if ps and ps.plan_id == plan_id:
            ps.title = title.strip() or ps.title
            ps.sport = sport.strip()
            try:
                ps.duration_min = max(0, int(durata_min))
            except ValueError:
                pass
            d = _parse_date(date_)
            ps.date = datetime.combine(d, time(12, 0)) if d else None
            ps.description = description.strip()
            session.add(ps)
            session.commit()
    return RedirectResponse(
        f"/plans/{plan_id}?{urlencode({'msg': 'Sessione aggiornata'})}", status_code=303)


@app.get("/settings", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def settings_page(request: Request):
    provider = anthropic_client.effective_provider()
    openai_models = await anthropic_client.list_openai_models() if settings.openai_api_key else []
    p = profilemod.load()
    with Session(engine) as session:
        measured_max = session.exec(select(func.max(Workout.max_hr))).one()
    rest_hr, max_hr, _ = profilemod.hr_anchors(p, measured_max=measured_max)
    return templates.TemplateResponse(request, "settings.html", {
        "provider": provider,
        "model": anthropic_client.effective_model(),
        "openai_models": openai_models,
        "has_openai": bool(settings.openai_api_key),
        "has_anthropic": bool(settings.anthropic_api_key),
        "has_password": bool(app_password()),
        "profile": p,
        "age": profilemod.age(p),
        "bmi": profilemod.bmi(p),
        "eff_rest_hr": round(rest_hr),
        "eff_max_hr": round(max_hr),
        "measured_max_hr": round(measured_max) if measured_max else None,
        "message": request.query_params.get("msg"),
        "error": request.query_params.get("error"),
    })


@app.post("/settings/profile", dependencies=[Depends(require_auth)])
def settings_profile(height_cm: str = Form(""), weight_kg: str = Form(""),
                     birth_year: str = Form(""), sex: str = Form(""),
                     rest_hr: str = Form(""), max_hr: str = Form("")):
    profilemod.save({"height_cm": height_cm, "weight_kg": weight_kg,
                     "birth_year": birth_year, "sex": sex if sex in ("M", "F") else "",
                     "rest_hr": rest_hr, "max_hr": max_hr})
    return RedirectResponse(
        f"/settings?{urlencode({'msg': 'Dati personali salvati (rivedi la Forma: il carico è ricalcolato)'})}",
        status_code=303)


@app.post("/settings/profile/sync", dependencies=[Depends(require_auth)])
async def settings_profile_sync():
    """Pull weight and resting HR from Google Health into the profile."""
    try:
        overview = await google_health.fetch_health_overview(
            date.today() - timedelta(days=29), date.today())
    except google_health.GoogleHealthError as e:
        logger.warning("Profile sync from Google Health failed: %s", e)
        return RedirectResponse(
            f"/settings?{urlencode({'error': f'Google Health non disponibile: {e}'})}",
            status_code=303)
    vals, got = {}, []
    if (w := (overview.get("body") or {}).get("weight", {}).get("latest")):
        vals["weight_kg"] = round(float(w), 1)
        got.append(f"peso {vals['weight_kg']} kg")
    rh = (overview.get("metrics") or {}).get("resting_hr", {}).get("series") or []
    if rh:
        median = sorted(p["value"] for p in rh)[len(rh) // 2]
        vals["rest_hr"] = round(float(median))
        got.append(f"FC riposo {vals['rest_hr']} bpm")
    if not vals:
        return RedirectResponse(
            f"/settings?{urlencode({'error': 'Nessun dato di peso/FC a riposo da Google Health'})}",
            status_code=303)
    profilemod.save(vals)
    return RedirectResponse(
        f"/settings?{urlencode({'msg': 'Da Google Health: ' + ', '.join(got)})}",
        status_code=303)


@app.post("/settings", dependencies=[Depends(require_auth)])
async def settings_save(provider: str = Form("openai"), model: str = Form("")):
    provider = provider if provider in ("openai", "anthropic") else "openai"
    set_setting("ai_provider", provider)
    set_setting("ai_model", model.strip())
    logger.info("AI settings updated: provider=%s model=%s", provider, model.strip() or "(default)")
    return RedirectResponse(f"/settings?{urlencode({'msg': 'Impostazioni salvate'})}",
                            status_code=303)


@app.post("/settings/password", dependencies=[Depends(require_auth)])
def settings_password(new_password: str = Form(""), confirm: str = Form("")):
    new = new_password.strip()
    if len(new) < 4:
        return RedirectResponse(f"/settings?{urlencode({'error': 'Password troppo corta (min 4)'})}",
                                status_code=303)
    if new != confirm.strip():
        return RedirectResponse(f"/settings?{urlencode({'error': 'Le password non coincidono'})}",
                                status_code=303)
    set_setting("app_password", new)
    logger.info("App password changed")
    return RedirectResponse(f"/settings?{urlencode({'msg': 'Password aggiornata'})}",
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

def _upcoming_sessions(days: int = 7, limit: int = 4) -> list:
    """Planned, not-yet-done plan sessions from today onwards."""
    start = datetime.combine(date.today(), time.min)
    with Session(engine) as session:
        rows = session.exec(
            select(PlanSession, TrainingPlan)
            .join(TrainingPlan, TrainingPlan.id == PlanSession.plan_id)
            .where(PlanSession.done == False,  # noqa: E712
                   PlanSession.date >= start,
                   PlanSession.date <= start + timedelta(days=days))
            .order_by(PlanSession.date)).all()
    return [{"s": s, "plan": p} for s, p in rows][:limit]


# Google Health needs ~9s per call, far too slow to block the dashboard: the card
# is filled in client-side from here, and a short TTL keeps repeat visits instant
# without hammering the API. Single-user, single-container: memory is enough.
_HEALTH_CACHE: dict = {}
_HEALTH_TTL = timedelta(minutes=20)


@app.get("/api/health/summary", dependencies=[Depends(require_auth)])
async def api_health_summary():
    """Compact health snapshot for the dashboard card (cached, best-effort)."""
    now = datetime.utcnow()
    hit = _HEALTH_CACHE.get("data")
    if hit and now - hit["at"] < _HEALTH_TTL:
        return JSONResponse(hit["payload"])
    try:
        data = await google_health.fetch_health_overview(
            date.today() - timedelta(days=6), date.today())
    except google_health.GoogleHealthError as e:
        return JSONResponse({"error": str(e)}, status_code=502)

    def latest(key):
        m = (data.get("metrics") or {}).get(key)
        return None if not m else {"label": m["label"], "unit": m["unit"],
                                   "value": m["latest"], "dir": m.get("dir"),
                                   "delta": m.get("delta")}

    nights = data.get("sleep") or []
    payload = {
        "score": data.get("score"),
        "metrics": [m for m in (latest("resting_hr"), latest("hrv"), latest("spo2")) if m],
        "sleep_h": (round(sum(n["asleep_min"] for n in nights) / len(nights) / 60, 1)
                    if nights else None),
        "nights": len(nights),
    }
    _HEALTH_CACHE["data"] = {"at": now, "payload": payload}
    return JSONResponse(payload)


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

    with Session(engine) as session:
        analysis = session.get(PeriodSummary, _window_key("training", window))

    # "Current state" strip: independent of the selected window, and cheap enough
    # to render inline (the health card is fetched client-side instead — see
    # /api/health/summary — because Google Health takes seconds).
    _, form_summary = _fitness_series()
    upcoming = _upcoming_sessions()

    return templates.TemplateResponse(request, "dashboard.html", {
        "user": wahoo.get_user_name(),
        "form": form_summary, "upcoming": upcoming,
        "last_workout": max(workouts, key=lambda w: w.start_date) if workouts else None,
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
        "analysis_html": md.markdown(analysis.content, extensions=["tables"]) if analysis else None,
        "analysis_date": analysis.created_at if analysis else None,
        "message": request.query_params.get("msg"),
        "error": request.query_params.get("error"),
    })


@app.post("/analyze/period", dependencies=[Depends(require_auth)])
async def analyze_period(win: str = Form("30"), end: str = Form(""),
                         from_: str = Form("", alias="from"), to: str = Form(""),
                         regenerate: str = Form(default="")):
    """AI analysis of the training in the selected window (cached per window)."""
    window = resolve_window(win, end, from_, to)
    key = _window_key("training", window)
    redirect = f"/?{_window_qs(window)}"
    with Session(engine) as session:
        cached = session.get(PeriodSummary, key)
        if cached and not regenerate:
            return RedirectResponse(redirect, status_code=303)
        workouts = query_range(session, window["start"], window["end"], None)
        rows = [w.model_dump(exclude={"raw_summary", "fit_path", "updated_at"}) for w in workouts]
    if not rows:
        return RedirectResponse(f"/?{_window_qs(window)}&"
                                + urlencode({"error": "Nessuna attività nel periodo"}),
                                status_code=303)
    try:
        content = await anthropic_client.summarize_period(window["label"], rows)
    except anthropic_client.AnthropicError as e:
        return RedirectResponse(f"/?{_window_qs(window)}&{urlencode({'error': str(e)})}",
                                status_code=303)
    with Session(engine) as session:
        existing = session.get(PeriodSummary, key)
        if existing:
            existing.content = content
            existing.model = anthropic_client.effective_model()
            existing.created_at = datetime.utcnow()
            session.add(existing)
        else:
            session.add(PeriodSummary(key=key, content=content,
                                      model=anthropic_client.effective_model()))
        session.commit()
    return RedirectResponse(redirect, status_code=303)


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
    return templates.TemplateResponse(request, "workout_edit.html", {
        "w": w, "sports": sports, "default_rpe": formmod.sport_rpe(w.sport)})


@app.post("/workout/{workout_id}/edit", dependencies=[Depends(require_auth)])
def workout_edit(workout_id: int, name: str = Form(""), sport: str = Form(""),
                 distance_km: str = Form(""), ascent_m: str = Form(""),
                 moving_min: str = Form(""), avg_hr: str = Form(""),
                 avg_power: str = Form(""), calories: str = Form(""),
                 notes: str = Form(""), rpe: str = Form("")):
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
        w.notes = notes.strip()
        w.rpe = num(rpe)
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


def _fit_merge_target(session, start: datetime, fit_sport: str) -> Workout | None:
    """Pick the existing workout an uploaded FIT should merge into, or None.

    Two kinds of candidate:
      - a near-simultaneous activity (±25 min): a real duplicate, e.g. the same
        swim exported from swim.com and delivered by Wahoo.
      - a manual/plan workout logged the same calendar day: marking a plan
        session done stamps a placeholder noon time, so the real FIT (done in the
        morning or evening) falls outside the ±25 min window — yet it is exactly
        the data that session was waiting for. Guarded by sport so a swim FIT
        never absorbs that day's strength session.
    Merging keeps the same workout id, so the plan link and description survive.
    """
    lo, hi = start - timedelta(minutes=25), start + timedelta(minutes=25)
    day_lo = datetime.combine(start.date(), time.min)
    day_hi = datetime.combine(start.date(), time.max)
    near = list(session.exec(select(Workout).where(
        Workout.start_date >= lo, Workout.start_date <= hi)))
    near_ids = {w.id for w in near}

    # Same-day manual guard: the two are NOT close in time, so require a real
    # sport match. If the FIT sport is known (swim/bike/run/walk) the manual must
    # be the same family; if the FIT sport is unknown (generic "training"), only
    # merge into an equally family-less manual (a home/bodyweight session) so a
    # bodyweight FIT never lands on that day's ride.
    fe = google_health._sport_family(fit_sport)

    def sameday_ok(w: Workout) -> bool:
        return (google_health._matches_sport(w.sport, fit_sport) if fe
                else google_health._sport_family(w.sport) is None)

    sameday_manual = [w for w in session.exec(select(Workout).where(
        Workout.manual == True,  # noqa: E712
        Workout.start_date >= day_lo, Workout.start_date <= day_hi))
        if w.id not in near_ids and not w.has_fit and sameday_ok(w)]
    candidates = near + sameday_manual
    if not candidates:
        return None
    # prefer a data-less stub over a FIT-backed activity, then a matching sport
    # family, then the closest start time
    candidates.sort(key=lambda w: (
        w.has_fit,
        not google_health._matches_sport(w.sport, fit_sport),
        abs((w.start_date - start).total_seconds())))
    return candidates[0]


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

    fit_sport = session_data.get("sport") or ""
    with Session(engine) as session:
        match = _fit_merge_target(session, start, fit_sport)
        wid = match.id if match else int(start.timestamp())
        if match is None:
            session.add(Workout(id=wid, name=os.path.splitext(file.filename or "")[0] or "Attività",
                                sport="", start_date=start))
            session.commit()
        merged_manual = bool(match and match.manual)

    fit_path = os.path.join(settings.fit_dir, f"{wid}.fit")
    os.replace(tmp, fit_path)
    try:
        fitmod.parse_and_store(wid, fit_path)  # sets has_fit + FIT-authoritative fields
    except fitmod.FitParseError as e:
        return RedirectResponse(f"/?{urlencode({'error': f'FIT non elaborabile: {e}'})}",
                                status_code=303)
    logger.info("Ingested uploaded FIT -> workout %s (%s)", wid,
                "merge-manual" if merged_manual else "match" if match else "new")
    msg = ("FIT fuso con l'allenamento del piano: dati reali e FC uniti alla "
           "descrizione" if merged_manual else "FIT caricato: dati completi importati")
    return RedirectResponse(f"/workout/{wid}?{urlencode({'msg': msg})}", status_code=303)


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


# ---------------------------------------------------------------- health

@app.get("/healthz")
def healthz():
    return {"status": "ok"}
