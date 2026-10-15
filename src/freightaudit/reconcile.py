"""Quote-vs-bill reconciliation at charge-line level."""
from __future__ import annotations

import pandas as pd

from .parsers import parse_quote

OVERBILL_THRESHOLD = 15.0


def clean_edi(edi: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Drop exact re-transmissions (same invoice + line number) and isolate return legs.
    A repeated charge on a *new* line number is a real duplicate charge and is kept for audit.
    Keep line granularity: pre-aggregating would hide which charge drives the variance."""
    n0 = len(edi)
    e = edi.drop_duplicates()
    resent = n0 - len(e)
    returns = e[e.shipment == "RETURN"]
    e = e[e.shipment == "DELIVERY"]
    return e, {"resent_lines_dropped": resent, "return_lines_isolated": len(returns)}


def reconcile(world, naive: bool = False) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Returns (order_summary, line_detail, data_quality)."""
    edi = pd.DataFrame(world.edi)
    if naive:
        e, dq = edi, {"resent_lines_dropped": 0, "return_lines_isolated": 0}
    else:
        e, dq = clean_edi(edi)
    quotes = {q["po"]: q for q in world.quotes}
    orders = {o["po"]: o for o in world.orders}

    rows, lines = [], []
    for po, g in e.groupby("po"):
        q = quotes[po]
        parsed = parse_quote(q, naive_first_quote=naive)
        billed = g.groupby("charge_code").amount.sum().to_dict()      # sums across split invoices
        line_counts = g.groupby("charge_code").size().to_dict()
        codes = sorted(set(billed) | set(parsed["charges"]))
        for c in codes:
            qa, ba = parsed["charges"].get(c, 0.0), billed.get(c, 0.0)
            lines.append({"po": po, "charge_code": c, "quoted": qa, "billed": round(ba, 2),
                          "variance": round(ba - qa, 2), "billed_lines": line_counts.get(c, 0),
                          "unquoted": c not in parsed["charges"] and ba > 0})
        bw = g.loc[g.charge_code == "LH", "billed_weight"].dropna()
        variance = round(sum(billed.values()) - parsed["total"], 2)
        rows.append({"po": po, "carrier_billed": g.carrier.iloc[0], "carrier_quoted": q["carrier"],
                     "vendor": orders[po]["vendor"], "quoted_total": parsed["total"], "billed_total": round(sum(billed.values()), 2),
                     "variance": variance, "flagged": variance > OVERBILL_THRESHOLD,
                     "declared_weight": orders[po]["declared_weight"], "billed_weight": float(bw.iloc[0]) if len(bw) else None,
                     "residential": orders[po]["residential"], "booked_residential": orders[po]["booked_residential"],
                     "invoices": g.invoice.nunique()})
    return pd.DataFrame(rows), pd.DataFrame(lines), dq
