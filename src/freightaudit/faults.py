"""Fault determination: who pays for an overbilled LTL order?

Ten fault reasons across three parties. Rules are written so an ops lead can read
and challenge them; each verdict cites its evidence (data fields and/or emails).
Anything the rules can't settle goes to NEEDS_REVIEW rather than being guessed.
"""
from __future__ import annotations

import re

import pandas as pd

REASONS = {
    "carrier_rate_overbill":           ("Carrier",  "Linehaul billed above quoted rate, no weight change"),
    "carrier_unquoted_accessorial":    ("Carrier",  "Accessorial billed that was not quoted or requested"),
    "carrier_reweigh_no_certificate":  ("Carrier",  "Reweigh charged without an inspection certificate"),
    "carrier_duplicate_charge":        ("Carrier",  "Same charge billed twice on separate lines"),
    "vendor_unapproved_palletization": ("Vendor",   "Vendor palletized without approval"),
    "vendor_weight_misdeclared":       ("Vendor",   "Scale weight above vendor BOL, certificate on file"),
    "vendor_changed_carrier":          ("Vendor",   "Vendor tendered to a different carrier"),
    "internal_calculator_error":       ("Internal", "Our rate calculator under-quoted the lane"),
    "internal_missing_accessorial":    ("Internal", "Required accessorial not flagged at booking"),
    "needs_review":                    ("Review",   "Rules could not settle fault; human review"),
}
VARIANCE_ACCESSORIALS = {"BEYOND_AREA", "CA_COMPLIANCE", "INSIDE_DELIVERY"}


def vendor_domain(vendor: str) -> str:
    return f"{vendor.lower().replace(' ', '')}@example.com"


def _mentions(emails: list[dict], *patterns, sender: str | None = None) -> dict | None:
    """First email matching all patterns. ``sender`` pins evidence to the party it incriminates:
    a vendor-fault admission only counts if the vendor sent it, not a carrier claiming it did."""
    for m in emails:
        if sender and m["from"].lower() != sender:
            continue
        text = f"{m['subject']} {m['body']}".lower()
        if all(re.search(p, text) for p in patterns):
            return m
    return None


def lane_benchmarks(summary: pd.DataFrame, lines: pd.DataFrame) -> dict:
    """Typical quoted linehaul $/lb per carrier, used to spot calculator under-quotes."""
    lh = lines[lines.charge_code == "LH"][["po", "quoted"]].merge(summary[["po", "carrier_quoted", "declared_weight"]], on="po")
    lh["rate"] = lh.quoted / lh.declared_weight
    return lh.groupby("carrier_quoted").rate.median().to_dict()


def determine_fault(row, order_lines: pd.DataFrame, emails: list[dict], bench: dict) -> dict:
    L = order_lines.set_index("charge_code")
    ev = []

    def verdict(reason, *evidence):
        party, desc = REASONS[reason]
        return {"po": row.po, "fault_reason": reason, "fault_party": party, "description": desc,
                "evidence": "; ".join(e for e in evidence if e), "variance": row.variance}

    if row.carrier_billed != row.carrier_quoted:
        m = _mentions(emails, r"switch|carrier change|changed", sender=vendor_domain(row.vendor))
        if m:
            return verdict("vendor_changed_carrier", f"quoted {row.carrier_quoted}, billed by {row.carrier_billed}",
                           f"email from {m['from']} in {m['mailbox']}: \"{m['subject']}\"")
        return verdict("needs_review", f"carrier changed ({row.carrier_quoted} -> {row.carrier_billed}) with no notice found")

    dup = L[(L.billed_lines > 1) & (L.variance > 0)] if "billed_lines" in L else L.iloc[0:0]
    if len(dup):
        c = dup.index[0]
        return verdict("carrier_duplicate_charge", f"{c} billed on {int(dup.loc[c, 'billed_lines'])} lines, ${dup.loc[c, 'variance']:.2f} over quote")

    if "PALLETIZATION" in L.index and L.loc["PALLETIZATION", "unquoted"]:
        m = _mentions(emails, r"pallet", sender=vendor_domain(row.vendor))
        if m:
            return verdict("vendor_unapproved_palletization", "palletization billed, not quoted",
                           f"vendor email \"{m['subject']}\": {m['body'][:80]}")
        return verdict("carrier_unquoted_accessorial", "palletization billed, not quoted, no vendor notice found")

    reweighed = "REWEIGH" in L.index or (row.billed_weight and row.billed_weight > 1.1 * row.declared_weight)
    if reweighed:
        m = _mentions(emails, r"inspection certificate")
        w = f"billed {row.billed_weight:.0f} lb vs declared {row.declared_weight:.0f} lb"
        if m:
            return verdict("vendor_weight_misdeclared", w, f"carrier certificate email \"{m['subject']}\"")
        return verdict("carrier_reweigh_no_certificate", w, "no inspection certificate in shipping@ or dispositions@")

    missing = [c for c in ("RESIDENTIAL", "LIFTGATE") if c in L.index and L.loc[c, "unquoted"]]
    if missing and row.residential and not row.booked_residential:
        return verdict("internal_missing_accessorial", f"{', '.join(missing)} required (residential) but not flagged at booking")

    extra = [c for c in L.index if L.loc[c, "unquoted"] and c in VARIANCE_ACCESSORIALS]
    if extra:
        return verdict("carrier_unquoted_accessorial", f"unquoted {', '.join(extra)}; no delivery-condition evidence on file")

    if "LH" in L.index and L.loc["LH", "variance"] > 0:
        q_rate = L.loc["LH", "quoted"] / row.declared_weight
        b = bench.get(row.carrier_quoted, q_rate)
        if q_rate < 0.85 * b and L.loc["LH", "billed"] / row.declared_weight <= 1.15 * b:
            return verdict("internal_calculator_error", f"quoted ${q_rate:.3f}/lb vs carrier norm ${b:.3f}/lb; bill is at norm")
        return verdict("carrier_rate_overbill", f"linehaul ${L.loc['LH', 'billed']:.2f} billed vs ${L.loc['LH', 'quoted']:.2f} quoted, same weight")

    return verdict("needs_review", "variance not explained by any rule")
