"""Orchestrazione del programma adattivo: DB + AI attorno al motore puro program.py.

`recalc(ctx)` è idempotente e si può chiamare quando si vuole (notte, nuova
attività, seduta fatta/saltata, assenza, impostazioni): ricostruisce la finestra
dei prossimi giorni dalla settimana tipo, ricalcola il budget e
- crea le sedute mancanti (contenuto dall'AI, in UNA chiamata);
- aggiorna in place quelle future che cambiano abbastanza;
- per oggi/domani NON cambia nulla: scrive una proposta da accettare o rifiutare;
- toglie (o propone di togliere, se oggi/domani) quelle non più in programma;
- aggancia come «fatta» la seduta per cui è arrivata l'attività reale.
`ctx` arriva da main.py (CTL, ancore FC, FTP stimato) per non importare main.
"""
import asyncio
import json
import logging
from datetime import date, datetime, time, timedelta

from sqlmodel import Session, select

from . import anthropic_client, google_health, program as P
from . import form as formmod
from . import profile as profilemod
from .db import (BodyGirth, BodyMeasure, PeriodSummary, PlanSession, Program, TrainingPlan,
                 Workout, engine, get_setting, set_setting)

logger = logging.getLogger(__name__)
_LOCK = asyncio.Lock()
AUTOLINK_MIN_SHARE = 0.5     # un'attività "conta" come la seduta se dura almeno metà
READINESS_MAX_AGE_H = 12     # se Google non risponde, si usa l'ultima prontezza entro questo tempo
DONE_MIN_MIN = 25            # sotto questi minuti (es. tragitti) non soddisfa uno slot "sab o dom"


def jl(raw, default):
    """json.loads tollerante che garantisce il tipo del default."""
    try:
        v = json.loads(raw) if raw else default
    except (TypeError, ValueError):
        return default
    return v if isinstance(v, type(default)) else default


def family(sport: str) -> str:
    return google_health._sport_family(sport) or (sport or "").strip().lower()


def active_program(session) -> Program | None:
    return session.exec(select(Program).where(Program.active == True)  # noqa: E712
                        .order_by(Program.created_at.desc())).first()


def ensure_plan(session, prog: Program) -> TrainingPlan:
    """Il TrainingPlan che ospita le sedute concrete del programma."""
    plan = session.get(TrainingPlan, prog.plan_id) if prog.plan_id else None
    if not plan:
        plan = TrainingPlan(title=prog.title or "Programma", goal=prog.goal)
        session.add(plan)
        session.commit()
        session.refresh(plan)
        prog.plan_id = plan.id
        session.add(prog)
        session.commit()
    return plan


def _wload(w: Workout, anchors) -> float:
    rest, mx, sex = anchors
    return formmod.activity_load(w.avg_hr, (w.moving_s or 0) / 60, w.sport, w.rpe,
                                 rest, mx, sex)


def _external_estimate(session, sport: str, duration: int, anchors) -> tuple[float, float]:
    """(carico, rpe) atteso di una seduta esterna (es. corso master): la media delle
    ultime dello stesso sport, perché il contenuto lo decide il coach."""
    fam = family(sport)
    rows = session.exec(select(Workout).where(
        Workout.start_date >= datetime.utcnow() - timedelta(days=90))
        .order_by(Workout.start_date.desc())).all()
    same = [w for w in rows if family(w.sport) == fam and (w.moving_s or 0) >= 600][:8]
    sex = anchors[2]
    if same:
        load = sum(_wload(w, anchors) for w in same) / len(same)
        return load, P.rpe_for_load(load, duration, sex)
    rpe = P.nominal_rpe(sport)
    return P.load_at(rpe, duration, sex), rpe


def _rates(s, anchors) -> dict:
    """Taratura: carico/min mediano per sport dalle attività con FC degli ultimi 90 gg."""
    hist = s.exec(select(Workout).where(
        Workout.start_date >= datetime.utcnow() - timedelta(days=90))).all()
    rates = P.calibrate([(family(w.sport), (w.moving_s or 0) / 60, _wload(w, anchors))
                         for w in hist if w.avg_hr])
    logger.info("Program calibration (TRIMP/min): %s", {k: round(v, 2) for k, v in rates.items()})
    return rates


def _week_target_done(s, tpl: list, absences: list, wk: date, multiplier: float,
                      week_workouts: list, linked: set, anchors, rates: dict,
                      extras: list | None = None) -> tuple:
    """(target, fatto) della settimana che inizia `wk`. Se `extras` è una lista, ci
    aggiunge le attività fuori programma (non agganciate e non su uno slot).

    target = la settimana tipo completa (assenze escluse): pianificate tarate ×
    moltiplicatore + esterne previste. fatto = sedute del programma svolte +
    attività extra da CALIBRATION_MIN_MIN minuti in su (es. un'escursione); i
    tragitti brevi non agganciati restano attività di fondo."""
    full = P.expand_slots(tpl, absences, wk, wk + timedelta(days=6), wk, family=family)
    planned_week, ext_week = [], []
    for i in full:
        sl = i["slot"]
        dur = int(sl.get("duration") or 45)
        if sl.get("kind") == "external":
            ext_week.append(_external_estimate(s, sl.get("sport", ""), dur, anchors)[0])
        else:
            planned_week.append((_key(sl["id"], i["date"]), sl.get("sport", ""), dur,
                                 sl.get("intensity", "media")))
    target = P.week_budget(planned_week, ext_week, multiplier, anchors[2], rates, family)
    # conta ciò che realizza la settimana tipo (stesso giorno e sport di uno slot,
    # anche se non agganciato a una seduta: es. fatto prima di creare il programma)
    template_days = {(family(i["slot"].get("sport", "")), i["date"]) for i in full}
    done = 0.0
    for w in week_workouts:
        mins = (w.moving_s or 0) / 60
        on_plan = w.id in linked or (family(w.sport), w.start_date.date()) in template_days
        counted = on_plan or mins >= P.CALIBRATION_MIN_MIN
        load = _wload(w, anchors)
        if counted:
            done += load
        if extras is not None and not on_plan:
            extras.append({"id": w.id, "date": w.start_date, "name": w.name or w.sport,
                           "sport": w.sport, "min": round(mins), "load": round(load),
                           "counted": counted})
    return target, done


