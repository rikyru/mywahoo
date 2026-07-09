"""Fitness/form timeline from training load — the classic CTL/ATL/TSB model.

Per-activity load is estimated with a HR-based TRIMP (Banister) since most rides
have heart rate but not power. Then:
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


def trimp(avg_hr: float | None, dur_min: float, rest_hr: float, max_hr: float) -> float | None:
    """Banister HR-based training impulse for one activity (male coefficients)."""
    if not avg_hr or not dur_min or max_hr <= rest_hr:
        return None
    hrr = max(0.0, min(1.0, (avg_hr - rest_hr) / (max_hr - rest_hr)))
    return dur_min * hrr * 0.64 * math.exp(1.92 * hrr)


def daily_load(items: list[tuple], rest_hr: float, max_hr: float) -> dict:
    """items: (day: date, avg_hr, dur_min) -> {day: total load}."""
    loads: dict = {}
    for day, avg_hr, dur_min in items:
        t = trimp(avg_hr, dur_min, rest_hr, max_hr)
        if t is None:                    # no HR: light duration-based fallback
            t = (dur_min or 0) * 0.6
        loads[day] = loads.get(day, 0.0) + t
    return loads


def fitness_series(items: list[tuple], rest_hr: float = 55, max_hr: float = 190) -> list[dict]:
    """Daily CTL/ATL/TSB from the first activity to today (rest days = 0 load)."""
    days = [d for d, _, _ in items]
    if not days:
        return []
    loads = daily_load(items, rest_hr, max_hr)
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
