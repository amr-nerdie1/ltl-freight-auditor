"""One parser per carrier quote format, all returning the same shape:
    {"service": str, "total": float, "charges": {"LH": x, "FSC": y, "<ACCESSORIAL>": z, ...}}
"""
from __future__ import annotations

import json


def parse_kv(raw: str) -> dict:
    out = {"charges": {}}
    for part in raw.split("|"):
        k, v = part.split("=", 1)
        if k == "SVC":
            out["service"] = v
        elif k == "TOTAL":
            out["total"] = float(v)
        elif k.startswith("ACC:"):
            out["charges"][k[4:]] = float(v)
        else:
            out["charges"]["LH" if k == "LH" else "FSC"] = float(v)
    return out


def parse_simple_json(raw: str) -> dict:
    d = json.loads(raw)
    return {"service": d.get("service"), "total": float(d["total"]),
            "charges": {"LH": float(d["base"]), "FSC": float(d["fuel"]), **{k: float(v) for k, v in d.get("accessorials", {}).items()}}}


def parse_multi_json(raw: str, booked_total: float | None = None, booked_service: str | None = None) -> dict:
    """Several service levels per response. Pick the one actually booked, in three passes:
    1) total rate matches the booked total, 2) service level matches, 3) first valid entry.
    Taking the first entry blindly selected the wrong quote on a large share of orders."""
    qs = [q for q in json.loads(raw)["quotes"] if q.get("charges")]
    pick = None
    if booked_total is not None:
        pick = next((q for q in qs if abs(q["totalRate"] - booked_total) < 0.01), None)
    if pick is None and booked_service:
        pick = next((q for q in qs if q["serviceLevel"] == booked_service), None)
    pick = pick or qs[0]
    return {"service": pick["serviceLevel"], "total": float(pick["totalRate"]),
            "charges": {c["code"]: float(c["amt"]) for c in pick["charges"]}}


def parse_quote(q: dict, naive_first_quote: bool = False) -> dict:
    if q["format"] == "kv":
        return parse_kv(q["raw"])
    if q["format"] == "simple_json":
        return parse_simple_json(q["raw"])
    if naive_first_quote:
        return parse_multi_json(q["raw"])
    return parse_multi_json(q["raw"], q.get("booked_total"), q.get("booked_service"))
