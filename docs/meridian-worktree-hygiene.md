# Local worktree hygiene

Meridian has two different cleanup surfaces:

1. The server-side `meridian.worktree_cleanup` path cleans up a worktree that
   Meridian created for a sprint item and recorded in its database.
2. `python -m meridian.worktree_hygiene` inventories the actual Git worktree
   registry. This catches detached Codex/Claude trees, abandoned verification
   trees, and registrations that outlived their Meridian session row.

The second surface is dry-run by default:

```powershell
pixi run python -m meridian.worktree_hygiene --repo-root . --json
```

The plan protects paths or branches containing `docs`, `outputs`, `ooxml`,
`paper`, `dissertation`, or `megasprint-clean`. Add project-specific retention
paths explicitly:

```powershell
pixi run python -m meridian.worktree_hygiene --repo-root . `
  --keep-path C:\path\to\important-worktree
```

Unregistered scratch directories are not inferred globally because an app
directory such as `.codex/worktrees` can contain another repository's state.
Opt into an explicit scratch root when auditing it:

```powershell
pixi run worktree-hygiene --repo-root . `
  --orphan-root C:\Users\me\.codex\worktrees --json
```

Non-empty orphan directories require `--allow-orphans` in an apply run and
are copied into the archive before removal. Empty orphan directories may be
removed by an apply run. The command never deletes a directory merely because
it is under a broad home-directory path.

Applying a plan requires an archive outside the repository. Clean worktrees
can be removed with `--apply`; dirty worktrees require the additional
`--allow-dirty` acknowledgment. The archive contains metadata, status,
binary-safe staged/unstaged patches, and untracked files. Branch references
are never deleted.

```powershell
pixi run python -m meridian.worktree_hygiene --repo-root . `
  --apply `
  --archive-dir E:\MeridianData\worktree-quarantine-YYYYMMDD

pixi run python -m meridian.worktree_hygiene --repo-root . `
  --apply `
  --archive-dir E:\MeridianData\worktree-quarantine-YYYYMMDD `
  --allow-dirty
```

The command refuses to apply without an archive directory, refuses locked
trees, skips dirty trees unless explicitly acknowledged, and never removes
the repository root or a protected worktree. This is intentionally a local
operator command: a hosted Meridian process must not mutate a user's local
filesystem.
