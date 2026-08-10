#!/usr/bin/env python3
"""Render the article's tables and figures as PNGs for Medium.

Medium does not render markdown tables and cannot draw a diagram, so every
table and figure in docs/article-medium.md is generated here instead. Run:

    python3 docs/img/make_medium_graphics.py

Numbers come from the rebuilt-mesh verification pass (2026-08-07/08) recorded
in README.md and docs/DEPLOYMENT_PLAN.md. If a number changes there, change it
here -- these images are the only place a reader sees it.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, Rectangle

OUT = Path(__file__).resolve().parent / "medium"
DPI = 150

# Palette: the dataviz reference instance, validated against a #ffffff surface.
SURFACE = "#ffffff"
INK = "#0b0b0b"
INK2 = "#52514e"
MUTED = "#898781"
RULE = "#e1e0d9"
AXIS = "#c3c2b7"

BLUE = "#2a78d6"
ORANGE = "#eb6834"
GOOD = "#0ca30c"
CRITICAL = "#d03b3b"

BLUE_TINT = "#eef4fd"
# Sequential blue ramp, light -> dark (steps 100..600).
RAMP = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#2a78d6", "#256abf", "#184f95"]

SANS = "Liberation Sans"
MONO = "Liberation Mono"

plt.rcParams.update(
    {
        "font.family": "sans-serif",
        "font.sans-serif": [SANS, "DejaVu Sans"],
        "figure.facecolor": SURFACE,
        "savefig.facecolor": SURFACE,
        "text.color": INK,
    }
)


def _canvas(w_in: float, h_in: float):
    fig = plt.figure(figsize=(w_in, h_in), dpi=DPI)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    return fig, ax


def _save(fig, name: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT / name, dpi=DPI, facecolor=SURFACE)
    plt.close(fig)
    print(f"wrote {OUT / name}")


def _wrap(text: str, width_in: float, fontsize: float) -> list[str]:
    """Wrap to the column width. Char width ~0.5em for this face."""
    chars = max(8, int(width_in / (0.5 * fontsize / 72)))
    out: list[str] = []
    for para in text.split("\n"):
        out.extend(textwrap.wrap(para, chars) or [""])
    return out


def draw_table(
    name: str,
    title: str,
    columns: list[tuple[str, float]],
    rows: list[list[str]],
    *,
    subtitle: str | None = None,
    highlight: int | None = None,
    footnote: str | None = None,
    mono_cols: tuple[int, ...] = (),
    fig_w: float = 10.0,
) -> None:
    """A table rendered as a figure: hairline rules, no boxes, one accent row."""
    fs = 13.0
    head_fs = 11.5
    pad = 0.42  # inches of horizontal padding inside the figure
    table_w = fig_w - 2 * pad
    col_w = [f * table_w for f, in [(f,) for _, f in columns]]

    wrapped = [
        [_wrap(cell, col_w[i] - 0.22, fs) for i, cell in enumerate(row)] for row in rows
    ]
    line_h = 0.245  # inches per text line
    row_pad = 0.30
    row_h = [max(len(c) for c in row) * line_h + row_pad for row in wrapped]

    top_pad = 0.30
    title_h = 0.44
    sub_h = 0.30 if subtitle else 0.0
    head_h = 0.42
    foot_lines = _wrap(footnote, table_w, 11.0) if footnote else []
    foot_h = (0.20 + 0.21 * len(foot_lines)) if footnote else 0.0
    bottom_pad = 0.26
    fig_h = top_pad + title_h + sub_h + head_h + sum(row_h) + foot_h + bottom_pad

    fig, ax = _canvas(fig_w, fig_h)

    def Y(inches_from_top: float) -> float:
        return 1 - inches_from_top / fig_h

    def X(inches_from_left: float) -> float:
        return inches_from_left / fig_w

    y = top_pad
    ax.text(X(pad), Y(y + 0.22), title, fontsize=16.5, fontweight="bold", va="center")
    y += title_h
    if subtitle:
        ax.text(X(pad), Y(y + 0.10), subtitle, fontsize=11.5, color=INK2, va="center")
        y += sub_h

    # column x offsets
    xs = [pad]
    for w in col_w[:-1]:
        xs.append(xs[-1] + w)

    # header
    for (label, _), x in zip(columns, xs):
        ax.text(
            X(x),
            Y(y + head_h / 2),
            label.upper(),
            fontsize=head_fs,
            color=MUTED,
            va="center",
            fontweight="bold",
        )
    y += head_h
    ax.add_line(
        plt.Line2D([X(pad), X(pad + table_w)], [Y(y), Y(y)], color=AXIS, lw=1.2)
    )

    for r, row in enumerate(wrapped):
        h = row_h[r]
        if highlight is not None and r == highlight:
            ax.add_patch(
                Rectangle(
                    (X(pad - 0.14), Y(y + h)),
                    (table_w + 0.28) / fig_w,
                    h / fig_h,
                    facecolor=BLUE_TINT,
                    edgecolor="none",
                    zorder=0,
                )
            )
            ax.add_patch(
                Rectangle(
                    (X(pad - 0.14), Y(y + h)),
                    0.035 / fig_w,
                    h / fig_h,
                    facecolor=BLUE,
                    edgecolor="none",
                    zorder=1,
                )
            )
        for c, lines in enumerate(row):
            for li, line in enumerate(lines):
                ax.text(
                    X(xs[c]),
                    Y(y + row_pad / 2 + line_h * (li + 0.5)),
                    line,
                    fontsize=fs,
                    va="center",
                    color=INK if c == 0 else INK2,
                    fontweight="bold" if (c == 0 or r == highlight) else "normal",
                    family=MONO if c in mono_cols else SANS,
                )
        y += h
        if r < len(rows) - 1:
            ax.add_line(
                plt.Line2D(
                    [X(pad), X(pad + table_w)], [Y(y), Y(y)], color=RULE, lw=0.9
                )
            )

    ax.add_line(
        plt.Line2D([X(pad), X(pad + table_w)], [Y(y), Y(y)], color=AXIS, lw=1.2)
    )
    if footnote:
        for i, line in enumerate(foot_lines):
            ax.text(
                X(pad),
                Y(y + 0.26 + 0.21 * i),
                line,
                fontsize=11.0,
                color=MUTED,
                va="center",
            )

    _save(fig, name)


# --------------------------------------------------------------------------
# 1. Where the coordinator runs
# --------------------------------------------------------------------------
def fig_coordinator() -> None:
    draw_table(
        "01-coordinator-choice.png",
        "Where the coordinator runs sets the secret count",
        [("Coordinator host", 0.26), ("Legs it makes", 0.44), ("Long-lived secrets", 0.30)],
        [
            ["Cloud Run", "GCP→AWS, GCP→Azure, GCP→GCP", "potentially zero"],
            ["Bedrock AgentCore", "AWS→Azure, AWS→GCP", "at least one"],
            ["Microsoft Foundry", "Azure→AWS, Azure→GCP", "one or two, both unproven"],
        ],
        subtitle="Only a runtime that can mint a workload OIDC token for an audience you choose can federate outward.",
        highlight=0,
        footnote="Cloud Run is the only one of the three whose OIDC minting was confirmed here. AgentCore's was not tested — “zero secrets” is a property of this topology, not a law about cross-cloud agents.",
    )


# --------------------------------------------------------------------------
# 2. Three legs, three mechanisms, one seam
# --------------------------------------------------------------------------
def fig_three_legs() -> None:
    fig_w, fig_h = 10.0, 6.5
    fig, ax = _canvas(fig_w, fig_h)
    A = fig_h / fig_w  # aspect correction for x-units

    def X(i):
        return i / fig_w

    def Y(i):
        return 1 - i / fig_h

    ax.text(X(0.42), Y(0.46), "Three legs, three mechanisms, one seam",
            fontsize=16.5, fontweight="bold", va="center")
    ax.text(X(0.42), Y(0.82),
            "Two bearer tokens and a request signature — different shapes, one interface.",
            fontsize=11.5, color=INK2, va="center")

    lanes = [
        ("GCP → GCP", "in-cloud hop", BLUE,
         ["Mint Google ID token\naud = target service URL", "Call with bearer\nroles/run.invoker"],
         "bearer token"),
        ("GCP → AWS", "cross-cloud", BLUE,
         ["Mint Google ID token\nformat=full", "STS AssumeRole\nWithWebIdentity", "Sign request\nwith SigV4"],
         "request signature"),
        ("GCP → Azure", "cross-cloud", BLUE,
         ["Mint Google ID token\naud = api://<app-id>", "Entra federated\ncredential exchange", "Call with Entra\naccess token"],
         "bearer token"),
    ]

    lane_top = 1.35
    lane_h = 1.30
    label_w = 1.75
    chip_h = 0.86
    gap = 0.30
    right_margin = 0.42
    chip_area_x = 0.42 + label_w
    chip_area_w = fig_w - chip_area_x - right_margin - 1.55

    for li, (name, kind, color, steps, result) in enumerate(lanes):
        cy = lane_top + li * lane_h + lane_h / 2 - 0.18
        ax.text(X(0.42), Y(cy - 0.10), name, fontsize=13.5, fontweight="bold", va="center")
        ax.text(X(0.42), Y(cy + 0.20), kind, fontsize=10.5, color=MUTED, va="center")

        n = len(steps)
        chip_w = (chip_area_w - gap * (n - 1)) / n
        for si, step in enumerate(steps):
            x0 = chip_area_x + si * (chip_w + gap)
            ax.add_patch(
                FancyBboxPatch(
                    (X(x0), Y(cy + chip_h / 2)),
                    chip_w / fig_w,
                    chip_h / fig_h,
                    boxstyle="round,pad=0,rounding_size=0.008",
                    facecolor=BLUE_TINT,
                    edgecolor=BLUE,
                    linewidth=1.1,
                )
            )
            head, tail = step.split("\n")
            ax.text(X(x0 + chip_w / 2), Y(cy - 0.10), head, fontsize=10.8,
                    ha="center", va="center", fontweight="bold", color=INK)
            ax.text(X(x0 + chip_w / 2), Y(cy + 0.17), tail, fontsize=9.6,
                    ha="center", va="center", color=INK2, family=MONO)
            if si < n - 1:
                ax.annotate(
                    "", xy=(X(x0 + chip_w + gap - 0.06), Y(cy)),
                    xytext=(X(x0 + chip_w + 0.06), Y(cy)),
                    arrowprops=dict(arrowstyle="-|>", color=AXIS, lw=1.4),
                )
        x_end = chip_area_x + chip_area_w
        ax.annotate("", xy=(X(x_end + 0.34), Y(cy)), xytext=(X(x_end + 0.06), Y(cy)),
                    arrowprops=dict(arrowstyle="-|>", color=AXIS, lw=1.4))
        ax.text(X(x_end + 0.44), Y(cy), result, fontsize=11.0, va="center", color=INK2)

    # the seam
    seam_y = lane_top + 3 * lane_h + 0.22
    ax.add_patch(
        Rectangle((X(0.42), Y(seam_y + 0.92)), (fig_w - 0.84) / fig_w, 0.92 / fig_h,
                  facecolor="#f7f7f5", edgecolor=AXIS, linewidth=1.0)
    )
    ax.text(X(0.72), Y(seam_y + 0.32), "One seam: httpx.Auth",
            fontsize=12.5, fontweight="bold", va="center", family=MONO)
    ax.text(X(0.72), Y(seam_y + 0.64),
            "auth = credentials_for(peer, endpoint)   →   load_client(stack, endpoint, auth=auth)",
            fontsize=11.0, color=INK2, va="center", family=MONO)

    _save(fig, "02-three-legs.png")


# --------------------------------------------------------------------------
# 3. Warm consensus latency
# --------------------------------------------------------------------------
def fig_latency() -> None:
    fig_w, fig_h = 10.0, 5.2
    fig = plt.figure(figsize=(fig_w, fig_h), dpi=DPI)
    ax = fig.add_axes([0.235, 0.175, 0.72, 0.615])

    # Elapsed sits below a gap: it is the whole run, not a fourth leg.
    rows = [
        ("Elapsed (whole run)", 0.0, 1711, 1854, ORANGE),
        ("AWS — AgentCore", 1.9, 1027, 1109, BLUE),
        ("GCP — Cloud Run *", 2.8, 836, 948, BLUE),
        ("Azure — Container Apps", 3.7, 468, 512, BLUE),
    ]

    for label, y, lo, hi, color in rows:
        ax.barh(y, hi - lo, left=lo, height=0.30, color=color,
                edgecolor=SURFACE, linewidth=1.5, zorder=3)
        ax.text(hi + 55, y, f"{lo}–{hi} ms", va="center", fontsize=11.5,
                color=INK, fontweight="bold" if color == ORANGE else "normal")

    ax.set_yticks([r[1] for r in rows])
    ax.set_yticklabels([r[0] for r in rows], fontsize=12)
    ax.tick_params(axis="y", length=0, pad=8)
    ax.set_xlim(0, 2400)
    ax.set_ylim(-0.62, 4.15)
    ax.axhline(0.95, color=RULE, lw=1.0, zorder=1)
    ax.set_xlabel("milliseconds, warm runs on the rebuilt mesh", fontsize=11, color=INK2, labelpad=8)
    ax.tick_params(axis="x", colors=MUTED, labelsize=10.5)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.spines["bottom"].set_color(AXIS)
    ax.xaxis.grid(True, color=RULE, lw=0.9)
    ax.set_axisbelow(True)

    # the gap between the slowest leg and elapsed
    ax.annotate("", xy=(1711, 1.35), xytext=(1109, 1.35),
                arrowprops=dict(arrowstyle="<|-|>", color=MUTED, lw=1.2))
    ax.text(1410, 1.20, "+~1s coordinator fixed cost\ncontainer start, 3 card fetches, 3 mints",
            ha="center", va="top", fontsize=10.2, color=INK2, linespacing=1.45)

    fig.text(0.06, 0.930, "Elapsed is max(legs) + ~1s — not their sum",
             fontsize=16.5, fontweight="bold", ha="left")
    fig.text(0.06, 0.872,
             "The legs are issued concurrently. Quoting the slowest leg alone was wrong by 85% on the fastest run.",
             fontsize=11.5, color=INK2, ha="left")
    fig.text(0.06, 0.030, "* in-cloud hop — the coordinator and the GCP agent are both on Cloud Run",
             fontsize=10.2, color=MUTED, ha="left")

    _save(fig, "03-latency.png")


# --------------------------------------------------------------------------
# 4. The hosted 3x3 interop matrix
# --------------------------------------------------------------------------
def fig_matrix() -> None:
    fig_w, fig_h = 10.0, 6.1
    fig = plt.figure(figsize=(fig_w, fig_h), dpi=DPI)
    ax = fig.add_axes([0.20, 0.175, 0.62, 0.585])

    clients = ["a2a-sdk", "agent-framework", "google-adk"]
    servers = ["gcp *", "aws", "azure"]
    # None = the failed cell (interop finding 2, a transport failure)
    cells = [[992, 1328, 538], [504, 994, 570], [None, 5953, 471]]

    vmax = 6000
    for r in range(3):
        for c in range(3):
            v = cells[r][c]
            if v is None:
                face, txt, label = CRITICAL, "#ffffff", "transport"
            else:
                # sequential blue: darker = slower
                step = min(len(RAMP) - 1, int((v / vmax) ** 0.45 * len(RAMP)))
                face = RAMP[step]
                txt = "#ffffff" if step >= 3 else INK
                label = f"{v} ms"
            ax.add_patch(
                Rectangle((c + 0.015, 2 - r + 0.015), 0.97, 0.97,
                          facecolor=face, edgecolor=SURFACE, linewidth=3)
            )
            ax.text(c + 0.5, 2 - r + 0.58, label, ha="center", va="center",
                    fontsize=13, color=txt, fontweight="bold")
            ax.text(c + 0.5, 2 - r + 0.33, "ok" if v is not None else "failed",
                    ha="center", va="center", fontsize=10, color=txt)

    ax.set_xlim(0, 3)
    ax.set_ylim(0, 3)
    ax.set_xticks([0.5, 1.5, 2.5])
    ax.set_xticklabels([f"{s}\nserver" for s in servers], fontsize=12)
    ax.set_yticks([2.5, 1.5, 0.5])
    ax.set_yticklabels(clients, fontsize=12)
    ax.tick_params(length=0, pad=8)
    ax.xaxis.set_ticks_position("top")
    for s in ax.spines.values():
        s.set_visible(False)

    fig.text(0.06, 0.935, "The 3×3 matrix, hosted: 8 of 9",
             fontsize=16.5, fontweight="bold", ha="left")
    fig.text(0.06, 0.887,
             "Every cell is one real A2A call. Six of the eight passing cells crossed a vendor boundary.",
             fontsize=11.5, color=INK2, ha="left")
    fig.text(0.06, 0.085,
             "* in-cloud hop — gcp shares the coordinator's cloud, so those cells do not support the interop claim.\n"
             "The red cell is interop finding 2: ADK's own client against ADK's own server. Darker = slower.",
             fontsize=10.2, color=MUTED, ha="left", va="top")

    _save(fig, "04-matrix.png")


# --------------------------------------------------------------------------
# 5. The eight negative controls
# --------------------------------------------------------------------------
def fig_controls() -> None:
    fig_w, fig_h = 10.0, 5.4
    fig, ax = _canvas(fig_w, fig_h)

    def X(i):
        return i / fig_w

    def Y(i):
        return 1 - i / fig_h

    ax.text(X(0.42), Y(0.46), "Eight probes, each scoped to one leg",
            fontsize=16.5, fontweight="bold", va="center")
    ax.text(X(0.42), Y(0.82),
            "The mesh degrades on purpose, so a three-cloud run with one credential removed still exits 0.",
            fontsize=11.5, color=INK2, va="center")

    probes = [
        ("GCP leg, with its credential", "answers"),
        ("GCP leg, credential removed", "denied"),
        ("AWS leg, with its credential", "answers"),
        ("AWS leg, credential removed", "denied"),
        ("Azure leg, with its credential", "answers"),
        ("Azure leg, credential removed", "denied"),
        ("Unauthenticated curl at the ingress", "403"),
        ("Right identity, wrong audience", "rejected"),
    ]

    top = 1.35
    cell_h = 0.62
    col_w = (fig_w - 0.84 - 0.30) / 2
    for i, (probe, outcome) in enumerate(probes):
        col, row = i % 2, i // 2
        x0 = 0.42 + col * (col_w + 0.30)
        y0 = top + row * (cell_h + 0.16)
        ax.add_patch(
            Rectangle((X(x0), Y(y0 + cell_h)), col_w / fig_w, cell_h / fig_h,
                      facecolor="#f4faf4", edgecolor="#cfe8cf", linewidth=1.0)
        )
        # Liberation Sans has no U+2713; DejaVu Sans does.
        ax.text(X(x0 + 0.20), Y(y0 + cell_h / 2), "✓", fontsize=15,
                color=GOOD, va="center", ha="center", fontweight="bold",
                family="DejaVu Sans")
        ax.text(X(x0 + 0.40), Y(y0 + cell_h / 2 - 0.11), probe, fontsize=11.6,
                va="center", color=INK)
        ax.text(X(x0 + 0.40), Y(y0 + cell_h / 2 + 0.14), f"as expected — {outcome}",
                fontsize=10.0, va="center", color=INK2)

    y = top + 4 * (cell_h + 0.16) + 0.10
    ax.text(X(0.42), Y(y + 0.22),
            "All eight passed against infrastructure that had not existed an hour earlier.",
            fontsize=11.5, color=INK2, va="center", fontweight="bold")
    ax.text(X(0.42), Y(y + 0.50),
            "A denial absorbed by the median is indistinguishable from no denial at all — which is why no probe uses more than one leg.",
            fontsize=10.4, color=MUTED, va="center")

    _save(fig, "05-controls.png")


# --------------------------------------------------------------------------
# 6. Cold start vs warm
# --------------------------------------------------------------------------
def fig_cold_warm() -> None:
    fig_w, fig_h = 10.0, 4.0
    fig = plt.figure(figsize=(fig_w, fig_h), dpi=DPI)
    ax = fig.add_axes([0.255, 0.235, 0.66, 0.45])

    labels = ["Cold — first call into the leg", "Warm — every call after"]
    values = [27.8, 0.5]
    colors = [ORANGE, BLUE]
    for i, (v, c) in enumerate(zip(values, colors)):
        ax.barh(1 - i, v, height=0.42, color=c, edgecolor=SURFACE, linewidth=2, zorder=3)
        ax.text(v + 0.45, 1 - i, f"{v} s", va="center", fontsize=13.5,
                fontweight="bold", color=INK)

    ax.set_yticks([1, 0])
    ax.set_yticklabels(labels, fontsize=12)
    ax.tick_params(axis="y", length=0, pad=8)
    ax.set_xlim(0, 32)
    ax.set_ylim(-0.55, 1.55)
    ax.set_xlabel("seconds — the Azure leg, measured", fontsize=11, color=INK2, labelpad=8)
    ax.tick_params(axis="x", colors=MUTED, labelsize=10.5)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.spines["bottom"].set_color(AXIS)
    ax.xaxis.grid(True, color=RULE, lw=0.9)
    ax.set_axisbelow(True)

    ax.text(3.1, 0, "56× faster once warm", fontsize=11, color=INK2, va="center")

    fig.text(0.06, 0.905, "Scale to zero, and label what it costs",
             fontsize=16.5, fontweight="bold", ha="left")
    fig.text(0.06, 0.825,
             "Everything here idles at zero replicas. Mix these two regimes in one table and every conclusion drawn from it is wrong.",
             fontsize=11.5, color=INK2, ha="left")
    _save(fig, "06-cold-warm.png")


# --------------------------------------------------------------------------
# 7. Five traps
# --------------------------------------------------------------------------
def fig_traps() -> None:
    draw_table(
        "07-traps.png",
        "Five traps that look exactly like working configuration",
        [("What you'd assume", 0.30), ("What is actually true", 0.42), ("How it shows up", 0.28)],
        [
            ["Audience is authorization",
             "The caller picks the audience, so an audience-only condition proves only that somebody in that IdP minted a token. Pin the subject, by immutable numeric ID.",
             "Nothing. It works, for the wrong callers too."],
            ["AWS and Azure need the same setup",
             "AWS federates with accounts.google.com natively — creating an explicit IAM OIDC provider breaks it. Entra requires you to create the credential.",
             "InvalidIdentityToken, naming neither rule"],
            ["The IAM condition keys hold what their names say",
             "accounts.google.com:oaud is the token's aud. accounts.google.com:aud is its azp, a number. An audience string in :aud can never match.",
             "A denial that never mentions the key"],
            ["A minted token carries every claim",
             "The GCP metadata mint needs format=full. Without it Google trims claims — including email — and conditions reading them stop matching.",
             "A condition that silently stops matching"],
            ["An auth error is an auth error",
             "InvalidIdentityToken means the token never validated: a provider-setup bug. AccessDenied means it validated and your conditions did not match: a policy bug.",
             "Two different afternoons"],
        ],
        subtitle="None of these are typos. Each is something you can get wrong while being careful.",
        footnote="Which is why the coordinator logs the raw provider response at every auth boundary: in an agent system an error returns as a tool result, and the model in the middle will paraphrase “AccessDenied: condition accounts.google.com:sub did not match” into “there was an issue with the credentials.”",
        fig_w=11.0,
    )


# --------------------------------------------------------------------------
# 8. Scaffolding worth stealing
# --------------------------------------------------------------------------
def fig_scaffolding() -> None:
    draw_table(
        "08-scaffolding.png",
        "Scaffolding worth stealing",
        [("Structure", 0.34), ("What it buys", 0.66)],
        [
            ["One credential seam (httpx.Auth)",
             "Callers never know which of three mechanisms they are using — and auth attaches to the client, so the agent-card fetch is covered too."],
            ["One participant interface (convert())",
             "A cloud is an implementation, not a branch. Adding a fourth touches one file."],
            ["An instrument, not a demo",
             "Every failure is typed by layer — transport, protocol, timeout, authentication, provider — rather than just red."],
            ["Controls scoped to one leg",
             "A system built to survive failure will otherwise hide the exact failure you are testing for."],
        ],
        subtitle="Four structures did most of the work.",
        footnote="The general form of the last one: any system with graceful degradation needs its controls scoped to a single component.",
    )


if __name__ == "__main__":
    fig_coordinator()
    fig_three_legs()
    fig_latency()
    fig_matrix()
    fig_controls()
    fig_cold_warm()
    fig_traps()
    fig_scaffolding()
