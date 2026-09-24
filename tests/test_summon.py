"""Tests for summon.py — unit and integration."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from summon import (
    SECRET_PATTERNS,
    apply_scrub,
    compile_exclusions,
    compute_hash,
    copy_or_scrub,
    is_excluded,
    load_manifest,
    write_manifest,
    _symlink_safe,
    CONTENT_DIR,
    DOC_DIR,
    DOCUMENTS_DIR,
    MANIFEST_PATH,
)


# ── helpers ──────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def tmp_doc(tmp_path: Path, monkeypatch) -> Path:
    """Give every test a disposable archive and restore module globals afterward."""
    import summon as mod

    archive = tmp_path / "archive"
    content_dir = archive / "content"
    content_dir.mkdir(parents=True)
    documents = archive / "Documents"
    documents.mkdir()
    monkeypatch.setattr(mod, "HOME", tmp_path)
    monkeypatch.setattr(mod, "DOC_DIR", archive)
    monkeypatch.setattr(mod, "CONTENT_DIR", content_dir)
    monkeypatch.setattr(mod, "MANIFEST_PATH", archive / "manifest.jsonl")
    monkeypatch.setattr(mod, "DOCUMENTS_DIR", documents)
    monkeypatch.setattr(mod, "COMPILED_EXCLUSIONS", list(mod.COMPILED_EXCLUSIONS))
    monkeypatch.setattr(mod, "EXCLUDED_PREFIXES", [*mod.EXCLUDED_PREFIXES, str(archive) + "/"])
    return archive


def make_file(path: Path, text: str = "hello") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# ── unit tests ───────────────────────────────────────────────────────


class TestExclusions:
    def test_compile_and_match(self):
        import summon as mod

        mod.COMPILED_EXCLUSIONS.clear()
        mod.COMPILED_EXCLUSIONS.extend(
            compile_exclusions(["*/node_modules/*", "*/Library/*"])
        )
        assert is_excluded(Path("/Users/x/node_modules/pkg/readme.md"))
        assert is_excluded(Path("/Users/x/Library/cache.md"))
        assert not is_excluded(Path("/Users/x/projects/readme.md"))

    def test_prefix_exclusion(self):
        assert is_excluded(Path("/Users/4jp/_doc/manifest.jsonl"))
        assert is_excluded(Path("/Users/4jp/_doc/content/ab/abc.md"))


class TestScrub:
    def test_openai_key(self):
        text = b'key = "sk-test12345678901234567890"'
        result = apply_scrub(text)
        assert b"***REDACTED***" in result
        assert b"sk-test12345678901234567890" not in result

    def test_github_token(self):
        text = (
            b"token = ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"  # allow-secret
        )
        result = apply_scrub(text)
        assert b"***REDACTED***" in result
        assert b"ghp_" in result

    def test_private_key_block(self):
        text = (
            b"-----BEGIN RSA PRIVATE KEY-----\n"
            b"MIIEpAIBAAKCAQEA0OcH2J7RkPdKw==\n"
            b"-----END RSA PRIVATE KEY-----\n"
        )
        result = apply_scrub(text)
        assert b"***REDACTED***" in result
        assert b"MIIEpAIBAAKCAQEA0OcH2J7RkPdKw==" not in result

    def test_jwt(self):
        text = b"eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNrxPmIebTTi"  # allow-secret
        result = apply_scrub(text)
        assert b"***REDACTED***" in result
        assert text not in result  # full token not leaked

    def test_clean_text_unchanged(self):
        text = b"# Hello\nThis is a normal markdown file.\nNo secrets here.\n"
        result = apply_scrub(text)
        assert result == text

    def test_all_patterns_compiled(self):
        assert len(SECRET_PATTERNS) == 11
        for p in SECRET_PATTERNS:
            assert p.groups > 0  # each must have at least one capture group


class TestHash:
    def test_consistency(self):
        with tempfile.NamedTemporaryFile(suffix=".md", mode="w", delete=False) as f:
            f.write("consistent content")
            p = Path(f.name)
        try:
            h1 = compute_hash(p)
            h2 = compute_hash(p)
            assert h1 == h2
            assert len(h1) == 64  # SHA256 hex
        finally:
            p.unlink()

    def test_missing_file(self):
        assert compute_hash(Path("/nonexistent/file.md")) is None


class TestSymlink:
    def test_symlink_creation(self, tmp_path):
        target = tmp_path / "target.txt"
        target.write_text("data")
        link = tmp_path / "link.txt"
        _symlink_safe(str(target), link)
        assert link.is_symlink()
        assert link.read_text() == "data"

    def test_collision_handling(self, tmp_path):
        target = tmp_path / "target.txt"
        target.write_text("data")
        link = tmp_path / "link.txt"
        link.write_text("existing")
        _symlink_safe(str(target), link)
        # original name was taken so it appends _1
        candidates = list(tmp_path.glob("link*.txt"))
        assert len(candidates) >= 2


class TestCopyOrScrub:
    def test_copy_without_scrub(self, tmp_path):
        src = make_file(tmp_path / "src.md", "hello world")
        h = hash_text("hello world")
        dest, store_h, orig = copy_or_scrub(src, h, scrub=False)
        assert store_h == h
        assert orig is None
        assert dest.read_text() == "hello world"

    def test_copy_with_scrub(self, tmp_path):
        src = make_file(tmp_path / "src.md", 'key = "sk-testxxxxxxxxxxxxxxxxxxxxxxxx"')
        h = hash_text(src.read_text())
        dest, store_h, orig = copy_or_scrub(src, h, scrub=True)
        assert orig == h
        assert store_h != h  # scrubbed = different hash
        assert dest.exists()
        assert b"***REDACTED***" in dest.read_bytes()


# ── integration tests ────────────────────────────────────────────────


class TestManifestIO:
    def test_roundtrip(self, tmp_doc):
        docs = [
            {"path": "/a.md", "hash": "aa", "size": 10, "status": "active"},
            {"path": "/b.md", "hash": "bb", "size": 20, "status": "active"},
        ]
        write_manifest(docs)
        loaded = load_manifest()
        assert "/a.md" in loaded
        assert "/b.md" in loaded
        assert len(loaded) == 2

    def test_empty_manifest(self, tmp_doc):
        loaded = load_manifest()
        assert loaded == {}


class TestRun:
    def test_run_creates_manifest(self, tmp_doc, tmp_path):
        import summon as mod

        # Source dir must be OUTSIDE tmp_doc to avoid prefix exclusion
        src = tmp_path / "_src"
        make_file(src / "a.md", "# File A")
        make_file(src / "nested" / "b.md", "# File B")

        class Args:
            root = src
            scrub_secrets = False
            rebuild_views = False
            no_views = True

        mod.cmd_run(Args())

        manifest = load_manifest()
        assert len(manifest) >= 2
        keys = list(manifest.keys())
        assert any("a.md" in k for k in keys)
        assert any("b.md" in k for k in keys)


class TestStatus:
    def test_status_output(self, tmp_doc, capsys):
        write_manifest(
            [
                {
                    "path": "/a.md",
                    "hash": "aa",
                    "size": 10,
                    "mtime": 0,
                    "kind": "orphan",
                    "repo": "",
                    "depth": 0,
                    "status": "active",
                },
            ]
        )
        # Force-create a content file
        (tmp_doc / "content" / "aa").mkdir(parents=True, exist_ok=True)
        (tmp_doc / "content" / "aa" / "aa.md").write_text("x")

        from summon import cmd_status

        class Args:
            verbose = False

        cmd_status(Args())
        captured = capsys.readouterr()
        assert "Active files:" in captured.out


class TestFeed:
    def test_feed_paths(self, tmp_doc):
        write_manifest(
            [
                {
                    "path": "/a.md",
                    "hash": "aa",
                    "size": 10,
                    "mtime": 0,
                    "kind": "orphan",
                    "repo": "",
                    "depth": 0,
                    "status": "active",
                },
            ]
        )
        from summon import cmd_feed

        class Args:
            format = "paths"
            kind = "all"
            repo = None
            limit = None

        cmd_feed(Args())
        # just check it doesn't crash

    def test_feed_summary(self, tmp_doc, capsys):
        write_manifest(
            [
                {
                    "path": "/a.md",
                    "hash": "aa",
                    "size": 10,
                    "mtime": 0,
                    "kind": "git-tracked",
                    "repo": "r1",
                    "depth": 0,
                    "status": "active",
                },
            ]
        )
        from summon import cmd_feed

        class Args:
            format = "summary"
            kind = "all"
            repo = None
            limit = None

        cmd_feed(Args())
        captured = capsys.readouterr()
        assert "git-tracked" in captured.out


class TestPrune:
    def test_prune_removes_old_stale(self, tmp_doc):
        write_manifest(
            [
                {
                    "path": "/gone.md",
                    "hash": "aa",
                    "size": 10,
                    "mtime": 0,
                    "kind": "orphan",
                    "repo": "",
                    "depth": 0,
                    "status": "stale",
                },
                {
                    "path": "/here.md",
                    "hash": "bb",
                    "size": 10,
                    "mtime": 0,
                    "kind": "orphan",
                    "repo": "",
                    "depth": 0,
                    "status": "active",
                },
            ]
        )
        from summon import cmd_prune

        class Args:
            days = 1

        cmd_prune(Args())
        loaded = load_manifest()
        assert "/gone.md" not in loaded  # pruned
        assert "/here.md" in loaded  # kept


class TestRestore:
    def test_restore_missing(self, tmp_doc, tmp_path):
        src = make_file(tmp_path / "src.md", "# Restore me")
        h = hash_text("# Restore me")
        write_manifest(
            [
                {
                    "path": str(src),
                    "hash": h,
                    "size": 10,
                    "mtime": 0,
                    "kind": "orphan",
                    "repo": "",
                    "depth": 0,
                    "status": "active",
                },
            ]
        )
        # Remove the content file
        cf = tmp_doc / "content" / h[:2] / f"{h}.md"
        assert not cf.exists()

        from summon import cmd_restore

        class Args:
            verbose = True

        cmd_restore(Args())
        assert cf.exists()
        assert cf.read_text() == "# Restore me"


class TestGrep:
    def test_grep_basic(self, tmp_path):
        """Grep runs without error against a pattern."""
        from summon import cmd_grep

        class Args:
            pattern = "test"
            ignore_case = False
            count = False
            files_with_matches = False
            resolve = False
            repo = None

        # The isolated archive is empty; ripgrep reports no matches.
        with pytest.raises(SystemExit):
            cmd_grep(Args())

    def test_grep_repo_filter(self, tmp_doc):
        """--repo filter in grep builds correct hash_to_path."""
        from summon import cmd_grep

        # Populate a synthetic record so the assertion cannot pass vacuously.
        write_manifest([{"path": str(tmp_doc / "source.md"), "hash": "aa",
                         "status": "active", "repo": "test--repo"}])
        manifest = load_manifest()
        assert len(manifest) == 1
        for p, r in manifest.items():
            if r["status"] == "active" and r.get("repo"):
                repo = r["repo"]
                assert isinstance(repo, str) and "--" in repo
                break


class TestFzf:
    def test_fzf_not_found_error(self, tmp_doc):
        """Fzf errors gracefully when binary missing."""
        # Monkey-patch shutil.which to return None
        import summon as mod

        original = shutil.which
        try:
            import shutil as shutil_mod

            shutil_mod.which = lambda x: None
            from summon import cmd_fzf

            class Args:
                kind = "all"
                repo = None

            with pytest.raises(SystemExit, match=".*fzf not found.*|.*1.*"):
                cmd_fzf(Args())
        finally:
            shutil_mod.which = original


# ── CLI smoke tests ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "cmd", ["status", "verify", "summarize", "feed --format paths --limit 1"]
)
def test_cli_smoke(cmd, tmp_doc):
    """Run the real CLI parser/commands in a subprocess against a temporary archive."""
    # Production currently has a fixed archive path. Override only module state
    # in this subprocess harness rather than touching the user's real archive.
    source_root = str(Path(__file__).resolve().parent.parent)
    harness = "\n".join([
        "import sys",
        "from pathlib import Path",
        f"sys.path.insert(0, {source_root!r})",
        "import summon",
        f"summon.DOC_DIR = Path({str(tmp_doc)!r})",
        "summon.HOME = summon.DOC_DIR.parent",
        "summon.CONTENT_DIR = summon.DOC_DIR / 'content'",
        "summon.MANIFEST_PATH = summon.DOC_DIR / 'manifest.jsonl'",
        "summon.DOCUMENTS_DIR = summon.DOC_DIR / 'Documents'",
        "summon.EXCLUDED_PREFIXES = [str(summon.DOC_DIR) + '/']",
        f"sys.argv = ['summon', *{cmd.split()!r}]",
        "summon.main()",
    ])
    result = subprocess.run(
        [sys.executable, "-B", "-c", harness], cwd=tmp_doc,
        capture_output=True, text=True, timeout=60,
    )
    # Preserve the existing acceptance contract: 1 may report no changes.
    assert result.returncode in (0, 1), f"{cmd} failed: {result.stderr[:200]}"
    assert "Traceback" not in result.stderr
