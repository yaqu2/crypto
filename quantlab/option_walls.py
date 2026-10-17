"""Descriptive option open-interest levels, never a directional trade signal."""

import math
from collections import defaultdict


def summarize(rows: list[dict], spot: float, *, max_distance_pct: float = 20) -> dict:
    if not math.isfinite(spot) or spot <= 0:
        raise ValueError("positive underlying spot is required")
    if not 0 < max_distance_pct <= 100:
        raise ValueError("max_distance_pct must be in (0, 100]")
    walls = defaultdict(float)
    excluded = defaultdict(int)
    for row in rows:
        option_type = str(row.get("optionType", "")).upper()
        expiry = str(row.get("expiry", ""))[:10]
        try:
            strike = float(row["strike"])
            oi = float(row["openInterest"])
            contract_size = float(row["contractSize"])
        except (KeyError, TypeError, ValueError):
            excluded["INVALID_NUMBER"] += 1
            continue
        if option_type not in ("CALL", "PUT") or not expiry or not all(
            math.isfinite(x) for x in (strike, oi, contract_size)
        ) or strike <= 0 or oi < 0 or contract_size <= 0:
            excluded["INVALID_ROW"] += 1
            continue
        if abs(strike / spot - 1) * 100 > max_distance_pct:
            excluded["OUTSIDE_DISTANCE"] += 1
            continue
        walls[(expiry, option_type, strike)] += oi * contract_size
    groups = defaultdict(list)
    for (expiry, option_type, strike), share_equivalent in walls.items():
        groups[(expiry, option_type)].append({
            "strike": strike, "shareEquivalentOpenInterest": round(share_equivalent, 4),
            "distanceFromSpotPct": round((strike / spot - 1) * 100, 4),
        })
    results = []
    for (expiry, option_type), levels in sorted(groups.items()):
        results.append({"expiry": expiry, "optionType": option_type,
                        "topLevels": sorted(levels, key=lambda x: (-x["shareEquivalentOpenInterest"],
                                                                   abs(x["distanceFromSpotPct"])))[:3]})
    return {"status": "OBSERVATIONAL_OI_LEVELS_ONLY" if results else "NO_GO_NO_USABLE_OPEN_INTEREST",
            "spot": spot, "maxDistancePct": max_distance_pct, "groups": results,
            "excludedRows": dict(excluded), "directionalForecast": None,
            "warning": "High open interest is not proof of price attraction, dealer positioning, or executable alpha."
                       " Open interest can lag, and adjusted contracts may be incomparable."}
