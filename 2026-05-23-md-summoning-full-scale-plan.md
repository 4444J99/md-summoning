# Plan: Summoning All Markdown into One Doc Directory

**Date:** 2026-05-23
**Status:** Draft
**Context:** 162,513 `.md` files across `~/` — 127,471 (78%) inside git repos, 35,042 (22%) orphans outside any repo. 375 git repos discovered.

---

## 1. Ontology — The Four Kinds of Markdown

Not all `.md` files are equal. The routing rule depends on *what kind of thing it is*:

| Kind | Count | Treatment |
|------|-------|-----------|
| **Git-tracked (clean)** | ~124K | Immutable. File stays in repo. Central dir gets a **symlink** pointing *to* the original. |
| **Git-tracked (dirty)** | ~3K | Working tree differs from HEAD. Must stash or snapshot before symlinking; notify. |
| **Orphan (movable)** | ~35K | Physically relocatable. File moves into central dir; breadcrumb `.md` left at old path. |
| **Already-symlinked** | rare | Resolve to real path first, then classify by the resolved file's kind. |

### Sub-classifications within orphans

- **Home-root orphans** (`~/foo.md`): depth=0, likely transient notes, session exports
- **Config-directory orphans**: files in `~/.config/` outside any repo
- **Dropbox orphans**: files in `~/Dropbox/` outside any repo
- **Node_modules orphans**: `.md` inside npm packages — exclude from summons

---

## 2. Where — The Summoning Ground

```
~/Meta/Documents/
├── MANIFEST.md                  # human-readable master index
├── provenance/
│   ├── registry.jsonl           # one JSON line per file, append-only
│   ├── registry-schema.json     # schema for the JSONL
│   └── breadcrumb-template.md   # template for orphan breadcrumb files
├── by-repo/                     # symlinks only — git-tracked originals stay put
│   ├── <org>--<repo>/
│   │   └── <relative-path>.md → ../../../<actual-path>
│   └── ...
├── by-depth/                    # actual files — orphans moved here
│   ├── depth-0/
│   │   ├── README.md
│   │   └── ...
│   ├── depth-1/
│   │   ├── <dirname>--<basename>.md
│   │   └── ...
│   └── depth-N/
└── flat/                        # all files, hash-named for tool indexing
    ├── a1b2c3d4e5f6.md          # symlink → by-repo or by-depth
    ├── a1b2c3d4e5f7.md
    └── ...
```

### Why three views?

| View | Purpose |
|------|---------|
| `by-repo/` | Navigate by provenance — "what .md files live in domus?" |
| `by-depth/` | Navigate by location — "what's in my home root?" |
| `flat/` | **Primary index for search tools** — all files in one flat namespace, collision-free by design |

### Name collision strategy

