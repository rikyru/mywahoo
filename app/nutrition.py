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

from . import google_health
from . import profile as profilemod
from .config import settings
from .db import BodyMeasure, PlanSession, Workout, engine

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


# Default weekly weight-change targets per goal, used to steer the feedback loop.
_TARGET_RATE_KG_WK = {"cut": -0.5, "bulk": 0.25, "maintain": 0.0}
_KCAL_PER_KG = 7700.0          # energy in ~1 kg of body mass
_WF_MAX_KCAL = 350.0           # cap per-cycle correction so it can't swing wildly


def weight_trend_kg_per_week(days: int = 28) -> dict | None:
    """Linear fit of the weigh-ins over the last `days` → kg/week. None unless
    there are enough readings spanning enough time to be meaningful."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    with Session(engine) as session:
        rows = session.exec(select(BodyMeasure)
                            .where(BodyMeasure.measured_at >= cutoff,
                                   BodyMeasure.weight_kg != None)  # noqa: E711
                            .order_by(BodyMeasure.measured_at)).all()
    pts = [(r.measured_at, r.weight_kg) for r in rows if r.weight_kg]
    if len(pts) < 4:
        return None
    span = (pts[-1][0] - pts[0][0]).total_seconds() / 86400.0
    if span < 14:
        return None
    x0 = pts[0][0]
    xs = [(t - x0).total_seconds() / 86400.0 for t, _ in pts]
    ys = [w for _, w in pts]
    n = len(xs)
    sx, sy = sum(xs), sum(ys)
    denom = n * sum(v * v for v in xs) - sx * sx
    if denom == 0:
        return None
    slope = (n * sum(a * b for a, b in zip(xs, ys)) - sx * sy) / denom   # kg/day
    return {"rate": slope * 7.0, "n": n, "span_days": round(span)}


def weight_feedback(base_tdee: float, cfg: dict) -> tuple:
    """How much to nudge the TDEE estimate so the ACTUAL weight trend matches the
    goal's target rate. Returns (kcal_correction, meta|None). Needs a real trend;
    capped so it self-corrects over weeks instead of oscillating."""
    if not cfg.get("weight_feedback", True) or not base_tdee:
        return 0.0, None
    tr = weight_trend_kg_per_week()
    if not tr:
        return 0.0, None
    goal = cfg.get("goal", "maintain")
    target = cfg.get("target_rate_kg_wk")
    if target is None:
        target = _TARGET_RATE_KG_WK.get(goal, 0.0)
    corr = (target - tr["rate"]) * _KCAL_PER_KG / 7.0          # kcal/day
    corr = max(-_WF_MAX_KCAL, min(_WF_MAX_KCAL, corr))
    meta = {"rate_reale_kg_sett": round(tr["rate"], 2),
            "rate_obiettivo_kg_sett": target, "correzione_kcal": round(corr),
            "pesate": tr["n"], "giorni": tr["span_days"]}
    return corr, meta


def build_targets(tdee: float | None = None) -> dict | None:
    """Compute the current targets from the latest weigh-in + profile config.
    Returns {"targets": {...}, "basis": {...}} or None if there's no BMR yet.

    Closes the weight→nutrition loop: the measured/estimated TDEE is corrected by
    the ACTUAL bodyweight trend, so if a cut stalls (or runs too fast) the kcal
    target self-adjusts, and the corrected targets are what get pushed to
    planmydinner."""
    b = _latest_body()
    p = profilemod.load()
    cfg = profilemod.nutrition_cfg()
    bmr = b.bmr if b else None
    weight = (b.weight_kg if b else None) or p.get("weight_kg")
    lean = None
    if b and b.weight_kg and b.body_fat is not None:
        lean = b.weight_kg * (1 - b.body_fat / 100.0)

    base = tdee if (tdee and tdee > 0) else ((bmr or 0) * cfg["activity_factor"])
    corr, wf_meta = weight_feedback(base, cfg) if base else (0.0, None)
    eff_tdee = (base + corr) if base else None

    targets = compute_targets(bmr, lean, weight, cfg["goal"], cfg["adjust_pct"],
                              cfg["activity_factor"], cfg["protein_per_kg_lean"],
                              tdee=eff_tdee)
    if not targets:
        return None
    source = "misurato" if tdee else "stima (BMR×fattore)"
    if wf_meta:
        source += f" + trend peso ({wf_meta['correzione_kcal']:+d} kcal)"
    basis = {"goal": cfg["goal"], "adjust_pct": cfg["adjust_pct"], "bmr": bmr,
             "tdee": round(eff_tdee) if eff_tdee else round((bmr or 0) * cfg["activity_factor"]),
             "tdee_source": source, "weight_feedback": wf_meta,
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
    s = (sport or "").strip().lower()
    rate = (_KCAL_PER_MIN.get(s)
            or _KCAL_PER_MIN.get(google_health.canonical_sport(s).lower(), 7.0))
    return (minutes or 0) * rate


def _agg_training(items: list[tuple]) -> dict:
    """items: [(iso_date, sport, minutes)] -> {iso: {"kcal": float, "parts": [str]}}
    sommando le kcal stimate per giorno."""
    by: dict = {}
    for iso, sport, mins in items:
        k = _session_kcal(sport, mins)
        if k <= 0:
            continue
        d = by.setdefault(iso, {"kcal": 0.0, "parts": []})
        d["kcal"] += k
        if mins:
            d["parts"].append(f"{sport} {mins}′")
    return by


def _periodize(base_kcal: float, planned: list[tuple], actual: list[tuple]) -> dict:
    """Core puro della periodizzazione. `planned`/`actual` sono liste
    (iso_date, sport, minuti): le sessioni PIANIFICATE non ancora fatte e le
    ATTIVITÀ realmente svolte. Per ogni giorno il surplus è il MASSIMO tra reale
    e pianificato (il reale vince a parità), così:
    - un giorno futuro è guidato dal piano (carburante in anticipo);
    - oggi prima di allenarti usa comunque il piano (pre-fuel);
    - quando ti alleni e segni la sessione «fatta» il surplus NON sparisce,
      perché subentra l'attività reale di quel giorno.
    Ritorna {iso: {kcal, training_note}}; i giorni senza allenamento sono omessi."""
    plan_by, real_by = _agg_training(planned), _agg_training(actual)
    out: dict = {}
    for iso in set(plan_by) | set(real_by):
        p, r = plan_by.get(iso), real_by.get(iso)
        chosen = r if (r and (not p or r["kcal"] >= p["kcal"])) else p
        if not chosen or chosen["kcal"] <= 0:
            continue
        entry = {"kcal": round(base_kcal + chosen["kcal"])}
        note = " + ".join(chosen["parts"])
        if note:
            entry["training_note"] = note
        out[iso] = entry
    return out


def periodized_map(base_kcal: float, back_days: int = 180, fwd_days: int = 6) -> dict:
    """Mappa {iso: {kcal, training_note}} dei giorni con allenamento nella finestra
    [oggi-back_days, oggi+fwd_days]. Le sessioni PIANIFICATE contano solo da oggi in
    avanti (carburante per ciò che devi ancora fare); i giorni PASSATI e oggi usano
    l'ATTIVITÀ REALE svolta (Workout sincronizzati), così lo storico riflette ciò
    che hai davvero fatto e il surplus non dipende dal flag «fatta». Per data si
    prende il massimo tra reale e pianificato (vedi _periodize).

    Inviando SEMPRE la mappa completa (passato incluso), planmydinner — che
    sovrascrive la periodizzazione a ogni push — mantiene storicamente i target
    corretti dei giorni di allenamento, anche a ritroso."""
    today = date.today()
    end = today + timedelta(days=fwd_days)
    back_start = today - timedelta(days=back_days)
    lo_plan = datetime.combine(today, time.min)        # pianificato: da oggi in poi
    hi = datetime.combine(end, time.max)
    lo_act = datetime.combine(back_start, time.min)     # reale: finestra passata..oggi
    hi_today = datetime.combine(today, time.max)
    with Session(engine) as s:
        planned = [(p.date.date().isoformat(), p.sport, p.duration_min)
                   for p in s.exec(select(PlanSession).where(
                       PlanSession.date != None,  # noqa: E711
                       PlanSession.date >= lo_plan, PlanSession.date <= hi,
                       PlanSession.done == False)).all()]  # noqa: E712
        actual = [(w.start_date.date().isoformat(),
                   google_health.canonical_sport(w.sport),
                   round((w.moving_s or 0) / 60))
                  for w in s.exec(select(Workout).where(
                      Workout.start_date >= lo_act, Workout.start_date <= hi_today)).all()]
    return _periodize(base_kcal, planned, actual)


def periodized_daily(base_kcal: float, days: int = 7) -> dict:
    """Solo la finestra oggi→+(days-1), senza storico (compat). Vedi periodized_map."""
    return periodized_map(base_kcal, back_days=0, fwd_days=days - 1)


async def recompute_and_push() -> bool:
    """Recompute targets (per the configured TDEE basis) and push them. Used
    both by the settings button and automatically after each weigh-in."""
    res = await build_targets_auto()
    if not res:
        return False
    # Periodizzazione: più kcal nei giorni di allenamento. Mandiamo anche lo
    # storico (back_days) coi target corretti dall'attività reale, così lo Storico
    # di planmydinner non sottostima i giorni allenati del passato.
    daily = periodized_map(res["targets"]["kcal"])
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


async def today_nutrition() -> dict | None:
    """Oggi vs obiettivo da planmydinner (per la card dashboard di OpenFit):
    {target_kcal, nutrition, training_note, remaining}. None se non configurato."""
    if not is_configured():
        return None
    base = settings.planmydinner_url.rstrip("/")
    prof = settings.planmydinner_profile
    today = date.today().isoformat()
    async with httpx.AsyncClient(timeout=10) as client:
        j = await _get(client, base, "/integration/summary",
                       {"profile_id": prof, "start_date": today, "end_date": today})
    if not j:
        return None
    day = (j.get("days") or [{}])[0]
    target = day.get("target_kcal") or (j.get("targets") or {}).get("kcal")
    nut = day.get("nutrition")
    remaining = (target - nut["kcal"]) if (target and nut) else None
    return {"target_kcal": target, "nutrition": nut,
            "training_note": day.get("training_note"), "remaining": remaining}


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
