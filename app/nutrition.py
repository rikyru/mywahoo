"""Read-only integration with planmydinner (separate app) to bring nutrition
context — plan adherence, energy/macros of tracked meals, preferences — into the
health AI analysis and chat. Its API is open on the LAN; we only read.

planmydinner exposes a purpose-built /integration/summary (versioned) with daily
and average kcal + macros and adherence. Calories are for TRACKED meals only
(coverage over planned slots), not necessarily the full daily intake, so they are
labelled as such and never treated as a complete energy balance. planmydinner has
no meal-quality index, so quality is left to the AI to judge from the macro split,
protein-per-kg and the in-plan vs free/mensa ratio.
"""
import logging
from datetime import date, datetime, time, timedelta

import httpx
from sqlmodel import Session, select

from . import profile as profilemod
from .config import settings
from .db import BodyMeasure, PlanSession, engine

logger = logging.getLogger(__name__)


def is_configured() -> bool:
    return bool(settings.planmydinner_url)


# ---------------------------------------------------------------- targets
# Body-composition–driven kcal/macro targets, pushed to planmydinner so the
# meal plan is sized on measured data (real BMR + lean mass) instead of a
# generic formula. The goal (cut/maintain/bulk) and knobs are user-configurable
# in the profile; this is the single place that turns them into numbers.

def compute_targets(bmr: float | None, lean_kg: float | None, weight_kg: float | None,
                    goal: str, adjust_pct: float, activity_factor: float,
                    protein_per_kg_lean: float, tdee: float | None = None) -> dict | None:
    """kcal + macro targets from BMR/lean mass and the goal. `tdee` (measured
    daily burn, e.g. from Google) is used when given; otherwise TDEE is
    estimated as BMR × activity_factor. Returns None if BMR is unknown."""
    if not bmr or bmr <= 0:
        return None
    base = tdee if (tdee and tdee > 0) else bmr * (activity_factor or 1.45)

    if goal == "cut":
        kcal = base * (1 - (adjust_pct or 0) / 100.0)
    elif goal == "bulk":
        kcal = base * (1 + (adjust_pct or 0) / 100.0)
    else:  # maintain
        kcal = base
    kcal = max(kcal, bmr * 1.05)            # never prescribe below ~BMR

    # protein on lean mass (fallback: 75% of body weight as a lean proxy)
    lean = lean_kg if (lean_kg and lean_kg > 0) else ((weight_kg or 0) * 0.75)
    protein_g = min((protein_per_kg_lean or 2.0) * lean, 230) if lean else 0.0
    fat_g = (kcal * 0.27) / 9.0             # 27% of energy from fat
    carbs_g = max((kcal - protein_g * 4 - fat_g * 9) / 4.0, 0.0)

    return {"kcal": round(kcal), "protein_g": round(protein_g),
            "carbs_g": round(carbs_g), "fat_g": round(fat_g)}


def _latest_body() -> BodyMeasure | None:
    with Session(engine) as session:
        return session.exec(select(BodyMeasure)
                            .order_by(BodyMeasure.measured_at.desc())).first()


def build_targets(tdee: float | None = None) -> dict | None:
    """Compute the current targets from the latest weigh-in + profile config.
    Returns {"targets": {...}, "basis": {...}} or None if there's no BMR yet."""
    b = _latest_body()
    p = profilemod.load()
    cfg = profilemod.nutrition_cfg()
    bmr = b.bmr if b else None
    weight = (b.weight_kg if b else None) or p.get("weight_kg")
    lean = None
    if b and b.weight_kg and b.body_fat is not None:
        lean = b.weight_kg * (1 - b.body_fat / 100.0)
    targets = compute_targets(bmr, lean, weight, cfg["goal"], cfg["adjust_pct"],
                              cfg["activity_factor"], cfg["protein_per_kg_lean"], tdee)
    if not targets:
        return None
    basis = {"goal": cfg["goal"], "adjust_pct": cfg["adjust_pct"], "bmr": bmr,
             "tdee": round(tdee) if tdee else round((bmr or 0) * cfg["activity_factor"]),
             "tdee_source": "misurato" if tdee else "stima (BMR×fattore)",
             "lean_kg": round(lean, 1) if lean else None,
             "protein_per_kg_lean": cfg["protein_per_kg_lean"]}
    return {"targets": targets, "basis": basis}


