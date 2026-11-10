from datetime import datetime

from app.db import BodyGirth
from app.main import girth_changes, girth_tone


def test_girth_changes_vs_previous_and_first():
    rows = [BodyGirth(id=1, measured_at=datetime(2026, 9, 1), vita_cm=98.0, braccio_cm=33.0),
            BodyGirth(id=2, measured_at=datetime(2026, 9, 20), vita_cm=97.0),
            BodyGirth(id=3, measured_at=datetime(2026, 10, 1), vita_cm=96.4, braccio_cm=33.5)]
    deltas, summary = girth_changes(rows)
    assert deltas[2] == {"vita": -1.0}
    assert deltas[3] == {"vita": -0.6, "braccio": 0.5}   # braccio vs la misura del 1/9
    vita = next(s for s in summary if s["field"] == "vita")
    assert (vita["value"], vita["prev"], vita["total"], vita["days"]) == (96.4, -0.6, -1.6, 30)
    assert [s["field"] for s in summary] == ["vita", "braccio"]


def test_girth_tone():
    assert girth_tone("vita", -1.0) == "gd-good"
    assert girth_tone("vita", 0.8) == "gd-warn"
    assert girth_tone("vita", 0.1) == ""          # rumore
    assert girth_tone("braccio", 1.0) == ""       # verso non univoco
