"""Quick smoke test: boot the app, exercise pages, webhook validation,
FIT helper functions (NP, downsampling, AI stats) with synthetic streams."""
import os
import tempfile

tmp = tempfile.mkdtemp()
os.environ.update({
    "WAHOO_CLIENT_ID": "123",
    "WAHOO_CLIENT_SECRET": "x",
    "WAHOO_REDIRECT_URI": "http://localhost:8080/oauth/callback",
    "WAHOO_WEBHOOK_TOKEN": "hook-secret",
    "ANTHROPIC_API_KEY": "test",
    "APP_SECRET_KEY": "test-secret",
    "APP_BASE_URL": "http://localhost:8080",
    "DB_PATH": os.path.join(tmp, "test.db"),
    "FIT_DIR": os.path.join(tmp, "fits"),
})

from fastapi.testclient import TestClient
from app.main import app

with TestClient(app) as client:
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}, r.text
    print("healthz OK")

    r = client.get("/login")
    assert r.status_code == 200 and "Accedi con Wahoo" in r.text
    print("login page OK")

    r = client.get("/", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/login"
    print("auth redirect OK")

    r = client.get("/login/wahoo", follow_redirects=False)
    loc = r.headers["location"]
    assert "api.wahooligan.com/oauth/authorize" in loc and "state=" in loc \
        and "workouts_read" in loc, loc
    print("wahoo authorize redirect OK")

    r = client.get("/oauth/callback?code=abc&state=wrong", follow_redirects=False)
    assert r.status_code == 303 and "invalid_state" in r.headers["location"]
    print("CSRF state validation OK")

    # --- webhook validation ---
    r = client.post("/webhook/wahoo", json={"event_type": "workout_summary"})
    assert r.status_code == 401, r.status_code
    r = client.post("/webhook/wahoo", json={"webhook_token": "wrong"})
    assert r.status_code == 401
    print("webhook rejects missing/bad token OK")

    r = client.post("/webhook/wahoo", json={
        "webhook_token": "hook-secret",
        "event_type": "workout_summary",
        "workout_summary": {"workout": {}},  # no id -> ignored in background
    })
    assert r.status_code == 200 and r.json() == {"status": "accepted"}, r.text
    print("webhook accepts valid token OK")

    # --- seed data and render pages ---
    from datetime import datetime, timedelta
    from sqlmodel import Session, select
    from app.db import AiAnalysis, WahooToken, Workout, WorkoutStream, engine, pack_streams
    recent = datetime.utcnow() - timedelta(days=3)  # within the default dashboard window

    streams = {
        "t": list(range(0, 3600, 2)),
        "power": [200 + (i % 50) for i in range(1800)],
        "hr": [140 + (i % 20) for i in range(1800)],
        "cadence": [88] * 1800,
        "speed": [8.5] * 1800,
        "alt": [120 + (i % 100) for i in range(1800)],
        "latlng": [[45.07 + i * 1e-5, 7.68 + i * 1e-5] for i in range(1800)],
    }
    with Session(engine) as s:
        s.add(WahooToken(id=1, user_id=1, user_name="Test", access_token="t",
                         refresh_token="r", expires_at=9999999999))
        s.add(Workout(id=7, name="Giro test", sport="Biking",
                      start_date=recent,
                      duration_s=3700, moving_s=3600, distance_m=30000,
                      ascent_m=420, avg_speed_ms=8.3, max_speed_ms=15.1,
                      avg_hr=148, max_hr=171, avg_power=210, max_power=520,
                      np_power=228, avg_cadence=88, calories=800, has_fit=True))
        s.add(WorkoutStream(workout_id=7, data=pack_streams(streams), n_records=1800))
        s.add(AiAnalysis(workout_id=7, content="## Valutazione\nBuon giro.",
                         model="claude-sonnet-4-6"))
        s.commit()

    import base64, json as jsonlib
    import itsdangerous
    signer = itsdangerous.TimestampSigner("test-secret")
    payload = base64.b64encode(jsonlib.dumps({"authed": True}).encode())
    client.cookies.set("ofit_session", signer.sign(payload).decode())

    r = client.get("/")
    assert r.status_code == 200 and "Giro test" in r.text and "km totali" in r.text
    print("dashboard render OK")

    r = client.get("/?period=week&sport=Biking&sort=distance&order=asc")
    assert r.status_code == 200
    print("dashboard filters OK")

    # "current state" strip: rendered inline, must not wait on Google Health
    assert "Forma" in r.text and "In programma" in r.text and "healthGlance" in r.text
    assert "fitness" in r.text and "freschezza" in r.text
    print("dashboard glance strip OK")

    from datetime import date as _date
    from app.main import fmt_day
    assert fmt_day(_date(2026, 7, 16)) == "Gio 16/07", fmt_day(_date(2026, 7, 16))
    assert fmt_day(_date(2026, 7, 19)) == "Dom 19/07"
    print("italian day names OK")

    r = client.get("/static/favicon.svg")
    assert r.status_code == 200 and r.text.startswith("<svg")
    r = client.get("/")
    assert 'rel="icon"' in r.text and "favicon.svg?v=" in r.text  # cache-busted per file
    assert "style.css?v=" in r.text
    print("favicon + per-file cache busting OK")

    r = client.get("/workout/7")
    assert r.status_code == 200 and "Buon giro" in r.text and "Rigenera" in r.text
    assert '"latlng"' in r.text  # downsampled streams embedded for charts/map
    print("workout detail + streams + cached analysis OK")

    r = client.get("/")
    assert r.status_code == 200 and "Analisi allenamenti" in r.text and "Analizza con AI" in r.text
    print("dashboard training analysis OK")

    r = client.get("/settings")
    assert r.status_code == 200 and "Motore AI" in r.text
    print("settings page OK")

    # --- calendar + training plans ---
    from app.db import ChatMessage, PlanSession, TrainingPlan
    with Session(engine) as s:
        plan = TrainingPlan(title="Piano test", goal="rimettersi in forma")
        s.add(plan)
        s.commit()
        s.refresh(plan)
        pid = plan.id
        ps = PlanSession(plan_id=pid, order=0, day_label="Lun", date=recent,
                         title="Circuito corpo libero", sport="Corpo libero",
                         duration_min=40, description="3x squat, 3x push up")
        s.add(ps)
        s.commit()
        s.refresh(ps)
        sid = ps.id

    r = client.get("/calendar")
    assert r.status_code == 200 and "Circuito corpo libero" in r.text
    print("calendar shows planned session OK")

    r = client.get("/plans")
    assert r.status_code == 200 and "Piano test" in r.text and "0/1" in r.text
    print("plans list OK")

    r = client.get(f"/plans/{pid}")
    assert r.status_code == 200 and "Circuito corpo libero" in r.text
    print("plan detail OK")

    # marking done records what was ACTUALLY done (overriding the plan). The RPE
    # estimate calls the AI, which the smoke test must not do: stub it out.
    import app.anthropic_client as ac
    async def _fake_rpe(*a, **k):
        return 7.5
    ac_real, ac.estimate_rpe = ac.estimate_rpe, _fake_rpe
    r = client.post(f"/plans/{pid}/session/{sid}/done", follow_redirects=False,
                    data={"done_notes": "3x12 squat, 3x8 push up, 2x1' plank",
                          "done_min": "45", "done_date": recent.strftime("%Y-%m-%d")})
    ac.estimate_rpe = ac_real
    assert r.status_code == 303
    with Session(engine) as s:
        done = s.get(PlanSession, sid)
        assert done.done and done.workout_id
        w = s.get(Workout, done.workout_id)
        assert w and w.manual and w.name == "Circuito corpo libero"
        assert w.notes == "3x12 squat, 3x8 push up, 2x1' plank", w.notes
        assert w.moving_s == 45 * 60, w.moving_s
        assert w.rpe == 7.5 and w.avg_hr is None  # estimate must not fake measured HR
    print("plan session done -> manual workout with actual notes + RPE OK")

    r = client.get(f"/workout/{w.id}")
    assert r.status_code == 200 and "Cosa hai fatto" in r.text and "3x12 squat" in r.text
    print("workout page shows notes OK")

    # the notes must reach the AI payloads (otherwise the analysis can't judge it)
    from app.anthropic_client import _activity_log, _health_payload
    log = _activity_log([w.model_dump()])
    assert log[0]["cosa_ha_fatto"].startswith("3x12 squat"), log
    assert log[0]["nome"] == "Circuito corpo libero"
    payload = _health_payload({"metrics": {}, "body": {}}, [w.model_dump()])
    assert "3x12 squat" in jsonlib.dumps(payload, default=str)
    print("notes reach AI health payload OK")

    r = client.post(f"/workout/{w.id}/edit", follow_redirects=False,
                    data={"name": "Circuito corpo libero", "sport": "Corpo libero",
                          "notes": "corretto: 4x12 squat"})
    assert r.status_code == 303
    with Session(engine) as s:
        assert s.get(Workout, w.id).notes == "corretto: 4x12 squat"
    print("workout notes editable OK")

    r = client.post(f"/plans/{pid}/session/{sid}/undo", follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        un = s.get(PlanSession, sid)
        assert not un.done and un.workout_id is None
        assert s.get(Workout, w.id) is None  # manual workout removed on undo
    print("plan session undo OK")

    # --- AI chat to adapt a session ("piove, dammi qualcosa a casa") ---
    captured = {}
    async def _fake_chat(ctx, history):
        captured["ctx"], captured["history"] = ctx, history
        return {"risposta": "Piove: ecco un circuito equivalente a casa.",
                "proposta": {"title": "Circuito indoor", "sport": "Corpo libero",
                             "durata_min": 50, "rpe": 6.0,
                             "description": "4 giri: 15 squat, 10 push up, 1' plank"}}
    ac.chat_plan_session, real_chat = _fake_chat, ac.chat_plan_session
    r = client.post(f"/plans/{pid}/session/{sid}/chat",
                    json={"message": "oggi piove, non esco in bici"})
    ac.chat_plan_session = real_chat
    assert r.status_code == 200, r.text
    d = r.json()
    assert "circuito equivalente" in d["reply"]
    # the AI must be told the planned load, the goal, the athlete and the form
    assert captured["ctx"]["sessione_pianificata"]["carico_trimp"] > 0
    assert captured["ctx"]["obiettivo_del_piano"] == "rimettersi in forma"
    assert "atleta" in captured["ctx"] and "forma_attuale" in captured["ctx"]
    assert captured["history"][-1]["content"] == "oggi piove, non esco in bici"
    # the proposal's load is computed here, not taken on trust from the AI
    assert d["proposal"]["carico_trimp"] > 0 and d["planned_load"] > 0
    print(f"plan session AI chat OK (pianificato ~{d['planned_load']}, "
          f"proposto ~{d['proposal']['carico_trimp']})")

    # thread persisted and readable (not raw JSON) on the Conversations page
    with Session(engine) as s:
        ps = s.get(PlanSession, sid)
        assert ps.conversation_id
        msgs = list(s.exec(select(ChatMessage).where(
            ChatMessage.conversation_id == ps.conversation_id)))
        assert len(msgs) == 2 and msgs[0].role == "user"
        assert "[Proposta] Circuito indoor" in msgs[1].content and "{" not in msgs[1].content
    r = client.get(f"/plans/{pid}")
    assert r.status_code == 200 and "Circuito indoor" in r.text  # thread replayed
    assert f'id="chatLog{sid}"' in r.text  # panel present while the session is open
    print("plan chat thread persisted + replayed OK")

    # --- edit the whole plan by chat ("mi aggiungi giovedì un corpo libero?") ---
    captured_plan = {}
    async def _fake_plan_chat(ctx, history):
        captured_plan["ctx"] = ctx
        return {"risposta": "Ok, aggiungo un corpo libero giovedì.",
                "azioni": [{"tipo": "aggiungi", "date": "2026-07-23", "day": "Gio 23/07",
                            "title": "Corpo libero", "sport": "Corpo libero",
                            "durata_min": 40, "description": "circuito full body"},
                           {"tipo": "modifica", "session_id": sid, "durata_min": 55},
                           {"tipo": "rimuovi", "session_id": 999999}]}  # bad id -> dropped
    ac.chat_plan, real_plan_chat = _fake_plan_chat, ac.chat_plan
    r = client.post(f"/plans/{pid}/chat", json={"message": "mi aggiungi giovedì un corpo libero?"})
    ac.chat_plan = real_plan_chat
    assert r.status_code == 200, r.text
    d = r.json()
    # the AI must see what's done, what's ahead, today, and the athlete
    assert captured_plan["ctx"]["oggi"] and captured_plan["ctx"]["sessioni"]
    assert any("stato" in row for row in captured_plan["ctx"]["sessioni"])
    # invalid action (unknown session id) is filtered out; two valid remain
    tipi = sorted(a["tipo"] for a in d["actions"])
    assert tipi == ["aggiungi", "modifica"], d["actions"]
    assert d["actions"][0]["carico_trimp"] > 0  # proposed session load quantified
    sessions_before = None
    with Session(engine) as s:
        sessions_before = len(s.exec(select(PlanSession).where(PlanSession.plan_id == pid)).all())

    # apply the confirmed actions
    import json as _json
    r = client.post(f"/plans/{pid}/apply", follow_redirects=False,
                    data={"actions": _json.dumps(d["actions"])})
    assert r.status_code == 303
    added_sid = None
    with Session(engine) as s:
        after = s.exec(select(PlanSession).where(PlanSession.plan_id == pid)).all()
        assert len(after) == sessions_before + 1  # one session added
        added = [x for x in after if x.title == "Corpo libero" and x.sport == "Corpo libero"]
        assert added and added[0].date.date() == _date(2026, 7, 23) and added[0].duration_min == 40
        assert s.get(PlanSession, sid).duration_min == 55  # the modify applied
        added_sid = added[0].id
    r = client.get(f"/plans/{pid}")
    assert r.status_code == 200 and "Modifica il piano con l'AI" in r.text
    print("plan-level AI edit: add + modify applied on confirm, bad action dropped OK")

    # a done session must never be edited/removed by the apply route
    with Session(engine) as s:
        ps = s.get(PlanSession, added_sid)
        ps.done = True
        s.add(ps); s.commit()
    r = client.post(f"/plans/{pid}/apply", follow_redirects=False,
                    data={"actions": _json.dumps([{"tipo": "rimuovi", "session_id": added_sid}])})
    with Session(engine) as s:
        assert s.get(PlanSession, added_sid) is not None  # done session untouched
        s.get(PlanSession, added_sid).done = False        # restore for later tests
        s.add(s.get(PlanSession, added_sid)); s.commit()
    print("plan-level edit guards done sessions OK")

    # --- move a planned session to another day (Wed -> Thu) ---
    from datetime import date as _date, time as _time
    r = client.post(f"/plans/{pid}/session/{sid}/edit", follow_redirects=False,
                    data={"title": "Nuoto", "sport": "Nuoto", "durata_min": "45",
                          "date": "2026-07-16", "description": "1500m"})
    assert r.status_code == 303
    with Session(engine) as s:
        moved = s.get(PlanSession, sid)
        assert moved.date.date() == _date(2026, 7, 16) and moved.sport == "Nuoto"
    r = client.get("/calendar?month=2026-07")
    assert r.status_code == 200 and "Nuoto" in r.text  # shows on the new day
    print("plan session rescheduled to another day OK")

    # --- uploaded FIT merges into the same-day plan/manual workout ---
    from app.main import _fit_merge_target
    from app.db import Workout as _W
    with Session(engine) as s:
        # a plan session marked done: manual swim at Thursday noon (placeholder)
        s.add(_W(id=555, name="Nuoto", sport="Nuoto", manual=True,
                 notes="1500m a stile", rpe=6.0,
                 start_date=datetime(2026, 7, 16, 12, 0)))
        # an unrelated same-day strength session that must NOT absorb a swim FIT
        s.add(_W(id=556, name="Forza", sport="Forza", manual=True,
                 start_date=datetime(2026, 7, 16, 12, 0)))
        s.commit()
        # real swim FIT done Thursday evening: merges into the manual swim,
        # never into the strength session at the same placeholder time
        m = _fit_merge_target(s, datetime(2026, 7, 16, 19, 30), "lap_swimming")
        assert m and m.id == 555, m
        # a family-less FIT ("training") only merges into a family-less manual
        # (the strength one), not the swim
        m2 = _fit_merge_target(s, datetime(2026, 7, 16, 20, 0), "training")
        assert m2 and m2.id == 556, m2
        # a bike FIT: no same-day bike manual and no ±25min neighbour -> new activity
        m3 = _fit_merge_target(s, datetime(2026, 7, 20, 8, 0), "cycling")
        assert m3 is None, m3
    print("FIT merge target: same-day manual swim, sport-guarded OK")

    # --- confirmed merge of a real recording into a manual/plan workout ---
    from app.main import _merge_candidate, _can_merge
    from app.db import pack_streams as _pack
    noon = datetime(2026, 6, 25, 12, 0)  # unique date, no other seeded workout here
    with Session(engine) as s:
        # a plan-done bodyweight workout: description, no HR, placeholder noon time
        s.add(_W(id=901, name="Circuito casa", sport="Corpo libero", manual=True,
                 notes="3x12 squat, 3x10 push up", rpe=6.5, start_date=noon,
                 moving_s=40 * 60, duration_s=40 * 60))
        # the real recording from the watch (Google Health), same day, evening
        s.add(_W(id=902, name="Strength", sport="Strength Training", manual=False,
                 start_date=datetime(2026, 6, 25, 19, 10), avg_hr=131, max_hr=158,
                 calories=280, moving_s=44 * 60, duration_s=46 * 60,
                 raw_summary='{"source": "google_health"}'))
        s.add(WorkoutStream(workout_id=902, data=_pack({"t": [0, 1], "hr": [120, 130]}),
                            n_records=2))
        # a same-day walk that must NOT be offered for a bodyweight manual
        s.add(_W(id=903, name="Passeggiata", sport="Walking", manual=False,
                 start_date=datetime(2026, 6, 25, 18, 0), avg_hr=95, distance_m=2000))
        s.commit()
        keep = s.get(_W, 901)
        cand = _merge_candidate(s, keep)
        assert cand and cand.id == 902, cand              # the strength rec, not the walk
        assert not _can_merge(keep, s.get(_W, 903))       # walk guarded out by sport

    r = client.post("/workout/901/merge", data={"absorb_id": "902"}, follow_redirects=False)
    assert r.status_code == 303
    from app.db import IgnoredImport as _Ign
    with Session(engine) as s:
        keep = s.get(_W, 901)
        assert keep.avg_hr == 131 and keep.calories == 280       # real data adopted
        assert keep.start_date.hour == 19                        # real time, not noon
        assert keep.notes.startswith("3x12") and keep.rpe == 6.5 # description kept
        assert keep.sport == "Corpo libero" and keep.manual      # user's label kept
        assert s.get(_W, 902) is None                            # duplicate removed
        assert s.get(WorkoutStream, 901) and s.get(WorkoutStream, 902) is None  # stream moved
        assert _merge_candidate(s, keep) is None                 # nothing left to merge
        # the absorbed Google import is blacklisted so a sync can't re-create it
        assert s.get(_Ign, 902) is not None

    # a stale/invalid pair is rejected (keep now has data; 903 is a walk anyway)
    r = client.post("/workout/901/merge", data={"absorb_id": "903"}, follow_redirects=False)
    assert r.status_code == 303 and "error" in r.headers["location"]
    print("confirmed merge: fused, description kept, guarded, no re-import OK")

    # "keep separate": dismiss must stop the suggestion for that pair, permanently
    with Session(engine) as s:
        s.add(_W(id=911, name="Yoga casa", sport="Yoga", manual=True, notes="mobilità",
                 start_date=datetime(2026, 6, 26, 12, 0), moving_s=1800, duration_s=1800))
        s.add(_W(id=912, name="Sessione", sport="Yoga", manual=False, avg_hr=88,
                 start_date=datetime(2026, 6, 26, 20, 0), moving_s=1800, duration_s=1800,
                 raw_summary='{"source": "google_health"}'))
        s.commit()
        assert _merge_candidate(s, s.get(_W, 911)).id == 912
    r = client.post("/workout/911/merge/dismiss", data={"absorb_id": "912"},
                    follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        assert _merge_candidate(s, s.get(_W, 911)) is None       # no longer suggested
        assert s.get(_W, 912) is not None                        # both kept, untouched
    print("merge dismiss: pair kept separate, not re-proposed OK")

    # --- profile: drives the TRIMP scale, so a wrong range skews all of Forma ---
    from app import profile as profilemod
    r = client.post("/settings/profile", follow_redirects=False,
                    data={"height_cm": "178", "weight_kg": "74.5", "birth_year": "1992",
                          "sex": "M", "rest_hr": "48", "max_hr": "",
                          "ai_notes": "Ginocchio sx delicato, niente salti. A casa solo tappetino."})
    assert r.status_code == 303
    p = profilemod.load()
    assert p["height_cm"] == 178 and p["weight_kg"] == 74.5 and p["rest_hr"] == 48
    assert profilemod.age(p) and profilemod.bmi(p) == 23.5, profilemod.bmi(p)
    print(f"profile saved OK (BMI {profilemod.bmi(p)}, età {profilemod.age(p)})")

    # the free-text memory must reach the AI context (so every prompt sees it)
    assert "Ginocchio sx delicato" in profilemod.ai_context(p)["note_da_rispettare"]
    r = client.get("/settings")
    assert r.status_code == 200 and "Ginocchio sx delicato" in r.text and "Note per l'AI" in r.text
    # a Google-Health profile sync must not wipe the note (partial save)
    profilemod.save({"weight_kg": 75.0})
    assert profilemod.load()["ai_notes"].startswith("Ginocchio")
    # and the note is length-capped so it can't blow up every prompt
    profilemod.save({"ai_notes": "x" * 5000})
    assert len(profilemod.load()["ai_notes"]) == profilemod.AI_NOTES_MAX
    profilemod.save({"ai_notes": "Ginocchio sx delicato, niente salti. A casa solo tappetino."})
    print("AI memory note: saved, reaches context, survives partial save, capped OK")

    # empty max_hr: higher of the measured peak and Tanaka (208-0.7*age)
    rest, mx, sex = profilemod.hr_anchors(p, measured_max=171)
    assert (rest, sex) == (48.0, "M") and abs(mx - (208 - 0.7 * profilemod.age(p))) < 0.1
    # a measured peak above the age estimate is real and must win
    assert profilemod.hr_anchors(p, measured_max=198)[1] == 198.0
    # an explicit value overrides both
    assert profilemod.hr_anchors({**p, "max_hr": 195}, measured_max=171)[1] == 195.0
    print(f"HR anchors OK (rest {rest:.0f}, max {mx:.0f} da Tanaka, 198 se misurata)")

    assert profilemod.ai_context(p)["peso_kg"] == 74.5
    print("profile reaches AI context OK")

    r = client.get("/settings")
    assert r.status_code == 200 and "I miei dati" in r.text and "178" in r.text
    print("settings profile section OK")

    r = client.get("/form")
    assert r.status_code == 200 and "Forma" in r.text
    print("form page renders with profile-driven load OK")

    # health card endpoint: served from cache on the second call (Google is ~9s)
    import app.main as mainmod
    calls = []
    async def _fake_overview(*a, **k):
        calls.append(1)
        return {"metrics": {"resting_hr": {"label": "FC a riposo", "unit": "bpm",
                                           "latest": 48, "dir": "down", "delta": -1,
                                           "series": [{"value": 48}]}},
                "body": {}, "sleep": [{"date": "2026-07-14", "asleep_min": 402}],
                "score": {"score": 71, "label": "Buono", "n_inputs": 3}}
    real_overview = mainmod.google_health.fetch_health_overview
    mainmod.google_health.fetch_health_overview = _fake_overview
    mainmod._HEALTH_CACHE.clear()
    try:
        r = client.get("/api/health/summary")
        assert r.status_code == 200
        d = r.json()
        assert d["score"]["score"] == 71 and d["sleep_h"] == 6.7, d
        assert d["metrics"][0]["value"] == 48
        client.get("/api/health/summary")
        assert len(calls) == 1, f"TTL cache not used: {len(calls)} calls"
        print(f"health summary endpoint OK (sonno {d['sleep_h']}h, cache hit)")
    finally:
        mainmod.google_health.fetch_health_overview = real_overview
        mainmod._HEALTH_CACHE.clear()

    # a Google outage must degrade the card, never break the dashboard
    async def _boom(*a, **k):
        raise mainmod.google_health.GoogleHealthError("token scaduto")
    mainmod.google_health.fetch_health_overview = _boom
    try:
        r = client.get("/api/health/summary")
        assert r.status_code == 502 and "token scaduto" in r.json()["error"]
        assert client.get("/").status_code == 200
        print("health outage degrades card only OK")
    finally:
        mainmod.google_health.fetch_health_overview = real_overview
        mainmod._HEALTH_CACHE.clear()

# --- load model: every source must land on the same TRIMP scale ---
from app.form import activity_load, sport_rpe, trimp

assert sport_rpe("Yoga") == 3.0 and sport_rpe("Corpo libero") == 6.0
assert sport_rpe("HIIT in casa") == 8.5 and sport_rpe("Sconosciuto") == 5.0
print("per-sport RPE defaults OK")

# Every label we tell the AI to use must map to a real icon and a per-sport RPE:
# an off-vocabulary label ("Home workout") silently gets the generic icon and 5.0
from app.anthropic_client import SPORT_VOCAB
from app.main import sport_icon
for label in [s.strip() for s in SPORT_VOCAB.split(",")]:
    assert sport_icon(label) != "🔵", f"{label}: icona generica"
    assert sport_rpe(label) != 5.0 or label == "Riposo", f"{label}: RPE default generico"
print(f"AI sport vocabulary maps to icons + RPE OK ({len(SPORT_VOCAB.split(','))} etichette)")

# measured HR wins over any estimate
m = activity_load(140, 60, "Cycling", rpe=2, rest_hr=55, max_hr=190)
assert abs(m - trimp(140, 60, 55, 190)) < 1e-9
print(f"measured HR takes precedence OK (TRIMP={m:.1f})")

# an explicit RPE must beat the sport default, and harder must mean more load
easy = activity_load(None, 40, "Corpo libero", rpe=3)
hard = activity_load(None, 40, "Corpo libero", rpe=9)
default = activity_load(None, 40, "Corpo libero")
assert easy < default < hard, (easy, default, hard)
# yoga must no longer cost the same as HIIT for the same duration
assert activity_load(None, 40, "Yoga") < activity_load(None, 40, "HIIT") / 2
print(f"RPE-based load OK (yoga={activity_load(None, 40, 'Yoga'):.0f} "
      f"< corpo libero={default:.0f} < HIIT={activity_load(None, 40, 'HIIT'):.0f})")

# --- nutrition: calories + macros from planmydinner's integration summary ---
from datetime import date as _d
from app.nutrition import _shape

_summary = {  # shape of planmydinner /integration/summary
    "adherence": {"planned_slots": 16, "free_meals": 3, "not_eaten_slots": 0,
                  "in_plan_consumed": 15, "adherence_score": 0.94, "free_meal_quota": 2},
    "days": [
        {"date": "2026-07-14", "free_meals": 0,
         "nutrition": {"kcal": 840.5, "protein_g": 68.4, "carbs_g": 101.2, "fat_g": 15.8}},
        {"date": "2026-07-15", "free_meals": 1,
         "nutrition": {"kcal": 1026.3, "protein_g": 56.6, "carbs_g": 97.0, "fat_g": 48.3}},
    ],
    "averages": {"kcal": 933.4, "protein_g": 62.5, "carbs_g": 99.1, "fat_g": 32.1,
                 "days_with_data": 2},
}
n = _shape(_summary, {"allergies": ["noci"]}, _d(2026, 7, 14), _d(2026, 7, 15), 74.5)
assert n["aderenza_al_piano"]["punteggio_pct"] == 94
trk = n["alimentazione_tracciata"]
assert trk["kcal_medie"] == 933 and trk["proteine_g_medie"] == 62
# protein-per-kg is the quality signal derived from profile weight
assert trk["proteine_g_per_kg"] == round(62.5 / 74.5, 2)
assert len(trk["per_giorno"]) == 2 and "PASTI TRACCIATI" in trk["nota"]
assert n["profilo"]["allergies"] == ["noci"]
# no data -> None, so the AI simply omits the whole section
assert _shape({"averages": {"days_with_data": 0}, "adherence": {}}, None,
              _d(2026, 7, 14), _d(2026, 7, 15), 74.5) is None
print(f"nutrition shape OK (kcal {trk['kcal_medie']}, "
      f"proteine {trk['proteine_g_per_kg']} g/kg, aderenza {n['aderenza_al_piano']['punteggio_pct']}%)")

# --- sleep: a daytime nap must not take a night's place, but must be counted ---
from app.google_health import _parse_sleep_point, _split_sleep

def _sleep_pt(start, end, asleep):
    return {"sleep": {"interval": {"startTime": start, "endTime": end},
                      "summary": {"minutesAsleep": asleep}, "stages": []}}

# night A (bed 14th evening), afternoon nap on the 15th, night B (bed 15th evening)
night_a = _sleep_pt("2026-07-14T23:00:00+02:00", "2026-07-15T07:00:00+02:00", 460)
nap_15  = _sleep_pt("2026-07-15T15:00:00+02:00", "2026-07-15T16:40:00+02:00", 95)
night_b = _sleep_pt("2026-07-15T23:30:00+02:00", "2026-07-16T07:00:00+02:00", 445)
fragment = _sleep_pt("2026-07-16T03:00:00+02:00", "2026-07-16T03:40:00+02:00", 38)  # <90 at night
micro_nap = _sleep_pt("2026-07-16T14:00:00+02:00", "2026-07-16T14:10:00+02:00", 9)   # <20 daytime

assert _parse_sleep_point(night_a)["kind"] == "night"
assert _parse_sleep_point(nap_15)["kind"] == "nap"          # long siesta is still a nap
assert _parse_sleep_point(fragment) is None                  # night fragment dropped
assert _parse_sleep_point(micro_nap) is None                 # daytime noise dropped
print("sleep classification (night / nap / noise) OK")

episodes = [_parse_sleep_point(p) for p in (night_a, nap_15, night_b)]
nights, naps = _split_sleep([e for e in episodes if e], days=7)
# the nap shares the 15th with night B but must NOT replace it
assert [n["date"] for n in nights] == ["2026-07-14", "2026-07-15"]
assert nights[-1]["asleep_min"] == 445  # last night is night B, not the 95' nap
assert "kind" not in nights[-1]         # internal tag stripped
assert [n["date"] for n in naps] == ["2026-07-15"] and naps[0]["asleep_min"] == 95
print("sleep split: nap kept for recovery, night preserved OK")

# a night split into two records on the same date collapses to one (the longest)
frag_nights, _ = _split_sleep([
    _parse_sleep_point(_sleep_pt("2026-07-14T21:30:00+02:00", "2026-07-14T23:00:00+02:00", 85)),
    _parse_sleep_point(_sleep_pt("2026-07-14T23:30:00+02:00", "2026-07-15T06:30:00+02:00", 400)),
], days=7)
assert len(frag_nights) == 1 and frag_nights[0]["asleep_min"] == 400
print("sleep dedupe: one night per date OK")

# --- same-day sport guard for merging a manual workout with a Google exercise ---
from app.google_health import _sameday_sport_ok
assert _sameday_sport_ok("Corpo libero", "strength_training")   # both family-less -> ok
assert _sameday_sport_ok("Forza", "calisthenics")
assert _sameday_sport_ok("Corsa", "running")                    # same known family
assert not _sameday_sport_ok("Corpo libero", "walking")         # walk known, manual family-less
assert not _sameday_sport_ok("Nuoto", "biking")                 # different known families
print("Google same-day sport guard OK")

# --- FIT helpers with synthetic data (no FIT file needed) ---
from app.fit import ai_stats, compute_normalized_power, downsample

np_val = compute_normalized_power(streams["power"], streams["t"])
assert np_val and 200 <= np_val <= 260, np_val
print(f"normalized power computation OK (NP={np_val})")

ds = downsample(streams, max_points=100)
assert len(ds["t"]) == 100 and len(ds["power"]) == 100
assert ds["latlng"] and len(ds["latlng"]) <= 1800
print("downsampling OK")

stats = ai_stats(streams)
assert stats["potenza_w"]["media"] > 0
assert len(stats["potenza_w"]["per_decimi_di_sessione"]) == 10
assert "drift_cardiaco_pct" in stats
print(f"AI stats OK (drift={stats['drift_cardiaco_pct']}%)")

print("\nALL SMOKE TESTS PASSED")