def week_summary(anchors) -> dict | None:
    """Carico della settimana corrente per la pagina Programma (stessi conti del
    ricalcolo, stesse ancore FC): {pos, target, done}."""
    today = date.today()
    wk0 = P.monday(today)
    with Session(engine) as s:
        prog = active_program(s)
        if not prog:
            return None
        ws = s.exec(select(Workout).where(
            Workout.start_date >= datetime.combine(wk0, time.min),
            Workout.start_date <= datetime.combine(today, time.max))).all()
        linked = {ps.workout_id for ps in s.exec(select(PlanSession).where(
            PlanSession.workout_id != None)).all()}  # noqa: E711
        pos = P.block_position(prog.start_date.date(), today, prog.block_weeks,
                               jl(prog.block_multipliers_json, [1.0]))
        extras: list = []
        target, done = _week_target_done(
            s, jl(prog.template_json, []), jl(prog.absences_json, []), wk0,
            pos["multiplier"], ws, linked, anchors, _rates(s, anchors), extras)
        rd = jl(prog.readiness_json, {})
    return {"pos": pos, "target": target, "done": done,
            "extras": sorted(extras, key=lambda x: x["date"]), "readiness": rd or None}


# ------------------------------------------------------------ pagella settimanale

def _report_key(monday: date) -> str:
    return f"pagella:{monday.isoformat()}"


def cached_report(monday: date) -> dict | None:
    with Session(engine) as s:
        row = s.get(PeriodSummary, _report_key(monday))
    if row is None:
        return None
    return jl(row.content, {}) or None


async def weekly_report(monday: date, anchors, force: bool = False) -> dict | None:
    """Pagella della settimana che inizia `monday`: tre voti deterministici
    (allenamento, recupero, alimentazione) + commento AI. In cache: una chiamata
    AI a settimana (o quando la rigeneri)."""
    from . import nutrition
    if not force and (hit := cached_report(monday)):
        return hit
    sunday = monday + timedelta(days=6)
    lo, hi = datetime.combine(monday, time.min), datetime.combine(sunday, time.max)
    with Session(engine) as s:
        prog = active_program(s)
        if not prog or not prog.plan_id:
            return None
        sess = s.exec(select(PlanSession).where(PlanSession.plan_id == prog.plan_id,
                                                PlanSession.date >= lo, PlanSession.date <= hi)).all()
        # i segnaposto di "salta tutta la settimana" non sono sedute in più
        planned = [p for p in sess if not (p.reason or "").endswith("(tutta la settimana)")]
        done = [p for p in planned if p.done]
        skipped = [p for p in planned if p.skipped]
        ws = s.exec(select(Workout).where(Workout.start_date >= lo, Workout.start_date <= hi)).all()
        linked = {ps.workout_id for ps in s.exec(select(PlanSession).where(
            PlanSession.workout_id != None)).all()}  # noqa: E711
        pos = P.block_position(prog.start_date.date(), monday, prog.block_weeks,
                               jl(prog.block_multipliers_json, [1.0]))
        extras: list = []
        target, done_load = _week_target_done(
            s, jl(prog.template_json, []), jl(prog.absences_json, []), monday,
            pos["multiplier"], ws, linked, anchors, _rates(s, anchors), extras)
    t_score = P.training_score(len(planned), len(done), done_load, target)

    wellness, sleep_h = [], []
    if google_health.is_authenticated():
        try:   # 28 giorni: la prontezza di ogni giorno si confronta con la tua media recente
            ov = await google_health.fetch_health_overview(sunday - timedelta(days=27), sunday)
            wellness = [p["value"] for p in ov.get("score_series") or []
                        if monday.isoformat() <= p["date"] <= sunday.isoformat()]
            sleep_h = [n["asleep_min"] / 60 for n in ov.get("sleep") or []
                       if monday.isoformat() <= n["date"] <= sunday.isoformat()]
        except google_health.GoogleHealthError as e:
            logger.info("Weekly report without Google data: %s", e)
    r_score = P.recovery_score(wellness, sleep_h)

    at = ((await nutrition.fetch_nutrition(monday, sunday)) or {}).get("alimentazione_tracciata") or {}
    balances = [r["saldo_vs_obiettivo"] for r in at.get("per_giorno") or []
                if r.get("saldo_vs_obiettivo") is not None]
    weight = profilemod.load().get("weight_kg")
    tg = nutrition.build_targets()
    prot_target = (tg["targets"]["protein_g"] / weight) if tg and weight else None
    n_score = P.nutrition_score(balances, at.get("proteine_g_per_kg"), prot_target)

    numbers = {
        "settimana": f"{monday.isoformat()} → {sunday.isoformat()}", "blocco": P.block_label(pos),
        "sedute_previste": len(planned), "sedute_fatte": len(done),
        "sedute_saltate": [{"giorno": p.day_label, "titolo": p.title, "motivo": p.reason} for p in skipped],
        "carico_fatto_trimp": round(done_load), "carico_previsto_trimp": round(target),
        "fuori_programma": [{"nome": e["name"], "minuti": e["min"], "trimp": e["load"]}
                            for e in extras if e["counted"]],
        "prontezza_media": round(sum(wellness) / len(wellness)) if wellness else None,
        "sonno_medio_h": round(sum(sleep_h) / len(sleep_h), 1) if sleep_h else None,
        "giorni_entro_150kcal": (f"{sum(1 for b in balances if abs(b) <= 150)}/{len(balances)}"
                                 if balances else None),
        "saldo_medio_kcal": round(sum(balances) / len(balances)) if balances else None,
        "proteine_g_kg": at.get("proteine_g_per_kg"),
        "proteine_obiettivo_g_kg": round(prot_target, 2) if prot_target else None,
    }
    scores = {"allenamento": t_score, "recupero": r_score, "alimentazione": n_score}
    try:
        text = await anthropic_client.weekly_report_text({"voti": scores, "numeri": numbers})
    except anthropic_client.AnthropicError as e:
        logger.warning("Weekly report text failed: %s", e)
        text = ""
    data = {"week": monday.isoformat(), "scores": scores, "numbers": numbers, "text": text,
            "created_at": datetime.utcnow().isoformat()}
    with Session(engine) as s:
        row = s.get(PeriodSummary, _report_key(monday)) or PeriodSummary(
            key=_report_key(monday), content="")
        row.content = json.dumps(data, ensure_ascii=False, default=str)
        row.model = anthropic_client.effective_model()
        row.created_at = datetime.utcnow()
        s.add(row)
        s.commit()
    return data


