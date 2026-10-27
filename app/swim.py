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


def laps_to_passages(values: dict, total_s: float, distance_m: int) -> tuple[list, str | None]:
    """Tempi delle singole vasche dal form {metri a fine vasca: "tempo vasca"} ->
    passaggi cumulativi [[metri, s], ...]. Vanno compilate dall'inizio senza buchi;
    l'ultima vasca, se vuota, si ricava dal tempo finale."""
    laps = []
    for m, txt in sorted(values.items()):
        if not str(txt or "").strip():
            continue
        t = parse_time(txt)
        if not t or not 0 < m <= distance_m:
            return [], f"Vasca ai {m} m non valida: «{txt}»"
        laps.append((m, t))
    if not laps:
        return [], None
    pool = laps[0][0]
    for i, (m, _) in enumerate(laps):
        if m != (i + 1) * pool:
            return [], (f"Manca il tempo della vasca ai {(i + 1) * pool} m: con i tempi per "
                        "vasca vanno messi in ordine dalla prima, senza buchi.")
    pts, cum = [], 0.0
    for m, t in laps:
        cum = round(cum + t, 2)
        pts.append([m, cum])
    if pts[-1][0] == distance_m:
        if abs(cum - total_s) > 1.0:
            return [], (f"Le vasche sommano {fmt(cum)} ma il tempo finale è {fmt(total_s)}: "
                        "controlla i tempi (o lascia vuota l'ultima vasca).")
        pts.pop()          # l'arrivo è il tempo finale
    elif cum >= total_s:
        return [], f"Le vasche inserite sommano già {fmt(cum)}, oltre il tempo finale {fmt(total_s)}."
    return pts, None


def parse_passages(values: dict, total_s: float, distance_m: int) -> tuple[list, str | None]:
    """Passaggi dal form: {metri: "tempo letto al cronometro"} (vuoti ammessi) ->
    ([[metri, secondi], ...] crescenti, errore|None). Il tempo finale non serve: è
    il tempo totale."""
    pts = []
    for m, txt in sorted(values.items()):
        if not str(txt or "").strip():
            continue
        t = parse_time(txt)
        if not t or not 0 < m < distance_m:
            return [], f"Passaggio ai {m} m non valido: «{txt}»"
        pts.append([m, t])
    prev = 0.0
    for m, t in pts + [[distance_m, total_s]]:
        if t <= prev:
            return [], (f"I passaggi devono crescere: ai {m} m hai {fmt(t)}, prima {fmt(prev)}. "
                        "Scrivi il tempo letto sul cronometro in quel punto, non quello della vasca.")
        prev = t
    # anche i tempi delle singole vasche possono crescere (17.5, 19.5, 21.9): un
    # passaggio a meno di metà del tempo "a passo medio" è quasi certamente quello
    for m, t in pts:
        if t < total_s * m / distance_m * 0.5:
            return [], (f"Ai {m} m {fmt(t)} è troppo veloce per un arrivo in {fmt(total_s)}: "
                        "scrivi il tempo letto sul cronometro in quel punto, non quello della vasca.")
    return pts, None


def fmt(s: float | None) -> str:
    """Secondi -> '1:23.45' (o '34.20' sotto il minuto)."""
    if s is None:
        return "–"
    m, sec = divmod(round(s, 2), 60)
    return f"{int(m)}:{sec:05.2f}" if m else f"{sec:.2f}"


def medley_leg(distance_m: int, at_m: int) -> str:
    """Frazione dei misti in cui cade il tratto che finisce a `at_m`."""
    return MEDLEY_ORDER[min(3, max(0, -(-at_m * 4 // distance_m) - 1))]


def as_passages(splits, pool_m: int) -> list:
    """Normalizza i passaggi salvati in [[metri, s cumulativi], ...]; accetta anche
    il vecchio formato (tempi delle singole vasche)."""
    if splits and not isinstance(splits[0], (list, tuple)):
        out, cum = [], 0.0
        for i, lap in enumerate(splits):
            cum += lap
            out.append([(i + 1) * pool_m, round(cum, 2)])
        return out
    return [list(p) for p in splits or []]


def segments(passages: list, total_s: float, distance_m: int, style: str) -> list[dict]:
    """Tratti tra un passaggio e il successivo (fino all'arrivo): tempo al passaggio
    e tempo del solo tratto."""
    pts = [p for p in passages if p[0] < distance_m] + [[distance_m, total_s]]
    if len(pts) == 1:
        return []
    out, prev_m, prev_t = [], 0, 0.0
    for m, t in pts:
        out.append({"at": f"{m} m" + (f" · {medley_leg(distance_m, m)}" if style == "misti" else ""),
                    "cum": fmt(t), "split": fmt(t - prev_t), "span": m - prev_m})
        prev_m, prev_t = m, t
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
            attempts.append({**r, "pb": is_pb, "fmt": fmt(r["time_s"]),
                             "pace_100": fmt(r["time_s"] * 100 / dist),
                             "segments": segments(as_passages(r.get("splits"), pool),
                                                  r["time_s"], dist, style)})
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