- **by-repo/**: `org--repo/path/file.md` — the org+repo prefix guarantees uniqueness across repos. Every repo has a `README.md`; both coexist as `orgA--repoA/README.md` and `orgB--repoB/README.md`.
- **by-depth/**: Path structure is already unique on disk. On move, preserve the relative path from the depth root.
- **flat/**: `sha256[:12].md` — first 12 hex chars of content hash. Collision probability for 162K files: ~`n²/2²⁰¹` → effectively zero. If by cosmic ray they collide, content is identical (same hash), so one `flat/` entry serves both, with two provenance entries.

---

## 3. Breadcrumb Design

### For orphans (file physically moved)

At the original location, write a visible `.md` file:

```markdown
# MD relocated to ~/Meta/Documents

- **Old:** `/Users/4jp/some/path/notes.md`
- **New:** `~/Meta/Documents/by-depth/depth-3/some/path/notes.md`
- **Moved:** 2026-05-23
- **Hash:** `a1b2c3d4e5f6`
- **SHA256:** `a1b2c3d4e5f67890abcdef1234567890abcdef1234567890abcdef1234567890`

Remove this breadcrumb after verifying the new location.
Reclaim: `mv ~/Meta/Documents/by-depth/depth-3/some/path/notes.md ~/some/path/notes.md && rm "$0"`
```

The last line is a runnable reversal command. `$0` is the breadcrumb file itself — you can source it or copy-paste.

### For git-tracked (file stays put)

No breadcrumb needed — the original file *is* the breadcrumb. A symlink in `by-repo/` points to it. The registry is the authoritative cross-reference.

---

## 4. Pipeline Architecture

### Phase 0 — Registry Build (read-only, idempotent)

```
Input: fd enumeration of all .md files
Step 0.1: Enumerate with fd (already fast, ~18s)
Step 0.2: For each file:
  a. Stat (size, mtime, mode)
  b. Read first 4KB → fast-path dedup pre-check
  c. Full SHA256 (sequential IO, ~1-2 min for 162K files)
  d. Check if inside .git → tag as tracked/orphan
  e. If tracked: identify repo root, compute depth from repo root
  f. If orphan: compute depth from home root
  g. Check if file is a symlink → resolve and retag
Step 0.3: Write registry.jsonl (append per file)
Step 0.4: Write dedup report (hash collisions)
```

**IO budget:**
- 162K files × 1 read + 1 write (for hash) ≈ 324K IOPS
- At ~3000 IOPS on modern SSD → ~108s for sequential access
- With 4KB reads + SHA256 overhead → ~2 minutes total

**Memory budget:**
- Registry in memory: 162K × ~400 bytes ≈ 65MB
- Plus Python overhead → ~150MB RSS
- Acceptable on 16GB machine with Dropbox/Backblaze (~5GB used)

### Phase 1 — Dry Run (review before executing)

```
Output: full simulation report
- 35,042 orphans to move
- 127,471 git-tracked to symlink
- 0 dedup candidates (or N found)
- Disk impact:
  - New files in central dir: ~35K files (orphans moved)
  - New symlinks: ~127K (git-tracked) + ~35K (orphans in flat/) + ~127K (tracked in flat/)
  - Total central dir entries: ~325K (all symlinks except 35K real files)
  - Estimate: ~200MB actual data + negligible symlink space
- Breadcrumbs to create: 35,042
- Show top-10 most common depth values, largest repos, biggest files
```

**User must confirm before Phase 2.**

### Phase 2 — Execute Orphans (move + breadcrumb)

```
For each orphan file:
  1. Compute destination path in by-depth/<N>/<relative-path>
  2. Ensure destination directory exists (mkdir -p)
  3. mv file → destination
  4. Write breadcrumb .md at original path
  5. Append to registry.jsonl
  6. Commit to disk (fsync each batch of 1000)

Batch size: 1000 files per transaction
Checkpoint: every 10K files (save state for resume)
```

**Transaction safety:** If interrupted, the registry append is atomic per line (append-only). The checkpoint tells us where to resume.

### Phase 3 — Execute Tracked (symlink-in)

```
For each git-tracked file:
  1. Compute destination path in by-repo/<org>--<repo>/<relative-path>
  2. Ensure destination directory exists
  3. Create relative symlink → original file
  4. Append to registry.jsonl
```

**No destructive operations.** If Phase 3 fails halfway, just re-run — symlinks are cheap.

### Phase 4 — Flat Index

```
For every file in registry (both tracked and orphan):
  1. Compute flat name: sha256[:12] + .md
  2. Create symlink in flat/ → by-repo/<path> or by-depth/<path>
  3. Handle dedup: if flat name already exists, compare SHA256
     - If same content: skip (already indexed)
     - If collision (astronomically unlikely): use sha256[:16] + .md
```

### Phase 5 — Verification

```
1. Count all entries in registry → must match 162,513
2. Walk flat/ → every symlink resolves to an existing file
3. Walk by-repo/ → every symlink resolves to an existing file
4. Walk by-depth/ → every real file has a breadcrumb at its old location
5. For a random 1% sample:
   - Verify SHA256 matches registry
   - Verify breadcrumb points to correct new location
6. Report: "N files verified, M failed, K missing"
```

### Phase 6 — Registry Commit

```
1. git init ~/Meta/Documents/
2. git add provenance/ MANIFEST.md
3. git commit -m "md-summoning: initial registry 2026-05-23"
4. (Optional) git push to a backup remote
```

The central dir is now a git repo of *metadata* (registry, manifests) but actual content stays distributed. Symlinks are ephemeral views, not canonical storage.

---

## 5. Exclusion Rules

Some `.md` files should not be summoned:

| Pattern | Reason |
|---------|--------|
| `*/.git/objects/**` | Git object store (packed) |
| `*/node_modules/**` | NPM dependency docs (not user-authored) |
| `*/Library/**` | macOS system caches |
| `.Trash/**` | Deleted files |
| `*/.cache/**` | Cache files |
| `*/Caches/**` | macOS caches |
| `*/.specstory/history/**` | SpecStory session history (stay with project) |
| `*/.claude/projects/*/memory/**` | Claude project memories (stay with project) |
| `.md files < 100 bytes` | Likely stubs or garbage |
| `.md files that are binary` | Encrypted or corrupted files |
| `.md files that are symlinks already` | Resolve first, then decide |

These are configurable in a `summon.yaml` config file that ships with the script.

---

## 6. Recovery Protocol

### Undo an orphan move

Given a breadcrumb file at the old location:

```bash
# Option A: Use the reclaim command baked into the breadcrumb
# Option B: Bulk reverse from registry
python summon.py reverse --registry provenance/registry.jsonl
```

### Restore all orphan breadcrumbs → orphan files

```bash
python summon.py reverse --kind orphan --registry provenance/registry.jsonl
```

This reads registry.jsonl, finds all orphan entries, and runs `mv <new-path> <old-path>` then removes the breadcrumb.

### Remove all symlinks

Idempotent — just delete `~/Meta/Documents/by-repo/` and `~/Meta/Documents/flat/`. Originals are untouched.

### Full reversal

```bash
python summon.py reverse --all
```
1. Reverse all orphan moves (restore files, remove breadcrumbs)
2. Delete central directory
3. Leave no trace except the registry (which documents what happened)

---

## 7. Edge Cases

| Edge | Handling |
|------|----------|
| **Case-insensitive collision** (macOS APFS) | Append `--1`, `--2` to colliding name |
| **Path too long** (>255 chars) | Truncate filename, append hash for uniqueness |
| **Permission denied** | Log to `provenance/errors.log`, skip, continue |
| **File in use** (Dropbox/Backblaze) | Skip with warning; re-run after release |
| **Git worktrees** | `.git` is a file, not directory. Detect via `git rev-parse --git-dir` |
| **Git submodules** | Nested `.git` is a file pointing to parent's `modules/`. Tag with parent repo |
| **Same content, different paths** (README.md everywhere) | One `flat/` entry, multiple `by-repo/` entries |
| **Symlink chain** | Resolve to real path before hashing. Registry stores both |
| **File changed between Phase 0 and 2** | Re-hash before move. If changed, warn and skip |
| **Cross-filesystem move** | Use `cp + rm` instead of `mv` |
| **Breadcrumb already exists** | Check if our template. If yes skip. If not, append `--summoned` |
| **Empty directories** after moving orphans | Leave a `.md-empty-dir` breadcrumb noting what was moved |
| **Very large files** (>10MB .md) | Copy instead of move; warn |
| **Non-UTF8 .md files** | Detect encoding; copy as-is, flag in registry |

---

## 8. Maintenance

### Periodic sweep (cron or on-demand)

```bash
python summon.py sweep
```

1. Enumerate all `.md` files again
2. Cross-reference against registry
3. New files → summon them
4. Deleted originals (git-tracked no longer exist) → mark stale
5. Missing breadcrumbs → regenerate
6. Report: "N new, M stale, K missing breadcrumbs"

### Stale symlink cleanup

```bash
python summon.py prune
```

Walks `by-repo/` and `flat/`, removes dangling symlinks.

### Rebuild flat index

```bash
python summon.py reindex
```

Recreates entire `flat/` directory from registry, picking up new hashes.

---

## 9. Script Architecture

```
summon.py
├── cli/                      # Typer CLI
│   ├── build-registry       # Phase 0
│   ├── dry-run              # Phase 1
│   ├── summon               # Phase 2-4
│   ├── verify               # Phase 5
│   ├── reverse              # Recovery
│   ├── sweep                # Maintenance
│   └── prune                # Stale symlink cleanup
├── core/
│   ├── discover.py          # fd wrapper, file enumeration
│   ├── classify.py          # git-detection, depth computation
│   ├── hash.py              # SHA256 with fast-path 4KB pre-check
│   ├── route.py             # routing rules engine
│   ├── summon.py            # move + symlink + breadcrumb
│   ├── registry.py          # JSONL read/write/query
│   ├── breadcrumb.py        # breadcrumb create/verify/remove
│   └── dedup.py             # collision detection
├── config/
│   └── summon.yaml          # exclusions, paths, thresholds
└── tests/
    └── test_summon.py
```

### Dependencies

- `fd-find` (already installed via Homebrew)
- Python 3.12+ stdlib only (no external PyPI deps) — keeps it zero-install
- Optional: `rich` for progress bars (graceful fallback)

### Configuration (`summon.yaml`)

```yaml
central_dir: ~/Meta/Documents
exclusions:
  - "*/.git/objects/**"
  - "*/node_modules/**"
  - "*/Library/**"
  - ".Trash/**"
  - "*/.cache/**"
  - "*/.specstory/history/**"
  - "*/.claude/projects/*/memory/**"
min_file_bytes: 100
batch_size: 1000
hash_algorithm: sha256
flat_hash_length: 12
breadcrumb_template: provenance/breadcrumb-template.md
log_level: INFO
```

---

## 10. Resource Budget

| Resource | Budget | Actual (estimated) |
|----------|--------|-------------------|
| **Time (Phase 0)** | 5 min | ~2-3 min (fd 18s + hashing 90s + IO) |
| **Time (Phase 2)** | 10 min | ~5 min (35K moves, mostly same-filesystem rename) |
| **Time (Phase 3)** | 5 min | ~2 min (127K symlinks) |
| **Time (Phase 4)** | 5 min | ~3 min (162K symlinks for flat/) |
| **Time (Phase 5)** | 10 min | ~3 min (walk + 1% sample rehash) |
| **Total** | 35 min | ~15-20 min wall clock |
| **RAM (peak)** | 200MB | ~150MB (registry in memory) |
| **Disk (central dir)** | 500MB | ~200MB data + ~8MB symlink inodes |
| **Disk (breadcrumbs)** | 50MB | 35K × ~1.5KB |

---

## 11. Order of Operations

```
1. Write summon.yaml
2. Write provenance/breadcrumb-template.md
3. Write provenance/registry-schema.json
4. Phase 0 — build-registry
5. Review registry stats (how many orphans? by depth? by repo?)
6. Phase 1 — dry-run (show simulated operations)
7. USER CONFIRMS
8. Phase 2 — summon orphans
9. Phase 3 — symlink tracked
10. Phase 4 — build flat index
11. Phase 5 — verify
12. Phase 6 — commit registry
13. Write MANIFEST.md
```

---

## 12. Open Questions

1. **`~/Meta/Documents/`** — is this the right central location? Alternatives: `~/Docs/`, `~/MD-Summon/`.
2. **`by-depth/` vs `by-mtime/`** — depth is useful for drift risk; would mtime be more useful for daily browsing?
3. **Should git-tracked dirty files get special treatment?** Stash first? Snapshot to `by-repo/dirty/`?
4. **Should `.mdx` files be included?** The density map counted them. I'd say yes.
5. **Should the summon script itself be in chezmoi?** It's a tool, not a dotfile. `~/.local/bin/summon` would be clean.
6. **Who owns `~/Meta/Documents/` for git purposes?** Should it be a standalone git repo?
