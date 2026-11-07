from datetime import datetime, timedelta

from sqlmodel import Session, SQLModel, create_engine, select

from app import db, main
from app.db import IgnoredImport, PlanSession, TrainingPlan, Workout

T = datetime(2026, 10, 19, 17, 58)


def _engine(monkeypatch, rows, plan_wid=None):
    eng = create_engine("sqlite://")
    SQLModel.metadata.create_all(eng)
    with Session(eng) as s:
        for r in rows:
            s.add(r)
        if plan_wid:
            s.add(TrainingPlan(id=1, title="p"))
            s.add(PlanSession(plan_id=1, workout_id=plan_wid, sport="Nuoto"))
        s.commit()
    monkeypatch.setattr(main, "engine", eng)
    monkeypatch.setattr(db, "engine", eng)
    return eng


def _ids(eng):
    with Session(eng) as s:
        return {w.id for w in s.exec(select(Workout)).all()}


def test_wahoo_stub_folds_into_fit_and_is_blacklisted(monkeypatch):
    fit = Workout(id=1, name="Nuoto master", sport="Swimming", start_date=T, duration_s=3800,
                  moving_s=3800, distance_m=2200, avg_hr=134, has_fit=True, manual=True)
    stub = Workout(id=2, name="Nuotata serale", sport="Nuoto", start_date=T, duration_s=6600,
                   moving_s=6600, distance_m=2200, avg_hr=135, calories=640)
    ride = Workout(id=3, name="Bici", sport="Cycling", start_date=T + timedelta(minutes=5),
                   duration_s=900, distance_m=4500, avg_hr=120)        # altro sport: resta
    eng = _engine(monkeypatch, [fit, stub, ride])
    assert main.reconcile_fit_duplicates() == 1
    assert _ids(eng) == {1, 3}
    with Session(eng) as s:
        keep = s.get(Workout, 1)
        assert keep.avg_hr == 134 and keep.calories == 640   # il FIT vince, si completano i vuoti
        assert s.get(IgnoredImport, 2)                       # Wahoo/Google non lo ricreano


def test_plan_rows_and_far_activities_untouched(monkeypatch):
    fit = Workout(id=1, name="Nuoto", sport="Swimming", start_date=T, duration_s=3800, has_fit=True)
    plan = Workout(id=2, name="Nuoto — corso", sport="Nuoto", start_date=T, duration_s=3800, manual=True)
    later = Workout(id=3, name="Nuotata", sport="Nuoto", start_date=T + timedelta(hours=3), duration_s=3600)
    eng = _engine(monkeypatch, [fit, plan, later], plan_wid=2)
    assert main.reconcile_fit_duplicates() == 0
    assert _ids(eng) == {1, 2, 3}
