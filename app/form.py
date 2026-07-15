"""Fitness/form timeline from training load — the classic CTL/ATL/TSB model.

Per-activity load is estimated with a HR-based TRIMP (Banister) since most rides
have heart rate but not power. Activities without heart rate (home/bodyweight
sessions) fall back to an intensity estimate (RPE) mapped onto the same scale.
Then:
  CTL  = 42-day exponentially weighted load  -> "fitness" (chronic load)
  ATL  =  7-day exponentially weighted load  -> "fatigue" (acute load)
  TSB  = CTL - ATL                           -> "form" / freshness
Absolute values depend on the rest/max HR assumptions, but the TREND (are you
building fitness?) is robust to constant offsets.
"""
import math
from datetime import date, timedelta

CTL_DAYS = 42
ATL_DAYS = 7


def _trimp_from_hrr(hrr: float, dur_min: float, sex: str = "M") -> float:
    """Banister TRIMP given the heart-rate reserve fraction already computed."""
    a, b = (0.86, 1.67) if sex == "F" else (0.64, 1.92)
    hrr = max(0.0, min(1.0, hrr))
    return dur_min * hrr * a * math.exp(b * hrr)


def trimp(avg_hr: float | None, dur_min: float, rest_hr: float, max_hr: float,
          sex: str = "M") -> float | None:
    """Banister HR-based training impulse for one activity."""
    if not avg_hr or not dur_min or max_hr <= rest_hr:
        return None
    return _trimp_from_hrr((avg_hr - rest_hr) / (max_hr - rest_hr), dur_min, sex)


# Typical RPE (1-10) per sport, used only when an activity has neither heart rate
# nor an AI/user intensity estimate. Keyword-matched because manual and AI-created
# sessions use free-text sport labels.
SPORT_RPE = (
    (("yoga", "mobilit", "stretch", "pilates", "riposo", "rest"), 3.0),
    (("cammin", "walk", "hiking", "escursion", "trekking"), 3.5),
    (("hiit", "tabata", "sprint", "interval"), 8.5),
    (("corsa", "run", "jog"), 7.0),
    (("nuoto", "swim"), 6.5),
    (("forza", "strength", "pesi", "weight", "corpo libero", "calisthen", "circuit"), 6.0),
    (("bici", "cycl", "bike", "spinning", "mtb"), 6.0),
)
DEFAULT_RPE = 5.0


def sport_rpe(sport: str | None) -> float:
    s = (sport or "").lower()
    for keys, rpe in SPORT_RPE:
        if any(k in s for k in keys):
            return rpe
    return DEFAULT_RPE


def activity_load(avg_hr, dur_min, sport=None, rpe=None,
                  rest_hr: float = 55, max_hr: float = 190, sex: str = "M") -> float:
    """Load for one activity, best source first: measured HR > estimated RPE >
    typical RPE for the sport. RPE 1-10 is read as a %HRR fraction so every
    source lands on the same Banister TRIMP scale."""
    t = trimp(avg_hr, dur_min, rest_hr, max_hr, sex)
    if t is not None:
        return t
    r = rpe if rpe else sport_rpe(sport)
    return _trimp_from_hrr(r / 10.0, dur_min or 0, sex)


def daily_load(items: list[tuple], rest_hr: float, max_hr: float, sex: str = "M") -> dict:
    """items: (day: date, avg_hr, dur_min, sport, rpe) -> {day: total load}."""
    loads: dict = {}
    for day, avg_hr, dur_min, sport, rpe in items:
        loads[day] = loads.get(day, 0.0) + activity_load(
            avg_hr, dur_min, sport, rpe, rest_hr, max_hr, sex)
    return loads


def fitness_series(items: list[tuple], rest_hr: float = 55, max_hr: float = 190,
                   sex: str = "M") -> list[dict]:
    """Daily CTL/ATL/TSB from the first activity to today (rest days = 0 load)."""
    days = [i[0] for i in items]
    if not days:
        return []
    loads = daily_load(items, rest_hr, max_hr, sex)
    ctl = atl = 0.0
    out = []
    d, end = min(days), date.today()
    while d <= end:
        load = loads.get(d, 0.0)
        ctl += (load - ctl) / CTL_DAYS
        atl += (load - atl) / ATL_DAYS
        out.append({"date": d.isoformat(), "ctl": round(ctl, 1),
                    "atl": round(atl, 1), "tsb": round(ctl - atl, 1),
                    "load": round(load, 1)})
        d += timedelta(days=1)
    return out


def summarize(series: list[dict]) -> dict:
    """Current state + trend for the summary cards."""
    if not series:
        return {}
    last = series[-1]
    ref = series[-31] if len(series) > 31 else series[0]
    ctl_now, ctl_ref = last["ctl"], ref["ctl"]
    delta_pct = round((ctl_now - ctl_ref) / ctl_ref * 100) if ctl_ref else 0
    trend = ("in miglioramento" if delta_pct > 5 else
             "in calo" if delta_pct < -5 else "stabile")
    tsb = last["tsb"]
    state = ("fresco / scarico" if tsb > 5 else
             "in equilibrio" if tsb > -10 else
             "in carico" if tsb > -20 else "molto affaticato")
    return {"ctl": ctl_now, "atl": last["atl"], "tsb": tsb,
            "trend": trend, "delta_pct": delta_pct, "state": state,
            "ctl_ref": ctl_ref}
