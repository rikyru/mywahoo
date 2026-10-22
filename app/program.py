"""Motore del programma adattivo — logica PURA (niente DB, niente AI).

Idea: il lungo periodo è descritto (blocchi da N settimane, settimana tipo,
assenze); solo i prossimi WINDOW_DAYS diventano sessioni concrete. Ogni sessione
pianificata riceve un'intensità (RPE target) ricavata da un BUDGET settimanale
deterministico:

    target settimana = CTL×7 (il carico che sostieni oggi) × moltiplicatore della
                       settimana nel blocco (es. 1.0 / 1.05 / 1.1 / 0.7 scarico)
    residuo          = target − carico già fatto − esterni previsti (nuoto col coach)

e il residuo si ripartisce sugli slot pianificati in proporzione al loro carico
nominale, entro ±30%. Così se salti, fai di più o di meno, o il nuoto è stato
pesante, le sedute successive si riequilibrano da sole. L'AI interviene solo
dopo, per riempire gli slot di contenuto (program_runner.py).
"""
from datetime import date, timedelta

from . import form as formmod

WINDOW_DAYS = 10          # giorni materializzati come sessioni concrete
PROPOSAL_DAYS = 1         # oggi + domani: le modifiche diventano proposte
ALLOC_CLAMP = (0.7, 1.3)  # una seduta non scende/sale oltre questo × il nominale
RPE_RANGE = (2.0, 9.0)
WEEKDAYS_IT = ["lun", "mar", "mer", "gio", "ven", "sab", "dom"]


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def block_position(start: date, day: date, block_weeks: int,
                   multipliers: list[float]) -> dict:
    """Dove cade `day` nel ciclo di blocchi che parte dal lunedì di `start`."""
    bw = max(1, int(block_weeks or 1))
    wi = max(0, (monday(day) - monday(start)).days // 7)
    k = wi % bw
    mult = float(multipliers[k]) if k < len(multipliers or []) else 1.0
    return {"week_index": wi, "block": wi // bw + 1, "week_in_block": k + 1,
            "block_weeks": bw, "multiplier": mult, "deload": mult < 0.9}


def week_target(ctl: float | None, multiplier: float) -> float:
    """Carico settimanale target (TRIMP): ciò che sostieni oggi × moltiplicatore."""
    return max(0.0, float(ctl or 0.0) * 7.0 * float(multiplier))


def _matches(sport: str, allowed: list[str], family) -> bool:
    f = family(sport)
    for a in allowed or []:
        fa = family(a)
        if (f and fa and f == fa) or a.strip().lower() == (sport or "").strip().lower():
            return True
    return False


def is_absent(day: date, sport: str, absences: list[dict], family) -> bool:
    """True se `day` cade in un'assenza che non consente questo sport."""
    iso = day.isoformat()
    for a in absences or []:
        if str(a.get("start", "")) <= iso <= str(a.get("end", "")):
            if not _matches(sport, a.get("allowed") or [], family):
                return True
    return False


def _lower_family(s: str) -> str:
    return (s or "").strip().lower()


def expand_slots(template: list[dict], absences: list[dict], start: date, end: date,
                 today: date, family=_lower_family, keep_day: dict | None = None,
                 skipped: set | None = None, done_days: set | None = None) -> list[dict]:
    """Istanze {date, slot} della settimana tipo nella finestra [start, end].

    - slot disattivati e giorni di assenza (che non consentono lo sport) esclusi;
    - slot normale: un'istanza per OGNI giorno elencato (es. nuoto lun e mer);
    - slot con pick="one" ("sab o dom"): una sola istanza a settimana. Nessuna se
      quello sport è già stato fatto in uno di quei giorni (`done_days`:
      {(family, iso)}); altrimenti resta sul giorno già scelto (`keep_day`:
      {(slot_id, monday_iso): iso}) se ancora valido, se no il primo giorno
      disponibile da oggi in poi, saltando i giorni segnati come saltati
      (`skipped`: {(slot_id, iso)}) — così "sabato salto" lo sposta a domenica.
    """
    keep_day = keep_day or {}
    skipped = skipped or set()
    done_days = done_days or set()
    out: list[dict] = []
    wk = monday(start)
    while wk <= end:
        for slot in template or []:
            if not slot.get("enabled", True):
                continue
            days = sorted({int(x) for x in slot.get("days") or [] if 0 <= int(x) <= 6})
            if not days:
                continue
            sport = slot.get("sport", "")
            week_days = [wk + timedelta(days=x) for x in days]
            if slot.get("pick") != "one" or len(days) == 1:
                for d in week_days:
                    if start <= d <= end and not is_absent(d, sport, absences, family):
                        out.append({"date": d, "slot": slot, "multi": False})
                continue
            fam = family(sport)
            if any((fam, d.isoformat()) in done_days for d in week_days):
                continue                      # già fatto in questa settimana
            sid = slot.get("id", "")

            def ok(d):
                return (d >= today and (sid, d.isoformat()) not in skipped
                        and not is_absent(d, sport, absences, family))
            kept = keep_day.get((sid, wk.isoformat()))
            chosen = None
            if kept:
                kd = date.fromisoformat(kept)
                if kd in week_days and ok(kd):
                    chosen = kd
            if chosen is None:
                chosen = next((d for d in week_days if ok(d)), None)
            if chosen and start <= chosen <= end:
                out.append({"date": chosen, "slot": slot, "multi": True})
        wk += timedelta(days=7)
    out.sort(key=lambda x: (x["date"], x["slot"].get("id", "")))
    return out


def load_at(rpe: float, dur_min: float, sex: str = "M") -> float:
    """Carico (TRIMP) di una seduta di `dur_min` all'intensità `rpe` (1-10)."""
    return formmod.activity_load(None, dur_min, None, rpe, sex=sex)


def rpe_for_load(target: float, dur_min: float, sex: str = "M") -> float:
    """RPE (a mezzi punti) che porta una seduta di `dur_min` al carico `target`."""
    lo, hi = RPE_RANGE
    if dur_min <= 0:
        return lo
    if load_at(hi, dur_min, sex) <= target:
        return hi
    if load_at(lo, dur_min, sex) >= target:
        return lo
    a, b = lo, hi
    for _ in range(40):
        m = (a + b) / 2
        if load_at(m, dur_min, sex) < target:
            a = m
        else:
            b = m
    return round((a + b) / 2 * 2) / 2


# Intenzione del giorno -> scostamento dall'RPE tipico dello sport. Il budget scala
# il LIVELLO della settimana; questo mantiene la DIFFERENZA tra i giorni (martedì
# solo core resta più leggero del giovedì gambe carico).
INTENSITY_DELTA = {"leggera": -1.5, "media": 0.0, "intensa": 1.0}


def nominal_rpe(sport: str, intensity: str = "media") -> float:
    return formmod.sport_rpe(sport) + INTENSITY_DELTA.get(intensity, 0.0)


def allocate(target: float, done_load: float, external_loads: list[float],
             planned: list[tuple], sex: str = "M") -> dict:
    """Ripartisce il budget residuo sugli slot PIANIFICATI rimasti nella settimana.

    planned: [(key, sport, duration_min, intensity)]. Ritorna {key: {load, rpe,
    duration, nominal, ratio}} — ratio = carico assegnato / nominale (1 = seduta tipo)."""
    remaining = max(0.0, target - done_load - sum(external_loads or []))
    noms = {k: load_at(nominal_rpe(s, i), d, sex) for k, s, d, i in planned}
    tot = sum(noms.values())
    out = {}
    for k, s, d, _ in planned:
        nom = noms[k]
        share = remaining * nom / tot if tot else nom
        lo, hi = ALLOC_CLAMP
        tgt = min(max(share, lo * nom), hi * nom) if nom else 0.0
        out[k] = {"load": round(tgt), "rpe": rpe_for_load(tgt, d, sex), "duration": int(d),
                  "nominal": round(nom), "ratio": round(tgt / nom, 2) if nom else 1.0}
    return out


def needs_update(old_rpe: float | None, old_dur: int | None,
                 new_rpe: float, new_dur: int) -> bool:
    """Cambia abbastanza da valere una nuova seduta/proposta?"""
    if old_rpe is None or not old_dur:
        return True
    return abs(new_rpe - old_rpe) >= 1.0 or abs(new_dur - old_dur) > max(5, 0.15 * old_dur)


def is_proposal_day(d: date, today: date) -> bool:
    """Oggi e domani le modifiche diventano proposte da confermare."""
    return d <= today + timedelta(days=PROPOSAL_DAYS)


def _slug(s: str, used: set) -> str:
    import re
    base = re.sub(r"[^a-z0-9]+", "-", (s or "slot").lower()).strip("-")[:24] or "slot"
    out, n = base, 2
    while out in used:
        out, n = f"{base}-{n}", n + 1
    used.add(out)
    return out


def _int(v, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(float(v))))
    except (TypeError, ValueError):
        return default


