from datetime import datetime, timedelta

from app import fit as F

START = datetime(2026, 11, 2, 18, 0, 0)


def _lap(start_s, time_s, dist=50):
    return {"i": 0, "distance_m": dist, "time_s": time_s, "avg_hr": 104,
            "start": (START + timedelta(seconds=start_s)).isoformat()}


def test_lap_hr_avg_peak_with_tail_and_rest():
    # 0-45 s ripetuta (FC sale 120->150), pausa 45-75 s (picco 160 a 55 s, poi scende), 75-120 s seconda
    hr = [120 + i * 30 // 45 for i in range(45)] + [160 if i == 10 else 150 - i for i in range(30)] + [130] * 45
    streams = {"t": list(range(120)), "hr": hr}
    out = F.lap_hr([_lap(0, 45), _lap(75, 45)], streams, START)
    a, b = out
    assert 130 <= a["avg_hr"] <= 140                 # media nel tratto, non il 104 "fermo"
    assert a["peak_hr"] == 160                        # picco arrivato 10 s dopo lo stop
    assert a["rest_s"] == 30 and a["rest_end_hr"] == 123   # FC a fine pausa (ultimi 5 s)
    assert b["avg_hr"] == 130 and "rest_s" not in b


def test_lap_hr_untouched_without_start_or_hr():
    laps = [{"i": 1, "distance_m": 50, "time_s": 45, "avg_hr": 104, "start": None}]
    assert F.lap_hr(laps, {"t": [0, 1], "hr": [100, 101]}, START) == laps
    laps = [_lap(0, 45)]
    assert F.lap_hr(laps, {"t": [0, 1], "hr": [None, None]}, START) == laps
