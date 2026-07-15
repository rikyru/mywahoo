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
    from sqlmodel import Session
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
    from app.db import PlanSession, TrainingPlan
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

    # marking done records what was ACTUALLY done (overriding the plan)
    r = client.post(f"/plans/{pid}/session/{sid}/done", follow_redirects=False,
                    data={"done_notes": "3x12 squat, 3x8 push up, 2x1' plank",
                          "done_min": "45", "done_date": recent.strftime("%Y-%m-%d")})
    assert r.status_code == 303
    with Session(engine) as s:
        done = s.get(PlanSession, sid)
        assert done.done and done.workout_id
        w = s.get(Workout, done.workout_id)
        assert w and w.manual and w.name == "Circuito corpo libero"
        assert w.notes == "3x12 squat, 3x8 push up, 2x1' plank", w.notes
        assert w.moving_s == 45 * 60, w.moving_s
    print("plan session done -> manual workout with actual notes OK")

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