# ------------------------------------------------------------ promemoria corpo

WEIGH_EVERY_DAYS = 7         # pesata (la bilancia la manda HA)
MEASURE_EVERY_DAYS = 21      # misure col metro


def body_reminders() -> dict:
    """Promemoria pesata/misure + trend del peso e del girovita."""
    from . import nutrition
    now = datetime.utcnow()
    with Session(engine) as s:
        last_w = s.exec(select(BodyMeasure).where(BodyMeasure.weight_kg != None)  # noqa: E711
                        .order_by(BodyMeasure.measured_at.desc())).first()
        girths = s.exec(select(BodyGirth).order_by(BodyGirth.measured_at.desc())).all()
    last_g = girths[0] if girths else None
    dw = (now - last_w.measured_at).days if last_w else None
    dg = (now - last_g.measured_at).days if last_g else None
    # girovita: ultima misura vs la più vecchia negli ultimi ~60 giorni
    waist = [g for g in girths if g.vita_cm and (now - g.measured_at).days <= 60]
    waist_delta = (round(waist[0].vita_cm - waist[-1].vita_cm, 1)
                   if len(waist) >= 2 else None)
    tr = nutrition.weight_trend_kg_per_week()
    return {"weigh_due": dw is None or dw >= WEIGH_EVERY_DAYS, "days_since_weigh": dw,
            "last_weight": round(last_w.weight_kg, 1) if last_w else None,
            "measure_due": dg is None or dg >= MEASURE_EVERY_DAYS, "days_since_measure": dg,
            "trend_kg_week": round(tr["rate"], 2) if tr else None,
            "waist_delta_cm": waist_delta,
            "waist_span_days": (waist[0].measured_at - waist[-1].measured_at).days if len(waist) >= 2 else None}


def _day_label(d: date) -> str:
    return f"{P.WEEKDAYS_IT[d.weekday()].capitalize()} {d.strftime('%d/%m')}"


def _order(d: date, slot: dict, tpl: list) -> int:
    idx = next((i for i, s in enumerate(tpl) if s.get("id") == slot.get("id")), 0)
    return d.toordinal() * 100 + idx


def _key(slot_id: str, d: date) -> str:
    return f"{slot_id}@{d.isoformat()}"


def _clean_structure(raw) -> str | None:
    if not isinstance(raw, list):
        return None
    steps = []
    for st in raw:
        if not isinstance(st, dict):
            continue
        try:
            mins = float(st.get("min"))
            pct = float(st.get("pct_ftp"))
        except (TypeError, ValueError):
            continue
        if mins <= 0 or not (20 <= pct <= 200):
            continue
        rpm = st.get("rpm")
        steps.append({"min": round(mins, 1), "pct_ftp": round(pct),
                      "rpm": int(rpm) if isinstance(rpm, (int, float)) else None,
                      "note": str(st.get("note") or "")[:80]})
    return json.dumps(steps) if steps else None


async def recalc(ctx: dict, why: str = "") -> dict | None:
    """Ricalcolo serializzato: due eventi ravvicinati non creano sedute doppie."""
    async with _LOCK:
        return await _recalc(ctx, why)


def _nolink_key(sid: int, wid: int) -> str:
    return f"nolink:{sid}:{wid}"


def recent_motives(s, prog: Program, today: date) -> list[str]:
    """Motivi "stanco"/"dolore" delle sedute saltate tra ieri l'altro e domani."""
    if not prog or not prog.plan_id:
        return []
    out = []
    for ps in s.exec(select(PlanSession).where(
            PlanSession.plan_id == prog.plan_id, PlanSession.skipped == True,  # noqa: E712
            PlanSession.date >= datetime.combine(today - timedelta(days=2), time.min),
            PlanSession.date <= datetime.combine(today + timedelta(days=1), time.max))).all():
        r = (ps.reason or "").lower()
        if "dolore" in r or "malessere" in r:
            out.append("dolore")
        elif "stanco" in r:
            out.append("stanco")
    return out


