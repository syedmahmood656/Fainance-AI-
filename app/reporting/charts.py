"""Chart rendering with Matplotlib. Input is already-aggregated rows from the
database; this module only draws. It never computes or invents numbers."""
import matplotlib

matplotlib.use("Agg")  # headless: no display needed (Docker, MCP subprocess)
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

from app.utils.formatting import format_compact, format_money  # noqa: E402

_COLOR = "#3b6ea5"
_PALETTE = ["#3b6ea5", "#e07a5f", "#81b29a", "#f2cc8f", "#6d597a", "#b56576", "#355070",
            "#eaac8b", "#8ab17d", "#bc6c25", "#5e60ce", "#999999"]


def _finish(fig, out_path: str) -> str:
    fig.tight_layout()
    fig.savefig(out_path, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return out_path


def _axis_fmt(currency):
    return FuncFormatter(lambda v, _pos: format_compact(v, currency))


def horizontal_bars(labels, values, currency, title, subtitle, out_path) -> str:
    """Best for categories/vendors: long names stay readable. Largest bar on top."""
    labels, values = list(labels)[::-1], list(values)[::-1]
    fig, ax = plt.subplots(figsize=(10, max(4, 0.55 * len(labels) + 1.8)))
    bars = ax.barh(labels, values, color=_COLOR)
    top = max(values) if values else 0
    for bar, v in zip(bars, values):
        ax.text(bar.get_width() + top * 0.01, bar.get_y() + bar.get_height() / 2,
                format_money(v, currency), va="center", fontsize=9)
    ax.set_xlim(0, top * 1.22 if top else 1)
    ax.xaxis.set_major_formatter(_axis_fmt(currency))
    ax.set_title(title, fontsize=14, fontweight="bold", loc="left", pad=26)
    ax.text(0, 1.02, subtitle, transform=ax.transAxes, fontsize=9, color="#555555")
    ax.spines[["top", "right"]].set_visible(False)
    return _finish(fig, out_path)


def vertical_bars(labels, values, currency, title, subtitle, out_path) -> str:
    """Best for time series (months)."""
    fig, ax = plt.subplots(figsize=(10, 5.5))
    bars = ax.bar(labels, values, color=_COLOR)
    top = max(values) if values else 0
    for bar, v in zip(bars, values):
        if v:
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + top * 0.01,
                    format_compact(v, currency), ha="center", va="bottom", fontsize=9)
    ax.set_ylim(0, top * 1.15 if top else 1)
    ax.yaxis.set_major_formatter(_axis_fmt(currency))
    ax.set_title(title, fontsize=14, fontweight="bold", loc="left", pad=26)
    ax.text(0, 1.02, subtitle, transform=ax.transAxes, fontsize=9, color="#555555")
    ax.spines[["top", "right"]].set_visible(False)
    if len(labels) > 6:
        plt.setp(ax.get_xticklabels(), rotation=45, ha="right")
    return _finish(fig, out_path)


def pie(labels, values, currency, title, subtitle, out_path) -> str:
    fig, ax = plt.subplots(figsize=(9, 6.5))
    total = sum(values) or 1
    wedges, _ = ax.pie(values, colors=_PALETTE[: len(values)], startangle=90, counterclock=False,
                       wedgeprops={"width": 0.45, "edgecolor": "white"})
    legend = [f"{lab} — {format_money(v, currency)} ({v / total * 100:.0f}%)" for lab, v in zip(labels, values)]
    ax.legend(wedges, legend, loc="center left", bbox_to_anchor=(1, 0.5), frameon=False, fontsize=9)
    ax.set_title(title, fontsize=14, fontweight="bold", loc="left", pad=26)
    ax.text(0, 0, f"Total\n{format_money(total, currency)}", ha="center", va="center", fontsize=11)
    ax.text(0, 1.02, subtitle, transform=ax.transAxes, fontsize=9, color="#555555")
    return _finish(fig, out_path)


def line_timeline(labels, values, currency, title, subtitle, out_path, cumulative: bool = False) -> str:
    """Expense (y) against time (x). Filled line; the peak point is annotated."""
    fig, ax = plt.subplots(figsize=(10, 5.5))
    x = list(range(len(values)))
    ax.plot(x, values, color=_COLOR, linewidth=2.2, marker="o" if len(values) <= 40 else None, markersize=5)
    ax.fill_between(x, values, color=_COLOR, alpha=0.12)
    if values and max(values) > 0:
        peak = max(range(len(values)), key=values.__getitem__)
        ax.annotate(format_money(values[peak], currency), (peak, values[peak]), textcoords="offset points",
                    xytext=(0, 10), ha="center", fontsize=9, fontweight="bold")
        ax.set_ylim(0, max(values) * 1.18)
    step = max(1, -(-len(labels) // 10))               # about 10 tick labels at most
    ax.set_xticks(x[::step])
    ax.set_xticklabels(labels[::step], rotation=45 if len(labels) > 6 else 0, ha="right" if len(labels) > 6 else "center")
    ax.yaxis.set_major_formatter(_axis_fmt(currency))
    ax.set_ylabel("Cumulative expense" if cumulative else "Expense")
    ax.set_title(title, fontsize=14, fontweight="bold", loc="left", pad=26)
    ax.text(0, 1.02, subtitle, transform=ax.transAxes, fontsize=9, color="#555555")
    ax.grid(axis="y", alpha=0.25)
    ax.spines[["top", "right"]].set_visible(False)
    return _finish(fig, out_path)