async def measured_tdee(days: int = 28) -> float | None:
    """Average daily energy burn from Google Health (BMR + activity = TDEE).
    Best-effort: returns None if Google isn't connected or has no burn data."""
    from datetime import date as _date, timedelta
    from . import google_health
    try:
        overview = await google_health.fetch_health_overview(
            _date.today() - timedelta(days=days - 1), _date.today())
    except google_health.GoogleHealthError:
        return None
    series = ((overview or {}).get("metrics") or {}).get("calories_burned", {}).get("series") or []
    vals = [p["value"] for p in series if p.get("value")]
    return sum(vals) / len(vals) if vals else None


async def build_targets_auto() -> dict | None:
    """build_targets honoring the configured TDEE basis: the measured Google
    burn only when the user opted in, otherwise the conservative BMR estimate
    (trackers tend to overcount, so estimate is the safer default)."""
    tdee = None
    if profilemod.nutrition_cfg().get("tdee_basis") == "measured":
        tdee = await measured_tdee()
    return build_targets(tdee)


# Stima kcal bruciate per minuto, per sport, usata per la periodizzazione:
# nei giorni con allenamento pianificato il target del giorno sale di conseguenza.
_KCAL_PER_MIN = {
    "bici": 9.0, "ciclismo": 9.0, "mtb": 9.0, "corsa": 10.0, "running": 10.0,
    "nuoto": 9.0, "camminata": 5.0, "trekking": 6.0, "escursione": 6.0,
    "forza": 6.0, "palestra": 6.0, "corpo libero": 6.0, "yoga": 3.0,
}


def _session_kcal(sport: str, minutes: int | None) -> float:
    return (minutes or 0) * _KCAL_PER_MIN.get((sport or "").strip().lower(), 7.0)


def periodized_daily(base_kcal: float, days: int = 7) -> dict:
    """Target per-giorno per i prossimi `days`: ai giorni con sessione pianificata
    (non ancora fatta) somma le kcal stimate dell'allenamento al target base.
    Mappa {data ISO: {kcal, training_note}}; i giorni senza allenamento sono
    omessi (useranno il target piatto)."""
    start = date.today()
    end = start + timedelta(days=days - 1)
    lo = datetime.combine(start, time.min)
    hi = datetime.combine(end, time.max)
    with Session(engine) as s:
        rows = s.exec(select(PlanSession).where(
            PlanSession.date != None,  # noqa: E711
            PlanSession.date >= lo, PlanSession.date <= hi,
            PlanSession.done == False)).all()  # noqa: E712
    by_date: dict = {}
    for ps in rows:
        iso = ps.date.date().isoformat()
        by_date.setdefault(iso, []).append(ps)
    out: dict = {}
    for iso, sessions in by_date.items():
        extra = sum(_session_kcal(p.sport, p.duration_min) for p in sessions)
        if extra <= 0:
            continue
        note = " + ".join(f"{p.sport} {p.duration_min}′" for p in sessions if p.duration_min)
        entry = {"kcal": round(base_kcal + extra)}
        if note:
            entry["training_note"] = note
        out[iso] = entry
    return out


async def recompute_and_push() -> bool:
    """Recompute targets (per the configured TDEE basis) and push them. Used
    both by the settings button and automatically after each weigh-in."""
    res = await build_targets_auto()
    if not res:
        return False
    # Periodizzazione: più kcal nei giorni di allenamento pianificato.
    daily = periodized_daily(res["targets"]["kcal"])
    # In a cut, portions may only shrink — MA nei giorni di allenamento la
    # periodizzazione deve poter aumentare le porzioni (carburante guadagnato),
    # quindi se ci sono bump consentiamo l'upscale.
    cut = profilemod.nutrition_cfg().get("goal") == "cut"
    allow_upscale = (not cut) or bool(daily)
    return await push_targets(res["targets"], allow_upscale=allow_upscale, daily=daily)


async def push_targets(targets: dict, allow_upscale: bool = True,
                       daily: dict | None = None) -> bool:
    """Send the kcal/macro targets to planmydinner.

    Prefers the dedicated integration endpoint (POST /integration/apply-targets),
    which stores the targets AND re-scales the current plan's portions to match.
    Falls back to writing the raw planner rules on older planmydinner builds that
    don't expose the endpoint yet, so the push works across the transition.
    `allow_upscale` is False in a cut: portions may only shrink, never grow.
    `daily` (optional): per-date targets for periodization (training days).
    """
    if not is_configured():
        return False
    base = settings.planmydinner_url.rstrip("/")
    prof = settings.planmydinner_profile
    body = {**targets, "allow_upscale": allow_upscale}
    if daily:
        body["daily"] = daily
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(f"{base}/integration/apply-targets",
                                     params={"profile_id": prof}, json=body)
            if resp.status_code == 200:
                logger.info("Applied nutrition targets via planmydinner integration: %s",
                            targets)
                return True
            if resp.status_code in (404, 405):
                # Older planmydinner: fall back to the raw planner-rules write.
                rules_targets = dict(targets)
                if not allow_upscale:
                    rules_targets["allow_upscale"] = 0.0
                resp = await client.put(f"{base}/planner/rules/{prof}",
                                        json={"nutrition_targets": rules_targets})
                if resp.status_code == 200:
                    logger.info("Pushed nutrition targets to planmydinner (rules): %s",
                                targets)
                    return True
        logger.warning("planmydinner rejected targets (%s): %s",
                       resp.status_code, resp.text[:200])
    except httpx.HTTPError as e:
        logger.warning("Failed pushing targets to planmydinner: %s", e)
    return False


