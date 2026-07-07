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
    from datetime import datetime
    from sqlmodel import Session
    from app.db import AiAnalysis, WahooToken, Workout, WorkoutStream, engine, pack_streams

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
                      start_date=datetime(2026, 6, 9, 7, 0),
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
