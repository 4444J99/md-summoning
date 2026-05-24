#!/usr/bin/env python3
"""
Markdown density map: for each .md file, compute its directory-depth
distance to the nearest enclosing git repo.  Uses `fd` for fast
filesystem traversal.
"""

import subprocess
import sys
import time
from pathlib import Path
import numpy as np
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import LogNorm

HOME = Path.home()


def run_fd(args: list[str], timeout=120) -> list[str]:
    cmd = ["fd", "--no-ignore", "--hidden"] + args
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    lines = [l.strip() for l in result.stdout.split("\n") if l.strip()]
    return lines


def main():
    start = time.time()

    excludes = [
        "Library",
        ".Trash",
        ".cache",
        "Applications",
        "Backups.backupdb",
        ".Spotlight-V100",
        "Caches",
        "tmp",
    ]

    print("Finding .git directories …", file=sys.stderr)
    git_lines = subprocess.run(
        [
            "fd",
            "--no-ignore",
            "--hidden",
            "--type",
            "d",
            "--exclude",
            "Library",
            "--exclude",
            ".Trash",
            "--exclude",
            ".cache",
            ".git",
            str(HOME),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    git_roots = []
    for raw in git_lines.stdout.strip().split("\n"):
        raw = raw.strip()
        if not raw:
            continue
        p = Path(raw)
        if p.name == ".git":
            gr = p.parent.resolve()
            if gr != HOME:
                git_roots.append(gr)
    git_roots = list(set(git_roots))  # deduplicate
    git_root_set = set(git_roots)
    print(
        f"  Found {len(git_roots)} git repos in {time.time() - start:.1f}s",
        file=sys.stderr,
    )

    print("Finding .md files …", file=sys.stderr)
    md_lines = subprocess.run(
        [
            "fd",
            "--no-ignore",
            "--hidden",
            "--type",
            "f",
            "--exclude",
            "Library",
            "--exclude",
            ".Trash",
            "--exclude",
            ".cache",
            "--extension",
            "md",
            ".",
            str(HOME),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    md_paths = [Path(l.strip()) for l in md_lines.stdout.split("\n") if l.strip()]
    print(
        f"  Found {len(md_paths)} .md/.mdx files in {time.time() - start:.1f}s",
        file=sys.stderr,
    )

    print("Computing distances …", file=sys.stderr)
    depths = []
    outside_depths = []
    for md in md_paths:
        parent = md.parent.resolve()
        p = parent
        d = 0
        found = False
        while True:
            if p in git_root_set:
                depths.append(d)
                found = True
                break
            if p == p.parent or p == HOME:
                break
            p = p.parent
            d += 1
        if not found:
            outside_depths.append(0)

    print(
        f"  Inside repo: {len(depths)}  Outside: {len(outside_depths)}", file=sys.stderr
    )
    print(f"  Total time: {time.time() - start:.1f}s", file=sys.stderr)

    arr = np.array(depths, dtype=int)

    if len(arr) == 0:
        print("No markdown files inside git repos found.", file=sys.stderr)
        return

    max_depth = int(np.percentile(arr, 98)) + 1
    arr_clipped = np.clip(arr, 0, max_depth)
    total = len(depths) + len(outside_depths)

    # --- Build 2D data: depth vs file-size ---
    sizes_kb = []
    depths_2d = []
    md_inside_paths = []
    for md in md_paths:
        parent = md.parent.resolve()
        p = parent
        d = 0
        found = False
        while True:
            if p in git_root_set:
                found = True
                break
            if p == p.parent or p == HOME:
                break
            p = p.parent
            d += 1
        if found:
            depths_2d.append(d)
            sizes_kb.append(md.stat().st_size / 1024)
            md_inside_paths.append(md)

    # --- Plotting ---
    fig, axes = plt.subplots(1, 2, figsize=(16, 6))
    fig.suptitle("Markdown density relative to git repositories", fontsize=16, y=1.02)

    # Histogram
    ax = axes[0]
    bins = np.arange(0, max_depth + 2) - 0.5
    counts, _, _ = ax.hist(
        arr_clipped, bins=bins, color="#2b6cb0", edgecolor="white", linewidth=0.7
    )
    ax.set_xlabel("Directory levels from git root", fontsize=12)
    ax.set_ylabel("Number of .md files", fontsize=12)
    ax.set_title(f"Proximity to git repos (n={len(arr)})", fontsize=13)
    ax.set_xticks(range(max_depth + 1))

    top = max(counts) if len(counts) else 1
    for i, c in enumerate(counts):
        if c > 0:
            ax.text(
                i,
                c + top * 0.015,
                str(int(c)),
                ha="center",
                va="bottom",
                fontsize=8,
                fontweight="bold",
            )

    ax.text(
        0.97,
        0.95,
        f"Outside git repos:\n{len(outside_depths)} files",
        transform=ax.transAxes,
        ha="right",
        va="top",
        fontsize=11,
        color="#c0392b",
        bbox=dict(boxstyle="round,pad=0.3", facecolor="#ffe0e0", alpha=0.8),
    )

    # 2D density: depth vs file size
    ax = axes[1]
    if sizes_kb:
        d_arr = np.array(depths_2d, dtype=int)
        s_arr = np.array(sizes_kb)
        s_clipped = np.clip(s_arr, 0, np.percentile(s_arr, 98))
        h = ax.hist2d(
            d_arr,
            s_clipped,
            bins=[min(20, max_depth), 30],
            norm=LogNorm(),
            cmap="viridis",
        )
        ax.set_xlabel("Directory levels from git root", fontsize=12)
        ax.set_ylabel("File size (KB)", fontsize=12)
        ax.set_title("Depth vs file-size density (log count)", fontsize=13)
        fig.colorbar(h[3], ax=ax, label="Count (log)")
    else:
        ax.text(0.5, 0.5, "No data", transform=ax.transAxes, ha="center", va="center")

    fig.tight_layout()
    out = Path(
        "/var/folders/l9/zn9x070d4xqb1qb5wfzr9tjr0000gn/T/opencode/md_density_map.png"
    )
    fig.savefig(out, dpi=150, bbox_inches="tight")
    print(f"\nSaved → {out}", file=sys.stderr)

    # --- Summary ---
    print(f"\n{'=' * 60}")
    print(f"  MARKDOWN DENSITY MAP")
    print(f"{'=' * 60}")
    print(f"  Total .md files scanned:  {total}")
    print(
        f"  Inside a git repo:        {len(depths)} ({100 * len(depths) / total:.0f}%)"
    )
    print(
        f"  Outside any git repo:     {len(outside_depths)} ({100 * len(outside_depths) / total:.0f}%)"
    )
    print(f"  Git repos discovered:     {len(git_roots)}")
    if len(arr):
        print(f"  Depth stats (levels from repo root):")
        print(f"    Mean:   {arr.mean():.1f}")
        print(f"    Median: {np.median(arr):.0f}")
        print(f"    p25:    {np.percentile(arr, 25):.0f}")
        print(f"    p75:    {np.percentile(arr, 75):.0f}")
        print(f"    p99:    {np.percentile(arr, 99):.0f}")
        print(f"    Max:    {arr.max()}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
