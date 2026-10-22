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

from . import (ai_usage, anthropic_client, bodycomp, cycling as cyclingmod,
               exercises as exercisesmod, fit as fitmod, form as formmod,
               google_health, gpx as gpxmod, nutrition, profile as profilemod,
               program as programmod, program_runner, progress as progressmod,
               vo2max as vo2maxmod, wahoo)
from .config import settings, setup_logging
from .db import (AiAnalysis, BodyGirth, BodyMeasure, ChatMessage, ClimbEffort,
                 Conversation, CustomEffort, CustomSegment, IgnoredImport,
                 PeriodSummary, PlanSession, Program, RouteAssessment, TrainingPlan, Workout,
                 WorkoutStream, engine, get_setting, init_db, set_setting)

setup_logging()
logger = logging.getLogger(__name__)

app = FastAPI(title=settings.app_name, docs_url=None, redoc_url=None)

# Signed session cookie. HttpOnly is always set by the middleware;
# The cookie is not Secure-only by default, so login works over plain HTTP on the
# LAN as well as through the HTTPS tunnel (SESSION_HTTPS_ONLY=true to require HTTPS).
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.app_secret_key or "dev-only-insecure",
    session_cookie="ofit_session",
    same_site="lax",
    https_only=settings.session_https_only,
    max_age=60 * 60 * 24 * 30,
)

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=BASE_DIR / "templates")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

@app.on_event("startup")
async def on_startup() -> None:
    missing = settings.validate()
    if missing:
        logger.warning("Missing required env vars: %s — the app will not work correctly",
                       ", ".join(missing))
    init_db()
    logger.info("%s started (provider=%s, model=%s, db=%s, fits=%s)",
                settings.app_name, settings.ai_provider, settings.ai_model,
                settings.db_path, settings.fit_dir)
    _fire_and_forget(_nightly_recompute_loop())   # ricalcolo target notturno
    _fire_and_forget(_activity_import_loop())     # Pixel Watch (Google) ogni 2 ore


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
templates.env.filters["exercise_links"] = exercisesmod.linkify
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
    if any(k in s for k in ("forza", "strength", "corpo", "pesi", "hiit", "gym",
                            "palestra", "wod", "circuit", "calisthen", "functional",
                            "bodyweight", "bootcamp", "weightlift")):
        return "🏋️"
    if any(k in s for k in ("yoga", "stretch", "mobilit", "pilates")):
        return "🧘"
    if any(k in s for k in ("riposo", "rest", "recupero")):
        return "😴"
    return "🔵"


# One display label per sport family, so "Bici" (dal piano), "Cycling" (dal FIT)
# and "Ciclismo" (da Google) — and the swim/run/walk/strength equivalents — never
# appear as different activity types in lists, filters and the type breakdown.
def sport_label(sport: str) -> str:
    return google_health.canonical_sport(sport) or "Altro"


templates.env.globals["fmt_speed"] = fmt_speed
templates.env.globals["app_name"] = settings.app_name
templates.env.globals["sport_icon"] = sport_icon
templates.env.globals["sport_label"] = sport_label
templates.env.filters["sport_label"] = sport_label
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
    ws = list(session.exec(stmt.order_by(Workout.start_date.desc())))
    if sport:   # by canonical label: "Ciclismo" also picks up "Cycling"/"Bici"
        want = sport_label(sport)
        ws = [w for w in ws if sport_label(w.sport) == want]
    return ws


def _sport_options(session: Session) -> list[str]:
    """Distinct activity types, one per sport family (for filters/datalists)."""
    return sorted({sport_label(s) for s in session.exec(select(Workout.sport).distinct()) if s})


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
        type_count[sport_label(w.sport)] += 1
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


def _energy_series(data: dict | None, nutri: dict | None) -> list[dict]:
    """Per-day {date, burned, intake} for the energy-balance chart. Burned is the
    complete Google total; intake is tracked-meals only (may be None on a day)."""
    burned = ((data or {}).get("metrics", {}).get("calories_burned") or {}).get("series") or []
    days = ((nutri or {}).get("alimentazione_tracciata") or {}).get("per_giorno") or []
    intake_by = {d["data"]: d["kcal"] for d in days if d.get("kcal") is not None}
    return [{"date": p["date"], "burned": p["value"], "intake": intake_by.get(p["date"])}
            for p in burned]


HEALTH_STALE_DAYS = 2   # data not refreshed for longer than this -> warn


def _latest_health_date(data: dict) -> str | None:
    """Freshest date across metrics + sleep — to tell if Google data has stopped."""
    dates = [s[-1]["date"] for m in (data.get("metrics") or {}).values()
             if (s := m.get("series")) and s[-1].get("date")]
    dates += [n["date"] for n in (data.get("sleep") or []) if n.get("date")]
    return max(dates) if dates else None


def _health_stale_days(data: dict) -> int | None:
    """Days since the freshest Google data, or None if there is none / it's fresh.
    Only meaningful for the current window (a past window is old by design)."""
    last = _latest_health_date(data)
    if not last:
        return None
    try:
        days = (date.today() - date.fromisoformat(last)).days
    except ValueError:
        return None
    return days if days > HEALTH_STALE_DAYS else None


def _body_context() -> dict:
    """Latest smart-scale reading + recent history for the body-composition card.
    Independent of Google Health."""
    with Session(engine) as session:
        rows = session.exec(select(BodyMeasure)
                            .order_by(BodyMeasure.measured_at.desc()).limit(60)).all()
    if not rows:
        return {"body_latest": None, "body_series_json": "[]", "body_rows": []}
    newest_first = list(rows)
    rows = list(reversed(rows))   # oldest -> newest
    series = [{"date": r.measured_at.strftime("%Y-%m-%d"),
               "weight": r.weight_kg, "fat": r.body_fat, "muscle": r.muscle_kg}
              for r in rows]
    return {"body_latest": rows[-1], "body_series_json": json.dumps(series),
            "body_rows": newest_first[:20]}


def _girth_context() -> dict:
    """Tape-measure circumferences: recent history + a trend series for the chart."""
    with Session(engine) as session:
        rows = session.exec(select(BodyGirth)
                            .order_by(BodyGirth.measured_at.desc()).limit(60)).all()
    if not rows:
        return {"girth_rows": [], "girth_series_json": "[]"}
    chrono = list(reversed(rows))   # oldest -> newest
    series = [{"date": r.measured_at.strftime("%Y-%m-%d"), "vita": r.vita_cm,
               "fianchi": r.fianchi_cm, "coscia": r.coscia_cm, "braccio": r.braccio_cm}
              for r in chrono]
    return {"girth_rows": list(rows)[:20], "girth_series_json": json.dumps(series)}


@app.post("/body/manual", dependencies=[Depends(require_auth)])
def body_manual(background: BackgroundTasks, weight_kg: str = Form(""),
                body_fat: str = Form(""), muscle_kg: str = Form(""),
                water_pct: str = Form(""), visceral: str = Form(""),
                bmi: str = Form(""), bmr: str = Form(""), bone_kg: str = Form(""),
                metabolic_age: str = Form(""), date_: str = Form("", alias="date")):
    """Add a body-composition reading by hand (alongside the smart-scale pushes),
    so weight and composition keep a history and trend even without the scale."""
    def num(s):
        try:
            return float(str(s).replace(",", ".")) if str(s).strip() != "" else None
        except ValueError:
            return None

    d = _parse_date(date_)
    measured = (datetime.combine(d, datetime.utcnow().time()) if d
                else datetime.utcnow())
    m = BodyMeasure(
        measured_at=measured, weight_kg=num(weight_kg), body_fat=num(body_fat),
        muscle_kg=num(muscle_kg), water_pct=num(water_pct), visceral=num(visceral),
        bmi=num(bmi), bmr=num(bmr), bone_kg=num(bone_kg),
        metabolic_age=num(metabolic_age))
    if not any((m.weight_kg, m.body_fat, m.muscle_kg, m.water_pct, m.visceral,
                m.bmi, m.bmr, m.bone_kg, m.metabolic_age)):
        return RedirectResponse(f"/health?{urlencode({'error': 'Inserisci almeno un valore'})}",
                                status_code=303)
    # auto-BMI from weight + profile height when not provided
    if m.weight_kg and not m.bmi:
        h = profilemod.load().get("height_cm")
        if h:
            m.bmi = round(m.weight_kg / (float(h) / 100) ** 2, 1)
    with Session(engine) as session:
        session.add(m)
        session.commit()
    # a new weigh-in feeds the weight->nutrition loop (recompute + push targets)
    if nutrition.is_configured():
        background.add_task(nutrition.recompute_and_push)
    return RedirectResponse(f"/health?{urlencode({'msg': 'Misura corporea aggiunta'})}",
                            status_code=303)


@app.post("/body/{mid}/delete", dependencies=[Depends(require_auth)])
def body_delete(mid: int):
    with Session(engine) as session:
        m = session.get(BodyMeasure, mid)
        if m:
            session.delete(m)
            session.commit()
    return RedirectResponse(f"/health?{urlencode({'msg': 'Misura eliminata'})}",
                            status_code=303)


@app.post("/girth/manual", dependencies=[Depends(require_auth)])
def girth_manual(vita_cm: str = Form(""), fianchi_cm: str = Form(""),
                 petto_cm: str = Form(""), braccio_cm: str = Form(""),
                 coscia_cm: str = Form(""), polpaccio_cm: str = Form(""),
                 collo_cm: str = Form(""), note: str = Form(""),
                 date_: str = Form("", alias="date")):
    """Add a tape-measure reading (circumferences in cm) to track shape over time."""
    def num(s):
        try:
            return float(str(s).replace(",", ".")) if str(s).strip() != "" else None
        except ValueError:
            return None

    d = _parse_date(date_)
    measured = (datetime.combine(d, datetime.utcnow().time()) if d
                else datetime.utcnow())
    g = BodyGirth(measured_at=measured, vita_cm=num(vita_cm), fianchi_cm=num(fianchi_cm),
                  petto_cm=num(petto_cm), braccio_cm=num(braccio_cm),
                  coscia_cm=num(coscia_cm), polpaccio_cm=num(polpaccio_cm),
                  collo_cm=num(collo_cm), note=(note or "").strip() or None)
    if not any((g.vita_cm, g.fianchi_cm, g.petto_cm, g.braccio_cm, g.coscia_cm,
                g.polpaccio_cm, g.collo_cm)):
        return RedirectResponse(f"/health?{urlencode({'error': 'Inserisci almeno una misura'})}",
                                status_code=303)
    with Session(engine) as session:
        session.add(g)
        session.commit()
    return RedirectResponse(f"/health?{urlencode({'msg': 'Misure (metro) aggiunte'})}",
                            status_code=303)


@app.post("/girth/{gid}/delete", dependencies=[Depends(require_auth)])
def girth_delete(gid: int):
    with Session(engine) as session:
        g = session.get(BodyGirth, gid)
        if g:
            session.delete(g)
            session.commit()
    return RedirectResponse(f"/health?{urlencode({'msg': 'Misura eliminata'})}",
                            status_code=303)


