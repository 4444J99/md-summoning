"""Tests for content-addressed shard placement in copy_or_scrub."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import summon


@pytest.fixture
def tmp_content_dir(tmp_path: Path):
    """Set up a temporary CONTENT_DIR for summon module."""
    content_dir = tmp_path / "content"
    content_dir.mkdir(parents=True, exist_ok=True)
    orig_content_dir = summon.CONTENT_DIR
    summon.CONTENT_DIR = content_dir
    yield content_dir
    summon.CONTENT_DIR = orig_content_dir


def test_scrubbed_content_addressing_shard_location(tmp_content_dir: Path, tmp_path: Path):
    """Verify scrubbed files are stored under their content-hash shard, not original-hash shard."""
    raw_content = b'key = "sk-12345678901234567890"\n'
    src_file = tmp_path / "test_doc.md"
    src_file.write_bytes(raw_content)

    raw_hash = hashlib.sha256(raw_content).hexdigest()
    # Confirm that raw hash prefix and scrubbed hash prefix differ
    scrubbed_bytes = summon.apply_scrub(raw_content)
    expected_store_hash = hashlib.sha256(scrubbed_bytes).hexdigest()

    assert raw_hash[:2] != expected_store_hash[:2], "Fixture must change SHA prefix after redaction"

    dest, store_hash, orig_hash = summon.copy_or_scrub(src_file, raw_hash, scrub=True)

    # 1. Returned path matches stored hash shard
    assert store_hash == expected_store_hash
    assert dest.parent.name == store_hash[:2]
    assert dest.name == f"{store_hash}.md"

    # 2. Stored bytes hash to its name
    assert hashlib.sha256(dest.read_bytes()).hexdigest() == store_hash

    # 3. ensure_content_file resolves it using stored hash
    resolved = summon.ensure_content_file(store_hash)
    assert resolved == dest

    # 4. original_hash remains correct
    assert orig_hash == raw_hash

    # 5. Source is unchanged
    assert src_file.read_bytes() == raw_content

    # 6. Idempotence: repeated copy returns same dest without error
    dest2, store_hash2, orig_hash2 = summon.copy_or_scrub(src_file, raw_hash, scrub=True)
    assert dest2 == dest
    assert store_hash2 == store_hash
    assert orig_hash2 == orig_hash


def test_unscrubbed_content_addressing(tmp_content_dir: Path, tmp_path: Path):
    """Verify unscrubbed files are stored under their original content-hash shard."""
    raw_content = b"# Plain Markdown\nNo secrets here.\n"
    src_file = tmp_path / "plain_doc.md"
    src_file.write_bytes(raw_content)

    raw_hash = hashlib.sha256(raw_content).hexdigest()

    dest, store_hash, orig_hash = summon.copy_or_scrub(src_file, raw_hash, scrub=False)

    assert store_hash == raw_hash
    assert orig_hash is None
    assert dest.parent.name == raw_hash[:2]
    assert dest.name == f"{raw_hash}.md"
    assert dest.read_bytes() == raw_content

    # ensure_content_file resolves it
    resolved = summon.ensure_content_file(raw_hash)
    assert resolved == dest

    # Source is unchanged
    assert src_file.read_bytes() == raw_content
