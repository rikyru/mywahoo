"""Tempi cronometrati a nuoto: parsing dei tempi/passaggi e progressi per gara
(distanza + stile). Puro, senza DB."""
import re

STYLES = {"libero": "Stile libero", "dorso": "Dorso", "rana": "Rana",
          "delfino": "Delfino", "misti": "Misti"}
# ordine delle frazioni nei misti individuali
MEDLEY_ORDER = ["delfino", "dorso", "rana", "libero"]


def parse_time(text) -> float | None:
    """'1:23.45', "1'23\"45", '1.23.45', '83.4', '83,4' -> secondi. None se illeggibile."""
    s = str(text or "").strip().replace(",", ".").replace("''", '"')
    if not s:
        return None
    s = re.sub(r"[’'′]", ":", s).replace('"', ".").replace("″", ".")
    parts = s.split(":")
    if len(parts) == 1 and s.count(".") == 2:          # 1.23.45 = min.sec.cent
        m, sec, cent = s.split(".")
        parts = [m, f"{sec}.{cent}"]
    try:
        nums = [float(p) for p in parts if p != ""]
    except ValueError:
        return None
    if not nums or len(nums) > 3:
        return None
    total = 0.0
    for n in nums:
        total = total * 60 + n
    return round(total, 2) if total > 0 else None


def parse_splits(text, total_s: float | None, distance_m: int, pool_m: int = 25) -> list[float]:
    """Passaggi -> tempi delle singole vasche (s). Accetta sia i parziali di ogni
    vasca sia i passaggi cumulativi (es. 25 m a 0:16, 50 m a 0:34, ...): se i valori
    sono crescenti e superano le frazioni plausibili sono cumulativi. Il passaggio
    finale mancante viene completato con il tempo totale."""
    vals = [v for v in (parse_time(t) for t in re.split(r"[\s;/|]+|,(?=\s)", str(text or "")))
            if v]
    if not vals:
        return []
    laps_n = max(1, distance_m // pool_m)
    increasing = all(b > a for a, b in zip(vals, vals[1:]))
    cumulative = len(vals) > 1 and increasing and (
        (total_s and abs(vals[-1] - total_s) < 0.6)
        or (total_s and sum(vals) > total_s * 1.15)
        or vals[-1] > vals[0] * 1.6)
    if cumulative:
        if total_s and len(vals) == laps_n - 1 and vals[-1] < total_s:
            vals.append(total_s)
        laps = [vals[0]] + [round(b - a, 2) for a, b in zip(vals, vals[1:])]
    else:
        laps = vals
        if total_s and len(laps) == laps_n - 1 and sum(laps) < total_s:
            laps.append(round(total_s - sum(laps), 2))
    return [round(x, 2) for x in laps]


def fmt(s: float | None) -> str:
    """Secondi -> '1:23.45' (o '34.20' sotto il minuto)."""
    if s is None:
        return "–"
    m, sec = divmod(round(s, 2), 60)
    return f"{int(m)}:{sec:05.2f}" if m else f"{sec:.2f}"


def lap_labels(style: str, distance_m: int, pool_m: int, n: int) -> list[str]:
    """Etichette delle vasche: per i misti anche la frazione (delfino, dorso...)."""
    out = []
    per_leg = max(1, (distance_m // 4) // pool_m) if style == "misti" else 0
    for i in range(n):
        lab = f"{(i + 1) * pool_m} m"
        if per_leg:
            lab += f" · {MEDLEY_ORDER[min(3, i // per_leg)]}"
        out.append(lab)
    return out


def events(rows: list[dict]) -> list[dict]:
    """Raggruppa per gara (distanza+stile+vasca): record, ultimo, primo, progressi.
    `rows`: {id, date (iso), distance_m, style, time_s, splits, pool_m, note}."""
    groups: dict = {}
    for r in rows:
        groups.setdefault((r["distance_m"], r["style"], r.get("pool_m") or 25), []).append(r)
    out = []
    for (dist, style, pool), rs in groups.items():
        rs = sorted(rs, key=lambda r: r["date"])
        best = min(rs, key=lambda r: r["time_s"])
        first, last = rs[0], rs[-1]
        pb_so_far, attempts = None, []
        for r in rs:
            is_pb = pb_so_far is not None and r["time_s"] < pb_so_far   # migliora il record
            pb_so_far = r["time_s"] if (is_pb or pb_so_far is None) else pb_so_far
            splits = r.get("splits") or []
            attempts.append({**r, "pb": is_pb, "fmt": fmt(r["time_s"]),
                             "pace_100": fmt(r["time_s"] * 100 / dist),
                             "laps": list(zip(lap_labels(style, dist, pool, len(splits)),
                                              [fmt(x) for x in splits]))})
        out.append({
            "key": f"{dist}-{style}-{pool}", "distance_m": dist, "style": style, "pool_m": pool,
            "label": f"{dist} {STYLES.get(style, style).lower()}" + ("" if pool == 25 else f" (vasca {pool})"),
            "n": len(rs), "best": fmt(best["time_s"]), "best_date": best["date"],
            "best_s": best["time_s"], "last": fmt(last["time_s"]), "last_date": last["date"],
            "delta_s": round(last["time_s"] - first["time_s"], 2) if len(rs) > 1 else None,
            "delta_pct": round((last["time_s"] / first["time_s"] - 1) * 100, 1) if len(rs) > 1 else None,
            "series": [{"date": r["date"], "s": r["time_s"]} for r in rs],
            "attempts": attempts[::-1],
        })
    return sorted(out, key=lambda e: (-e["n"], e["distance_m"], e["style"]))