async def compute_readiness(ctx: dict) -> dict:
    """Semaforo di oggi: prontezza Google (HRV, FC a riposo, sonno), forma (TSB) e
    motivi recenti di "Non la faccio". Best-effort: senza Google resta verde."""
    today = date.today()
    score, stale = None, False
    if google_health.is_authenticated():
        try:
            ov = await google_health.fetch_health_overview(today - timedelta(days=27), today)
            score = (ov.get("score") or {}).get("score")
        except google_health.GoogleHealthError as e:
            logger.info("Readiness without fresh Google data: %s", e)
    with Session(engine) as s:
        prog = active_program(s)
        motives = recent_motives(s, prog, today)
        last = jl(prog.readiness_json, {}) if prog else {}
    if score is None and last.get("score") is not None:
        # Google a volte risponde 503: un errore passeggero non deve far diventare
        # verde il semaforo -> ultima prontezza valida se recente
        try:
            age = datetime.utcnow() - datetime.fromisoformat(last.get("at", ""))
        except ValueError:
            age = timedelta(days=1)
        if age < timedelta(hours=READINESS_MAX_AGE_H):
            score, stale = last["score"], True
    rd = P.readiness(score, (ctx.get("form") or {}).get("tsb"), motives)
    rd["score"] = score
    if stale:
        rd["why"].append("dato di prontezza delle ultime ore (Google non ha risposto)")
    return rd


