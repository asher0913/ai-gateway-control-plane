"""Redraw docs/incident_timeline.png from results/simulation.json (run `aigw simulate --out` first)."""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SHOWN = {
    "single endpoint, no retries": "#9e9e9e",
    "fallback to other endpoints": "#fd8d3c",
    "fallback + circuit breaker": "#6baed6",
    "fallback + breaker + latency-aware": "#08519c",
}


def main() -> None:
    report = json.loads((ROOT / "results" / "simulation.json").read_text())
    fig, (top, bottom) = plt.subplots(2, 1, figsize=(10, 5.6), sharex=True)
    for policy in report["policies"]:
        if policy["policy"] not in SHOWN:
            continue
        minutes = range(len(policy["success_timeline_per_minute"]))
        colour = SHOWN[policy["policy"]]
        top.plot(
            minutes, [100 * x for x in policy["success_timeline_per_minute"]], color=colour, label=policy["policy"]
        )
        p95 = [float("nan") if x is None else x for x in policy["p95_latency_timeline_per_minute"]]
        bottom.plot(minutes, p95, color=colour)
    for incident in report["scenario"]["incidents"]:
        for ax in (top, bottom):
            ax.axvspan(incident["start"] / 60, incident["end"] / 60, color="#fdd0a2", alpha=0.4, lw=0)
        top.text((incident["start"] + incident["end"]) / 120, 8, f"primary {incident['kind']}", ha="center", fontsize=8)
    top.set(ylabel="success rate % (admitted)", ylim=(0, 102), title="One hour of traffic with two injected incidents")
    bottom.set(ylabel="p95 latency (s)", xlabel="minute")
    top.legend(frameon=False, fontsize=8, loc="lower left")
    for ax in (top, bottom):
        ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(ROOT / "docs" / "incident_timeline.png", dpi=150)
    plt.close(fig)


if __name__ == "__main__":
    main()
