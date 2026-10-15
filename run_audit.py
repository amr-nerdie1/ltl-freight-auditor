"""Run one weekly LTL audit.

    python run_audit.py           # offline plan, no API key needed
    ANTHROPIC_API_KEY=... python run_audit.py --live
"""
import argparse
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "src"))
from freightaudit import AuditAgent, evaluate, reconcile, simulate  # noqa: E402

NAVY, TEAL, GREY, AMBER = "#0B1F3A", "#0E8C8C", "#9AA5B1", "#D97706"
plt.rcParams.update({"font.size": 11, "axes.spines.top": False, "axes.spines.right": False,
                     "axes.titleweight": "bold", "axes.titlesize": 13})
PARTY_COLOR = {"Carrier": TEAL, "Vendor": NAVY, "Internal": AMBER, "Review": GREY}


def figures(agent, world):
    out = ROOT / "figures"; out.mkdir(exist_ok=True)
    v = pd.DataFrame(agent.verdicts.values())
    g = v.groupby(["fault_party", "fault_reason"]).variance.sum().reset_index().sort_values("variance")
    fig, ax = plt.subplots(figsize=(8.5, 4.8))
    ax.barh(g.fault_reason.str.replace("_", " "), g.variance / 1000, color=g.fault_party.map(PARTY_COLOR))
    for i, x in enumerate(g.variance):
        ax.text(x / 1000, i, f" ${x/1000:,.1f}K", va="center", fontsize=9)
    from matplotlib.patches import Patch
    ax.legend(handles=[Patch(color=c, label=p) for p, c in PARTY_COLOR.items() if p in set(g.fault_party)],
              frameon=False, loc="lower right"); ax.set_xlabel("Overbilled $K")
    ax.set_title("Who pays: overbilled $ by fault reason and party")
    fig.tight_layout(); fig.savefig(out / "01_fault_breakdown.png", dpi=160); plt.close(fig)

    truth = pd.DataFrame(world.orders).set_index("po")
    naive, _, _ = reconcile(world, naive=True)
    clean = agent.summary
    def ff(s): return int((truth.loc[s[s.flagged].po].true_fault == "none").sum())
    fig, ax = plt.subplots(figsize=(6.5, 4))
    vals = [ff(naive), ff(clean)]
    b = ax.bar(["Naive\n(first quote, raw EDI)", "Audited\n(3-pass match, deduped,\nreturns isolated)"], vals, color=[GREY, TEAL])
    for r, x in zip(b, vals):
        ax.text(r.get_x() + r.get_width() / 2, x, str(x), ha="center", va="bottom", fontweight="bold")
    ax.set_yticks([]); ax.set_title("False overbilling flags (orders billed correctly)")
    fig.tight_layout(); fig.savefig(out / "02_false_flags.png", dpi=160); plt.close(fig)


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--live", action="store_true"); args = ap.parse_args()
    world = simulate()
    agent = AuditAgent(world, ROOT / "output")
    if args.live:
        agent.run_live()
    else:
        print(agent.run_offline())
    figures(agent, world)
    print("\nAccuracy vs hidden truth (synthetic):", evaluate(agent, world))


if __name__ == "__main__":
    main()