async def _recalc(ctx: dict, why: str) -> dict | None:
    today = date.today()
    anchors = ctx["anchors"]
    sex = anchors[2]
    stats = {"created": 0, "updated": 0, "proposals": 0, "removed": 0, "linked": 0}
    with Session(engine) as s:
        if not active_program(s):
            return None
    rd = await compute_readiness(ctx)          # rete: fuori dalla sessione DB
    rd_note = (f" · semaforo {rd['level']}: {', '.join(rd['why'])}"
               if rd["factor"] < 1 else "")
    with Session(engine) as s:
        prog = active_program(s)
        if not prog:
            return None
        plan = ensure_plan(s, prog)
        start_p = prog.start_date.date()
        end = today + timedelta(days=P.WINDOW_DAYS - 1)
        if prog.end_date:
            end = min(end, prog.end_date.date())
        tpl = jl(prog.template_json, [])
        absences = jl(prog.absences_json, [])
        routines = {r.get("id"): r for r in jl(prog.routines_json, []) if isinstance(r, dict)}
        mults = jl(prog.block_multipliers_json, [1.0])

        wk0 = P.monday(today)
        ws = s.exec(select(Workout).where(
            Workout.start_date >= datetime.combine(wk0, time.min),
            Workout.start_date <= datetime.combine(today, time.max))).all()
        sessions = s.exec(select(PlanSession).where(PlanSession.plan_id == plan.id)).all()
        linked = {ps.workout_id for ps in s.exec(select(PlanSession).where(
            PlanSession.workout_id != None)).all()}  # noqa: E711

        # 1) seduta fatta senza «segna fatta»: aggancia l'attività reale (stesso
        #    giorno, stesso sport, durata almeno metà — un tragitto non è i rulli)
        for ps in sessions:
            if (ps.done or ps.skipped or not ps.kind or not ps.date
                    or ps.date.date() > today or ps.date.date() < wk0):
                continue
            fam = family(ps.sport)
            w = next((w for w in ws if w.start_date.date() == ps.date.date()
                      and family(w.sport) == fam and w.id not in linked
                      and (w.moving_s or 0) / 60 >= AUTOLINK_MIN_SHARE * (ps.duration_min or 0)
                      and not get_setting(_nolink_key(ps.id, w.id))),   # annullato a mano
                     None)
            if w:
                ps.done, ps.workout_id, ps.proposal_json = True, w.id, None
                linked.add(w.id)
                s.add(ps)
                stats["linked"] += 1

        # 2) istanze della settimana tipo nella finestra
        by_key: dict = {}
        keep_day, skipped = {}, set()
        for ps in sessions:
            if not ps.slot_id or not ps.date:
                continue
            d = ps.date.date()
            by_key.setdefault(_key(ps.slot_id, d), ps)
            if ps.skipped:
                skipped.add((ps.slot_id, d.isoformat()))
            elif not ps.done and d >= today:
                keep_day[(ps.slot_id, P.monday(d).isoformat())] = d.isoformat()
        done_days = {(family(w.sport), w.start_date.date().isoformat()) for w in ws
                     if (w.moving_s or 0) / 60 >= DONE_MIN_MIN}
        inst = P.expand_slots(tpl, absences, today, end, today, family=family,
                              keep_day=keep_day, skipped=skipped, done_days=done_days)

        # 3) budget per settimana -> intensità di ogni seduta pianificata, con il
        #    carico "tipo" di ogni sport tarato sullo storico misurato (FC)
        rates = _rates(s, anchors)
        alloc: dict = {}
        weeks: dict = {}
        ext_est: dict = {}
        for wk in sorted({P.monday(i["date"]) for i in inst}):
            pos = P.block_position(start_p, wk, prog.block_weeks, mults)
            target, done_load = _week_target_done(
                s, tpl, absences, wk, pos["multiplier"], ws if wk == wk0 else [],
                linked, anchors, rates)
            ext_loads, planned = [], []
            for i in inst:
                if P.monday(i["date"]) != wk:
                    continue
                sl = i["slot"]
                k = _key(sl["id"], i["date"])
                ex = by_key.get(k)
                if ex and (ex.done or ex.skipped):
                    continue                   # già nel fatto, o fuori dal conto
                dur = int(sl.get("duration") or 45)
                if sl.get("kind") == "external":
                    ext_est[k] = _external_estimate(s, sl.get("sport", ""), dur, anchors)
                    ext_loads.append(ext_est[k][0])
                else:
                    planned.append((k, sl.get("sport", ""), dur, sl.get("intensity", "media")))
            # semaforo: abbassa solo oggi e domani (il recupero è un segnale a breve)
            levels = ({k: pos["multiplier"] * rd["factor"] for k, *_ in planned
                       if P.is_proposal_day(date.fromisoformat(k.split("@")[1]), today)}
                      if rd["factor"] < 1 else None)
            alloc.update(P.allocate(target, done_load, ext_loads, planned, sex,
                                    rates=rates, family=family, level=pos["multiplier"],
                                    levels=levels))
            for k in levels or {}:
                alloc[k]["note"] = rd_note
            weeks[wk] = {"pos": pos, "target": target, "done": done_load}

        # 4) per ogni istanza: crea l'esterna, decide cosa riempire con l'AI
        to_fill = []
        for i in inst:
            sl, d = i["slot"], i["date"]
            k = _key(sl["id"], d)
            ex = by_key.get(k)
            if ex and (ex.done or ex.skipped):
                continue
            wi = weeks[P.monday(d)]
            dur = int(sl.get("duration") or 45)
            if sl.get("kind") == "external":
                if not ex:
                    _, rpe = ext_est.get(k) or _external_estimate(s, sl.get("sport", ""), dur, anchors)
                    s.add(PlanSession(
                        plan_id=plan.id, order=_order(d, sl, tpl), day_label=_day_label(d),
                        date=datetime.combine(d, time(12, 0)),
                        title=f"{sl.get('sport')} — {sl.get('focus') or 'seduta esterna'}"[:200],
                        sport=google_health.canonical_sport(sl.get("sport", ""))[:60],
                        duration_min=dur, description=sl.get("focus") or "",
                        slot_id=sl["id"], kind="external", rpe_target=rpe,
                        reason=P.reason_text(wi["pos"], None, wi["target"], wi["done"])
                        + " · seduta esterna: il contenuto lo decide il coach"))
                    stats["created"] += 1
                continue
            a = alloc[k]
            prop = jl(ex.proposal_json, {}) if ex else {}
            if ex and ex.description:
                if prop.get("rejected") and not P.needs_update(
                        prop.get("rpe"), prop.get("duration"), a["rpe"], a["duration"]):
                    continue                   # hai già detto di no a questa modifica
                cur_rpe, cur_dur = ex.rpe_target, ex.duration_min
                if prop and not prop.get("rejected") and not prop.get("remove"):
                    cur_rpe, cur_dur = prop.get("rpe_target"), prop.get("duration_min")
                if not P.needs_update(cur_rpe, cur_dur, a["rpe"], a["duration"]):
                    ex.reason = P.reason_text(wi["pos"], a, wi["target"], wi["done"],
                                              rpe=ex.rpe_target) + a.get("note", "")
                    s.add(ex)
                    continue
            to_fill.append({"key": k, "slot": sl, "date": d, "alloc": a,
                            "existing_id": ex.id if ex else None, "week": wi})

        # 5) sedute non più in programma (assenza, slot cambiato o spostato)
        inst_keys = {_key(i["slot"]["id"], i["date"]) for i in inst}
        horizon = today + timedelta(days=P.WINDOW_DAYS - 1)
        for ps in sessions:
            if not ps.kind or ps.done or ps.skipped or not ps.date:
                continue
            d = ps.date.date()
            if d < today or d > horizon or _key(ps.slot_id, d) in inst_keys:
                continue
            prop = jl(ps.proposal_json, {})
            if P.is_proposal_day(d, today):
                if not prop.get("remove"):
                    ps.proposal_json = json.dumps(
                        {"remove": True,
                         "reason": "non più in programma (assenza, giorno spostato o slot cambiato)"})
                    s.add(ps)
                    stats["proposals"] += 1
            else:
                s.delete(ps)
                stats["removed"] += 1

        fill_ctx = _fill_context(s, prog, plan, routines, to_fill, ctx, today) if to_fill else None
        prog_id = prog.id
        s.commit()

    # 6) contenuto dall'AI fuori dalla sessione DB (chiamata di rete)
    content: dict = {}
    if fill_ctx:
        try:
            for r in await anthropic_client.fill_program_sessions(fill_ctx):
                if isinstance(r, dict) and r.get("key"):
                    content[str(r["key"])] = r
        except anthropic_client.AnthropicError as e:
            logger.warning("Program fill failed, using placeholders: %s", e)

    with Session(engine) as s:
        prog = s.get(Program, prog_id)
        tpl = jl(prog.template_json, [])
        for f in to_fill:
            sl, d, a, wi = f["slot"], f["date"], f["alloc"], f["week"]
            c = content.get(f["key"]) or {}
            names = [routines[r]["name"] for r in sl.get("routines") or [] if r in routines]
            fields = {
                "title": str(c.get("title") or f"{sl.get('sport')} — {sl.get('focus') or 'seduta'}")[:200],
                "description": str(c.get("description") or
                                   ("Routine: " + ", ".join(names) + "\n" if names else "")
                                   + (sl.get("focus") or "")),
                "duration_min": P._int(c.get("duration_min"), a["duration"], 5, 480),
                "rpe_target": a["rpe"],
                "structure_json": _clean_structure(c.get("structure")) if sl.get("indoor") else None,
                "reason": P.reason_text(wi["pos"], a, wi["target"], wi["done"]) + a.get("note", ""),
            }
            ex = s.get(PlanSession, f["existing_id"]) if f["existing_id"] else None
            if ex is None:
                s.add(PlanSession(
                    plan_id=prog.plan_id, order=_order(d, sl, tpl), day_label=_day_label(d),
                    date=datetime.combine(d, time(12, 0)),
                    sport=google_health.canonical_sport(sl.get("sport", ""))[:60],
                    slot_id=sl["id"], kind="planned", **fields))
                stats["created"] += 1
            elif ex.done or ex.skipped:
                continue                       # cambiata mentre l'AI pensava
            elif ex.description and P.is_proposal_day(d, date.today()):
                ex.proposal_json = json.dumps({**fields, "structure": jl(fields["structure_json"], [])})
                s.add(ex)
                stats["proposals"] += 1
            else:
                for k_, v in fields.items():
                    setattr(ex, k_, v)
                ex.proposal_json = None
                s.add(ex)
                stats["updated"] += 1
        parts = [f"{v} {k}" for k, v in (("create", stats["created"]), ("aggiornate", stats["updated"]),
                                          ("proposte", stats["proposals"]), ("tolte", stats["removed"]),
                                          ("agganciate", stats["linked"])) if v]
        prog.last_recalc_at = datetime.utcnow()
        prog.last_recalc_log = (why + ": " if why else "") + (", ".join(parts) or "nessuna modifica")
        prog.readiness_json = json.dumps({**rd, "at": datetime.utcnow().isoformat()})
        s.add(prog)
        s.commit()
    logger.info("Program recalc (%s): %s", why, stats)
    return stats


