"""Parse a planned route GPX (e.g. exported from Komoot) into a summary:
distance, ascent/descent, gradients and an elevation profile. Used to assess a
route's feasibility against the rider's history."""
import math
import xml.etree.ElementTree as ET

from .fit import ascent_from_elevations

GRID_M = 25          # resample spacing for a stable elevation profile
GRAD_WINDOW_M = 100  # gradient computed over this rolling distance


class GpxError(Exception):
    pass


def _haversine(lat1, lon1, lat2, lon2) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _points(data: bytes) -> list[tuple]:
    """Extract (lat, lon, ele|None) from trkpt/rtept, namespace-agnostic."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise GpxError(f"GPX non valido: {e}") from e
    pts = []
    for el in root.iter():
        if el.tag.split("}")[-1] not in ("trkpt", "rtept"):
            continue
        try:
            lat, lon = float(el.get("lat")), float(el.get("lon"))
        except (TypeError, ValueError):
            continue
        ele = None
        for ch in el:
            if ch.tag.split("}")[-1] == "ele":
                try:
                    ele = float(ch.text)
                except (TypeError, ValueError):
                    ele = None
        pts.append((lat, lon, ele))
    return pts


def parse_gpx(data: bytes) -> dict:
    """Return a route summary dict. Raises GpxError on unusable input."""
    pts = _points(data)
    if len(pts) < 2:
        raise GpxError("Il GPX non contiene un percorso (troppo pochi punti).")

    # cumulative distance (m) + elevation carried over gaps
    cum = [0.0]
    for i in range(1, len(pts)):
        cum.append(cum[-1] + _haversine(pts[i - 1][0], pts[i - 1][1], pts[i][0], pts[i][1]))
    total_m = cum[-1]
    if total_m < 100:
        raise GpxError("Percorso troppo corto o coordinate non valide.")

    have_ele = any(p[2] is not None for p in pts)
    eles = []
    last = next((p[2] for p in pts if p[2] is not None), 0.0)
    for _, _, e in pts:
        last = e if e is not None else last
        eles.append(last)

    summary: dict = {
        "distance_km": round(total_m / 1000, 1),
        "n_points": len(pts),
        "ascent_m": None, "descent_m": None, "max_gradient_pct": None,
        "pct_over_6": None, "pct_over_10": None, "longest_climb_km": None,
        "ascent_per_km": None, "profile": [],
    }
    if not have_ele:
        return summary

    # resample elevation to a uniform GRID_M grid via linear interpolation
    grid_e, j = [], 0
    d = 0.0
    while d <= total_m:
        while j < len(cum) - 1 and cum[j + 1] < d:
            j += 1
        if cum[j + 1] == cum[j]:
            grid_e.append(eles[j])
        else:
            f = (d - cum[j]) / (cum[j + 1] - cum[j])
            grid_e.append(eles[j] + f * (eles[j + 1] - eles[j]))
        d += GRID_M
    # light smoothing
    sm = [sum(grid_e[max(0, i - 2):i + 3]) / len(grid_e[max(0, i - 2):i + 3])
          for i in range(len(grid_e))]

    ascent = ascent_from_elevations(sm)
    descent = ascent_from_elevations(list(reversed(sm)))

    w = max(1, GRAD_WINDOW_M // GRID_M)
    grads = [(sm[i + w] - sm[i]) / (w * GRID_M) * 100 for i in range(len(sm) - w)]
    over6 = sum(1 for g in grads if g > 6)
    over10 = sum(1 for g in grads if g > 10)
    # longest sustained climb: consecutive grid steps with gentle+ positive grade
    longest = cur = 0
    for g in grads:
        cur = cur + 1 if g > 1 else 0
        longest = max(longest, cur)

    km = total_m / 1000
    summary.update({
        "ascent_m": round(ascent),
        "descent_m": round(descent),
        "max_gradient_pct": round(max(grads), 1) if grads else None,
        "pct_over_6": round(over6 / len(grads) * 100) if grads else None,
        "pct_over_10": round(over10 / len(grads) * 100) if grads else None,
        "longest_climb_km": round(longest * GRID_M / 1000, 1),
        "ascent_per_km": round(ascent / km, 1) if km else None,
    })
    # downsample the profile for the chart (~200 points)
    step = max(1, len(sm) // 200)
    summary["profile"] = [[round(i * GRID_M / 1000, 2), round(sm[i])]
                          for i in range(0, len(sm), step)]
    return summary
