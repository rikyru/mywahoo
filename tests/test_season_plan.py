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


def test_easy_extra_classification():
    from datetime import datetime as dt
    from app.db import Workout
    anchors = (66.0, 197.0, "M")
    z2 = Workout(id=1, sport="Ciclismo indoor", start_date=dt(2026, 11, 10), avg_hr=120, avg_power=110, moving_s=3600)
    hard = Workout(id=2, sport="Ciclismo indoor", start_date=dt(2026, 11, 10), avg_hr=150, avg_power=150, moving_s=3600)
    walk = Workout(id=3, sport="Camminata", start_date=dt(2026, 11, 10), moving_s=3600)
    hike_hard = Workout(id=4, sport="Camminata", start_date=dt(2026, 11, 10), avg_hr=160, moving_s=7200)
    assert R.is_easy_extra(z2, anchors, 159) and not R.is_easy_extra(hard, anchors, 159)
    assert R.is_easy_extra(z2, anchors) and not R.is_easy_extra(hard, anchors)     # solo FC
    assert R.is_easy_extra(walk, anchors) and not R.is_easy_extra(hike_hard, anchors)


def test_analysis_context_has_program_choices(monkeypatch):
    # le analisi AI devono vedere fase, rulli, tappe future, assenze future e sedute già decise
    eng = _engine(monkeypatch)
    plan = {"fasi": [{"blocco": 1, "nome": "Scarico", "obiettivo": "assorbire"},
                     {"blocco": 2, "nome": "Base sweet spot", "obiettivo": "88-95%"}],
            "tappe": [{"data": "2026-11-01", "cosa": "vecchia"},
                      {"data": "2026-12-12", "cosa": "Test FTP"}]}
    p = _prog(plan, eng)
    p.notes = "d'inverno bici sui rulli"
    p.equipment_json = json.dumps(["tappetino", "rulli"])
    p.template_json = json.dumps([{"id": "bici", "sport": "Ciclismo", "days": [5, 6], "pick": "one",
                                   "duration": 60, "indoor": True, "enabled": True}])
    p.absences_json = json.dumps([{"start": "2026-11-01", "end": "2026-11-02"},
                                  {"start": "2026-12-22", "end": "2027-01-06", "note": "vacanze"}])
    with Session(eng) as s:
        s.add(p)
        s.add(PlanSession(plan_id=1, title="Rulli sweet spot", sport="Ciclismo",
                          date=datetime(2026, 11, 21), duration_min=60))
        s.add(PlanSession(plan_id=1, title="troppo lontana", sport="Nuoto",
                          date=datetime(2026, 12, 1), duration_min=60))
        s.commit()
    c = R.analysis_context(159, today=date(2026, 11, 17))
    assert c["fase_del_blocco"]["nome"] == "Base sweet spot"
    assert c["note_programma"] == "d'inverno bici sui rulli"
    assert c["settimana_tipo"][0]["indoor_sui_rulli"] and c["settimana_tipo"][0]["giorni"] == ["sab", "dom"]
    assert [t["cosa"] for t in c["prossime_tappe"]] == ["Test FTP"]
    assert [a.get("note") for a in c["assenze_in_arrivo"]] == ["vacanze"]
    assert [x["titolo"] for x in c["prossime_sedute_gia_pianificate"]] == ["Rulli sweet spot"]
    assert c["ftp_attuale_w"] == 159