def _fill_context(s, prog: Program, plan: TrainingPlan, routines: dict, to_fill: list,
                  ctx: dict, today: date) -> dict:
    """Cosa serve all'AI per riempire le sedute: slot con budget, routine, attrezzatura,
    ultime sedute svolte (per la progressione)."""
    recent = s.exec(select(PlanSession).where(
        PlanSession.plan_id == plan.id, PlanSession.done == True,  # noqa: E712
        PlanSession.date >= datetime.combine(today - timedelta(days=21), time.min))
        .order_by(PlanSession.date)).all()
    slots = []
    for f in to_fill:
        sl, d, a, pos = f["slot"], f["date"], f["alloc"], f["week"]["pos"]
        slots.append({
            "key": f["key"], "data": d.isoformat(), "giorno": _day_label(d),
            "sport": sl.get("sport"), "focus": sl.get("focus"),
            "intensita_del_giorno": sl.get("intensity", "media"),
            "durata_min": a["duration"], "rpe_target": a["rpe"], "indoor": bool(sl.get("indoor")),
            "routine_fisse": [{"nome": routines[r]["name"], "durata_min": routines[r]["duration_min"]}
                              for r in sl.get("routines") or [] if r in routines],
            "blocco": pos["block"], "settimana_del_blocco": f"{pos['week_in_block']}/{pos['block_weeks']}",
            "scarico": pos["deload"]})
    return {
        "oggi": today.isoformat(),
        "programma": {"obiettivo": prog.goal, "note": prog.notes},
        "attrezzatura": jl(prog.equipment_json, []),
        "atleta": profilemod.ai_context(),
        "ftp_stimato_w": (ctx.get("ftp") or {}).get("ftp"),
        "ultime_sedute_svolte": [{"data": r.date.date().isoformat(), "sport": r.sport,
                                  "titolo": r.title, "contenuto": (r.description or "")[:300]}
                                 for r in recent[-8:]],
        "slot": slots,
    }


# ------------------------------------------------------------ user actions

def _own_session(s, sid: int) -> PlanSession | None:
    prog = active_program(s)
    ps = s.get(PlanSession, sid)
    if not prog or not ps or ps.plan_id != prog.plan_id:
        return None
    return ps


def accept_proposal(sid: int) -> bool:
    with Session(engine) as s:
        ps = _own_session(s, sid)
        prop = jl(ps.proposal_json, {}) if ps else {}
        if not ps or not prop or prop.get("rejected") or ps.done:
            return False
        if prop.get("remove"):
            s.delete(ps)
        else:
            for k in ("title", "description", "duration_min", "rpe_target", "structure_json", "reason"):
                if k in prop:
                    setattr(ps, k, prop[k])
            ps.proposal_json = None
            s.add(ps)
        s.commit()
    return True


def reject_proposal(sid: int) -> bool:
    """Tieni la seduta com'è: la stessa modifica non verrà riproposta."""
    with Session(engine) as s:
        ps = _own_session(s, sid)
        prop = jl(ps.proposal_json, {}) if ps else {}
        if not ps or not prop:
            return False
        ps.proposal_json = json.dumps({"rejected": True, "remove": bool(prop.get("remove")),
                                       "rpe": prop.get("rpe_target"),
                                       "duration": prop.get("duration_min")})
        s.add(ps)
        s.commit()
    return True


