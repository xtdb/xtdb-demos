"""Render the AUC explainer figure from the live model — the two panels a room
needs: what the score distributions look like, and the ROC curve that summarises
them. Real data, so the number on the slide and the number in the picture agree.

Run: uv run --extra analysis python explain_auc.py [out.png]
"""

from __future__ import annotations

import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
from sklearn.metrics import roc_auc_score, roc_curve

import model as M
from queries import connect
from registry import FEATURE_ORDER

BG = "#0a0a0a"
FG = "#e5e5e5"
MUTED = "#737373"
GRID = "#262626"
LEGIT = "#38bdf8"
FRAUD = "#f87171"
ACCENT = "#fbbf24"


def scored():
    """(labels, probabilities) for the settled rows the model was scored on."""
    with connect() as c:
        pipe, meta = M.load_latest(c)
        if pipe is None:
            raise SystemExit("no model trained yet — run the demo's training first")
        horizon = M.sim_now(c) - M.training_outcome_horizon()
    df = M.extract(resolved_before=horizon)
    p = pipe.predict_proba(df[FEATURE_ORDER].astype(float))[:, 1]
    return df.label.astype(int).to_numpy(), p, meta


def main(out: Path):
    y, p, meta = scored()
    auc = roc_auc_score(y, p)
    fpr, tpr, _ = roc_curve(y, p)

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 5.2), facecolor=BG)
    fig.subplots_adjust(left=0.06, right=0.97, top=0.80, bottom=0.13, wspace=0.22)

    for ax in (ax1, ax2):
        ax.set_facecolor(BG)
        for s in ax.spines.values():
            s.set_color(GRID)
        ax.tick_params(colors=MUTED, labelsize=9)
        ax.grid(True, color=GRID, linewidth=0.6, alpha=0.7)
        ax.set_axisbelow(True)

    # --- left: what the model actually produces -----------------------------
    bins = np.linspace(0, 1, 41)
    ax1.hist(p[y == 0], bins=bins, color=LEGIT, alpha=0.75, label=f"legit (n={(y==0).sum():,})",
             density=True, edgecolor=BG, linewidth=0.4)
    ax1.hist(p[y == 1], bins=bins, color=FRAUD, alpha=0.75, label=f"fraud (n={(y==1).sum():,})",
             density=True, edgecolor=BG, linewidth=0.4)
    ax1.set_yscale("log")
    ax1.set_xlabel("p(fraud) the model assigned", color=FG, fontsize=10)
    ax1.set_ylabel("share of rows (log)", color=FG, fontsize=10)
    ax1.set_title("Two populations, ranked", color=FG, fontsize=12, pad=10, loc="left")
    leg = ax1.legend(frameon=False, labelcolor=FG, fontsize=9, loc="upper center")
    leg.get_frame().set_alpha(0)
    ax1.text(0.5, 0.55,
             "AUC = the chance a random red row\nsits to the right of a random blue one",
             transform=ax1.transAxes, color=MUTED, fontsize=9.5, ha="center", style="italic")

    # --- right: the curve the metric is named after -------------------------
    ax2.fill_between(fpr, tpr, color=ACCENT, alpha=0.13)
    ax2.plot(fpr, tpr, color=ACCENT, linewidth=2.2)
    ax2.plot([0, 1], [0, 1], color=MUTED, linewidth=1.1, linestyle=(0, (4, 4)))
    ax2.set_xlim(0, 1)
    ax2.set_ylim(0, 1.002)
    ax2.set_xlabel("false positive rate — legit rows we'd flag", color=FG, fontsize=10)
    ax2.set_ylabel("true positive rate — fraud we'd catch", color=FG, fontsize=10)
    ax2.set_title("ROC: every threshold at once", color=FG, fontsize=12, pad=10, loc="left")
    ax2.text(0.55, 0.42, f"AUC = {auc:.4f}", color=ACCENT, fontsize=17, fontweight="bold")
    ax2.text(0.55, 0.33, "area under the curve", color=MUTED, fontsize=9.5)
    ax2.text(0.62, 0.10, "coin flip (0.5)", color=MUTED, fontsize=8.5, rotation=32)

    fig.text(0.06, 0.93, "What AUC measures", color=FG, fontsize=17, fontweight="bold")
    fig.text(0.06, 0.875,
             f"live fraud demo · model {meta['version']} · {len(y):,} settled transactions",
             color=MUTED, fontsize=9.5)
    fig.text(0.06, 0.035,
             "Fraud is ~5% of rows, so accuracy is useless — always saying 'legit' scores 95%. "
             "AUC only looks at ordering, and ignores where you put the 0.5 cutoff.",
             color=MUTED, fontsize=8.5)

    fig.savefig(out, dpi=170, facecolor=BG)
    print(out)


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "explain_auc.png"))