async def _get(client: httpx.AsyncClient, base: str, path: str, params: dict):
    try:
        resp = await client.get(base + path, params=params)
        return resp.json() if resp.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None


async def fetch_nutrition(start: date, end: date) -> dict | None:
    """Compact nutrition view for the window, or None when planmydinner isn't
    configured / has no data for the period (so the AI simply omits it)."""
    if not is_configured():
        return None
    base = settings.planmydinner_url.rstrip("/")
    prof = settings.planmydinner_profile

    async with httpx.AsyncClient(timeout=15) as client:
        summary = await _get(client, base, "/integration/summary",
                             {"profile_id": prof, "start_date": start.isoformat(),
                              "end_date": end.isoformat()})
        profile = await _get(client, base, f"/profiles/{prof}", {})

    return _shape(summary, profile, start, end, profilemod.load().get("weight_kg"))


def _shape(summary, profile, start: date, end: date, weight_kg) -> dict | None:
    """Turn the raw /integration/summary (+ profile) into the compact Italian view
    the AI prompts consume, or None when there's no usable data."""
    summary = summary if isinstance(summary, dict) else {}
    adh = summary.get("adherence") or {}
    avg = summary.get("averages") or {}
    days = summary.get("days") or []
    if not (avg.get("days_with_data") or adh.get("planned_slots") or adh.get("free_meals")):
        return None

    out: dict = {"periodo": f"{start.isoformat()} → {end.isoformat()}"}

    if adh:
        score = adh.get("adherence_score") or 0
        out["aderenza_al_piano"] = {
            "punteggio_pct": round(score * 100) if score <= 1 else round(score),
            "pasti_pianificati": adh.get("planned_slots"),
            "pasti_da_piano_consumati": adh.get("in_plan_consumed"),
            "pasti_liberi_usati": adh.get("free_meals"),
            "quota_pasti_liberi": adh.get("free_meal_quota"),
            "pasti_saltati": adh.get("not_eaten_slots"),
        }

    if avg.get("days_with_data"):
        prot = avg.get("protein_g")
        macros = {
            "nota": "valori dei PASTI TRACCIATI (pasti pianificati), non "
                    "necessariamente l'intero introito giornaliero",
            "giorni_con_dati": avg.get("days_with_data"),
            "kcal_medie": _r(avg.get("kcal")),
            "proteine_g_medie": _r(prot),
            "carboidrati_g_medi": _r(avg.get("carbs_g")),
            "grassi_g_medi": _r(avg.get("fat_g")),
        }
        # protein per kg: the single most useful quality signal for an athlete
        if prot and weight_kg:
            macros["proteine_g_per_kg"] = round(prot / weight_kg, 2)
        # per-day energy/macros so the AI can see variability, not just the mean
        macros["per_giorno"] = [
            {"data": d.get("date"), "kcal": _r((d.get("nutrition") or {}).get("kcal")),
             "proteine_g": _r((d.get("nutrition") or {}).get("protein_g")),
             "carboidrati_g": _r((d.get("nutrition") or {}).get("carbs_g")),
             "grassi_g": _r((d.get("nutrition") or {}).get("fat_g")),
             "pasti_liberi": d.get("free_meals")}
            for d in days if (d.get("nutrition") or {}).get("kcal") is not None]
        out["alimentazione_tracciata"] = macros

    if profile:
        prefs = {k: v for k in ("preferences", "allergies", "excluded_foods")
                 if (v := profile.get(k))}
        if prefs:
            out["profilo"] = prefs
    return out


def _r(v, nd: int = 0):
    """Round a possibly-None number, keeping None as None."""
    try:
        return round(float(v), nd) if v is not None else None
    except (TypeError, ValueError):
        return None