@app.get("/records", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def records_page(request: Request):
    """Personal bests computed from the whole history."""
    return templates.TemplateResponse(request, "records.html",
                                      {"records": _personal_records()})


@app.get("/esercizi", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def exercises_page(request: Request):
    """Reference of the bodyweight exercises the AI proposes, with form cues and a
    how-to video link each."""
    return templates.TemplateResponse(request, "exercises.html",
                                      {"exercises": exercisesmod.catalog()})


@app.get("/health", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def health_page(request: Request, win: str = "30", end: str = ""):
    window = resolve_window(win, end, request.query_params.get("from", ""),
                            request.query_params.get("to", ""))
    base = {"window": window, "windows": WINDOWS, **_body_context(), **_girth_context(),
            "today": date.today().isoformat(),
            "message": request.query_params.get("msg"),
            "error": request.query_params.get("error")}
    if not google_health.is_authenticated():
        return templates.TemplateResponse(request, "health.html",
                                          {**base, "connected": False, "data": None})
    try:
        data = await google_health.fetch_health_overview(window["start"].date(),
                                                         window["end"].date())
    except google_health.GoogleNotAuthenticatedError:
        return templates.TemplateResponse(request, "health.html",
                                          {**base, "connected": False, "expired": True,
                                           "data": None})
    except google_health.GoogleHealthError as e:
        return templates.TemplateResponse(request, "health.html",
                                          {**base, "connected": True, "data": None,
                                           "error": str(e)})
    nutri = await nutrition.fetch_nutrition(window["start"].date(), window["end"].date())
    with Session(engine) as session:
        insight = session.get(PeriodSummary, _health_key(window))
    return templates.TemplateResponse(request, "health.html", {
        **base, "connected": True, "data": data, "windows": WINDOWS,
        "nutrition": nutri,
        "stale_days": _health_stale_days(data) if window["is_current"] else None,
        "last_data": _latest_health_date(data),
        "vo2max": _vo2max_estimate(data),
        "energy_json": json.dumps(_energy_series(data, nutri)),
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
        _attach_vo2max(data)
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


async def _coach_turn(request: Request, title_prefix: str = "") -> JSONResponse:
    """One turn of THE coach (dashboard, Salute, Programma, conversazioni salvate):
    grounded in the window's health data, activities and nutrition, persisted
    (Conversation). With an active program it also sees the program and may
    propose actions on it, returned for the user to confirm (/program/apply).
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
            conv = Conversation(title=f"{title_prefix}{msg}"[:60])
            session.add(conv)
            session.commit()
            session.refresh(conv)
        conv_id = conv.id
        prior = list(session.exec(select(ChatMessage)
                                  .where(ChatMessage.conversation_id == conv_id)
                                  .order_by(ChatMessage.created_at)))
        has_program = program_runner.active_program(session) is not None
    history = ([{"role": m.role, "content": m.content} for m in prior]
               + [{"role": "user", "content": msg}])[-12:]

    window = resolve_window(body.get("win", "30"), body.get("end", ""),
                            body.get("from", ""), body.get("to", ""))
    try:  # health data is best-effort: the assistant still works without it
        data = await google_health.fetch_health_overview(window["start"].date(),
                                                         window["end"].date())
    except google_health.GoogleHealthError:
        data = {"metrics": {}, "body": {}, "sleep": [], "score": None}
    _attach_vo2max(data)
    with Session(engine) as session:
        workouts = [w.model_dump(exclude={"raw_summary", "fit_path", "updated_at"})
                    for w in query_range(session, window["start"], window["end"], None)]
    nutri = await nutrition.fetch_nutrition(window["start"].date(), window["end"].date())
    _, form_summary = _fitness_series()
    program = program_runner.chat_context(_program_ctx()) if has_program else None
    try:
        out = await anthropic_client.coach_chat(data, workouts, history, nutri,
                                                form_summary, program=program)
    except anthropic_client.AnthropicError as e:
        return JSONResponse({"error": str(e)}, status_code=502)

    reply = out.get("risposta") or ""
    plan_req = out.get("piano_richiesto")
    actions = out.get("azioni") or []
    names = {}
    with Session(engine) as session:
        for a in actions:
            sid = a.get("session_id")
            if isinstance(sid, int) and (ps := session.get(PlanSession, sid)):
                names[sid] = f"{ps.day_label} «{ps.title}»"
    for a in actions:
        a["label"] = _program_action_label(a, names)
    stored = reply
    if plan_req:
        stored += f"\n\n[Piano proposto] {plan_req.get('obiettivo')} · {plan_req.get('giorni')} giorni"
    if actions:
        stored += "\n\n[Proposte] " + "; ".join(a["label"] for a in actions)
    with Session(engine) as session:
        session.add(ChatMessage(conversation_id=conv_id, role="user", content=msg))
        session.add(ChatMessage(conversation_id=conv_id, role="assistant", content=stored))
        c = session.get(Conversation, conv_id)
        c.updated_at = datetime.utcnow()
        session.add(c)
        session.commit()
    return JSONResponse({"reply": reply, "plan_request": plan_req, "actions": actions,
                         "conversation_id": conv_id})


@app.post("/health/chat", dependencies=[Depends(require_auth)])
async def health_chat(request: Request):
    return await _coach_turn(request)


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
SPORT_CHOICES = {"bici": ("Ciclismo", "bike", 15.0),
                 "escursione": ("Escursione", "walk", 3.0),
                 "corsa": ("Corsa", "run", 3.0)}
SPORT_FAMILY = {label: fam for label, fam, _ in SPORT_CHOICES.values()}
SPORT_FAMILY["Bici"] = "bike"   # label of routes uploaded before the rename


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


def _index_climbs(session, w: Workout) -> None:
    """Detect the climbs of a bike ride and store them (with GPS) as ClimbEfforts.
    Idempotent: replaces any existing efforts for the workout."""
    for old in session.exec(select(ClimbEffort).where(
            ClimbEffort.workout_id == w.id)).all():
        session.delete(old)
    streams = fitmod.load_streams(w.id)
    if streams:
        for c in cyclingmod.detect_segments(streams):
            if not c.get("start_ll") or not c.get("top_ll"):
                continue  # no GPS -> can't match it across rides
            session.add(ClimbEffort(
                workout_id=w.id, date=w.start_date, kind=c["kind"],
                path_json=json.dumps(c.get("path") or []),
                start_km=c["start_km"], gain_m=c["gain_m"], length_m=c["length_m"],
                avg_grade=c["avg_grade"], max_grade=c["max_grade"], time_s=c["time_s"],
                speed_kmh=c["speed_kmh"], vam=c["vam"], avg_hr=c["avg_hr"],
                start_lat=c["start_ll"][0], start_lng=c["start_ll"][1],
                top_lat=c["top_ll"][0], top_lng=c["top_ll"][1]))
    # also time this ride on every user-defined segment
    for seg in session.exec(select(CustomSegment)).all():
        _match_custom(session, seg, w, streams)
    # best 20-min power (real if a power meter, else estimated) for the FTP trend
    if streams and streams.get("t"):
        power = cyclingmod._fill(streams.get("power") or [])
        if not (w.avg_power and any(power)):
            power = cyclingmod.estimate_power_series(
                streams, cyclingmod.rider_mass(profilemod.load().get("weight_kg")))
        b = cyclingmod.best_rolling_avg(streams["t"], power, 1200) if power else None
        w.best20_w = round(b) if b else None
    w.climbs_indexed = True
    session.add(w)


def _ftp_series(window_days: int = 42) -> list[dict]:
    """Estimated FTP over time: per ride 0.95×best-20min power, plus a rolling
    6-week eFTP. Robust: drop rides whose 20-min estimate is physiologically
    implausible (> ceiling W/kg) and reject a lone spike in the rolling best."""
    with Session(engine) as session:
        rides = session.exec(select(Workout).where(Workout.best20_w != None)  # noqa: E711
                             .order_by(Workout.start_date)).all()
    ceiling = (profilemod.load().get("weight_kg") or cyclingmod.DEFAULT_RIDER_KG) \
        * cyclingmod.FTP_WKG_CEILING
    pts = [(w.start_date.date(), w.best20_w) for w in rides
           if w.best20_w and w.best20_w <= ceiling]
    out = []
    for d, b in pts:
        lo = d - timedelta(days=window_days)
        best = cyclingmod.robust_best([b2 for d2, b2 in pts if lo <= d2 <= d])
        out.append({"date": d.isoformat(), "ride": round(b * 0.95),
                    "ftp": round(best * 0.95)})
    return out


def _match_custom(session, seg: CustomSegment, w: Workout, streams=None) -> None:
    """(Re)compute w's timed effort on custom segment seg; store, update or drop."""
    ex = session.exec(select(CustomEffort).where(
        CustomEffort.segment_id == seg.id, CustomEffort.workout_id == w.id)).first()
    streams = streams if streams is not None else fitmod.load_streams(w.id)
    m = cyclingmod.match_segment(
        streams, [seg.start_lat, seg.start_lng], [seg.mid_lat, seg.mid_lng],
        [seg.end_lat, seg.end_lng]) if streams else None
    if m:
        eff = ex or CustomEffort(segment_id=seg.id, workout_id=w.id, date=w.start_date)
        eff.time_s, eff.distance_m = m["time_s"], m["distance_m"]
        eff.speed_kmh, eff.avg_hr = m["speed_kmh"], m["avg_hr"]
        session.add(eff)
    elif ex:
        session.delete(ex)


def _ensure_climbs_indexed() -> None:
    """Lazily index climbs for any bike ride with a FIT that isn't done yet."""
    with Session(engine) as session:
        todo = [w for w in session.exec(select(Workout).where(
            Workout.has_fit == True, Workout.climbs_indexed == False))  # noqa: E712
            if google_health._sport_family(w.sport) == "bike"]
        for w in todo:
            _index_climbs(session, w)
        if todo:
            session.commit()


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
    _ensure_climbs_indexed()          # also fills best20_w for the FTP trend
    series, summary = _fitness_series()
    ftp_series = _ftp_series()
    shown = series
    if FORM_MONTHS[months] and series:
        cutoff = (date.today() - timedelta(days=FORM_MONTHS[months] * 30)).isoformat()
        shown = [p for p in series if p["date"] >= cutoff]
        ftp_series = [p for p in ftp_series if p["date"] >= cutoff]
    with Session(engine) as session:
        insight = session.get(PeriodSummary, f"form:{date.today().isoformat()}")
    return templates.TemplateResponse(request, "form.html", {
        "months": months, "windows": FORM_MONTHS, "summary": summary,
        "series_json": json.dumps(shown),
        "ftp_series_json": json.dumps(ftp_series),
        "ftp_now": ftp_series[-1]["ftp"] if ftp_series else None,
        "ftp_now_wkg": (round(ftp_series[-1]["ftp"]
                        / (profilemod.load().get("weight_kg") or cyclingmod.DEFAULT_RIDER_KG), 2)
                        if ftp_series else None),
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


@app.get("/segments", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def segments_page(request: Request):
    """Recurring climbs (Strava-segment style): the same climb matched across
    rides by GPS, to compare efforts over time."""
    import statistics
    _ensure_climbs_indexed()
    with Session(engine) as session:
        efforts = session.exec(select(ClimbEffort).order_by(ClimbEffort.date)).all()
        custom = session.exec(select(CustomSegment).order_by(CustomSegment.created_at)).all()
        custom_effs = session.exec(select(CustomEffort).order_by(CustomEffort.date)).all()
    items = [{"start_ll": [e.start_lat, e.start_lng], "top_ll": [e.top_lat, e.top_lng],
              "kind": e.kind, "path": json.loads(e.path_json or "[]"),
              "date": e.date, "time_s": e.time_s, "speed_kmh": e.speed_kmh, "vam": e.vam,
              "avg_hr": e.avg_hr, "gain_m": e.gain_m, "length_m": e.length_m,
              "avg_grade": e.avg_grade, "workout_id": e.workout_id} for e in efforts]
    segments = []
    for cl in cyclingmod.cluster_segments(items):
        if len(cl) < 2:
            continue  # a segment is a climb/descent ridden at least twice
        cl.sort(key=lambda x: x["date"])
        best = min(x["time_s"] for x in cl)
        for x in cl:
            x["is_pr"] = x["time_s"] == best
        latest = cl[-1]
        segments.append({
            "efforts": cl, "count": len(cl), "kind": latest["kind"],
            "gain_m": round(statistics.median(x["gain_m"] for x in cl)),
            "length_km": round(statistics.median(x["length_m"] for x in cl) / 1000, 1),
            "grade": round(statistics.median(x["avg_grade"] for x in cl), 1),
            "best_time_s": best, "latest": latest,
            "latest_is_pr": latest["time_s"] == best,
            # path of the latest effort (or any with GPS) for the mini-map
            "path": next((x["path"] for x in reversed(cl) if x.get("path")), []),
        })
    # climbs first, then by how often ridden and recency
    segments.sort(key=lambda s: (s["kind"] != "climb", -s["count"],
                                 -s["latest"]["date"].timestamp()))

    # user-defined segments (shown even with one effort — they were created on purpose)
    by_seg: dict = defaultdict(list)
    for e in custom_effs:
        by_seg[e.segment_id].append(e)
    custom_segments = []
    for seg in custom:
        effs = sorted(by_seg.get(seg.id, []), key=lambda e: e.date)
        best = min((e.time_s for e in effs), default=0)
        custom_segments.append({
            "id": seg.id, "name": seg.name, "length_km": round(seg.length_m / 1000, 2),
            "path": json.loads(seg.path_json or "[]"), "best_time_s": best,
            "count": len(effs),
            # ride to redraw on: the source ride, else any ride that passes through
            "redraw_workout_id": seg.source_workout_id or (effs[0].workout_id if effs else None),
            "efforts": [{"date": e.date, "time_s": e.time_s, "speed_kmh": e.speed_kmh,
                         "avg_hr": e.avg_hr, "workout_id": e.workout_id,
                         "is_pr": e.time_s == best} for e in effs],
            "latest_is_pr": bool(effs) and effs[-1].time_s == best,
        })
    custom_segments.sort(key=lambda s: (-s["count"]))
    return templates.TemplateResponse(request, "segments.html", {
        "segments": segments, "custom_segments": custom_segments, "n_efforts": len(items),
        "message": request.query_params.get("msg"),
        "error": request.query_params.get("error")})


def _bike_progress(today) -> dict:
    """Season-over-season cycling progress data: repeated-climb/segment PR board,
    this-vs-last overlays (aligned by day-of-season), and 'a oggi' volumes.
    Shared by the /progress page and the AI season report."""
    import statistics
    _ensure_climbs_indexed()
    with Session(engine) as session:
        bikes = [w for w in session.exec(select(Workout).order_by(Workout.start_date))
                 if google_health._sport_family(w.sport) == "bike"]
        climb_effs = session.exec(select(ClimbEffort).order_by(ClimbEffort.date)).all()
        customs = session.exec(select(CustomSegment)).all()
        custom_effs = session.exec(select(CustomEffort).order_by(CustomEffort.date)).all()

    # --- PR board: recurring climbs (matched by GPS) + user-defined segments
    climb_items = [{"start_ll": [e.start_lat, e.start_lng], "top_ll": [e.top_lat, e.top_lng],
                    "kind": e.kind, "date": e.date, "time_s": e.time_s, "vam": e.vam,
                    "avg_hr": e.avg_hr, "speed_kmh": e.speed_kmh, "gain_m": e.gain_m,
                    "length_m": e.length_m, "avg_grade": e.avg_grade} for e in climb_effs]
    segments = []
    for cl in cyclingmod.cluster_segments(climb_items):
        if not cl or cl[0].get("kind") != "climb":
            continue  # a descent isn't a fitness signal
        cmp = progressmod.segment_compare(cl, today)
        if not cmp or cmp["n_efforts"] < 2:
            continue
        segments.append({
            "name": f"Salita {round(statistics.median(x['gain_m'] for x in cl))} m · "
                    f"{round(statistics.median(x['avg_grade'] for x in cl), 1)}%",
            "length_km": round(statistics.median(x["length_m"] for x in cl) / 1000, 1),
            "type": "climb", **cmp})
    by_seg = defaultdict(list)
    for e in custom_effs:
        by_seg[e.segment_id].append({"date": e.date, "time_s": e.time_s,
                                     "avg_hr": e.avg_hr, "speed_kmh": e.speed_kmh})
    for seg in customs:
        cmp = progressmod.segment_compare(by_seg.get(seg.id, []), today)
        if not cmp or cmp["n_efforts"] < 2:
            continue
        segments.append({"name": seg.name or f"Segmento {round(seg.length_m / 1000, 1)} km",
                         "length_km": round(seg.length_m / 1000, 1), "type": "custom", **cmp})
    # a year-over-year comparison first, then PRs this season, then most ridden
    segments.sort(key=lambda s: (s["time_delta_pct"] is None,
                                 not s["is_pr_this_season"], -s["n_efforts"]))

    # --- overlays: this season vs last, aligned by day-of-season
    eftp_pts = [{"date": w.start_date, "value": round(w.best20_w * 0.95)}
                for w in bikes if w.best20_w]
    series, _ = _fitness_series()
    ctl_pts = [{"date": p["date"], "value": p["ctl"]} for p in series]
    vol_pts = [{"date": w.start_date, "value": (w.moving_s or 0) / 3600} for w in bikes]
    # efficiency: the best climb of each ride, VAM per heart-beat (cheap, honest-ish)
    eff_by_ride = defaultdict(list)
    for e in climb_effs:
        if e.kind == "climb" and e.vam and e.avg_hr:
            eff_by_ride[e.workout_id].append((e.vam, e.avg_hr))
    bike_date = {w.id: w.start_date for w in bikes}
    eff_pts = []
    for wid, lst in eff_by_ride.items():
        if wid in bike_date:
            vam, hr = max(lst)
            eff_pts.append({"date": bike_date[wid], "value": round(vam / hr, 2)})

    overlays = {"eftp": progressmod.align_by_season(eftp_pts, today),
                "ctl": progressmod.align_by_season(ctl_pts, today),
                "efficiency": progressmod.align_by_season(eff_pts, today),
                "volume": progressmod.cumulative_by_season(vol_pts, today)}
    vol = progressmod.season_volume(
        [{"date": w.start_date, "moving_s": w.moving_s, "distance_m": w.distance_m,
          "ascent_m": w.ascent_m} for w in bikes], today)
    return {"segments": segments, "overlays": overlays, "vol": vol,
            "ftp": _estimated_ftp(), "n_bikes": len(bikes)}


@app.get("/progress", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def progress_page(request: Request):
    """Progressi bici: confronto stagione vs stagione precedente."""
    today = date.today()
    cur = progressmod.season_year(today)
    data = _bike_progress(today)
    with Session(engine) as session:
        report = session.get(PeriodSummary, f"season:{cur}")
    return templates.TemplateResponse(request, "progress.html", {
        "today": today.isoformat(), "cur": cur, "prev": cur - 1,
        "segments": data["segments"], "overlays_json": json.dumps(data["overlays"]),
        "vol": data["vol"], "ftp": data["ftp"], "n_bikes": data["n_bikes"],
        "report_html": md.markdown(report.content, extensions=["tables"]) if report else None,
        "report_date": report.created_at if report else None,
        "error": request.query_params.get("error")})


def _season_report_payload(data: dict, cur: int) -> dict:
    """Compact, AI-ready view of the progress data (keeps the prompt small)."""
    def trend(line: list[dict]):
        if not line or len(line) < 2:
            return None
        return {"inizio": line[0]["value"], "ora": line[-1]["value"],
                "delta_pct": progressmod._pct(line[-1]["value"], line[0]["value"])}
    ov = data["overlays"]
    segs = [{"nome": s["name"], "km": s["length_km"], "tipo": s["type"],
             "tempo_quest_anno_s": (s["this"] or {}).get("time_s"),
             "tempo_anno_scorso_s": (s["last"] or {}).get("time_s"),
             "vam_quest_anno": (s["this"] or {}).get("vam"),
             "vam_anno_scorso": (s["last"] or {}).get("vam"),
             "fc_quest_anno": (s["this"] or {}).get("avg_hr"),
             "fc_anno_scorso": (s["last"] or {}).get("avg_hr"),
             "delta_tempo_pct": s["time_delta_pct"], "delta_fc": s["hr_delta"],
             "giudizio": s["verdict"]}
            for s in data["segments"][:8] if s["time_delta_pct"] is not None]
    return {"stagione": cur, "a_oggi_vs_anno_scorso": data["vol"],
            "ftp_stimato": data["ftp"],
            "salite_segmenti_confronto": segs,
            "efficienza_vam_per_fc": trend(ov["efficiency"].get(cur)),
            "eftp_stagione": trend(ov["eftp"].get(cur)),
            "nota": "potenza/eFTP STIMATI (niente misuratore): pesa di più salite/segmenti ed efficienza"}


@app.post("/progress/report", dependencies=[Depends(require_auth)])
async def progress_report():
    """Generate (and cache) the AI season-over-season report."""
    today = date.today()
    cur = progressmod.season_year(today)
    key = f"season:{cur}"
    data = _bike_progress(today)
    if not data["segments"] and not (data["vol"]["rides"]["this"]):
        return JSONResponse({"error": "Non ci sono abbastanza uscite in bici di questa stagione."},
                            status_code=400)
    try:
        content = await anthropic_client.season_report(_season_report_payload(data, cur))
    except anthropic_client.AnthropicError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    with Session(engine) as session:
        existing = session.get(PeriodSummary, key)
        if existing:
            existing.content = content
            existing.created_at = datetime.utcnow()
            session.add(existing)
        else:
            session.add(PeriodSummary(key=key, content=content,
                                      model=anthropic_client.effective_model()))
        session.commit()
    return JSONResponse({"html": md.markdown(content, extensions=["tables"])})


@app.post("/workout/{workout_id}/segment/create", dependencies=[Depends(require_auth)])
def segment_create(workout_id: int, name: str = Form(""),
                   start_km: str = Form(""), end_km: str = Form(""),
                   start_lat: str = Form(""), start_lng: str = Form(""),
                   end_lat: str = Form(""), end_lng: str = Form(""),
                   segment_id: str = Form("")):
    """Create a custom segment from a stretch of this ride — chosen by clicking
    the map (start/end points) or by km — then time every ride through it. With a
    segment_id, re-defines that segment's track instead of creating a new one."""
    def num(s):
        try:
            return float(s)
        except (TypeError, ValueError):
            return None

    streams = fitmod.load_streams(workout_id)
    seg_def = None
    if streams and None not in (num(start_lat), num(start_lng), num(end_lat), num(end_lng)):
        seg_def = cyclingmod.segment_from_points(
            streams, [num(start_lat), num(start_lng)], [num(end_lat), num(end_lng)])
    elif streams and None not in (num(start_km), num(end_km)):
        seg_def = cyclingmod.segment_from_km(streams, num(start_km), num(end_km))
    if not seg_def:
        return RedirectResponse(
            f"/workout/{workout_id}?{urlencode({'error': 'Segmento non valido: seleziona il tratto sulla mappa'})}",
            status_code=303)
    with Session(engine) as session:
        editing = session.get(CustomSegment, int(segment_id)) if num(segment_id) else None
        seg = editing or CustomSegment()
        if name.strip() or not seg.name:
            seg.name = name.strip() or f"Segmento {round(seg_def['length_m'] / 1000, 1)} km"
        seg.length_m, seg.path_json = seg_def["length_m"], json.dumps(seg_def["path"])
        seg.source_workout_id = workout_id
        seg.start_lat, seg.start_lng = seg_def["start_ll"]
        seg.mid_lat, seg.mid_lng = seg_def["mid_ll"]
        seg.end_lat, seg.end_lng = seg_def["end_ll"]
        session.add(seg)
        session.commit()
        session.refresh(seg)
        if editing:  # boundaries changed -> recompute every effort
            for e in session.exec(select(CustomEffort).where(
                    CustomEffort.segment_id == seg.id)).all():
                session.delete(e)
        rides = [w for w in session.exec(select(Workout).where(
            Workout.has_fit == True))  # noqa: E712
            if google_health._sport_family(w.sport) == "bike"]
        for w in rides:
            _match_custom(session, seg, w)
        session.commit()
        n = len(session.exec(select(CustomEffort).where(
            CustomEffort.segment_id == seg.id)).all())
    verb = "aggiornato" if editing else "creato"
    return RedirectResponse(
        f"/segments?{urlencode({'msg': f'Segmento «{seg.name}» {verb}: {n} passaggi trovati'})}",
        status_code=303)


@app.post("/segments/{segment_id}/rename", dependencies=[Depends(require_auth)])
def segment_rename(segment_id: int, name: str = Form("")):
    with Session(engine) as session:
        seg = session.get(CustomSegment, segment_id)
        if seg and name.strip():
            seg.name = name.strip()[:120]
            session.add(seg)
            session.commit()
    return RedirectResponse(f"/segments?{urlencode({'msg': 'Segmento rinominato'})}",
                            status_code=303)


@app.post("/segments/{segment_id}/delete", dependencies=[Depends(require_auth)])
def segment_delete(segment_id: int):
    with Session(engine) as session:
        for e in session.exec(select(CustomEffort).where(
                CustomEffort.segment_id == segment_id)).all():
            session.delete(e)
        seg = session.get(CustomSegment, segment_id)
        if seg:
            session.delete(seg)
        session.commit()
    return RedirectResponse(f"/segments?{urlencode({'msg': 'Segmento eliminato'})}",
                            status_code=303)


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
        sports = _sport_options(session)
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
                    sport=google_health.canonical_sport(sport) or "Allenamento",
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


_BG_TASKS: set = set()   # tiene vivi i task fire-and-forget finché non finiscono


def _fire_and_forget(coro) -> None:
    import asyncio
    task = asyncio.create_task(coro)
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)


def _program_ctx() -> dict:
    """What the program engine needs from the rest of the app (passed in so
    program_runner never imports main)."""
    _, summary = _fitness_series()
    return {"ctl": (summary or {}).get("ctl") or 0.0, "anchors": _hr_anchors(),
            "ftp": _estimated_ftp(), "form": summary}


def _kick_program(why: str) -> None:
    """Ricalcolo del programma in background dopo un evento (nuova attività,
    seduta fatta/annullata), poi riallinea i target nutrizionali se è cambiato
    qualcosa. Va chiamato da codice async (serve il loop in esecuzione)."""
    async def run():
        try:
            st = await program_runner.recalc(_program_ctx(), why)
            if st and nutrition.is_configured() and any(
                    st[k] for k in ("created", "updated", "removed", "linked")):
                await nutrition.recompute_and_push()
        except Exception as e:  # noqa: BLE001
            logger.warning("Program recalc (%s) failed: %s", why, e)
    _fire_and_forget(run())


IMPORT_EVERY_S = 2 * 3600   # attività dal Pixel Watch (Google): ogni 2 ore


async def _import_activities_and_recalc(why: str, push_nutrition: bool = True) -> dict | None:
    """Importa le attività nuove da Google (Pixel Watch: casa, nuoto, camminate),
    unisce i doppioni coi segnaposto del piano, poi ricalcola il programma — così
    la seduta fatta si spunta da sola e il budget della settimana si aggiorna
    senza premere Sync."""
    if google_health.is_authenticated():
        try:
            n = await google_health.enrich_workouts(max_pages=2)
            if n:
                logger.info("Periodic Google import (%s): %s workouts created/updated", why, n)
                reconcile_plan_duplicates()
        except google_health.GoogleHealthError as e:
            logger.warning("Periodic Google import skipped (%s): %s", why, e)
    st = await program_runner.recalc(_program_ctx(), why)
    if push_nutrition and st and nutrition.is_configured() and any(
            st[k] for k in ("created", "updated", "removed", "linked")):
        await nutrition.recompute_and_push()
    return st


async def _activity_import_loop() -> None:
    """Ogni IMPORT_EVERY_S: import da Google + ricalcolo del programma.
    Best-effort: gli errori non fermano il loop."""
    import asyncio
    logger.info("Activity import loop started (every %ss)", IMPORT_EVERY_S)
    while True:
        await asyncio.sleep(IMPORT_EVERY_S)
        try:
            await _import_activities_and_recalc("import periodico")
        except Exception as e:  # noqa: BLE001
            logger.warning("Periodic import/recalc failed: %s", e)


async def _nightly_recompute_loop() -> None:
    """Ricalcola e ri-invia i target a planmydinner ogni notte (03:00 locali),
    così il fabbisogno segue peso e allenamenti anche senza pesarsi o rigenerare
    il piano. Best-effort: gli errori non fermano il loop."""
    import asyncio
    from datetime import datetime, timedelta
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(settings.timezone)
    except Exception:
        tz = None
    while True:
        now = datetime.now(tz)
        nxt = now.replace(hour=3, minute=0, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        await asyncio.sleep(max(60.0, (nxt - now).total_seconds()))
        # prima le attività del giorno (Pixel Watch via Google) e il programma (il
        # nuovo giorno entra nella finestra), poi i target nutrizionali, così la
        # periodizzazione vede le sedute appena create
        try:
            await _import_activities_and_recalc("notturno", push_nutrition=False)
        except Exception as e:  # noqa: BLE001
            logger.warning("Nightly program recalc failed: %s", e)
        try:
            if nutrition.is_configured():
                ok = await nutrition.recompute_and_push()
                logger.info("Nightly target recompute pushed=%s", ok)
        except Exception as e:  # noqa: BLE001
            logger.warning("Nightly recompute failed: %s", e)


async def _generate_and_store_plan(goal: str, n_days: int, start_d) -> int | None:
    """Ask the AI for a plan and persist it as TrainingPlan + PlanSessions.
    Returns the plan id, or None if the AI gave nothing usable."""
    data = await anthropic_client.generate_plan(goal, n_days, start_d.isoformat())
    sessions = data.get("sessions") or []
    if not sessions:
        return None
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
                title=str(s.get("title", ""))[:200], sport=google_health.canonical_sport(str(s.get("sport", "")))[:60],
                duration_min=int(s.get("durata_min") or 0),
                description=str(s.get("description", ""))))
        session.commit()
        pid = plan.id
    # Nuovi allenamenti in programma → riallinea i target per-giorno
    # (periodizzazione) su planmydinner, best-effort in background.
    if nutrition.is_configured():
        _fire_and_forget(nutrition.recompute_and_push())
    return pid


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
        pid = await _generate_and_store_plan(goal, n, start_d)
    except anthropic_client.AnthropicError as e:
        return RedirectResponse(f"/plans?{urlencode({'err': str(e)})}", status_code=303)
    if not pid:
        return RedirectResponse(
            f"/plans?{urlencode({'err': 'AI non ha restituito un piano valido'})}",
            status_code=303)
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
        plan_chat = [{"role": m.role, "content": m.content}
                     for m in session.exec(select(ChatMessage)
                         .where(ChatMessage.conversation_id == plan.conversation_id)
                         .order_by(ChatMessage.created_at))] if plan.conversation_id else []
        # done sessions whose manual workout could still absorb a real recording
        mergeable = set()
        for s in sessions:
            if s.done and s.workout_id:
                kw = session.get(Workout, s.workout_id)
                if kw and _merge_candidate(session, kw):
                    mergeable.add(s.id)
    done = sum(1 for s in sessions if s.done)
    loads = {s.id: round(_session_load(s.sport, s.duration_min)) for s in sessions}
    return templates.TemplateResponse(request, "plan.html", {
        "plan": plan, "sessions": sessions, "done": done, "total": len(sessions),
        "loads": loads, "chats_json": json.dumps(chats), "mergeable": mergeable,
        "plan_chat_json": json.dumps(plan_chat),
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


def _safe_next(nxt: str, fallback: str) -> str:
    """Only same-site relative redirects (no //host or scheme)."""
    return nxt if nxt.startswith("/") and not nxt.startswith("//") else fallback


@app.post("/plans/{plan_id}/session/{sid}/done", dependencies=[Depends(require_auth)])
async def plan_session_done(plan_id: int, sid: int, done_notes: str = Form(""),
                            done_min: str = Form(""), done_date: str = Form(""),
                            next: str = Form("")):
    """Mark a session done, recording what was *actually* done (defaults to the
    planned session) as the workout notes, so the AI can judge the real load."""
    back = _safe_next(next, f"/plans/{plan_id}")
    with Session(engine) as session:
        ps = session.get(PlanSession, sid)
        if not ps or ps.plan_id != plan_id or ps.done:
            return RedirectResponse(back, status_code=303)
        title, sport, planned_min = ps.title, ps.sport, ps.duration_min
        is_program = bool(ps.kind)
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
            return RedirectResponse(back, status_code=303)
        start = (datetime.combine(d, time(12, 0)) if d
                 else (ps.date or datetime.utcnow()))
        wid = _create_manual_workout(title, sport, start, mins, notes, rpe=rpe)
        ps.done = True
        ps.workout_id = wid
        ps.proposal_json = None
        session.add(ps)
        session.commit()
    # se la registrazione reale (Wahoo/Google/FIT) di questa sessione è già
    # arrivata, assorbila subito nel segnaposto appena creato
    try:
        reconcile_plan_duplicates()
    except Exception as e:
        logger.warning("Reconcile after plan-done failed: %s", e)
    if is_program:
        _kick_program("seduta fatta")
    sep = "&" if "?" in back else "?"
    return RedirectResponse(f"{back}{sep}{urlencode({'msg': 'Sessione segnata come fatta'})}",
                            status_code=303)


@app.post("/plans/{plan_id}/session/{sid}/undo", dependencies=[Depends(require_auth)])
async def plan_session_undo(plan_id: int, sid: int, next: str = Form("")):
    is_program = False
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
            is_program = bool(ps.kind)
    if is_program:
        _kick_program("seduta annullata")
    return RedirectResponse(_safe_next(next, f"/plans/{plan_id}"), status_code=303)


# ---------------------------------------------------------------- programma adattivo

def _program_redirect(msg: str = "", error: str = "", path: str = "/program"):
    q = urlencode({k: v for k, v in (("msg", msg), ("error", error)) if v})
    return RedirectResponse(f"{path}?{q}" if q else path, status_code=303)


async def _program_recalc_now(why: str) -> tuple[dict | None, str]:
    """Ricalcolo sincrono (l'utente aspetta il risultato) + target nutrizione in
    background. Ritorna (stats, errore)."""
    try:
        st = await program_runner.recalc(_program_ctx(), why)
    except Exception as e:  # noqa: BLE001
        logger.exception("Program recalc (%s) failed", why)
        return None, f"Ricalcolo non riuscito: {e}"
    if st and nutrition.is_configured() and any(
            st[k] for k in ("created", "updated", "removed", "linked")):
        _fire_and_forget(nutrition.recompute_and_push())
    return st, ""


@app.get("/program", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def program_page(request: Request):
    today = date.today()
    jl = program_runner.jl
    with Session(engine) as session:
        prog = program_runner.active_program(session)
        if not prog:
            return templates.TemplateResponse(request, "program.html", {
                "prog": None, "has_notes": bool(profilemod.load().get("ai_notes")),
                "error": request.query_params.get("error")})
        tpl = jl(prog.template_json, [])
        routines = {r["id"]: r for r in jl(prog.routines_json, [])
                    if isinstance(r, dict) and r.get("id")}
        slot_by_id = {s.get("id"): s for s in tpl}
        wk0 = programmod.monday(today)
        rows = session.exec(select(PlanSession).where(
            PlanSession.plan_id == prog.plan_id,
            PlanSession.date >= datetime.combine(wk0, time.min))
            .order_by(PlanSession.date, PlanSession.order)).all() if prog.plan_id else []
        ftp_w = (_estimated_ftp() or {}).get("ftp")

        days: dict = {}
        proposals = []
        for ps in rows:
            slot = slot_by_id.get(ps.slot_id) or {}
            prop = jl(ps.proposal_json, {})
            steps = jl(ps.structure_json, [])
            for st in steps:
                st["watt"] = round(ftp_w * st["pct_ftp"] / 100) if ftp_w and st.get("pct_ftp") else None
            v = {"ps": ps, "desc": exercisesmod.linkify(ps.description or ""),
                 "routines": [{**routines[r], "html": exercisesmod.linkify(routines[r].get("content", ""))}
                              for r in slot.get("routines") or [] if r in routines],
                 "steps": steps, "indoor": slot.get("indoor"),
                 "pick_one": slot.get("pick") == "one",
                 "past": ps.date.date() < today if ps.date else False,
                 "is_today": ps.date.date() == today if ps.date else False,
                 "proposal": prop if prop and not prop.get("rejected") else None}
            if v["proposal"]:
                v["prop_desc"] = exercisesmod.linkify(prop.get("description") or "")
                proposals.append(v)
            key = ps.date.date().isoformat() if ps.date else "—"
            days.setdefault(key, {"label": ps.day_label, "items": []})["items"].append(v)
    wsum = program_runner.week_summary(_hr_anchors()) or {}
    pos = wsum.get("pos") or programmod.block_position(today, today, 1, [1.0])
    target, done_load = wsum.get("target", 0.0), wsum.get("done", 0.0)
    return templates.TemplateResponse(request, "program.html", {
        "prog": prog, "pos": pos, "block_label": programmod.block_label(pos),
        "target": round(target), "done_load": round(done_load),
        "days": days, "proposals": proposals, "today": today.isoformat(),
        "absences": jl(prog.absences_json, []), "equipment": jl(prog.equipment_json, []),
        "ftp": ftp_w, "template": tpl,
        "weekdays": programmod.WEEKDAYS_IT,
        "message": request.query_params.get("msg"), "error": request.query_params.get("error")})


@app.post("/program/create", dependencies=[Depends(require_auth)])
async def program_create(goal: str = Form("")):
    try:
        await program_runner.create_from_notes(goal)
    except anthropic_client.AnthropicError as e:
        return _program_redirect(error=str(e))
    return _program_redirect(
        msg="Programma precompilato dalla tua memoria AI: controlla e salva per generare le sedute",
        path="/program/settings")


@app.get("/program/settings", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def program_settings(request: Request):
    jl = program_runner.jl
    with Session(engine) as session:
        prog = program_runner.active_program(session)
        if not prog:
            return RedirectResponse("/program", status_code=303)
    return templates.TemplateResponse(request, "program_settings.html", {
        "prog": prog, "template": jl(prog.template_json, []),
        "routines": jl(prog.routines_json, []), "absences": jl(prog.absences_json, []),
        "equipment": "\n".join(jl(prog.equipment_json, [])),
        "multipliers": ", ".join(f"{m:g}" for m in jl(prog.block_multipliers_json, [1.0])),
        "weekdays": programmod.WEEKDAYS_IT,
        "message": request.query_params.get("msg"), "error": request.query_params.get("error")})


@app.post("/program/settings", dependencies=[Depends(require_auth)])
async def program_settings_save(request: Request):
    form = await request.form()

    def g(k: str) -> str:
        return str(form.get(k) or "").strip()

    routines, i = [], 0
    while f"r-{i}-name" in form:
        if not form.get(f"r-{i}-del"):
            routines.append({"id": g(f"r-{i}-id") or g(f"r-{i}-name"), "name": g(f"r-{i}-name"),
                             "duration_min": g(f"r-{i}-duration"), "content": g(f"r-{i}-content"),
                             "video_url": g(f"r-{i}-video")})
        i += 1
    routines = programmod.sanitize_routines(routines)
    rids = [r["id"] for r in routines]

    slots, i = [], 0
    while f"s-{i}-sport" in form:
        if not form.get(f"s-{i}-del"):
            slots.append({"id": g(f"s-{i}-id") or g(f"s-{i}-sport"),
                          "days": [d for d in range(7) if form.get(f"s-{i}-day-{d}")],
                          "pick": "one" if form.get(f"s-{i}-pick") else "each",
                          "sport": g(f"s-{i}-sport"), "kind": g(f"s-{i}-kind"),
                          "duration": g(f"s-{i}-duration"), "focus": g(f"s-{i}-focus"),
                          "intensity": g(f"s-{i}-intensity"),
                          "routines": [r for r in rids if form.get(f"s-{i}-rt-{r}")],
                          "indoor": bool(form.get(f"s-{i}-indoor")),
                          "enabled": bool(form.get(f"s-{i}-enabled"))})
        i += 1
    template = programmod.sanitize_template(
        slots, {r["id"]: r["duration_min"] for r in routines})

    absences, i = [], 0
    while f"a-{i}-start" in form:
        st, en = _parse_date(g(f"a-{i}-start")), _parse_date(g(f"a-{i}-end"))
        if st and not form.get(f"a-{i}-del"):
            en = en if en and en >= st else st
            absences.append({"start": st.isoformat(), "end": en.isoformat(),
                             "note": g(f"a-{i}-note")[:120],
                             "allowed": [x.strip()[:40] for x in g(f"a-{i}-allowed").split(",")
                                         if x.strip()]})
        i += 1

    try:
        bw = max(1, min(8, int(g("block_weeks") or 4)))
    except ValueError:
        bw = 4
    mults = []
    for x in g("multipliers").replace(";", ",").split(","):
        try:
            mults.append(max(0.3, min(1.5, float(x))))
        except ValueError:
            continue
    mults = (mults + [1.0] * bw)[:bw]

    with Session(engine) as session:
        prog = program_runner.active_program(session)
        if not prog:
            return RedirectResponse("/program", status_code=303)
        prog.title = g("title")[:120] or prog.title
        prog.goal = g("goal")[:1000]
        sd, ed = _parse_date(g("start")), _parse_date(g("end"))
        if sd:
            prog.start_date = datetime.combine(sd, time.min)
        prog.end_date = datetime.combine(ed, time.min) if ed else None
        prog.block_weeks = bw
        prog.block_multipliers_json = json.dumps(mults)
        prog.template_json = json.dumps(template)
        prog.routines_json = json.dumps(routines)
        prog.absences_json = json.dumps(absences)
        prog.equipment_json = json.dumps([x.strip()[:60] for x in g("equipment").splitlines()
                                          if x.strip()])
        prog.notes = g("notes")[:3000]
        session.add(prog)
        session.commit()
    st, err = await _program_recalc_now("impostazioni")
    return _program_redirect(msg="Impostazioni salvate" + (f" · {_recalc_msg(st)}" if st else ""),
                             error=err)


def _recalc_msg(st: dict | None) -> str:
    if not st:
        return "nessun programma attivo"
    parts = [f"{st[k]} {lbl}" for k, lbl in (("created", "create"), ("updated", "aggiornate"),
                                              ("proposals", "proposte"), ("removed", "tolte"),
                                              ("linked", "agganciate")) if st.get(k)]
    return "ricalcolo: " + (", ".join(parts) or "nessuna modifica")


@app.post("/program/recalc", dependencies=[Depends(require_auth)])
async def program_recalc():
    st, err = await _program_recalc_now("manuale")
    return _program_redirect(msg=_recalc_msg(st) if not err else "", error=err)


@app.post("/program/session/{sid}/{action}", dependencies=[Depends(require_auth)])
async def program_session_action(sid: int, action: str, request: Request):
    form = await request.form()
    if action == "skip":     # "Non la faccio": motivo, nota, ed eventualmente tutta la settimana
        ok = program_runner.skip_session(sid, str(form.get("motive") or ""),
                                         str(form.get("note") or "")[:200],
                                         form.get("scope") == "week")
    else:
        fn = {"unskip": program_runner.unskip_session,
              "accept": program_runner.accept_proposal,
              "reject": program_runner.reject_proposal}.get(action)
        ok = bool(fn and fn(sid))
    if not ok:
        return _program_redirect(error="Azione non valida su questa seduta")
    if action in ("skip", "unskip"):
        st, err = await _program_recalc_now("seduta saltata" if action == "skip" else "seduta ripristinata")
        return _program_redirect(msg=_recalc_msg(st) if not err else "", error=err)
    if action == "accept" and nutrition.is_configured():
        _fire_and_forget(nutrition.recompute_and_push())
    return _program_redirect(msg="Proposta accettata" if action == "accept"
                             else "Ok, la seduta resta com'era")


def _program_action_label(a: dict, names: dict | None = None) -> str:
    """Etichetta leggibile di un'azione; `names` = {session_id: "Sab 24/10 titolo"}."""
    t = a.get("tipo")
    if a.get("session_id") is not None:
        sess = (names or {}).get(a.get("session_id"), f"seduta {a.get('session_id')}")
        if t == "salta":
            return f"salta {sess}"
        if t == "modifica_seduta":
            return (f"adatta {sess} → «{a.get('title') or '…'}»"
                    + (f", {a.get('duration_min')}′" if a.get("duration_min") else "")
                    + (f", RPE {a.get('rpe')}" if a.get("rpe") is not None else ""))
    if t == "assenza":
        ok = ", ".join(a.get("allowed") or []) or "nessun allenamento"
        return f"assenza {a.get('start')} → {a.get('end')} ({ok})"
    if t == "rimuovi_assenza":
        return f"togli assenza #{a.get('index')}"
    if t == "slot":
        ch = {k: v for k, v in a.items() if k not in ("tipo", "slot_id")}
        return f"slot {a.get('slot_id')}: {json.dumps(ch, ensure_ascii=False)}"
    if t == "attrezzatura":
        return ("attrezzatura " + " ".join([f"+{x}" for x in a.get("aggiungi") or []]
                                           + [f"−{x}" for x in a.get("rimuovi") or []]))
    if t == "fine":
        return f"fine programma {a.get('date')}"
    if t == "nota":
        return f"nota: {a.get('testo')}"
    return str(t)


@app.post("/program/apply", dependencies=[Depends(require_auth)])
async def program_apply(request: Request):
    """Applica le azioni confermate nella chat del coach e ricalcola."""
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "richiesta non valida"}, status_code=400)
    done = program_runner.apply_actions(body.get("actions") or [])
    if not done:
        return JSONResponse({"done": [], "msg": "Nessuna modifica applicabile"})
    st, err = await _program_recalc_now("chat")
    return JSONResponse({"done": done, "msg": err or _recalc_msg(st)})


def _session_load(sport: str, minutes: float, rpe: float | None = None) -> float:
    """Estimated TRIMP load of a planned/proposed session (no HR by definition)."""
    rest_hr, max_hr, sex = _hr_anchors()
    return formmod.activity_load(None, minutes, sport, rpe, rest_hr, max_hr, sex)


def _plan_edit_context(session, plan, sessions) -> dict:
    """What the AI needs to edit a plan in progress: goal, today, every session
    with its state/load and (for done ones) what was actually done, plus form and
    athlete profile."""
    rows = []
    for s in sessions:
        row = {"session_id": s.id, "titolo": s.title, "sport": s.sport,
               "durata_min": s.duration_min,
               "data": s.date.date().isoformat() if s.date else None,
               "carico_trimp": round(_session_load(s.sport, s.duration_min)),
               "stato": "fatta" if s.done else "da fare"}
        if s.done and s.workout_id:
            w = session.get(Workout, s.workout_id)
            if w:
                row["svolto"] = {"cosa": (w.notes or "")[:400] or None,
                                 "fc_media": round(w.avg_hr) if w.avg_hr else None,
                                 "durata_min": round((w.moving_s or 0) / 60)}
        rows.append(row)
    _, form_summary = _fitness_series()
    return {"obiettivo_del_piano": plan.goal, "titolo_del_piano": plan.title,
            "oggi": date.today().isoformat(), "sessioni": rows,
            "atleta": profilemod.ai_context(), "forma_attuale": form_summary}


@app.post("/plans/{plan_id}/chat", dependencies=[Depends(require_auth)])
async def plan_chat(plan_id: int, request: Request):
    """Edit a plan in progress by chat ("mi metti giovedì un corpo libero?"). The
    AI weighs what's been done and what's coming; it returns a reply plus proposed
    actions (add/modify/remove) the UI applies only on confirmation."""
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "richiesta non valida"}, status_code=400)
    msg = str(body.get("message", "")).strip()[:2000]
    if not msg:
        return JSONResponse({"error": "nessuna domanda"}, status_code=400)

    with Session(engine) as session:
        plan = session.get(TrainingPlan, plan_id)
        if not plan:
            return JSONResponse({"error": "piano non trovato"}, status_code=404)
        conv = session.get(Conversation, plan.conversation_id) if plan.conversation_id else None
        if conv is None:
            conv = Conversation(title=f"Piano · {plan.title}"[:60])
            session.add(conv)
            session.commit()
            session.refresh(conv)
            plan.conversation_id = conv.id
            session.add(plan)
            session.commit()
        conv_id = conv.id
        sessions = session.exec(select(PlanSession).where(
            PlanSession.plan_id == plan_id).order_by(PlanSession.order)).all()
        ctx = _plan_edit_context(session, plan, sessions)
        prior = list(session.exec(select(ChatMessage)
                                  .where(ChatMessage.conversation_id == conv_id)
                                  .order_by(ChatMessage.created_at)))
    history = ([{"role": m.role, "content": m.content} for m in prior]
               + [{"role": "user", "content": msg}])[-12:]
    try:
        out = await anthropic_client.chat_plan(ctx, history)
    except anthropic_client.AnthropicError as e:
        return JSONResponse({"error": str(e)}, status_code=502)

    reply = out.get("risposta") or ""
    actions = _valid_plan_actions(out.get("azioni") or [], {s.id for s in sessions},
                                  {s.id for s in sessions if s.done})
    # quantify each proposed session so the confirm card can show its load
    for a in actions:
        if a["tipo"] in ("aggiungi", "modifica") and a.get("durata_min"):
            a["carico_trimp"] = round(_session_load(a.get("sport", ""), a["durata_min"]))

    stored = reply
    if actions:
        stored += "\n\n[Proposte] " + "; ".join(_action_label(a) for a in actions)
    with Session(engine) as session:
        session.add(ChatMessage(conversation_id=conv_id, role="user", content=msg))
        session.add(ChatMessage(conversation_id=conv_id, role="assistant", content=stored))
        c = session.get(Conversation, conv_id)
        c.updated_at = datetime.utcnow()
        session.add(c)
        session.commit()
    return JSONResponse({"reply": reply, "actions": actions})


def _valid_plan_actions(actions: list, ids: set, done_ids: set) -> list:
    """Keep only well-formed actions that don't touch a done session."""
    out = []
    for a in actions if isinstance(actions, list) else []:
        if not isinstance(a, dict):
            continue
        tipo = a.get("tipo")
        if tipo == "aggiungi":
            if a.get("title") and a.get("durata_min"):
                out.append(a)
        elif tipo in ("modifica", "rimuovi"):
            sid = a.get("session_id")
            if sid in ids and sid not in done_ids:
                out.append(a)
    return out


def _action_label(a: dict) -> str:
    if a["tipo"] == "aggiungi":
        return f"aggiungi «{a.get('title')}» ({a.get('sport')}, {a.get('durata_min')}min) il {a.get('date')}"
    if a["tipo"] == "rimuovi":
        return f"rimuovi sessione {a.get('session_id')}"
    return f"modifica sessione {a.get('session_id')}"


@app.post("/plans/{plan_id}/apply", dependencies=[Depends(require_auth)])
def plan_apply(plan_id: int, actions: str = Form(...)):
    """Apply confirmed plan edits (add/modify/remove sessions)."""
    try:
        parsed = json.loads(actions)
    except json.JSONDecodeError:
        return RedirectResponse(f"/plans/{plan_id}?{urlencode({'msg': 'Nessuna modifica'})}",
                                status_code=303)
    with Session(engine) as session:
        plan = session.get(TrainingPlan, plan_id)
        if not plan:
            return RedirectResponse("/plans", status_code=303)
        sess = session.exec(select(PlanSession).where(
            PlanSession.plan_id == plan_id)).all()
        by_id = {s.id: s for s in sess}
        done_ids = {s.id for s in sess if s.done}
        next_order = max((s.order for s in sess), default=-1) + 1
        n = 0
        for a in _valid_plan_actions(parsed, set(by_id), done_ids):
            if a["tipo"] == "aggiungi":
                d = _parse_date(str(a.get("date", "")))
                session.add(PlanSession(
                    plan_id=plan_id, order=next_order, day_label=str(a.get("day", ""))[:40],
                    date=datetime.combine(d, time(12, 0)) if d else None,
                    title=str(a.get("title", ""))[:200], sport=google_health.canonical_sport(str(a.get("sport", "")))[:60],
                    duration_min=int(a.get("durata_min") or 0),
                    description=str(a.get("description", ""))))
                next_order += 1
                n += 1
            elif a["tipo"] == "rimuovi":
                session.delete(by_id[a["session_id"]])
                n += 1
            elif a["tipo"] == "modifica":
                ps = by_id[a["session_id"]]
                if "title" in a:
                    ps.title = str(a["title"])[:200]
                if "sport" in a:
                    ps.sport = google_health.canonical_sport(str(a["sport"]))[:60]
                if "durata_min" in a:
                    try:
                        ps.duration_min = max(0, int(a["durata_min"]))
                    except (TypeError, ValueError):
                        pass
                if "description" in a:
                    ps.description = str(a["description"])
                if "date" in a:
                    d = _parse_date(str(a["date"]))
                    ps.date = datetime.combine(d, time(12, 0)) if d else ps.date
                session.add(ps)
                n += 1
        session.commit()
    return RedirectResponse(
        f"/plans/{plan_id}?{urlencode({'msg': f'{n} modifica/e applicata/e al piano'})}",
        status_code=303)


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


@app.post("/plans/{plan_id}/session/{sid}/delete", dependencies=[Depends(require_auth)])
def plan_session_delete(plan_id: int, sid: int):
    """Remove a single session from the plan, leaving the plan (and any real
    workout already logged for it) intact."""
    with Session(engine) as session:
        ps = session.get(PlanSession, sid)
        if ps and ps.plan_id == plan_id:
            session.delete(ps)
            session.commit()
    return RedirectResponse(
        f"/plans/{plan_id}?{urlencode({'msg': 'Sessione rimossa dal piano'})}",
        status_code=303)


@app.post("/plans/{plan_id}/session/{sid}/edit", dependencies=[Depends(require_auth)])
def plan_session_edit(plan_id: int, sid: int, title: str = Form(""),
                      sport: str = Form(""), durata_min: str = Form(""),
                      date_: str = Form("", alias="date"), description: str = Form(""),
                      next: str = Form("")):
    """Manual edit; empty fields are left unchanged. On a program session the
    edit also counts as "keep it like this": the engine won't rewrite it unless
    the weekly budget changes a lot."""
    with Session(engine) as session:
        ps = session.get(PlanSession, sid)
        if ps and ps.plan_id == plan_id:
            ps.title = title.strip() or ps.title
            if sport.strip():
                ps.sport = google_health.canonical_sport(sport)
            try:
                ps.duration_min = max(0, int(durata_min)) if durata_min.strip() else ps.duration_min
            except ValueError:
                pass
            d = _parse_date(date_)
            if d:
                ps.date = datetime.combine(d, time(12, 0))
            ps.description = description.strip() or ps.description
            if ps.kind:
                ps.reason = "modificata a mano"
                ps.proposal_json = json.dumps({"rejected": True, "rpe": ps.rpe_target,
                                               "duration": ps.duration_min})
            session.add(ps)
            session.commit()
    back = _safe_next(next, f"/plans/{plan_id}")
    sep = "&" if "?" in back else "?"
    return RedirectResponse(f"{back}{sep}{urlencode({'msg': 'Sessione aggiornata'})}",
                            status_code=303)


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
        "nutrition_cfg": profilemod.nutrition_cfg(),
        "planmydinner_on": nutrition.is_configured(),
        "credit": ai_usage.credit_status(),
        "message": request.query_params.get("msg"),
        "error": request.query_params.get("error"),
    })


@app.post("/settings/credit", dependencies=[Depends(require_auth)])
def settings_credit(amount: str = Form(""), since: str = Form(""), alert: str = Form(""),
                    price_in: str = Form(""), price_cached: str = Form(""),
                    price_out: str = Form("")):
    """Credito OpenAI caricato (per stimare il residuo) e prezzi del modello."""
    ai_usage.save_credit(amount, since, alert, price_in, price_cached, price_out)
    return RedirectResponse(f"/settings?{urlencode({'msg': 'Credito AI salvato'})}#credito",
                            status_code=303)


@app.post("/settings/profile", dependencies=[Depends(require_auth)])
def settings_profile(height_cm: str = Form(""), weight_kg: str = Form(""),
                     birth_year: str = Form(""), sex: str = Form(""),
                     rest_hr: str = Form(""), max_hr: str = Form(""),
                     ai_notes: str = Form("")):
    profilemod.save({"height_cm": height_cm, "weight_kg": weight_kg,
                     "birth_year": birth_year, "sex": sex if sex in ("M", "F") else "",
                     "rest_hr": rest_hr, "max_hr": max_hr, "ai_notes": ai_notes})
    return RedirectResponse(
        f"/settings?{urlencode({'msg': 'Dati personali salvati (rivedi la Forma: il carico è ricalcolato)'})}",
        status_code=303)


@app.post("/settings/nutrition", dependencies=[Depends(require_auth)])
def settings_nutrition(goal: str = Form("cut"), tdee_basis: str = Form("estimate"),
                       adjust_pct: str = Form(""), activity_factor: str = Form(""),
                       protein_per_kg_lean: str = Form("")):
    """Save the meal-plan goal config (drives the planmydinner targets)."""
    profilemod.save_nutrition_cfg({
        "goal": goal, "tdee_basis": tdee_basis, "adjust_pct": adjust_pct,
        "activity_factor": activity_factor, "protein_per_kg_lean": protein_per_kg_lean})
    return RedirectResponse(
        f"/settings?{urlencode({'msg': 'Obiettivo alimentare salvato'})}", status_code=303)


@app.get("/api/nutrition/targets", dependencies=[Depends(require_auth)])
async def api_nutrition_targets():
    """Preview the kcal/macro targets computed from the latest weigh-in + goal.
    Uses the measured TDEE from Google when available, else the BMR estimate."""
    res = await nutrition.build_targets_auto()
    if not res:
        return JSONResponse({"error": "Serve una pesata con BMR (sali sulla bilancia)"},
                            status_code=400)
    return JSONResponse(res)


@app.get("/api/nutrition/today", dependencies=[Depends(require_auth)])
async def api_nutrition_today():
    """Nutrizione di oggi vs obiettivo (da planmydinner) per la card dashboard."""
    data = await nutrition.today_nutrition()
    return JSONResponse(data or {})


@app.post("/nutrition/push-targets", dependencies=[Depends(require_auth)])
async def nutrition_push_targets():
    """Compute the targets and write them into planmydinner's planner rules."""
    if not nutrition.is_configured():
        return JSONResponse({"error": "planmydinner non configurato"}, status_code=400)
    res = await nutrition.build_targets_auto()
    if not res:
        return JSONResponse({"error": "Serve una pesata con BMR (sali sulla bilancia)"},
                            status_code=400)
    daily = nutrition.periodized_map(res["targets"]["kcal"])
    allow_upscale = (res["basis"].get("goal") != "cut") or bool(daily)
    ok = await nutrition.push_targets(res["targets"], allow_upscale=allow_upscale, daily=daily)
    if not ok:
        return JSONResponse({"error": "planmydinner ha rifiutato o non risponde"},
                            status_code=502)
    return JSONResponse({"status": "ok", "daily_days": len(daily), **res})


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
        try:
            reconcile_plan_duplicates()
        except Exception as e:
            logger.warning("Plan duplicate reconcile failed: %s", e)
        _kick_program("nuova attività")
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


# fields Home Assistant may send -> BodyMeasure columns (aliases accepted)
_BODY_FIELDS = {
    "weight": "weight_kg", "weight_kg": "weight_kg", "peso": "weight_kg",
    "impedance": "impedance", "impedenza": "impedance",
    "bmi": "bmi", "body_fat": "body_fat", "fat": "body_fat", "massa_grassa": "body_fat",
    "water": "water_pct", "water_pct": "water_pct", "acqua": "water_pct",
    "bone": "bone_kg", "bone_mass": "bone_kg", "massa_ossea": "bone_kg",
    "muscle": "muscle_kg", "muscle_mass": "muscle_kg", "massa_muscolare": "muscle_kg",
    "visceral": "visceral", "grasso_viscerale": "visceral",
    "bmr": "bmr", "metabolic_age": "metabolic_age", "eta_metabolica": "metabolic_age",
}


@app.post("/webhook/body")
async def webhook_body(request: Request, background: BackgroundTasks):
    """Smart-scale reading pushed by Home Assistant. Validates a shared token,
    stores a BodyMeasure (dedup within a few minutes of settling pushes) and
    refreshes the profile weight so load/FTP/energy use the real value. When
    planmydinner is configured, the meal-plan targets are recomputed and pushed
    in the background (so the webhook answers HA immediately)."""
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid JSON")
    received = payload.get("token") or request.headers.get("x-webhook-token", "")
    if not settings.body_webhook_token or \
            not secrets.compare_digest(str(received), settings.body_webhook_token):
        logger.warning("Body webhook with invalid token rejected")
        raise HTTPException(401, "Invalid webhook token")

    def num(v):
        try:
            f = float(v)
            return f if f == f else None   # drop NaN
        except (TypeError, ValueError):
            return None

    vals = {col: num(payload[k]) for k, col in _BODY_FIELDS.items()
            if k in payload and num(payload[k]) is not None}
    if not vals.get("weight_kg"):
        return JSONResponse({"error": "peso mancante"}, status_code=400)

    # The scale only sends weight + impedance instantly; the other metrics are
    # derived (ESPHome recomputes them on a slow poll, so pushed values are
    # often stale/missing). We recompute them here from weight+impedance+profile
    # so every reading is complete and internally consistent. Computed values
    # are authoritative and override anything HA may have sent.
    prof = profilemod.load()
    derived = bodycomp.compute(vals.get("weight_kg"), vals.get("impedance"),
                               prof.get("height_cm"), profilemod.age(prof),
                               prof.get("sex"))
    vals.update(derived)

    now = datetime.utcnow()
    with Session(engine) as session:
        recent = session.exec(select(BodyMeasure).where(
            BodyMeasure.measured_at >= now - timedelta(minutes=5))
            .order_by(BodyMeasure.measured_at.desc())).first()
        m = recent or BodyMeasure(measured_at=now)
        for col, v in vals.items():
            setattr(m, col, v)
        session.add(m)
        session.commit()
    profilemod.save({"weight_kg": vals["weight_kg"]})   # keep profile weight current
    logger.info("Body measure stored: %s kg (%s fields)", vals["weight_kg"], len(vals))
    # New weigh-in -> refresh the planmydinner targets (real BMR/lean changed).
    # Background so HA gets its 200 without waiting on Google + planmydinner.
    if nutrition.is_configured() and vals.get("bmr"):
        background.add_task(nutrition.recompute_and_push)
    return JSONResponse({"status": "ok"})


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
    except google_health.GoogleNotAuthenticatedError:
        return JSONResponse({"reconnect": True})   # token dead -> prompt re-login
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
        "metrics": [m for m in (latest("steps"), latest("resting_hr"), latest("hrv"),
                                latest("calories_burned")) if m],
        "sleep_h": (round(sum(n["asleep_min"] for n in nights) / len(nights) / 60, 1)
                    if nights else None),
        "nights": len(nights),
        "stale_days": _health_stale_days(data),   # connected but data stopped flowing
    }
    _HEALTH_CACHE["data"] = {"at": now, "payload": payload}
    return JSONResponse(payload)


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def dashboard(request: Request, win: str = "30", end: str = "",
              sport: str = "", sort: str = "date", order: str = "desc"):
    window = resolve_window(win, end, request.query_params.get("from", ""),
                            request.query_params.get("to", ""))
    with Session(engine) as session:
        sport = sport_label(sport) if sport else ""
        workouts = query_range(session, window["start"], window["end"], sport or None)
        all_sports = _sport_options(session)

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
        "credit": ai_usage.credit_status(),
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


@app.post("/analyze/chat", dependencies=[Depends(require_auth)])
async def analyze_chat(request: Request):
    """Coach chat on the dashboard and the program page (same coach as /health/chat)."""
    return await _coach_turn(request, "Coach · ")


@app.post("/analyze/plan", dependencies=[Depends(require_auth)])
async def analyze_plan(goal: str = Form(""), giorni: str = Form("7")):
    """Generate + save a plan from the coach chat's proposal (returns its URL)."""
    goal = goal.strip()
    if not goal:
        return JSONResponse({"error": "obiettivo mancante"}, status_code=400)
    try:
        n = max(1, min(60, int(giorni)))
    except (TypeError, ValueError):
        n = 7
    try:
        pid = await _generate_and_store_plan(goal, n, date.today())
    except anthropic_client.AnthropicError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    if not pid:
        return JSONResponse({"error": "AI non ha restituito un piano valido"}, status_code=502)
    return JSONResponse({"plan_id": pid, "url": f"/plans/{pid}"})


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
        # Drop stream + cached analysis, then the workout itself
        stream = session.get(WorkoutStream, workout_id)
        if stream:
            session.delete(stream)
        analysis = session.get(AiAnalysis, workout_id)
        if analysis:
            session.delete(analysis)
        # Blacklist the id so a delete stays deleted: both Google imports and
        # Wahoo rows re-appear at the next sync otherwise. A full resync bypasses
        # the blacklist if the user really wants everything back.
        if not session.get(IgnoredImport, workout_id):
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
        sports = _sport_options(session)
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
        w.sport = google_health.canonical_sport(sport) or w.sport
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
        merge = _merge_candidate(session, w)  # a real recording to fuse in (on confirm)

    streams_json = "null"
    streams = fitmod.load_streams(workout_id)
    if streams:
        streams_json = json.dumps(fitmod.downsample(streams))

    # Estimated power + climbs for bike rides without a power meter (GPS physics)
    est_power, climbs, ftp = None, [], None
    if streams and google_health._sport_family(w.sport) == "bike" and not w.avg_power:
        mass = cyclingmod.rider_mass(profilemod.load().get("weight_kg"))
        est_power = cyclingmod.ride_power_stats(streams, mass)
        climbs = cyclingmod.detect_climbs(streams)
        ftp = _estimated_ftp()

    # Time in HR zones + this ride's VO2max estimate (any sport with a HR stream)
    hr_zones = []
    ride_vo2max = None
    if streams and any(streams.get("hr") or []):
        rest_hr, max_hr, _ = _hr_anchors()
        hr_zones = fitmod.hr_zone_distribution(streams, rest_hr, max_hr)
        # Workload-vs-HR VO2max for THIS session (same method as the health page):
        # oxygen cost of the power (bike, real meter or GPS estimate) or pace (run)
        # at submaximal HR, extrapolated to HRmax.
        fam = google_health._sport_family(w.sport)
        weight = profilemod.load().get("weight_kg")
        workload = None
        if fam == "bike" and weight:
            real = streams.get("power") or []
            power = real if any(p for p in real) else \
                cyclingmod.estimate_power_series(streams, cyclingmod.rider_mass(weight))
            # coasting (0 W) counts as baseline cost so the best-effort window is the
            # one actually pushing watts, not a descent with the HR still high
            workload = [vo2maxmod.cycling_vo2(p, weight) or 3.5 for p in power]
        elif fam == "run":
            workload = [vo2maxmod.running_vo2(v) or 3.5 for v in (streams.get("speed") or [])]
        if workload:
            t = streams.get("t") or []
            hrs = streams.get("hr") or []
            ride_vo2max = (vo2maxmod.best_effort_vo2max(t, workload, hrs, rest_hr, max_hr, 600)
                           or vo2maxmod.best_effort_vo2max(t, workload, hrs, rest_hr, max_hr, 300))

    can_segment = bool(streams and streams.get("latlng")
                       and any(p for p in streams["latlng"])
                       and google_health._sport_family(w.sport) == "bike")
    # editing an existing segment on this ride's map
    edit_seg = None
    esid = request.query_params.get("edit_segment")
    if can_segment and esid and esid.isdigit():
        with Session(engine) as session:
            s = session.get(CustomSegment, int(esid))
        if s:
            edit_seg = {"id": s.id, "name": s.name,
                        "path": json.loads(s.path_json or "[]")}
    # Parziali (nuoto e attività a giri): da laps_json, con backfill pigro dal FIT
    laps = None
    if w.laps_json:
        try:
            laps = json.loads(w.laps_json)
        except (ValueError, TypeError):
            laps = None
    elif w.has_fit and w.fit_path:
        try:
            laps = fitmod.parse_laps(w.fit_path)
        except Exception:
            laps = None
        if laps:
            with Session(engine) as s2:
                wk = s2.get(Workout, workout_id)
                if wk:
                    wk.laps_json = json.dumps(laps)
                    s2.add(wk)
                    s2.commit()

    laps_summary = None
    if laps:
        tot_d = sum(l["distance_m"] for l in laps)
        tot_t = sum(l["time_s"] for l in laps)
        hrs = [l["avg_hr"] for l in laps if l["avg_hr"]]
        sw = [l["swolf"] for l in laps if l["swolf"] is not None]
        strk = [l["strokes"] for l in laps if l["strokes"] is not None]
        laps_summary = {
            "distance_m": round(tot_d),
            "time_s": round(tot_t, 1),
            "pace_100_s": round(tot_t / tot_d * 100) if tot_d else None,
            "avg_hr": round(sum(hrs) / len(hrs)) if hrs else None,
            "avg_swolf": round(sum(sw) / len(sw)) if sw else None,
            "strokes": sum(strk) if strk else None,
        }

    return templates.TemplateResponse(request, "workout.html", {
        "w": w,
        "streams_json": streams_json,
        "merge": merge, "can_segment": can_segment, "edit_seg": edit_seg,
        "est_power": est_power, "climbs": climbs, "ftp": ftp, "hr_zones": hr_zones,
        "ride_vo2max": ride_vo2max, "ride_vo2max_cat": _vo2max_category(ride_vo2max),
        "laps": laps, "laps_summary": laps_summary,
        "analysis_html": md.markdown(analysis.content, extensions=["tables"]) if analysis else None,
        "analysis_date": analysis.created_at if analysis else None,
        "message": request.query_params.get("msg"),
        "error": request.query_params.get("error"),
    })


# FTP proxy: best 20-min estimated power over recent rides × 0.95. Computing the
# power series for every bike ride is heavy, so cache the result for a while.
_FTP_CACHE: dict = {}
_FTP_TTL = timedelta(hours=6)


def _estimated_ftp() -> dict | None:
    hit = _FTP_CACHE.get("v")
    if hit and datetime.utcnow() - hit["at"] < _FTP_TTL:
        return hit["val"]
    cutoff = datetime.utcnow() - timedelta(days=120)
    weight = profilemod.load().get("weight_kg") or cyclingmod.DEFAULT_RIDER_KG
    mass = cyclingmod.rider_mass(profilemod.load().get("weight_kg"))
    ceiling = weight * cyclingmod.FTP_WKG_CEILING   # 20' oltre ~6 W/kg senza powermeter: non credibile
    with Session(engine) as session:
        rides = session.exec(select(Workout).where(Workout.start_date >= cutoff)).all()
    best20, best5 = [], []
    for r in rides:
        if google_health._sport_family(r.sport) != "bike" or r.avg_power:
            continue
        s = fitmod.load_streams(r.id)
        if not s:
            continue
        power = cyclingmod.estimate_power_series(s, mass)   # compute once, reuse
        b = cyclingmod.best_rolling_avg(s["t"], power, 1200)
        if b and round(b) <= ceiling:          # scarta le uscite fisiologicamente implausibili
            best20.append(round(b))
        b5 = cyclingmod.best_rolling_avg(s["t"], power, 300)
        if b5:
            best5.append(round(b5))
    # miglior 20' robusto (ignora un picco isolato) × 0.95, come il grafico eFTP
    best20_robust = cyclingmod.robust_best(best20)
    ftp = round(best20_robust * 0.95) if best20_robust else None
    wkg = round(ftp / weight, 2) if ftp else None
    # best 5-min power feeds the cycling VO2max estimate (artifact-robust max)
    peak5 = cyclingmod.robust_best(best5)
    val = ({"ftp": ftp, "wkg": wkg, "n_rides": len(best20), "best_5min": peak5}
           if ftp or peak5 else None)
    _FTP_CACHE["v"] = {"at": datetime.utcnow(), "val": val}
    return val


_PR_CACHE: dict = {}
_PR_TTL = timedelta(hours=12)


def _personal_records() -> list[dict]:
    """All-time personal bests: distances/duration/ascent/speed from the activity
    fields, power peaks (20'/5'/1') from the ride streams, and best climb VAM/gain
    from detected climbs. Each links to its workout. Cached (stream scan is heavy)."""
    hit = _PR_CACHE.get("v")
    if hit and datetime.utcnow() - hit["at"] < _PR_TTL:
        return hit["val"]
    mass = cyclingmod.rider_mass(profilemod.load().get("weight_kg"))
    with Session(engine) as session:
        ws = list(session.exec(select(Workout)))
        climbs = list(session.exec(select(ClimbEffort).where(ClimbEffort.kind == "climb")))
    fam = google_health._sport_family
    rides = [w for w in ws if fam(w.sport) == "bike"]
    runs = [w for w in ws if fam(w.sport) == "run"]
    recs: list[dict] = []

    def top(items):   # list[(value, workout)] -> highest
        return max(items, key=lambda t: t[0]) if items else (None, None)

    def add(label, value, w, value_str):
        if w is not None and value:
            recs.append({"label": label, "value": value_str,
                         "date": w.start_date, "workout_id": w.id})

    v, w = top([(x.distance_m, x) for x in rides if x.distance_m])
    add("Uscita più lunga (bici)", v, w, f"{(v or 0) / 1000:.1f} km")
    v, w = top([(x.distance_m, x) for x in runs if x.distance_m])
    add("Corsa più lunga", v, w, f"{(v or 0) / 1000:.1f} km")
    v, w = top([((x.moving_s or x.duration_s or 0), x) for x in ws])
    add("Attività più lunga", v, w, fmt_duration(int(v or 0)))
    v, w = top([(x.ascent_m, x) for x in ws if x.ascent_m])
    add("Più dislivello in un'uscita", v, w, f"{(v or 0):.0f} m")
    v, w = top([(x.avg_speed_ms, x) for x in rides
                if x.avg_speed_ms and (x.distance_m or 0) >= 10000])
    add("Velocità media più alta (bici ≥10 km)", v, w, f"{(v or 0) * 3.6:.1f} km/h")

    # power peaks across all rides (real stream if present, else GPS estimate)
    peaks = {1200: (0.0, None), 300: (0.0, None), 60: (0.0, None)}
    for r in rides:
        s = fitmod.load_streams(r.id)
        if not s:
            continue
        real = s.get("power") or []
        power = real if any(p for p in real) else cyclingmod.estimate_power_series(s, mass)
        if not power or not any(power):
            continue
        for win in peaks:
            b = cyclingmod.best_rolling_avg(s["t"], power, win)
            if b and b > peaks[win][0]:
                peaks[win] = (b, r)
    for win, label in ((1200, "Miglior potenza 20′"), (300, "Miglior potenza 5′"),
                       (60, "Miglior potenza 1′")):
        b, r = peaks[win]
        add(label, b, r, f"{round(b)} W")

    if climbs:
        cv = max(climbs, key=lambda c: c.vam or 0)
        if cv.vam:
            recs.append({"label": "Miglior VAM in salita", "value": f"{cv.vam} m/h",
                         "date": cv.date, "workout_id": cv.workout_id})
        cg = max(climbs, key=lambda c: c.gain_m or 0)
        if cg.gain_m:
            recs.append({"label": "Salita più dura (dislivello)",
                         "value": f"{cg.gain_m:.0f} m", "date": cg.date,
                         "workout_id": cg.workout_id})
    _PR_CACHE["v"] = {"at": datetime.utcnow(), "val": recs}
    return recs


def _vo2max_estimate(overview: dict) -> dict | None:
    """Estimated VO2max for the health page: Uth from resting/max HR (+ a trend
    from the resting-HR series) and, when recent rides give a 5-min power, an ACSM
    cycling estimate. The Pixel Watch's own Firstbeat value isn't exposed by the
    Google API, so this fills the gap. None when there's nothing to anchor on."""
    rest_anchor, max_hr, max_sex = _hr_anchors()
    metrics = overview.get("metrics", {}) or {}
    rest_series = (metrics.get("resting_hr") or {}).get("series") or []
    rest_latest = (metrics.get("resting_hr") or {}).get("latest") or rest_anchor
    hr_v = vo2maxmod.uth(rest_latest, max_hr)
    trend = vo2maxmod.trend_from_resting(rest_series, max_hr)
    cyc_v = best5 = None
    ftp = _estimated_ftp()
    if ftp and ftp.get("best_5min"):
        best5 = ftp["best_5min"]
        cyc_v = vo2maxmod.from_cycling_power(best5, profilemod.load().get("weight_kg"))
    garmin = _estimated_vo2max_activity()   # workload-vs-HR, Garmin/Firstbeat style
    if not (hr_v or cyc_v or garmin):
        return None
    headline = garmin or hr_v or cyc_v
    p = profilemod.load()
    by = p.get("birth_year")
    age = (date.today().year - int(by)) if by else None
    return {"garmin": garmin, "hr": hr_v, "cycling": cyc_v, "best_5min_w": best5,
            "headline": headline, "categoria": _vo2max_category(headline),
            "bands": list(vo2maxmod.bounds(age, max_sex)),
            "rest_hr": round(rest_latest) if rest_latest else None,
            "max_hr": round(max_hr) if max_hr else None, "trend": trend}


def _vo2max_category(vo2: float | None) -> str | None:
    """Garmin-style qualitative rating (Scarso…Superiore) for a VO2max value,
    using the athlete's age & sex from the profile."""
    if not vo2:
        return None
    p = profilemod.load()
    by = p.get("birth_year")
    age = (date.today().year - int(by)) if by else None
    _, _, sex = _hr_anchors()
    return vo2maxmod.category(vo2, age, sex)


_VO2_CACHE: dict = {}
_VO2_TTL = timedelta(hours=6)


def _estimated_vo2max_activity() -> float | None:
    """Garmin/Firstbeat-style VO2max from recent runs & rides: the oxygen cost of
    the pace (run) or power (ride) at a steady submaximal HR, extrapolated to
    HRmax through the HR reserve. Robust best of the per-activity medians.
    Scanning streams is heavy, so the result is cached (6h)."""
    hit = _VO2_CACHE.get("v")
    if hit and datetime.utcnow() - hit["at"] < _VO2_TTL:
        return hit["val"]
    rest_hr, max_hr, _ = _hr_anchors()
    weight = profilemod.load().get("weight_kg")
    mass = cyclingmod.rider_mass(weight)
    cutoff = datetime.utcnow() - timedelta(days=120)
    with Session(engine) as session:
        acts = session.exec(select(Workout).where(Workout.start_date >= cutoff)).all()
    per_activity = []
    for a in acts:
        fam = google_health._sport_family(a.sport)
        if fam not in ("run", "bike"):
            continue
        s = fitmod.load_streams(a.id)
        if not s or not any(s.get("hr") or []):
            continue
        hrs = s.get("hr") or []
        t = s.get("t") or []
        if fam == "run":
            vo2 = [vo2maxmod.running_vo2(v) or 3.5 for v in (s.get("speed") or [])]
        elif weight:
            real = s.get("power") or []
            power = real if any(p for p in real) else cyclingmod.estimate_power_series(s, mass)
            vo2 = [vo2maxmod.cycling_vo2(p, weight) or 3.5 for p in power]
        else:
            continue
        # hardest sustained effort of the session (10' window, then 5')
        est = (vo2maxmod.best_effort_vo2max(t, vo2, hrs, rest_hr, max_hr, 600)
               or vo2maxmod.best_effort_vo2max(t, vo2, hrs, rest_hr, max_hr, 300))
        if est:
            per_activity.append(est)
    val = cyclingmod.robust_best(per_activity) if per_activity else None
    _VO2_CACHE["v"] = {"at": datetime.utcnow(), "val": val}
    return val


def _attach_vo2max(overview: dict) -> dict:
    """Add a compact VO2max estimate to the overview so the AI can comment on it.
    Mutates and returns `overview`."""
    v = _vo2max_estimate(overview)
    if v and v.get("headline"):
        overview["vo2max_stimata"] = {
            "ml_kg_min": v["headline"], "categoria": v.get("categoria"),
            "metodo": ("attività: pace/potenza vs FC, stile Garmin"
                       if v.get("garmin") else "FC riposo/max (Uth)"),
            "nota": "stima di tendenza (±10-15%), non il valore dell'orologio"}
    return overview


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
    try:
        fused = reconcile_plan_duplicates()  # runs even if Google is down
        if fused:
            msg += f", {fused} doppioni del piano uniti"
    except Exception as e:
        logger.warning("Plan duplicate reconcile failed: %s", e)
    _kick_program("sync")
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


# Measured fields moved from the real recording into the manual/plan workout on a
# merge; everything else (name, sport, notes, rpe, manual flag, id) is kept.
_MEASURED_FIELDS = ("start_date", "duration_s", "moving_s", "distance_m", "ascent_m",
                    "avg_speed_ms", "max_speed_ms", "avg_hr", "max_hr", "avg_power",
                    "max_power", "np_power", "avg_cadence", "calories", "tss",
                    "intensity_factor")


def _can_merge(keep: Workout, absorb: Workout) -> bool:
    """True if `absorb` (a real recording) can fuse into `keep` (a data-less
    manual/plan workout) on the same day with a compatible sport."""
    return bool(
        keep and absorb and keep.id != absorb.id
        and keep.manual and not keep.has_fit and not keep.avg_hr and not keep.distance_m
        and not absorb.manual and (absorb.avg_hr or absorb.distance_m)
        and absorb.start_date.date() == keep.start_date.date()
        and google_health._sameday_sport_ok(keep.sport, absorb.sport))


def _nomerge_key(keep_id: int, absorb_id: int) -> str:
    return f"nomerge:{keep_id}:{absorb_id}"


def _merge_candidate(session, keep: Workout) -> Workout | None:
    """The real recording (e.g. a Pixel-Watch session via Google Health) that
    could be `keep`'s actual data — same day, compatible sport, data-rich.
    Pairs the user chose to keep separate are skipped so a sync can't re-nag."""
    if not (keep.manual and not keep.has_fit and not keep.avg_hr and not keep.distance_m):
        return None
    day_lo = datetime.combine(keep.start_date.date(), time.min)
    day_hi = datetime.combine(keep.start_date.date(), time.max)
    cands = [x for x in session.exec(select(Workout).where(
        Workout.start_date >= day_lo, Workout.start_date <= day_hi))
        if _can_merge(keep, x) and not get_setting(_nomerge_key(keep.id, x.id))]
    # prefer one with HR, then the richest (most calories)
    cands.sort(key=lambda x: (x.avg_hr is None, -(x.calories or 0)))
    return cands[0] if cands else None


def _merge_workouts(session, keep: Workout, absorb: Workout) -> None:
    """Move `absorb`'s measured data + streams into `keep`, then delete `absorb`.
    `keep` keeps its id, so a PlanSession link and the description survive."""
    for f in _MEASURED_FIELDS:
        if (v := getattr(absorb, f)):
            setattr(keep, f, v)
    if absorb.has_fit:
        keep.has_fit, keep.fit_path = True, absorb.fit_path
    keep.updated_at = datetime.utcnow()

    stream = session.get(WorkoutStream, absorb.id)
    old = session.get(WorkoutStream, keep.id)
    if old:
        session.delete(old)
    if stream:
        blob, n = stream.data, stream.n_records
        session.delete(stream)
        session.flush()  # free the PK before re-inserting under keep.id
        session.add(WorkoutStream(workout_id=keep.id, data=blob, n_records=n))
    for wid in (keep.id, absorb.id):  # analyses are stale after the merge
        if (a := session.get(AiAnalysis, wid)):
            session.delete(a)
    # absorb's data now lives on keep; a Google import would otherwise re-appear at
    # the next sync (its uid = absorb.id), so blacklist it — same as manual delete
    if _is_google_import(absorb) and not session.get(IgnoredImport, absorb.id):
        session.add(IgnoredImport(id=absorb.id))
    session.delete(absorb)
    session.add(keep)
    session.commit()


def reconcile_plan_duplicates() -> int:
    """Collapse duplicate rows of a completed plan session into its placeholder.

    Marking a plan session done stamps a manual workout with a placeholder time
    (noon / the scheduled hour). The same activity recorded for real — a Wahoo
    third-party stub, a Google import, an uploaded FIT — lands as a SEPARATE row
    at the real hour, so a time-window match misses it and the list shows two
    activities. Fold every same-day, compatible-sport recording into the plan
    placeholder, which keeps its id so the plan link and description survive.

    Unlike the UI merge suggestion (`_merge_candidate`), this runs automatically
    on every sync and does not require the placeholder to still be empty — a plan
    row already enriched with partial data still absorbs the real recording. Pairs
    the user chose to keep separate (`_nomerge_key`) are respected. Returns the
    number of rows folded in.
    """
    merged = 0
    with Session(engine) as session:
        plan_wids = {ps.workout_id for ps in session.exec(
            select(PlanSession).where(PlanSession.workout_id != None)).all()  # noqa: E711
            if ps.workout_id}
        for ps in session.exec(select(PlanSession).where(
                PlanSession.workout_id != None)).all():  # noqa: E711
            keep = session.get(Workout, ps.workout_id)
            if not keep or not keep.start_date:
                continue
            day_lo = datetime.combine(keep.start_date.date(), time.min)
            day_hi = datetime.combine(keep.start_date.date(), time.max)
            dups = [x for x in session.exec(select(Workout).where(
                        Workout.start_date >= day_lo, Workout.start_date <= day_hi)).all()
                    # another real recording of the same session: not the placeholder
                    # itself, not a second plan placeholder, same sport family, and
                    # not a pair the user explicitly split
                    if x.id != keep.id and x.id not in plan_wids
                    and not x.manual
                    and google_health._sameday_sport_ok(keep.sport, x.sport)
                    and not get_setting(_nomerge_key(keep.id, x.id))]
            # fondi per ultimo ciò che ha il FIT: i suoi dati (autorevoli, coi
            # parziali) devono prevalere sul riepilogo Wahoo/Google
            dups.sort(key=lambda x: bool(x.has_fit))
            for x in dups:
                _merge_workouts(session, keep, x)  # commits; keep keeps its id
                merged += 1
                logger.info("Plan dedupe: folded %s into plan workout %s (%s)",
                            x.id, keep.id, keep.sport)
    return merged


@app.post("/workout/{keep_id}/merge", dependencies=[Depends(require_auth)])
def workout_merge(keep_id: int, absorb_id: int = Form(...)):
    """Confirmed fuse of a real recording into a manual/plan workout."""
    with Session(engine) as session:
        keep = session.get(Workout, keep_id)
        absorb = session.get(Workout, absorb_id)
        if not _can_merge(keep, absorb):  # a stale button must not merge the wrong pair
            return RedirectResponse(
                f"/workout/{keep_id}?{urlencode({'error': 'Fusione non più valida'})}",
                status_code=303)
        _merge_workouts(session, keep, absorb)
    return RedirectResponse(
        f"/workout/{keep_id}?{urlencode({'msg': 'Allenamento fuso: dati reali e FC uniti alla descrizione'})}",
        status_code=303)


@app.post("/workout/{keep_id}/merge/dismiss", dependencies=[Depends(require_auth)])
def workout_merge_dismiss(keep_id: int, absorb_id: int = Form(...)):
    """Keep the two workouts separate: never suggest merging this pair again."""
    set_setting(_nomerge_key(keep_id, absorb_id), "1")
    return RedirectResponse(f"/workout/{keep_id}", status_code=303)


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
    # Collapse a same-day plan session (and its Wahoo/Google twin) into this FIT:
    # un FIT caricato prima che arrivassero il segnaposto del piano o la riga
    # Wahoo resterebbe altrimenti separato.
    try:
        reconcile_plan_duplicates()
        # se questo FIT è stato assorbito nel segnaposto del piano, segui l'id
        # sopravvissuto (che ora porta lo stesso fit_path) per il redirect
        with Session(engine) as s:
            if s.get(Workout, wid) is None:
                surv = s.exec(select(Workout).where(Workout.fit_path == fit_path)).first()
                if surv:
                    wid = surv.id
    except Exception as e:
        logger.warning("Reconcile after FIT upload failed: %s", e)
    _kick_program("FIT caricato")
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

    row = w.model_dump(exclude={"raw_summary", "fit_path", "updated_at", "laps_json"})
    # Parziali del nuoto: passali all'AI strutturati (ripetute + riepilogo), non
    # la stringa JSON grezza, così può leggere ritmo/SWOLF/stile/struttura.
    if w.laps_json:
        try:
            laps = json.loads(w.laps_json)
        except (ValueError, TypeError):
            laps = None
        if laps:
            row["parziali"] = [{k: lp.get(k) for k in
                                ("i", "distance_m", "time_s", "pace_100_s",
                                 "strokes", "stroke", "swolf", "avg_hr")} for lp in laps]
            tot_d = sum(l["distance_m"] for l in laps)
            tot_t = sum(l["time_s"] for l in laps)
            sw = [l["swolf"] for l in laps if l.get("swolf") is not None]
            row["parziali_riepilogo"] = {
                "distanza_m": round(tot_d), "tempo_s": round(tot_t),
                "passo_100_s": round(tot_t / tot_d * 100) if tot_d else None,
                "swolf_medio": round(sum(sw) / len(sw)) if sw else None,
                "n_ripetute": len(laps),
            }
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
