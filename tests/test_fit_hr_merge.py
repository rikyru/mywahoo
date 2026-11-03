from datetime import datetime, timedelta

from app import google_health as G


def test_stale_fit_hr_is_detected():
    t = list(range(3600))
    stale = [104] * 900 + [130] * 900 + [140] * 1800          # 2 cambi in un'ora
    live = [100 + (i // 20) % 30 for i in t]                  # cambia ogni 20 s
    assert G.hr_changes_per_hour(t, stale) < G.STALE_HR_CHANGES_PER_HOUR
    assert G.hr_changes_per_hour(t, live) >= G.STALE_HR_CHANGES_PER_HOUR
    assert G.hr_changes_per_hour(t, [None] * 3600) is None


def test_merge_takes_nearest_google_sample_by_clock_time():
    start = datetime(2026, 11, 2, 18, 0, 53)
    # Google ogni 2 s, partendo 1 s dopo l'inizio
    samples = [(start + timedelta(seconds=1 + 2 * k), 90 + k) for k in range(60)]
    t = [0, 1, 2, 3, 10, 200]                 # 200 s: oltre i dati Google
    hr, hit = G.merge_hr(t, start, samples)
    assert hr[:5] == [90, 90, 90, 91, 94] or hr[:5] == [90, 90, 91, 91, 94]
    assert hr[-1] is None and hit == 5
