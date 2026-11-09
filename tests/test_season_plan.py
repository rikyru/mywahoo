import json
from datetime import date, datetime

from sqlmodel import Session, SQLModel, create_engine

from app import program_runner as R
from app.db import PlanSession, Program, TrainingPlan

START = datetime(2026, 10, 22)        # blocchi da 4 settimane dal lunedì 19/10


def _prog(plan: dict | None, eng=None, fixed_done_on=None):
    p = Program(id=1, title="t", start_date=START, block_weeks=4, plan_id=1,
                block_multipliers_json="[1.0, 1.05, 1.1, 0.7]",
                season_json=json.dumps(plan) if plan else "")
    if eng is not None:
        with Session(eng) as s:
            s.add(TrainingPlan(id=1, title="p"))
            if fixed_done_on:
                s.add(PlanSession(plan_id=1, kind="fixed", done=True, slot_id="bici",
                                  date=datetime.combine(fixed_done_on, datetime.min.time())))
            s.commit()
    return p


def _engine(monkeypatch):
    eng = create_engine("sqlite://")
    SQLModel.metadata.create_all(eng)
    monkeypatch.setattr(R, "engine", eng)
    return eng


def test_needs_plan_first_time_block_start_and_after_test(monkeypatch):
    eng = _engine(monkeypatch)
    assert R.season_needs_plan(_prog(None, eng), date(2026, 11, 10)) == "primo piano di stagione"
    plan = {"generato": "2026-11-09", "settimane": [{"lunedi": "2026-11-09"}]}
    p = _prog(plan)
    assert R.season_needs_plan(p, date(2026, 11, 12)) is None            # metà blocco: niente
    assert "blocco 2" in R.season_needs_plan(p, date(2026, 11, 16))      # lunedì del blocco 2
    eng2 = _engine(monkeypatch)
    p = _prog(plan, eng2, fixed_done_on=date(2026, 12, 12))
    assert "test" in R.season_needs_plan(p, date(2026, 12, 13))


def test_week_theme_and_phase():
    plan = {"fasi": [{"blocco": 2, "nome": "Soglia", "obiettivo": "alzare la soglia"}],
            "settimane": [{"lunedi": "2026-11-16", "temi": {"bici-weekend": "Sweet spot 2×15′ 88–93%"}}]}
    tema, fase = R._week_theme(_prog(plan), "bici-weekend", date(2026, 11, 21))
    assert tema == "Sweet spot 2×15′ 88–93%" and fase["nome"] == "Soglia"
    assert R._week_theme(_prog(plan), "core", date(2026, 11, 21))[0] is None


def test_no_double_replan_right_after_test(monkeypatch):
    _engine(monkeypatch)
    # piano rifatto sabato 13/12 dopo il test: il lunedì 14 (inizio blocco 3) non si rifà
    plan = {"generato": "2026-12-13", "settimane": [{"lunedi": "2026-12-07"}]}
    assert R.season_needs_plan(_prog(plan), date(2026, 12, 14)) is None


def test_goal_rises_with_a_better_test():
    from app import main
    assert main._goal_ftp(159) == 180
    assert main._goal_ftp(172) == 185          # 172 × 1.08 = 185.8 -> 185
    assert main._goal_ftp(None) == 180
