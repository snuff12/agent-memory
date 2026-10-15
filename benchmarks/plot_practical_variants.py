#!/usr/bin/env python3
"""Render the published diagnostic result with optional matplotlib."""
import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import ScalarFormatter
    result = json.loads(args.result.read_text())
    methods = ["lexical", "lexical_followup", "semantic", "hybrid", "graph", "hybrid_graph"]
    labels = ["Current lexical", "Lexical + follow-up", "Semantic pilot", "Lexical + semantic", "Lightweight graph", "Hybrid + graph"]
    colors = ["#556477", "#176f8c", "#af6332", "#af6332", "#71823a", "#71823a"]
    completed = [result["summary"][method]["complete"] for method in methods]
    latency = [result["summary"][method]["median_ms"] for method in methods]
    with plt.rc_context({"font.family": "DejaVu Sans", "font.size": 10, "svg.fonttype": "none"}):
        fig, axes = plt.subplots(1, 2, figsize=(12.5, 5.2), gridspec_kw={"width_ratios": [1, 1.05]})
        for ax in axes:
            ax.spines[["top", "right", "left"]].set_visible(False)
            ax.set_axisbelow(True)
            ax.grid(axis="x", color="#e0e4e8", linewidth=.6)
            ax.invert_yaxis()
        axes[0].barh(labels, completed, color=colors, height=.6)
        axes[0].set_xlim(0, 38)
        axes[0].set_xticks([0, 10, 20, 30, 38])
        axes[0].set_title("Read every predeclared evidence span", loc="left", fontsize=12, pad=16)
        axes[0].set_xlabel("Completed span checks / 38 answerable cases")
        for index, count in enumerate(completed):
            axes[0].text(count + .4, index, "%d / 38" % count, va="center", fontsize=10)
        axes[1].barh(range(6), latency, color=colors, height=.6)
        axes[1].set_yticks(range(6), [""] * 6)
        axes[1].set_xscale("log")
        axes[1].set_xlim(8, 7000)
        axes[1].set_xticks([10, 100, 1000])
        axes[1].xaxis.set_major_formatter(ScalarFormatter())
        axes[1].set_title("Current adapter latency", loc="left", fontsize=12, pad=16)
        axes[1].set_xlabel("Median retrieval + source reads, ms (log scale)")
        for index, milliseconds in enumerate(latency):
            axes[1].text(milliseconds * 1.10, index, "%s ms" % ("%.1f" % milliseconds if milliseconds < 100 else "%.0f" % milliseconds), va="center", fontsize=10)
        fig.suptitle("Local semantic / graph diagnostic — same source-read budget", fontsize=15, x=.02, ha="left", y=.98)
        fig.text(.02, .025, "89 documents · 42 tasks · at most 3 × 1,200 UTF-8-byte reads · fixed model and retrieval rules\n"
                 "Selected diagnostic cases, not answer accuracy or a ranking of systems. Semantic latency includes a new worker/model load per query.", fontsize=8.5, color="#495463")
        fig.subplots_adjust(left=.17, right=.97, top=.80, bottom=.22, wspace=.28)
        fig.savefig(args.output, metadata={"Date": None})
        plt.close(fig)
        if args.output.suffix.lower() == ".svg":
            args.output.write_text("\n".join(line.rstrip() for line in args.output.read_text().splitlines()) + "\n")


if __name__ == "__main__":
    main()
