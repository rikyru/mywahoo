from datetime import datetime, timedelta

from sqlmodel import Session, SQLModel, create_engine

from app import nutrition
from app.db import BodyMeasure


def _engine(monkeypatch, weighins):
    eng = create_engine("sqlite://")
    SQLModel.metadata.create_all(eng)
    with Session(eng) as s:
        for at, kg in weighins:
            s.add(BodyMeasure(measured_at=at, weight_kg=kg))
        s.commit()
    monkeypatch.setattr(nutrition, "engine", eng)


def test_repeated_weighins_count_once(monkeypatch):
    t0 = datetime.utcnow() - timedelta(days=21)
    days = [(t0, 82.0), (t0 + timedelta(days=7), 81.5),
            (t0 + timedelta(days=14), 81.0), (t0 + timedelta(days=21), 80.5)]
    # stesso giorno ripetuto 9 volte con un valore fuori linea
    dup = [(t0 + timedelta(days=7, minutes=i), 83.0) for i in range(9)]
    _engine(monkeypatch, days + dup)
    tr = nutrition.weight_trend_kg_per_week()
    assert tr["n"] == 4
    # giorno 7 = media (81.5 + 9×83.0)/10 = 82.85 → il trend resta in calo
    assert tr["rate"] < 0


def test_trend_plain(monkeypatch):
    t0 = datetime.utcnow() - timedelta(days=21)
    _engine(monkeypatch, [(t0 + timedelta(days=7 * i), 82.0 - 0.5 * i) for i in range(4)])
    tr = nutrition.weight_trend_kg_per_week()
    assert abs(tr["rate"] + 0.5) < 1e-6
