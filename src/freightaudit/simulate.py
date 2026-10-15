"""Synthetic LTL quotes, EDI bills and ops-mailbox emails (all invented).

Every order carries a hidden ``true_fault`` so the auditor's accuracy can be measured.
The generator reproduces the traps that make real LTL auditing hard:

* each carrier returns quotes in a different format (key-value string, multi-quote JSON, simple JSON)
* one carrier quotes several service levels; you must match the one actually booked
* EDI bills arrive as one row per charge line, sometimes duplicated, sometimes across two invoices
* return shipments land in the same EDI feed and must be isolated from delivery variance
* the evidence that decides who pays usually sits in an email, not in the data
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass

CARRIERS = {  # name: quote format
    "NorthLine": "kv", "Ridgeway": "kv",
    "Metroway": "multi_json", "Coastal": "multi_json",
    "AMX Freight": "simple_json", "Pinnacle": "simple_json",
}
VENDORS = [f"Vendor {c}" for c in "ABCDEFGHIJKLMNOP"]

# true fault -> (party, share of orders)
FAULTS = {
    "none": ("-", 0.62),
    "carrier_rate_overbill": ("Carrier", 0.06),
    "carrier_unquoted_accessorial": ("Carrier", 0.06),
    "carrier_reweigh_no_certificate": ("Carrier", 0.04),
    "carrier_duplicate_charge": ("Carrier", 0.03),
    "vendor_unapproved_palletization": ("Vendor", 0.04),
    "vendor_weight_misdeclared": ("Vendor", 0.04),
    "vendor_changed_carrier": ("Vendor", 0.03),
    "internal_calculator_error": ("Internal", 0.04),
    "internal_missing_accessorial": ("Internal", 0.04),
}
FUEL = 0.24


@dataclass
class World:
    orders: list[dict]
    quotes: list[dict]     # raw API responses as the carrier sent them
    edi: list[dict]        # one row per billed charge line
    inbox: list[dict]      # emails in shared ops mailboxes


def _quote_raw(fmt: str, svc: str, lh: float, acc: dict[str, float], alt_svc_lh: float | None):
    fuel = round(lh * FUEL, 2)
    total = round(lh + fuel + sum(acc.values()), 2)
    if fmt == "kv":
        parts = [f"LH={lh:.2f}", f"FSC={fuel:.2f}"] + [f"ACC:{k}={v:.2f}" for k, v in acc.items()] + [f"SVC={svc}", f"TOTAL={total:.2f}"]
        return "|".join(parts), total
    if fmt == "simple_json":
        return json.dumps({"base": lh, "fuel": fuel, "accessorials": acc, "service": svc, "total": total}), total
    # multi-quote JSON: several service levels; the booked one is NOT always first
    quotes = [{"serviceLevel": svc, "totalRate": total,
               "charges": [{"code": "LH", "amt": lh}, {"code": "FSC", "amt": fuel}] + [{"code": k, "amt": v} for k, v in acc.items()]}]
    if alt_svc_lh:
        f2 = round(alt_svc_lh * FUEL, 2)
        quotes.insert(0, {"serviceLevel": "GUARANTEED" if svc == "STANDARD" else "STANDARD",
                          "totalRate": round(alt_svc_lh + f2 + sum(acc.values()), 2),
                          "charges": [{"code": "LH", "amt": alt_svc_lh}, {"code": "FSC", "amt": f2}] + [{"code": k, "amt": v} for k, v in acc.items()]})
    return json.dumps({"quotes": quotes}), total


def simulate(n_orders: int = 1500, seed: int = 3) -> World:
    rng = random.Random(seed)
    names, weights = list(FAULTS), [v[1] for v in FAULTS.values()]
    orders, quotes, edi, inbox = [], [], [], []
    inv = 900000
    for i in range(n_orders):
        po = str(26100000 + i * 7)
        carrier = rng.choice(list(CARRIERS))
        fmt = CARRIERS[carrier]
        vendor = rng.choice(VENDORS)
        weight = round(rng.uniform(150, 900))
        residential = rng.random() < 0.55
        lh = round(80 + weight * rng.uniform(0.28, 0.42), 2)
        acc_q = {"LIFTGATE": 85.0} if residential else {}
        if residential:
            acc_q["RESIDENTIAL"] = 95.0
        fault = rng.choices(names, weights)[0]
        svc = "STANDARD"

        booked_flags = {"residential": residential}
        if fault == "internal_missing_accessorial":
            booked_flags["residential"] = False          # our calculator didn't flag it at booking
            acc_q = {}
        q_lh = lh
        if fault == "internal_calculator_error":
            q_lh = round(lh * rng.uniform(0.70, 0.82), 2)  # our rate calculator under-quoted the lane
        alt = round(lh * 1.35, 2) if fmt == "multi_json" and rng.random() < 0.6 else None
        q_carrier = carrier
        if fault == "vendor_changed_carrier":   # booked with one carrier, vendor tendered to another
            q_carrier = rng.choice([c for c in CARRIERS if c != carrier and CARRIERS[c] == fmt])
        raw, q_total = _quote_raw(fmt, svc, q_lh, acc_q, alt)
        quotes.append({"po": po, "carrier": q_carrier, "format": fmt, "booked_total": q_total, "booked_service": svc, "raw": raw})

        # ---- billed lines
        b_lh, b_weight = lh, weight
        acc_b = {"LIFTGATE": 85.0, "RESIDENTIAL": 95.0} if residential else {}
        dupe = False
        if fault == "carrier_rate_overbill":
            b_lh = round(lh * rng.uniform(1.15, 1.40), 2)
        elif fault == "carrier_unquoted_accessorial":
            acc_b[rng.choice(["BEYOND_AREA", "CA_COMPLIANCE", "INSIDE_DELIVERY"])] = rng.choice([65.0, 110.0, 145.0])
        elif fault in ("carrier_reweigh_no_certificate", "vendor_weight_misdeclared"):
            b_weight = round(weight * rng.uniform(1.25, 1.6))
            b_lh = round(lh * b_weight / weight, 2)
            acc_b["REWEIGH"] = 35.0
        elif fault == "carrier_duplicate_charge":
            dupe = True
        elif fault == "vendor_unapproved_palletization":
            acc_b["PALLETIZATION"] = 120.0
            b_lh = round(lh * 1.12, 2)
        elif fault == "vendor_changed_carrier":
            b_lh = round(lh * rng.uniform(1.2, 1.5), 2)
        fuel_b = round(b_lh * FUEL, 2)
        lines = [("LH", b_lh), ("FSC", fuel_b)] + list(acc_b.items())
        inv += 1
        split = rng.random() < 0.12          # balance-due second invoice
        for j, (code, amt) in enumerate(lines):
            invoice = f"INV{inv}B" if split and j >= 2 else f"INV{inv}"
            edi.append({"po": po, "carrier": carrier, "invoice": invoice, "line_no": j + 1, "shipment": "DELIVERY",
                        "charge_code": code, "amount": amt, "billed_weight": b_weight if code == "LH" else None})
        if dupe:
            c, a = rng.choice(lines[2:] or lines[:1])
            edi.append({"po": po, "carrier": carrier, "invoice": f"INV{inv}", "line_no": len(lines) + 1, "shipment": "DELIVERY",
                        "charge_code": c, "amount": a, "billed_weight": None})
        if rng.random() < 0.06:              # a return leg billed on the same PO: not delivery variance
            edi.append({"po": po, "carrier": carrier, "invoice": f"INV{inv}R", "line_no": 1, "shipment": "RETURN",
                        "charge_code": "LH", "amount": round(lh * 0.9, 2), "billed_weight": weight})
        # an exact re-transmission of a line (same invoice + line number): harmless if deduped
        if rng.random() < 0.05:
            edi.append(dict(edi[-1]))

        orders.append({"po": po, "carrier": carrier, "vendor": vendor, "declared_weight": weight,
                       "residential": residential, "booked_residential": booked_flags["residential"],
                       "service": svc, "true_fault": fault, "true_party": FAULTS[fault][0]})

        # ---- evidence emails
        def mail(box, frm, subj, body):
            inbox.append({"mailbox": box, "from": frm, "subject": subj, "body": body, "po": po})
        if fault == "vendor_unapproved_palletization":
            mail("shipping@", f"{vendor.lower().replace(' ', '')}@example.com", f"PO {po} shipped",
                 f"Hi, PO {po} went out today. We put it on 2 pallets for safety. Thanks")
        if fault == "vendor_weight_misdeclared":
            mail("shipping@", f"billing@{carrier.lower().replace(' ', '')}.example", f"Weight inspection certificate - PO {po}",
                 f"Attached is the inspection certificate for PO {po}. Scale weight {b_weight} lb vs BOL {weight} lb.")
        if fault == "vendor_changed_carrier" and rng.random() < 0.75:   # notice not always sent
            mail("dispositions@", f"{vendor.lower().replace(' ', '')}@example.com", f"Carrier change PO {po}",
                 f"FYI we switched PO {po} to {carrier} because our usual carrier could not pick up.")
        if fault == "carrier_reweigh_no_certificate" and rng.random() < 0.5:
            mail("shipping@", f"billing@{carrier.lower().replace(' ', '')}.example", f"Invoice adjustment PO {po}",
                 f"Invoice for PO {po} was adjusted after reweigh. Please remit balance.")   # no certificate
        if rng.random() < 0.15:  # noise
            mail(rng.choice(["shipping@", "dispositions@"]), "customer@example.com", f"Where is my order {po}?",
                 f"Hi, checking on delivery for order {po}.")
    return World(orders, quotes, edi, inbox)
