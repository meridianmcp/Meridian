# Temporary artifact registry

`meridian/temp_artifacts.py` is a local-first, zero-hosted-dependency
registry for **temporary scripts and their run artifacts** — the impromptu
"just run this quick fix" scripts that otherwise leave no durable record of
what they were, what they read/wrote, or whether the copy on disk is still
the one that produced a given result.

## The problem this closes

`extensions/meridian-outputs` already answers an adjacent but narrower
question for **output files**: `fingerprint.py` (script content hash +
staleness) and `annotate.py` (per-output reproducibility notes). Neither
tracks the broader shape a temporary *script* itself needs — an exact
invoked command, declared inputs with their own fingerprints, an owner, an
environment/tool-version snapshot, or an explicit lifecycle (a superseded
fix script should never be silently deleted, just marked so). This module
adds that layer **without duplicating either existing ledger** — see
`cross_reference_outputs_fingerprint` below for how it composes with
`fingerprint.py` instead.

## Registering and checking a script

```python
from meridian import temp_artifacts as TA

root = TA.default_registry_root()["root"]   # project-local, gitignored,
                                             # never under OneDrive
entry = TA.register_artifact(
    root,
    name="backfill-thing",
    script_path=r"C:\Users\me\scratch\backfill_thing.py",
    command="python backfill_thing.py --dry-run=false",
    inputs=["data/raw/input.csv"],           # fingerprinted automatically
    outputs=["data/processed/thing.csv"],
    owner="adam",
    expiry_condition="until the v3 formula lands",
)

TA.check_artifact(root, entry["artifact_id"])
# -> {"status": "present_current", ...}
```

`check_artifact`/`check_all` are **read-only diagnostics** — they never
execute or delete the script they're checking, only re-read its current
bytes to compare against the hash recorded at registration time. Every
call returns one of:

| Status              | Meaning                                                        |
|----------------------|----------------------------------------------------------------|
| `present_current`    | Script exists, content unchanged since registration.           |
| `missing`             | Script no longer exists on disk.                                |
| `changed`             | Script exists, but its content hash no longer matches.          |
| `never_verified`      | Script exists, but no hash was recorded at registration to compare against. |
| `unreadable`          | Script exists but could not be read/hashed.                     |
| `superseded`          | Entry's lifecycle was explicitly marked superseded.              |
| `retired`             | Entry's lifecycle was explicitly marked retired.                 |

Lifecycle wins over a raw file comparison: `supersede_artifact`/
`retire_artifact` flip an entry's `lifecycle` field and bump its version —
they never delete the entry, mirroring `meridian/db/worktree_manifest.py`'s
own "mark superseded, never delete" discipline.

## Storage root — explicit, never a silent default

`default_registry_root(project_root=None)` prefers
`<project_root>/.meridian-temp-artifacts` (created and gitignored on first
use), unless that path resolves under a OneDrive-synced location — checked
via `meridian.local_resilience.assert_disk_only_prestage_path`, the same
detector already established for prestage/draft artifacts elsewhere in this
codebase — in which case it falls back to a machine-local directory under
`tempfile.gettempdir()` and reports the fallback explicitly:

```python
info = TA.default_registry_root("/path/to/repo")
info["root"]            # the resolved path — always returned, never hidden
info["used_fallback"]   # True only if the OneDrive fallback triggered
info["reason"]          # why, when used_fallback is True
```

Every public function also accepts an explicit `registry_root` override.

## Local paths vs. Meridian-shared state

A registry entry's own paths may be machine-local absolute paths — the
manifest itself is genuinely local, gitignored, per-machine state. The
boundary is enforced only where something crosses **into** shared Meridian
state (a note body, a sprint-item pointer, a handoff):

```python
TA.to_shared_safe_pointer(entry["script_path"], project_root=repo_root)
# -> {"pointer": "scripts/fix.py", "portable": True, "reason": None}
# or, for a path outside project_root:
# -> {"pointer": "<redacted-local-path:fix.py>#sha256:...", "portable": False, ...}

TA.assert_shared_safe(pointer)  # raises TempArtifactError on a raw absolute path
```

Both reuse `meridian.capability_manifest`'s own absolute-path detector
(`_ABSOLUTE_PATH_RE`) so "what counts as a disallowed local path" never
drifts between the capability-manifest boundary and this one.

## Corrupt/partial manifests are preserved, never overwritten

`register_artifact`/`check_artifact`/etc. raise `CorruptManifestError` if
the on-disk registry file exists but fails validation — the bad file is
left completely untouched. `quarantine_corrupt_registry(registry_root)`
moves it aside via an atomic rename (never a delete) so it stays available
for recovery, clearing the way for a fresh registry. `scan_for_incomplete_
writes(registry_root)` reports (read-only) whether a `.tmp` sibling from an
interrupted write is still on disk — the committed registry file itself is
never at risk either way, since every write goes through the same
`tmp + os.replace` atomic idiom `fingerprint.py`/`annotate.py` already use.

## Composing with the existing output ledgers

`cross_reference_outputs_fingerprint(registry_root, artifact_id,
outputs_dir)` is a **soft, optional** composition point with
`extensions/meridian-outputs/meridian_outputs/fingerprint.py`'s own
script-tagging ledger: when that package happens to be installed, it
compares this registry's recorded `script_sha256` against fingerprint.py's
`check_staleness` results for the same script path, proving the two systems
compute byte-identical hashes and can be correlated. `meridian` core never
imports `meridian_outputs` at module scope — this is a lazy, guarded import
that degrades to `{"available": False, ...}` when the extension isn't
installed, matching the core/extension boundary `meridian/local_resilience.py`
already established.

## CLI

```bash
python -m meridian.temp_artifacts register --name fix --script fix.py
python -m meridian.temp_artifacts check <artifact_id>       # or omit the id to check all
python -m meridian.temp_artifacts list --lifecycle active
python -m meridian.temp_artifacts supersede <artifact_id> --reason "..."
python -m meridian.temp_artifacts retire <artifact_id> --reason "..."
python -m meridian.temp_artifacts inspect
```

Omitting `--root` uses `default_registry_root()` and prints the resolved
path (and any OneDrive fallback notice) to stderr.