SKIP_MOTIVES = {"riposo": "riposo", "stanco": "stanco", "fuori": "fuori casa / impegno",
                "dolore": "dolore o malessere", "altro": "altro"}


def _skip(s, prog: Program, ps: PlanSession, motive: str = "", note: str = "",
          whole_week: bool = False) -> None:
    """Segna la seduta come non fatta, col motivo. Per uno slot «uno dei giorni»
    (bici sab o dom) `whole_week` la toglie anche dagli altri giorni della
    settimana, invece di lasciarla spostare dal motore."""
    label = SKIP_MOTIVES.get(motive, motive or "")
    why = "saltata" + (f" — {label}" if label else "") + (f": {note.strip()}" if note.strip() else "")
    ps.skipped, ps.proposal_json, ps.reason = True, None, why[:300]
    s.add(ps)
    if not whole_week or not ps.slot_id or not ps.date:
        return
    slot = next((x for x in jl(prog.template_json, []) if x.get("id") == ps.slot_id), None)
    if not slot or slot.get("pick") != "one":
        return
    wk = P.monday(ps.date.date())
    for dnum in slot.get("days") or []:
        d = wk + timedelta(days=int(dnum))
        if d == ps.date.date():
            continue
        other = s.exec(select(PlanSession).where(
            PlanSession.plan_id == prog.plan_id, PlanSession.slot_id == ps.slot_id,
            PlanSession.date >= datetime.combine(d, time.min),
            PlanSession.date <= datetime.combine(d, time.max))).first()
        if other and other.done:
            continue
        if other is None:   # segnaposto "saltata": impedisce al motore di spostarla qui
            other = PlanSession(plan_id=prog.plan_id, order=_order(d, slot, [slot]),
                                day_label=_day_label(d), date=datetime.combine(d, time(12, 0)),
                                title=ps.title, sport=ps.sport, duration_min=ps.duration_min,
                                slot_id=ps.slot_id, kind=ps.kind)
        other.skipped, other.proposal_json = True, None
        other.reason = why[:300] + " (tutta la settimana)"
        s.add(other)


def skip_session(sid: int, motive: str = "", note: str = "", whole_week: bool = False) -> bool:
    with Session(engine) as s:
        ps = _own_session(s, sid)
        if not ps or ps.done:
            return False
        _skip(s, active_program(s), ps, motive, note, whole_week)
        s.commit()
    return True


def unskip_session(sid: int) -> bool:
    with Session(engine) as s:
        ps = _own_session(s, sid)
        if not ps or not ps.skipped:
            return False
        ps.skipped = False
        s.add(ps)
        s.commit()
    return True


def _iso(v) -> str | None:
    try:
        return date.fromisoformat(str(v)[:10]).isoformat()
    except (TypeError, ValueError):
        return None


def apply_actions(azioni: list) -> list[str]:
    """Applica le azioni proposte dalla chat del programma. Ritorna cosa è stato
    fatto (in italiano), ignorando le azioni malformate."""
    done: list[str] = []
    with Session(engine) as s:
        prog = active_program(s)
        if not prog:
            return done
        tpl = jl(prog.template_json, [])
        absences = jl(prog.absences_json, [])
        equip = jl(prog.equipment_json, [])
        rmins = {r.get("id"): r.get("duration_min") or 0
                 for r in jl(prog.routines_json, []) if isinstance(r, dict)}
        for a in azioni if isinstance(azioni, list) else []:
            if not isinstance(a, dict):
                continue
            t = a.get("tipo")
            if t == "assenza":
                st, en = _iso(a.get("start")), _iso(a.get("end") or a.get("start"))
                if st and en and st <= en:
                    absences.append({"start": st, "end": en, "note": str(a.get("note") or "")[:120],
                                     "allowed": [str(x)[:40] for x in a.get("allowed") or []]})
                    done.append(f"assenza {st} → {en}")
            elif t == "rimuovi_assenza":
                i = a.get("index")
                if isinstance(i, int) and 0 <= i < len(absences):
                    ab = absences.pop(i)
                    done.append(f"tolta assenza {ab.get('start')} → {ab.get('end')}")
            elif t == "salta":
                ps = s.get(PlanSession, a.get("session_id")) if isinstance(a.get("session_id"), int) else None
                if ps and ps.plan_id == prog.plan_id and not ps.done:
                    _skip(s, prog, ps, str(a.get("motivo") or ""), "",
                          bool(a.get("intera_settimana")))
                    done.append(f"saltata {ps.day_label} {ps.title}"
                                + (" (tutta la settimana)" if a.get("intera_settimana") else ""))
            elif t == "modifica_seduta":
                ps = s.get(PlanSession, a.get("session_id")) if isinstance(a.get("session_id"), int) else None
                if ps and ps.plan_id == prog.plan_id and not ps.done and not ps.skipped:
                    if str(a.get("title") or "").strip():
                        ps.title = str(a["title"]).strip()[:200]
                    if str(a.get("description") or "").strip():
                        ps.description = str(a["description"]).strip()
                    if a.get("duration_min") is not None:
                        ps.duration_min = P._int(a.get("duration_min"), ps.duration_min, 5, 480)
                    try:
                        ps.rpe_target = max(1.0, min(10.0, float(a["rpe"]))) if a.get("rpe") is not None else ps.rpe_target
                    except (TypeError, ValueError):
                        pass
                    ps.structure_json = None if a.get("description") else ps.structure_json
                    ps.reason = "adattata su tua richiesta al coach"
                    ps.proposal_json = json.dumps({"rejected": True, "rpe": ps.rpe_target,
                                                   "duration": ps.duration_min})
                    s.add(ps)
                    done.append(f"adattata {ps.day_label} {ps.title}")
            elif t == "slot":
                sl = next((x for x in tpl if x.get("id") == a.get("slot_id")), None)
                if sl:
                    patch = dict(sl)
                    for k in ("enabled", "indoor"):
                        if isinstance(a.get(k), bool):
                            patch[k] = a[k]
                    for k in ("days", "duration", "focus"):
                        if a.get(k) is not None:
                            patch[k] = a[k]
                    clean = P.sanitize_template([patch], rmins)
                    if clean:
                        clean[0]["id"] = sl["id"]
                        tpl[tpl.index(sl)] = clean[0]
                        done.append(f"slot {sl['id']} aggiornato")
            elif t == "attrezzatura":
                low = {e.lower() for e in equip}
                for x in a.get("aggiungi") or []:
                    if str(x).strip() and str(x).strip().lower() not in low:
                        equip.append(str(x).strip()[:60])
                        done.append(f"+ {x}")
                rm = {str(x).strip().lower() for x in a.get("rimuovi") or []}
                if rm:
                    equip = [e for e in equip if e.lower() not in rm]
                    done.append("rimosso: " + ", ".join(sorted(rm)))
            elif t == "fine":
                en = _iso(a.get("date"))
                if en:
                    prog.end_date = datetime.combine(date.fromisoformat(en), time.min)
                    done.append(f"fine programma {en}")
            elif t == "nota":
                txt = str(a.get("testo") or "").strip()
                if txt:
                    prog.notes = ((prog.notes or "") + "\n" + txt).strip()[:3000]
                    done.append("nota aggiunta")
        prog.template_json = json.dumps(tpl)
        prog.absences_json = json.dumps(absences)
        prog.equipment_json = json.dumps(equip)
        s.add(prog)
        s.commit()
    return done