def sanitize_routines(raw) -> list[dict]:
    """Routine fisse da input non fidato (AI o form) -> forma canonica."""
    used: set = set()
    out = []
    for r in raw if isinstance(raw, list) else []:
        if not isinstance(r, dict) or not str(r.get("name") or "").strip():
            continue
        out.append({"id": _slug(str(r.get("id") or r["name"]), used),
                    "name": str(r["name"]).strip()[:80],
                    "duration_min": _int(r.get("duration_min"), 15, 1, 180),
                    "content": str(r.get("content") or "").strip()[:3000],
                    "video_url": str(r.get("video_url") or "").strip()[:500]})
    return out


BIKE_WORDS = ("cicl", "bici", "bike", "rulli", "spinning", "mtb", "gravel", "cycl")
MIN_SLOT_MIN = 10      # sotto: valore mancante/assurdo (es. 0 o 5') -> durata di default
DEFAULT_SLOT_MIN = 45


def is_bike(sport: str) -> bool:
    s = (sport or "").lower()
    return any(w in s for w in BIKE_WORDS)


def sanitize_template(raw, routines: dict | set | None = None) -> list[dict]:
    """Slot della settimana tipo da input non fidato -> forma canonica.

    `routines`: {id: durata_min} (o solo gli id). Paletti: una seduta non dura
    meno delle routine che include; una durata mancante o assurda diventa il
    default; "indoor" ha senso solo per la bici."""
    used: set = set()
    rmins = routines if isinstance(routines, dict) else {r: 0 for r in routines or ()}
    out = []
    for sl in raw if isinstance(raw, list) else []:
        if not isinstance(sl, dict) or not str(sl.get("sport") or "").strip():
            continue
        days = sorted({d for d in (_int(x, -1, -1, 6) for x in sl.get("days") or []) if d >= 0})
        if not days:
            continue
        sport = str(sl["sport"]).strip()[:40]
        rts = [r for r in (sl.get("routines") or []) if r in rmins]
        dur = _int(sl.get("duration"), 0, 0, 480)
        if dur < MIN_SLOT_MIN:
            dur = DEFAULT_SLOT_MIN
        dur = max(dur, sum(int(rmins.get(r) or 0) for r in rts))
        out.append({
            "id": _slug(str(sl.get("id") or sport), used),
            "days": days,
            "pick": "one" if sl.get("pick") == "one" and len(days) > 1 else "each",
            "sport": sport,
            "kind": "external" if sl.get("kind") == "external" else "planned",
            "duration": dur,
            "focus": str(sl.get("focus") or "").strip()[:120],
            "intensity": sl.get("intensity") if sl.get("intensity") in INTENSITY_DELTA else "media",
            "routines": rts,
            "indoor": bool(sl.get("indoor")) and is_bike(sport),
            "enabled": sl.get("enabled", True) is not False,
        })
    return out


def block_label(pos: dict) -> str:
    pct = round((pos["multiplier"] - 1) * 100)
    tag = "scarico" if pos["deload"] else (f"{pct:+d}%" if pct else "base")
    return (f"Blocco {pos['block']} · settimana {pos['week_in_block']}/"
            f"{pos['block_weeks']} ({tag})")


def reason_text(pos: dict, alloc: dict | None, target: float, done: float) -> str:
    """Motivo leggibile della seduta, mostrato in UI."""
    base = f"{block_label(pos)} · settimana {round(done)}/{round(target)} TRIMP fatti"
    if not alloc:
        return base
    r = alloc["ratio"]
    adj = ("più leggera del solito" if r < 0.95 else
           "più intensa del solito" if r > 1.05 else "seduta tipo")
    return f"{base} · RPE {alloc['rpe']:g} ({adj})"
