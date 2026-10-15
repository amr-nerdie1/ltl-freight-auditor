import json
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from freightaudit import AuditAgent, evaluate, reconcile, simulate  # noqa: E402
from freightaudit.parsers import parse_kv, parse_multi_json, parse_simple_json  # noqa: E402


def test_parsers_return_same_shape():
    kv = parse_kv("LH=400.00|FSC=96.00|ACC:LIFTGATE=85.00|SVC=STANDARD|TOTAL=581.00")
    sj = parse_simple_json(json.dumps({"base": 400, "fuel": 96, "accessorials": {"LIFTGATE": 85}, "service": "STANDARD", "total": 581}))
    assert kv["charges"] == sj["charges"] and kv["total"] == sj["total"] == 581.0


def test_multi_quote_matches_booked_not_first():
    raw = json.dumps({"quotes": [
        {"serviceLevel": "GUARANTEED", "totalRate": 700.0, "charges": [{"code": "LH", "amt": 560}, {"code": "FSC", "amt": 140}]},
        {"serviceLevel": "STANDARD", "totalRate": 500.0, "charges": [{"code": "LH", "amt": 400}, {"code": "FSC", "amt": 100}]}]})
    assert parse_multi_json(raw)["total"] == 700.0                       # naive: first entry
    assert parse_multi_json(raw, booked_total=500.0)["service"] == "STANDARD"
    assert parse_multi_json(raw, booked_service="STANDARD")["total"] == 500.0


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    w = simulate(n_orders=600, seed=9)
    a = AuditAgent(w, tmp_path_factory.mktemp("out"))
    a.run_offline()
    return w, a


def test_hygiene_removes_false_flags(run):
    w, a = run
    truth = pd.DataFrame(w.orders).set_index("po")
    naive, _, _ = reconcile(w, naive=True)
    ff = lambda s: (truth.loc[s[s.flagged].po].true_fault == "none").sum()   # noqa: E731
    assert ff(a.summary) < ff(naive)


def test_fault_accuracy(run):
    w, a = run
    e = evaluate(a, w)
    assert e["party_accuracy"] >= 0.9 and e["false_flags"] <= 5


def test_every_flagged_order_has_a_case_and_nothing_is_sent(run):
    w, a = run
    assert len(a.cases) == int(a.summary.flagged.sum())
    for c in a.cases:
        assert "Not sent" in (a.out / "cases" / c["file"]).read_text()


def test_split_invoices_are_summed(run):
    w, a = run
    edi = pd.DataFrame(w.edi)
    split_po = edi[edi.invoice.str.endswith("B")].po.iloc[0]
    row = a.summary.set_index("po").loc[split_po]
    delivery = edi[(edi.po == split_po) & (edi.shipment == "DELIVERY")].drop_duplicates()
    assert abs(row.billed_total - delivery.amount.sum()) < 0.01