def chat_context(ctx: dict) -> dict | None:
    """Ciò che la chat del programma deve vedere per tradurre i comandi in azioni."""
    today = date.today()
    with Session(engine) as s:
        prog = active_program(s)
        if not prog:
            return None
        pos = P.block_position(prog.start_date.date(), today, prog.block_weeks,
                               jl(prog.block_multipliers_json, [1.0]))
        upcoming = s.exec(select(PlanSession).where(
            PlanSession.plan_id == prog.plan_id,
            PlanSession.date >= datetime.combine(today - timedelta(days=2), time.min))
            .order_by(PlanSession.date)).all() if prog.plan_id else []
        return {
            "oggi": today.isoformat(), "giorno_settimana": P.WEEKDAYS_IT[today.weekday()],
            "programma": {"obiettivo": prog.goal, "blocco": P.block_label(pos),
                          "fine": prog.end_date.date().isoformat() if prog.end_date else None,
                          "note": prog.notes},
            "settimana_tipo": jl(prog.template_json, []),
            "giorni": "0=lun … 6=dom",
            "attrezzatura": jl(prog.equipment_json, []),
            "routine": [r.get("name") for r in jl(prog.routines_json, [])],
            "assenze": [{"index": i, **a} for i, a in enumerate(jl(prog.absences_json, []))],
            "sessioni_prossime": [
                {"session_id": p.id, "data": p.date.date().isoformat(), "sport": p.sport,
                 "titolo": p.title, "tipo": p.kind, "durata_min": p.duration_min,
                 "rpe_target": p.rpe_target,
                 "stato": "fatta" if p.done else "saltata" if p.skipped else "da fare",
                 **({"motivo": p.reason} if p.skipped and p.reason else {}),
                 # contenuto solo per le prossime da fare: serve per adattarle
                 **({"contenuto": (p.description or "")[:500]}
                    if not p.done and not p.skipped and p.date.date() <= today + timedelta(days=3)
                    else {})}
                for p in upcoming if p.date],
            "forma_attuale": ctx.get("form"),
        }


def default_end(today: date) -> date:
    """«Fino a quando non fa bello»: fine marzo (modificabile)."""
    y = today.year + 1 if today.month >= 4 else today.year
    return date(y, 3, 31)


async def create_from_notes(goal: str = "") -> int:
    """Crea il programma attivo precompilandolo dalla memoria AI dell'atleta."""
    p = profilemod.load()
    data = await anthropic_client.program_prefill(p.get("ai_notes") or "",
                                                  profilemod.ai_context()) or {}
    routines = P.sanitize_routines(data.get("routines"))
    tpl = P.sanitize_template(data.get("template"),
                              {r["id"]: r["duration_min"] for r in routines})
    equip = [str(x).strip()[:60] for x in data.get("equipment") or [] if str(x).strip()]
    today = date.today()
    with Session(engine) as s:
        for old in s.exec(select(Program).where(Program.active == True)).all():  # noqa: E712
            old.active = False
            s.add(old)
        prog = Program(title=str(data.get("title") or "Programma invernale")[:120],
                       goal=(goal or str(data.get("goal") or "")).strip()[:1000],
                       start_date=datetime.combine(today, time.min),
                       end_date=datetime.combine(default_end(today), time.min),
                       template_json=json.dumps(tpl), routines_json=json.dumps(routines),
                       equipment_json=json.dumps(equip),
                       notes=str(data.get("notes") or "")[:3000])
        s.add(prog)
        s.commit()
        s.refresh(prog)
        ensure_plan(s, prog)
        return prog.id
