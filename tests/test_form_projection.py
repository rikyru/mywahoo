from datetime import date, timedelta

from app import form as F


def test_projection_continues_the_series_with_planned_loads():
    last = {"date": "2026-10-31", "ctl": 48.0, "atl": 39.7, "tsb": 8.3, "load": 58.8}
    loads = {date(2026, 11, 2): 75.0, date(2026, 11, 7): 70.0}
    p = F.project(last, loads, date(2026, 11, 7))
    assert [x["date"] for x in p][0] == "2026-11-01" and p[-1]["date"] == "2026-11-07"
    assert p[0]["load"] == 0 and p[0]["atl"] < 39.7          # giorno di riposo: la fatica scende
    assert p[1]["load"] == 75 and p[1]["atl"] > p[0]["atl"]   # seduta: risale
    assert all(abs(x["tsb"] - (x["ctl"] - x["atl"])) <= 0.11 for x in p)


def test_projection_matches_the_real_model():
    # stessi carichi nella serie storica e nella proiezione -> stessi numeri
    d0 = date.today() - timedelta(days=5)
    items = [(d0 + timedelta(days=i), 140, 60, "Ciclismo", None) for i in range(0, 6, 2)]
    series = F.fitness_series(items, rest_hr=60, max_hr=190)
    cut = series[:-3]
    loads = {date.fromisoformat(x["date"]): x["load"] for x in series[-3:]}
    p = F.project(cut[-1], loads, date.fromisoformat(series[-1]["date"]))
    assert [round(x["ctl"], 0) for x in p] == [round(x["ctl"], 0) for x in series[-3:]]


def test_no_projection_without_future():
    assert F.project({"date": "2026-10-31", "ctl": 1, "atl": 1}, {}, date(2026, 10, 31)) == []
