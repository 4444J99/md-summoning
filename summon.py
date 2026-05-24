#!/usr/bin/env python3
"""md-summoning: centralize all ~/ .md files into ~/Meta/Documents/."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Iterator

logger = logging.getLogger("summon")


def load_config(config_path: Path) -> dict:
    text = config_path.read_text()
    if config_path.suffix in (".yaml", ".yml"):
        try:
            import yaml

            return yaml.safe_load(text)
        except ImportError:
            logger.warning("PyYAML not available, falling back to JSON parsing")
    return json.loads(text)


DEFAULT_CONFIG = {
    "central_dir": "~/Meta/Documents",
    "min_file_bytes": 100,
    "batch_size": 1000,
    "hash_algorithm": "sha256",
    "flat_hash_length": 12,
    "log_level": "INFO",
    "exclusions": [
        "*/.git/objects/**",
        "*/node_modules/**",
        "*/Library/**",
        ".Trash/**",
        "*/.Trash/**",
        "*/.cache/**",
        "*/Caches/**",
        "*/.specstory/history/**",
        "*/.claude/projects/*/memory/**",
        "*/.gemini/**",
        "*/.serena/**",
        "*/.codex/memory/**",
    ],
    "thresholds": {
        "very_large_file_bytes": 10_485_760,
        "flat_hash_collision_retry_length": 16,
        "checkpoint_interval": 10_000,
        "hardlink_dedup_threshold_bytes": 1024,
    },
    "paths": {
        "provenance_dir": "provenance",
        "registry_file": "provenance/registry.jsonl",
        "registry_schema": "provenance/registry-schema.json",
        "breadcrumb_template": "provenance/breadcrumb-template.md",
        "manifest": "MANIFEST.md",
        "errors_log": "provenance/errors.log",
    },
    "reporting": {
        "include_sample_paths": 10,
        "verify_sample_percent": 1,
    },
}


HOME = Path.home()
EXCLUSION_PATTERNS: list[re.Pattern] = []


def compile_exclusions(patterns: list[str]) -> list[re.Pattern]:
    compiled = []
    for pat in patterns:
        parts = pat.split("/")
        regex_parts = []
        for part in parts:
            if part == "**":
                regex_parts.append(".*")
            elif part == "*":
                regex_parts.append("[^/]*")
            else:
                regex_parts.append(re.escape(part))
        compiled.append(re.compile("^" + "/".join(regex_parts) + "$"))
    return compiled


def is_excluded(path: Path, compiled_patterns: list[re.Pattern]) -> bool:
    as_str = str(path)
    for pat in compiled_patterns:
        if pat.search(as_str):
            return True
    return False


@dataclass
class FileRecord:
    path: str
    hash: str
    hash_prefix: str
    size: int
    mtime: float
    kind: str
    depth: int
    central_path: str = ""
    repo: str = ""
    repo_root: str = ""
    flat_name: str = ""
    is_symlink: bool = False
    summoned_at: str = ""
    status: str = "active"
    note: str = ""


def enumerate_md_files(
    search_root: Path,
    exclusions: list[str],
) -> Iterator[Path]:
    compiled = compile_exclusions(exclusions)
    result = subprocess.run(
        [
            "fd",
            "--extension",
            "md",
            "--type",
            "f",
            "--no-ignore",
            "--absolute-path",
            ".",
        ],
        capture_output=True,
        text=True,
        cwd=search_root,
        timeout=300,
    )
    if result.returncode != 0:
        logger.error("fd failed: %s", result.stderr)
        sys.exit(1)
    for line in result.stdout.strip().splitlines():
        path = Path(line)
        if not path.exists():
            continue
        if is_excluded(path, compiled):
            continue
        yield path


def stat_file(path: Path) -> tuple[int, float, bool]:
    try:
        st = path.stat()
        is_sym = path.is_symlink()
        real = path.resolve()
        if real != path:
            st = real.stat()
        return st.st_size, st.st_mtime, is_sym
    except (OSError, PermissionError) as e:
        logger.warning("Cannot stat %s: %s", path, e)
        return 0, 0.0, False


def compute_hash(path: Path, algorithm: str = "sha256") -> str | None:
    try:
        h = hashlib.new(algorithm)
        with open(path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except (OSError, PermissionError, ValueError) as e:
        logger.warning("Cannot hash %s: %s", path, e)
        return None


def detect_git_root(path: Path) -> Path | None:
    for parent in [path] + list(path.parents):
        git_dir = parent / ".git"
        if git_dir.exists():
            return parent

        git_file = parent / ".git"
        if git_file.is_file():
            try:
                content = git_file.read_text().strip()
                match = re.match(r"^gitdir:\s+(.+)", content)
                if match:
                    resolved = Path(match.group(1)).resolve()
                    for p in [resolved] + list(resolved.parents):
                        if (p / "HEAD").exists():
                            return parent
            except (OSError, PermissionError):
                pass
    return None


def detect_repo_org(repo_root: Path) -> str:
    parts = (
        repo_root.relative_to(HOME).parts
        if HOME in repo_root.parents
        else repo_root.parts
    )
    return "--".join(parts[:2]) if len(parts) >= 2 else parts[0]


def classify_path(
    path: Path,
    repo_root: Path | None,
) -> tuple[str, int, str, str]:
    if repo_root:
        if path.is_symlink():
            return ("symlink-resolved", 0, "", str(repo_root))
        depth = len(path.relative_to(repo_root).parent.parts)
        repo_org = detect_repo_org(repo_root)
        return ("git-tracked", depth, repo_org, str(repo_root))
    else:
        depth = len(path.relative_to(HOME).parent.parts) if HOME in path.parents else 0
        return ("orphan", depth, "", "")


def build_registry(config: dict) -> list[FileRecord]:
    central = Path(config["central_dir"]).expanduser()
    exclusions = config.get("exclusions", [])
    algorithm = config.get("hash_algorithm", "sha256")
    min_bytes = config.get("min_file_bytes", 100)

    logger.info("Enumerating .md files from %s ...", HOME)
    paths = list(enumerate_md_files(HOME, exclusions))
    logger.info("Found %d files after exclusion filtering", len(paths))

    records: list[FileRecord] = []
    total = len(paths)
    start = time.time()

    for idx, path in enumerate(paths):
        size, mtime, is_sym = stat_file(path)
        if size < min_bytes:
            continue

        raw_hash = compute_hash(path, algorithm)
        if raw_hash is None:
            continue

        repo_root = detect_git_root(path)
        kind, depth, repo_name, repo_root_str = classify_path(path, repo_root)

        rec = FileRecord(
            path=str(path),
            hash=raw_hash,
            hash_prefix=raw_hash[: config["flat_hash_length"]],
            size=size,
            mtime=mtime,
            kind=kind,
            depth=depth,
            repo=repo_name,
            repo_root=repo_root_str,
            is_symlink=is_sym,
            summoned_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        )
        records.append(rec)

        if (idx + 1) % 1000 == 0:
            elapsed = time.time() - start
            rate = (idx + 1) / elapsed if elapsed > 0 else 0
            logger.info("  Progress: %d/%d (%.1f files/sec)", idx + 1, total, rate)

    elapsed = time.time() - start
    logger.info(
        "Registry built: %d records in %.1fs (%.1f files/sec)",
        len(records),
        elapsed,
        total / elapsed if elapsed > 0 else 0,
    )
    return records


def write_registry(records: list[FileRecord], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for rec in records:
            f.write(json.dumps(asdict(rec), sort_keys=True) + "\n")
    logger.info("Wrote %d records to %s", len(records), path)


def registry_stats(records: list[FileRecord]) -> dict:
    kinds: dict[str, int] = {}
    depth_hist: dict[int, int] = {}
    total_size = 0
    max_size = 0
    worst_path = ""

    for rec in records:
        kinds[rec.kind] = kinds.get(rec.kind, 0) + 1
        d = rec.depth
        depth_hist[d] = depth_hist.get(d, 0) + 1
        total_size += rec.size
        if rec.size > max_size:
            max_size = rec.size
            worst_path = rec.path

    orphan_count = kinds.get("orphan", 0)
    tracked_count = kinds.get("git-tracked", 0) + kinds.get("symlink-resolved", 0)

    dedup_counts: dict[str, int] = {}
    for rec in records:
        dedup_counts[rec.hash] = dedup_counts.get(rec.hash, 0) + 1
    collisions = sum(1 for c in dedup_counts.values() if c > 1)

    return {
        "total": len(records),
        "kinds": kinds,
        "orphans": orphan_count,
        "git_tracked": tracked_count,
        "total_size_bytes": total_size,
        "largest_file_bytes": max_size,
        "largest_file": worst_path,
        "depth_histogram": dict(sorted(depth_hist.items())),
        "dedup_candidates": collisions,
        "unique_hashes": len(dedup_counts),
    }


def cmd_build_registry(args):
    config = load_config(args.config) if args.config else DEFAULT_CONFIG
    config["flat_hash_length"] = args.hash_length or config["flat_hash_length"]
    config["min_file_bytes"] = args.min_bytes or config["min_file_bytes"]

    records = build_registry(config)

    out_path = Path(args.output or config["paths"]["registry_file"])
    if not out_path.is_absolute():
        central = Path(config["central_dir"]).expanduser()
        out_path = central / out_path
    write_registry(records, out_path)

    stats = registry_stats(records)
    print(f"\n=== Registry Summary ===")
    print(f"Total files:    {stats['total']}")
    print(f"Git-tracked:    {stats['git_tracked']}")
    print(f"Orphans:        {stats['orphans']}")
    print(f"Total size:     {stats['total_size_bytes'] / 1024 / 1024:.1f} MB")
    print(
        f"Largest file:   {stats['largest_file']} ({stats['largest_file_bytes'] / 1024:.1f} KB)"
    )
    print(
        f"Dedup cands:    {stats['dedup_candidates']} hash collisions across {stats['unique_hashes']} unique hashes"
    )
    print(f"\nDepth histogram (orphans):")
    for d, c in stats["depth_histogram"].items():
        if c > 0:
            bar = "#" * min(c // 1000, 60)
            print(f"  depth {d:2d}: {c:6d}  {bar}")


def cmd_stats(args):
    registry_path = Path(args.registry)
    if not registry_path.exists():
        logger.error("Registry not found: %s", registry_path)
        sys.exit(1)

    records = []
    with open(registry_path) as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(FileRecord(**json.loads(line)))

    stats = registry_stats(records)
    print(f"\n=== Registry Summary ===")
    print(f"Total files:    {stats['total']}")
    print(f"Git-tracked:    {stats['git_tracked']}")
    print(f"Orphans:        {stats['orphans']}")
    print(f"Total size:     {stats['total_size_bytes'] / 1024 / 1024:.1f} MB")
    print(
        f"Largest file:   {stats['largest_file']} ({stats['largest_file_bytes'] / 1024:.1f} KB)"
    )
    print(
        f"Dedup cands:    {stats['dedup_candidates']} / {stats['unique_hashes']} unique hashes"
    )
    print(f"\nBy kind: {stats['kinds']}")
    print(f"\nDepth histogram:")
    for d, c in stats["depth_histogram"].items():
        bar = "#" * min(c // 1000, 60)
        print(f"  depth {d:2d}: {c:6d}  {bar}")


def cmd_dry_run(args):
    registry_path = Path(args.registry)
    if not registry_path.exists():
        logger.error("Registry not found: %s", registry_path)
        sys.exit(1)

    records = []
    with open(registry_path) as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(FileRecord(**json.loads(line)))

    orphans = [r for r in records if r.kind == "orphan"]
    tracked = [r for r in records if r.kind == "git-tracked"]

    print(f"\n=== Dry Run: What Would Happen ===")
    print(f"Orphans to move:       {len(orphans)}")
    print(f"Git-tracked to symlink: {len(tracked)}")
    print(f"Flat entries to create: {len(records)}")
    print(f"Breadcrumbs to write:   {len(orphans)}")
    print(f"Central dir:            ~/Meta/Documents/")

    if args.verbose:
        print(f"\nTop 10 largest orphans:")
        by_size = sorted(orphans, key=lambda r: r.size, reverse=True)
        for rec in by_size[:10]:
            print(f"  {rec.path} ({rec.size / 1024:.1f} KB)")

        orphans_by_depth: dict[int, list[str]] = {}
        for rec in orphans:
            orphans_by_depth.setdefault(rec.depth, []).append(rec.path)
        print(f"\nOrphans by depth:")
        for d in sorted(orphans_by_depth):
            print(f"  depth {d}: {len(orphans_by_depth[d])} files")

        tracked_by_repo: dict[str, int] = {}
        for rec in tracked:
            tracked_by_repo[rec.repo] = tracked_by_repo.get(rec.repo, 0) + 1
        print(f"\nTop 10 repos by .md count:")
        for repo, count in sorted(tracked_by_repo.items(), key=lambda x: -x[1])[:10]:
            print(f"  {repo}: {count}")


def main():
    parser = argparse.ArgumentParser(description="md-summoning tool")
    parser.add_argument("--config", "-c", type=Path, help="Path to summon.yaml")
    parser.add_argument(
        "--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")

    sub = parser.add_subparsers(dest="command", required=True)

    p_build = sub.add_parser(
        "build-registry", help="Phase 0: enumerate, classify, hash"
    )
    p_build.add_argument("--output", "-o", help="Output path for registry.jsonl")
    p_build.add_argument("--hash-length", type=int, help="Flat hash prefix length")
    p_build.add_argument("--min-bytes", type=int, help="Minimum file size in bytes")
    p_build.set_defaults(func=cmd_build_registry)

    p_stats = sub.add_parser("stats", help="Print statistics from existing registry")
    p_stats.add_argument("registry", type=str, help="Path to registry.jsonl")
    p_stats.set_defaults(func=cmd_stats)

    p_dry = sub.add_parser("dry-run", help="Phase 1: simulate without executing")
    p_dry.add_argument("registry", type=str, help="Path to registry.jsonl")
    p_dry.set_defaults(func=cmd_dry_run)

    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    args.func(args)


if __name__ == "__main__":
    main()
