"""The audit agent: reconcile -> gather evidence -> decide fault -> package for a human.

Tools: run_reconciliation, list_flagged, search_inbox, determine_fault, package_case, write_report.
The agent stops at evidence packaging. Disputes to carriers, chargebacks to vendors and
fixes to our own rate calculator are drafted for a person to approve; nothing is sent.

With ANTHROPIC_API_KEY set, Claude drives the loop (``run_live``). Without it, ``run_offline``
runs the same tools in a fixed plan.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd

from .faults import REASONS, determine_fault, lane_benchmarks
from .reconcile import reconcile

SYSTEM = """You are an LTL freight audit agent. Each week: reconcile carrier bills against quotes,
then for each flagged order search the shipping@ and dispositions@ mailboxes for evidence,
determine the fault party with the determine_fault tool, and package each case for human approval.
Never state a dollar figure that a tool did not return. Never send anything: packaging is the last step."""

TOOLS = [
    {"name": "run_reconciliation", "description": "Reconcile all bills vs quotes. Returns counts and $ flagged.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "list_flagged", "description": "Flagged POs (overbilled > $15), largest variance first.",
     "input_schema": {"type": "object", "properties": {"limit": {"type": "integer"}}}},
    {"name": "search_inbox", "description": "Search ops mailboxes for emails mentioning a PO.",
     "input_schema": {"type": "object", "properties": {"po": {"type": "string"}, "mailbox": {"type": "string", "enum": ["shipping@", "dispositions@", "both"]}}, "required": ["po"]}},
    {"name": "determine_fault", "description": "Apply the written fault rules to one PO using its charge lines and the emails found.",
     "input_schema": {"type": "object", "properties": {"po": {"type": "string"}}, "required": ["po"]}},
    {"name": "package_case", "description": "Draft the dispute / chargeback / internal fix for one decided PO (not sent).",
     "input_schema": {"type": "object", "properties": {"po": {"type": "string"}}, "required": ["po"]}},
    {"name": "write_report", "description": "Write the weekly fault report workbook and summary.",
     "input_schema": {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}},
]


class AuditAgent:
    def __init__(self, world, out_dir="output"):
        self.w = world
        self.out = Path(out_dir); self.out.mkdir(parents=True, exist_ok=True)
        (self.out / "cases").mkdir(exist_ok=True)
        for p in (self.out / "cases").glob("*.md"):
            p.unlink()
        self.inbox = pd.DataFrame(world.inbox)
        self.summary = self.lines = None
        self.verdicts, self.cases, self.log = {}, [], []

    # ---------------------------------------------------------------- tools
    def run_reconciliation(self):
        self.summary, self.lines, dq = reconcile(self.w)
        self.bench = lane_benchmarks(self.summary, self.lines)
        f = self.summary[self.summary.flagged]
        return {"orders": len(self.summary), "flagged": len(f), "flagged_overbill_usd": round(f.variance.sum(), 2), **dq}

    def list_flagged(self, limit: int = 1000):
        f = self.summary[self.summary.flagged].sort_values("variance", ascending=False).head(limit)
        return f[["po", "carrier_billed", "vendor", "variance"]].to_dict("records")

    def search_inbox(self, po: str, mailbox: str = "both"):
        m = self.inbox[self.inbox.po == po] if len(self.inbox) else self.inbox
        if mailbox != "both":
            m = m[m.mailbox == mailbox]
        return m.to_dict("records")

    def determine_fault(self, po: str):
        row = self.summary.set_index("po").loc[po]
        row = row.copy(); row["po"] = po
        v = determine_fault(row, self.lines[self.lines.po == po], self.search_inbox(po), self.bench)
        self.verdicts[po] = v
        return v

    def package_case(self, po: str):
        v = self.verdicts[po]
        party = v["fault_party"]
        lines = self.lines[self.lines.po == po]
        table = "\n".join(f"| {r.charge_code} | {r.quoted:.2f} | {r.billed:.2f} | {r.variance:+.2f} |" for r in lines.itertuples())
        kind = {"Carrier": "Carrier dispute (draft)", "Vendor": "Vendor chargeback (draft)",
                "Internal": "Internal fix: rate calculator / booking flags", "Review": "Human review"}[party]
        body = (f"# {kind}: PO {po}\n\n**Reason:** {v['description']} (`{v['fault_reason']}`)\n\n"
                f"**Amount:** ${v['variance']:.2f} over quote\n\n**Evidence:** {v['evidence']}\n\n"
                f"| Charge | Quoted | Billed | Variance |\n|---|---|---|---|\n{table}\n\n"
                f"_Prepared by the audit agent. Not sent: requires approval in the recoveries queue._\n")
        path = self.out / "cases" / f"{po}_{party.lower()}.md"
        path.write_text(body)
        self.cases.append({"po": po, "party": party, "reason": v["fault_reason"], "amount": v["variance"], "file": path.name})
        return {"po": po, "drafted": kind, "file": path.name}

    def write_report(self, summary: str):
        v = pd.DataFrame(self.verdicts.values())
        path = self.out / "fault_report.xlsx"
        with pd.ExcelWriter(path) as xw:
            pd.DataFrame({"Summary": summary.split("\n")}).to_excel(xw, sheet_name="Summary", index=False)
            v.groupby(["fault_party", "fault_reason"]).agg(orders=("po", "count"), usd=("variance", "sum")).round(2).reset_index() \
                .to_excel(xw, sheet_name="By fault", index=False)
            v.sort_values("variance", ascending=False).to_excel(xw, sheet_name="Verdicts", index=False)
            self.lines[self.lines.po.isin(v.po)].to_excel(xw, sheet_name="Charge lines", index=False)
        (self.out / "SUMMARY.md").write_text(summary + "\n")
        return {"path": str(path)}

    # ---------------------------------------------------------------- loops
    def _call(self, name, args):
        out = getattr(self, name)(**(args or {}))
        self.log.append({"tool": name, "input": args})
        return out

    def run_offline(self):
        head = self._call("run_reconciliation", {})
        for f in self._call("list_flagged", {}):
            self._call("determine_fault", {"po": f["po"]})
            self._call("package_case", {"po": f["po"]})
        v = pd.DataFrame(self.verdicts.values())
        by = v.groupby("fault_party").variance.agg(["count", "sum"])
        get = lambda p, k: by.loc[p, k] if p in by.index else 0   # noqa: E731
        summary = "\n".join([
            f"Reconciled {head['orders']:,} LTL orders; {head['flagged']:,} overbilled by more than $15 (${head['flagged_overbill_usd']:,.0f}).",
            f"Data hygiene: dropped {head['resent_lines_dropped']} re-sent EDI lines, isolated {head['return_lines_isolated']} return-leg lines.",
            f"Carrier disputes drafted: {get('Carrier','count')} (${get('Carrier','sum'):,.0f} recoverable).",
            f"Vendor chargebacks drafted: {get('Vendor','count')} (${get('Vendor','sum'):,.0f}).",
            f"Internal fixes (calculator / booking flags): {get('Internal','count')} (${get('Internal','sum'):,.0f} of cost to stop recurring).",
            f"Held for human review: {get('Review','count')}.",
            "Nothing was sent. All cases await approval in the recoveries queue.",
        ])
        self._call("write_report", {"summary": summary})
        return summary

    def run_live(self, model=None, max_turns=200):
        import anthropic
        client = anthropic.Anthropic()
        model = model or os.environ.get("FREIGHT_AGENT_MODEL", "claude-sonnet-4-5")
        msgs = [{"role": "user", "content": "Run this week's LTL freight audit."}]
        for _ in range(max_turns):
            r = client.messages.create(model=model, max_tokens=2000, system=SYSTEM, tools=TOOLS, messages=msgs)
            msgs.append({"role": "assistant", "content": r.content})
            if r.stop_reason != "tool_use":
                break
            res = [{"type": "tool_result", "tool_use_id": b.id, "content": json.dumps(self._call(b.name, b.input), default=str)}
                   for b in r.content if b.type == "tool_use"]
            msgs.append({"role": "user", "content": res})
        return self.log


def evaluate(agent: "AuditAgent", world) -> dict:
    truth = pd.DataFrame(world.orders).set_index("po")
    v = pd.DataFrame(agent.verdicts.values()).set_index("po")
    flagged_truth = truth.loc[agent.summary[agent.summary.flagged].po]
    v = v.join(truth[["true_fault", "true_party"]])
    decided = v[v.fault_reason != "needs_review"]
    return {
        "flagged": len(v),
        "false_flags": int((flagged_truth.true_fault == "none").sum()),
        "missed_overbills": int(((truth.true_fault != "none") & ~truth.index.isin(v.index)).sum()),
        "party_accuracy": round(float((decided.fault_party == decided.true_party).mean()), 3),
        "reason_accuracy": round(float((decided.fault_reason == decided.true_fault).mean()), 3),
        "needs_review": int((v.fault_reason == "needs_review").sum()),
    }
