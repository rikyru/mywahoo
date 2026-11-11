from datetime import datetime

from app.bodycomp import daily_weighins, is_repush, weighin_changes
from app.db import BodyMeasure


def _m(at, w, imp=513.0, fat=27.3, mus=56.3):
    return BodyMeasure(measured_at=at, weight_kg=w, impedance=imp, body_fat=fat, muscle_kg=mus)


def test_repush_and_same_day_merge():
    rows = [_m(datetime(2026, 10, 22, 5, 34), 81.65),
            *[_m(datetime(2026, 10, 22, h, 0), 81.65) for h in (10, 11, 12, 14)],   # re-invii HA
            _m(datetime(2026, 10, 23, 19, 0), 81.65),                                # re-invio il giorno dopo
            _m(datetime(2026, 10, 29, 3, 16), 82.1, 520, 27.6, 56.45),
            _m(datetime(2026, 10, 29, 7, 0), 81.9, 515, 27.4, 56.55)]               # 2 pesate vere stesso giorno
    days = daily_weighins(rows)
    assert [d["date"].isoformat() for d in days] == ["2026-10-22", "2026-10-29"]
    assert days[1]["n"] == 2 and round(days[1]["weight_kg"], 2) == 82.0
    assert round(days[1]["body_fat"], 2) == 27.5


def test_changes_vs_previous_weighin():
    days = daily_weighins([_m(datetime(2026, 11, 4, 6), 81.2, 550, 27.4, 55.94),
                           _m(datetime(2026, 11, 11, 6), 81.4, 520, 27.3, 56.19)])
    ch = {c["field"]: c for c in weighin_changes(days)}
    assert ch["weight_kg"]["delta"] == 0.2 and ch["weight_kg"]["tone"] == ""   # rumore bilancia
    assert ch["body_fat"]["delta"] == -0.1 and ch["body_fat"]["tone"] == "gd-good"
    assert ch["muscle_kg"]["delta"] == 0.2 and ch["muscle_kg"]["tone"] == "gd-good"


def test_is_repush_needs_same_impedance():
    a = _m(datetime(2026, 11, 1), 81.4, 520)
    assert is_repush(a, _m(datetime(2026, 11, 2), 81.4, 520))
    assert not is_repush(a, _m(datetime(2026, 11, 2), 81.4, 530))   # stesso peso, pesata nuova
