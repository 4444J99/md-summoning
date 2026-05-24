#!/usr/bin/env python3
"""md-knowledge: canonical archive of all .md files at ~ into /Users/4jp/_doc/."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path

logger = logging.getLogger("summon")

HOME = Path.home()
DOC_DIR = Path("/Users/4jp/_doc")
CONTENT_DIR = DOC_DIR / "content"
MANIFEST_PATH = DOC_DIR / "manifest.jsonl"
DOCUMENTS_DIR = DOC_DIR / "Documents"
HASH_LEN = 12
EXCLUSIONS = [
    "*/node_modules/*",
    "*/Library/*",
    "*/.Trash/*",
    "*/.cache/*",
    "*/Caches/*",
    "*/.specstory/history/*",
    "*/.claude/projects/*/memory/*",
    "*/.gemini/*",
    "*/.serena/*",
    "*/.codex/memory/*",
    "*/.git/objects/*",
    "*/__pycache__/*",
    "*/vendor/*",
    "*/venv/*",
    "*/.venv/*",
    "*/dist/*",
    "*/build/*",
    ".Trash/*",
    "*/.local/share/Trash/*",
    "*/.gitbook/*",
    "*/node_modules/*",
    "*/.next/*",
]
EXCLUDED_PREFIXES = ["/Users/4jp/_doc/"]

COMPILED_EXCLUSIONS: list[re.Pattern] = []

SECRET_PATTERNS: list[re.Pattern] = [
    re.compile(r"(sk-[A-Za-z0-9]{20,})"),  # OpenAI
    re.compile(r"(sk-ant-[A-Za-z0-9]{20,})"),  # Anthropic
    re.compile(r"(gh[pousr]_[A-Za-z0-9]{36,})"),  # GitHub tokens
    re.compile(r"(xox[bpras]-[A-Za-z0-9-]{20,})"),  # Slack
    re.compile(r"(AKIA[0-9A-Z]{16})"),  # AWS access key ID
    re.compile(
        r"(-----BEGIN (RSA|OPENSSH|EC|DSA) PRIVATE KEY-----).+?"
        r"-----END \2 PRIVATE KEY-----",
        re.DOTALL,
    ),  # Private key block
    re.compile(
        r"(eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})"
    ),  # JWT
    re.compile(r"(AIza[0-9A-Za-z_-]{35})"),  # Google API key
    re.compile(r"(1qaz2wsx3edc[a-zA-Z0-9_\-]{16,})"),  # Telegram bot token
    re.compile(r"(sk_live_[0-9a-zA-Z]{20,})"),  # Stripe live key
    re.compile(r"(rk_live_[0-9a-zA-Z]{20,})"),  # Stripe live restricted
]


def apply_scrub(content: bytes) -> bytes:
    """Redact known secret patterns, replacing values with ***REDACTED***."""
    text = content.decode("utf-8", errors="replace")
    for pat in SECRET_PATTERNS:
        text = pat.sub(r"\1***REDACTED***", text)
    return text.encode("utf-8")


def copy_or_scrub(
    src: Path, hash_val: str, scrub: bool
) -> tuple[Path, str, str | None]:
    """Copy (or scrub) src into content store. Returns (dest_path, store_hash, original_hash)."""
    prefix = hash_val[:2]
    dest_dir = CONTENT_DIR / prefix
    dest_dir.mkdir(parents=True, exist_ok=True)

    if not scrub:
        dest = dest_dir / f"{hash_val}.md"
        if not dest.exists():
            shutil.copy2(src, dest)
        return dest, hash_val, None

    original_hash = hash_val
    scrubbed = apply_scrub(src.read_bytes())
    store_hash = hashlib.sha256(scrubbed).hexdigest()
    dest = dest_dir / f"{store_hash}.md"
    if not dest.exists():
        dest.write_bytes(scrubbed)
    return dest, store_hash, original_hash


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
        compiled.append(re.compile("/".join(regex_parts)))
    return compiled


def is_excluded(path: Path) -> bool:
    s = str(path)
    for pre in EXCLUDED_PREFIXES:
        if s.startswith(pre):
            return True
    for pat in COMPILED_EXCLUSIONS:
        if pat.fullmatch(s) or pat.search(s):
            return True
    return False


def enumerate_md_files(root: Path | None = None) -> list[Path]:
    scan_root = root if root else HOME
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
        cwd=scan_root,
        timeout=600,
    )
    if result.returncode != 0:
        logger.error("fd failed: %s", result.stderr)
        sys.exit(1)
    paths = []
    for line in result.stdout.strip().splitlines():
        p = Path(line)
        if p.exists() and not is_excluded(p):
            paths.append(p)
    return paths


def compute_hash(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while chunk := f.read(65536):
                h.update(chunk)
        return h.hexdigest()
    except (OSError, PermissionError):
        return None


def detect_git_root(path: Path) -> Path | None:
    for parent in [path] + list(path.parents):
        git = parent / ".git"
        if git.exists():
            return parent
        if git.is_file():
            try:
                m = re.match(r"gitdir:\s+(.+)", git.read_text().strip())
                if m:
                    resolved = Path(m.group(1)).resolve()
                    for p in [resolved] + list(resolved.parents):
                        if (p / "HEAD").exists():
                            return parent
            except (OSError, PermissionError):
                pass
    return None


def repo_relative_name(repo_root: Path) -> str:
    try:
        parts = repo_root.relative_to(HOME).parts
        if len(parts) >= 2:
            return "--".join(parts[:2])
        return parts[0]
    except ValueError:
        return repo_root.name


def ensure_content_file(hash_val: str) -> Path | None:
    prefix = hash_val[:2]
    dest_dir = CONTENT_DIR / prefix
    dest = dest_dir / f"{hash_val}.md"
    if dest.exists():
        return dest
    return None


def copy_to_content(src: Path, hash_val: str) -> Path:
    prefix = hash_val[:2]
    dest_dir = CONTENT_DIR / prefix
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{hash_val}.md"
    if not dest.exists():
        shutil.copy2(src, dest)
    return dest


def load_manifest() -> dict[str, dict]:
    seen = {}
    if MANIFEST_PATH.exists():
        with open(MANIFEST_PATH) as f:
            for line in f:
                line = line.strip()
                if line:
                    rec = json.loads(line)
                    seen[rec["path"]] = rec
    return seen


def write_manifest(records: list[dict]) -> None:
    tmp = MANIFEST_PATH.with_suffix(".jsonl.tmp")
    with open(tmp, "w") as f:
        for rec in records:
            f.write(json.dumps(rec, sort_keys=True) + "\n")
    tmp.rename(MANIFEST_PATH)
    logger.info("Manifest written: %d entries", len(records))


def cmd_run(args):
    global COMPILED_EXCLUSIONS
    COMPILED_EXCLUSIONS = compile_exclusions(EXCLUSIONS)

    logger.info("Enumerating .md files from %s ...", args.root or HOME)
    paths = enumerate_md_files(args.root)
    logger.info("Found %d files after exclusions", len(paths))

    existing = load_manifest()
    records = []
    new_count = 0
    stale_count = 0
    active_paths = set()
    total = len(paths)
    start = time.time()

    for idx, path in enumerate(paths):
        s = str(path)
        active_paths.add(s)
        raw_hash = compute_hash(path)
        if raw_hash is None:
            continue

        st = path.stat()
        repo_root = detect_git_root(path)
        if repo_root:
            kind = "git-tracked"
            repo = repo_relative_name(repo_root)
            try:
                depth = len(path.relative_to(repo_root).parent.parts)
            except ValueError:
                depth = 0
        else:
            kind = "orphan"
            repo = ""
            try:
                depth = len(path.relative_to(HOME).parent.parts)
            except ValueError:
                depth = 0

        content_path, store_hash, orig_hash = copy_or_scrub(
            path, raw_hash, args.scrub_secrets
        )
        if s not in existing:
            new_count += 1

        rec = {
            "path": s,
            "hash": store_hash,
            "size": st.st_size,
            "mtime": st.st_mtime,
            "kind": kind,
            "repo": repo,
            "depth": depth,
            "status": "active",
        }
        if orig_hash:
            rec["original_hash"] = orig_hash
        records.append(rec)

        if (idx + 1) % 5000 == 0:
            elapsed = time.time() - start
            logger.info("  %d/%d (%.1f/s)", idx + 1, total, (idx + 1) / elapsed)

    stale = [r for p, r in existing.items() if p not in active_paths]
    for rec in stale:
        rec["status"] = "stale"
        records.append(rec)
        stale_count += 1

    records.sort(key=lambda r: r["path"])
    write_manifest(records)

    elapsed = time.time() - start
    logger.info(
        "Done: %d active, %d new, %d stale, %d total in %.1fs",
        len(records) - stale_count,
        new_count,
        stale_count,
        len(records),
        elapsed,
    )

    if args.rebuild_views:
        rebuild_views(records)


def _symlink_safe(target: str, link: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    n = 1
    stem = link.stem
    suffix = link.suffix
    while link.exists() or link.is_symlink():
        link = link.with_name(f"{stem}_{n}{suffix}")
        n += 1
    link.symlink_to(target)


def rebuild_views(records: list[dict]):
    logger.info("Rebuilding symlink views in %s ...", DOCUMENTS_DIR)
    for d in [
        DOCUMENTS_DIR / "by-repo",
        DOCUMENTS_DIR / "by-depth",
        DOCUMENTS_DIR / "flat",
    ]:
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)

    link_count = 0
    for rec in records:
        if rec["status"] != "active":
            continue

        content_rel = f"../../content/{rec['hash'][:2]}/{rec['hash']}.md"
        content_abs = CONTENT_DIR / rec["hash"][:2] / f"{rec['hash']}.md"
        if not content_abs.exists():
            continue

        if rec["kind"] == "git-tracked" and rec["repo"]:
            link = DOCUMENTS_DIR / "by-repo" / rec["repo"] / Path(rec["path"]).name
            _symlink_safe(content_rel, link)
            link_count += 1
        elif rec["kind"] == "orphan":
            src = Path(rec["path"])
            try:
                rel = src.relative_to(HOME)
            except ValueError:
                rel = Path(*src.parts[1:]) if src.is_absolute() else src
            flat_name = rel.as_posix().replace("/", "--")
            link = DOCUMENTS_DIR / "by-depth" / f"depth-{rec['depth']}" / flat_name
            _symlink_safe(content_rel, link)
            link_count += 1

        hpre = rec["hash"][:HASH_LEN]
        link = DOCUMENTS_DIR / "flat" / f"{hpre}.md"
        _symlink_safe(content_rel, link)
        link_count += 1

    logger.info("Symlinks created: %d", link_count)


def cmd_status(args):
    if not MANIFEST_PATH.exists():
        logger.error("No manifest at %s", MANIFEST_PATH)
        sys.exit(1)

    records = []
    with open(MANIFEST_PATH) as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))

    active = [r for r in records if r["status"] == "active"]
    stale = [r for r in records if r["status"] == "stale"]
    tracked = [r for r in active if r["kind"] == "git-tracked"]
    orphans = [r for r in active if r["kind"] == "orphan"]

    total_size = sum(r["size"] for r in active)
    unique_hashes = len(set(r["hash"] for r in active))
    content_files = sum(1 for _ in CONTENT_DIR.rglob("*.md"))

    print(f"\n=== _doc Status ===")
    print(f"Active files:     {len(active)}")
    print(f"  Git-tracked:    {len(tracked)}")
    print(f"  Orphans:        {len(orphans)}")
    print(f"Stale (deleted):  {len(stale)}")
    print(f"Unique hashes:    {unique_hashes}")
    print(f"Content files:    {content_files}")
    print(f"Total size:       {total_size / 1024 / 1024:.1f} MB")
    print(f"Manifest:         {MANIFEST_PATH}")

    if args.verbose:
        if orphans:
            print(f"\nOrphans by depth:")
            depth_counts: dict[int, int] = {}
            for r in orphans:
                depth_counts[r["depth"]] = depth_counts.get(r["depth"], 0) + 1
            for d in sorted(depth_counts):
                print(f"  depth {d:2d}: {depth_counts[d]}")

        if tracked:
            print(f"\nTop repos by .md count:")
            repo_counts: dict[str, int] = {}
            for r in tracked:
                repo_counts[r["repo"]] = repo_counts.get(r["repo"], 0) + 1
            for repo, count in sorted(repo_counts.items(), key=lambda x: -x[1])[:15]:
                print(f"  {repo}: {count}")


def cmd_verify(args):
    """Verify content integrity against manifest."""
    records = load_manifest()
    active = [r for r in records.values() if r["status"] == "active"]
    total = len(active)
    missing = 0
    hash_ok = 0
    hash_bad = 0
    errors = []

    for idx, rec in enumerate(active):
        h = rec["hash"]
        cp = CONTENT_DIR / h[:2] / f"{h}.md"
        if not cp.exists():
            missing += 1
            errors.append(f"  MISSING {h[:12]}  {rec['path']}")
            if args.repair:
                src = Path(rec["path"])
                if src.exists():
                    shutil.copy2(src, cp)
                    errors.append(f"    -> repaired from {src}")
                else:
                    errors.append(f"    -> source gone, cannot repair")

        elif args.check_hash:
            actual = compute_hash(cp)
            if actual != h:
                hash_bad += 1
                errors.append(f"  HASH BAD {h[:12]}  {rec['path']}")
                if args.repair:
                    src = Path(rec["path"])
                    if src.exists():
                        shutil.copy2(src, cp)
                        errors.append(f"    -> repaired from {src}")
            else:
                hash_ok += 1

        if (idx + 1) % 10000 == 0:
            logger.info("  verified %d/%d", idx + 1, total)

    if not args.check_hash:
        hash_ok = total - missing

    print(f"\n=== Verify ===")
    print(f"Active entries:  {total}")
    print(f"Content OK:      {hash_ok}")
    print(
        f"Missing:         {missing}"
        + (" (repaired)" if args.repair and missing > 0 else "")
    )
    if args.check_hash:
        print(f"Hash mismatches: {hash_bad}")
    if args.verbose and errors:
        print(f"\nDetails:")
        for e in errors:
            print(e)


def cmd_summarize(args):
    """Show changes since last committed manifest."""
    records = load_manifest()
    active_set = {p for p, r in records.items() if r["status"] == "active"}
    active_recs = [r for r in records.values() if r["status"] == "active"]
    stale_recs = [r for r in records.values() if r["status"] == "stale"]

    added = [
        r
        for r in active_recs
        if r.get("status") == "active"
        and r["path"] not in {s["path"] for s in stale_recs}
    ]  # simplified below
    # Actually compute diff against last git commit's manifest
    try:
        result = subprocess.run(
            ["git", "show", "HEAD:manifest.jsonl"],
            capture_output=True,
            text=True,
            cwd=DOC_DIR,
            timeout=30,
        )
        if result.returncode != 0:
            print("No prior committed manifest to compare against.")
            return
        prev = {}
        for line in result.stdout.strip().splitlines():
            if line.strip():
                rec = json.loads(line)
                if rec["status"] == "active":
                    prev[rec["path"]] = rec
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError) as e:
        print(f"Cannot read prior manifest: {e}")
        return

    curr = {r["path"]: r for r in active_recs}
    prev_paths = set(prev)
    curr_paths = set(curr)

    added_paths = curr_paths - prev_paths
    removed_paths = prev_paths - curr_paths
    changed = []
    for p in curr_paths & prev_paths:
        if curr[p]["hash"] != prev[p]["hash"]:
            changed.append(p)

    print(f"\n=== Changes since last commit ===")
    print(f"New files:       {len(added_paths)}")
    print(f"Removed:         {len(removed_paths)}")
    print(f"Changed (hash):  {len(changed)}")
    print(f"Total active:    {len(curr_paths)}")

    if args.verbose and (added_paths or removed_paths or changed):
        if added_paths:
            print(f"\n--- Added ({len(added_paths)}) ---")
            for p in sorted(added_paths)[: args.limit]:
                r = curr[p]
                print(f"  + {r['hash'][:12]}  {r['kind']:12s}  {p}")
        if removed_paths:
            print(f"\n--- Removed ({len(removed_paths)}) ---")
            for p in sorted(removed_paths)[: args.limit]:
                print(f"  - {prev[p]['hash'][:12]}  {p}")
        if changed:
            print(f"\n--- Changed ({len(changed)}) ---")
            for p in sorted(changed)[: args.limit]:
                r = curr[p]
                print(f"  ~ {prev[p]['hash'][:12]} -> {r['hash'][:12]}  {p}")


def main():
    COMPILED_EXCLUSIONS.clear()
    COMPILED_EXCLUSIONS.extend(compile_exclusions(EXCLUSIONS))

    parser = argparse.ArgumentParser(description="md-knowledge: canonical .md archive")
    parser.add_argument(
        "--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )

    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="Build/update the archive")
    p_run.add_argument("--root", type=Path, help="Scan root (default: ~)")
    p_run.add_argument(
        "--no-views", action="store_true", help="Skip symlink view rebuild"
    )
    p_run.add_argument(
        "--scrub-secrets",
        action="store_true",
        help="Redact API keys, tokens, passwords before storing",
    )
    p_run.set_defaults(func=cmd_run)

    p_status = sub.add_parser("status", help="Print archive statistics")
    p_status.add_argument(
        "-v", "--verbose", action="store_true", help="Detailed breakdown"
    )
    p_status.set_defaults(func=cmd_status)

    p_verify = sub.add_parser("verify", help="Verify content integrity")
    p_verify.add_argument(
        "--check-hash", action="store_true", help="Recompute and verify SHA256"
    )
    p_verify.add_argument(
        "--repair",
        action="store_true",
        help="Re-copy missing/corrupt files from source",
    )
    p_verify.add_argument(
        "-v", "--verbose", action="store_true", help="Show each issue"
    )
    p_verify.set_defaults(func=cmd_verify)

    p_summarize = sub.add_parser("summarize", help="Show changes since last commit")
    p_summarize.add_argument(
        "-v", "--verbose", action="store_true", help="Show per-file details"
    )
    p_summarize.add_argument(
        "--limit", type=int, default=20, help="Max files per section (default 20)"
    )
    p_summarize.set_defaults(func=cmd_summarize)

    args = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    modified_args = args
    if hasattr(args, "no_views"):
        modified_args.rebuild_views = not args.no_views
    else:
        modified_args.rebuild_views = False

    args.func(modified_args)


if __name__ == "__main__":
    main()
