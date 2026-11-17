# Worktree inventory, 2026-10-15

Item: `gh-wt-inventory-reconcile` (read-only). Branch: `wave1/gh-wt-inventory-reconcile`.
Status: acceptance not fully met. The item stays claimed and is not completed. See "Acceptance status".

## Method

- Dry run: `python -m meridian.worktree_hygiene --repo-root <repo> --json`, no `--apply`. Exit 0. The output was kept outside the repository and is not committed. `<repo>` is the absolute path of this worktree, which is the same root as `.`.
- Registrations: `git worktree list --porcelain` gives 211 entries.
- Dirty: `git --no-optional-locks status --porcelain` is non-empty.
- Merged: `git merge-base --is-ancestor HEAD dev` exits 0. `dev` resolved to `83d230365baefca66b2124e0bb8df96bd5ff8e9b` at run time.
- Each registration goes into one bucket, with this precedence: primary checkout (first porcelain entry), then scratch copy (path under the user temp directory), then detached (no branch), then clean or dirty crossed with merged or unmerged.

## Bucket counts (211 registrations)

| Bucket | Count |
|---|---|
| primary checkout | 1 |
| scratch copy | 5 |
| clean-merged | 5 |
| dirty-merged | 45 |
| dirty-unmerged | 41 |
| clean-unmerged | 86 |
| detached | 28 |
| **Total** | **211** |

- The buckets sum to the registration count (211).
- Dirty total: 97 (includes the primary checkout, the 5 scratch copies and 5 dirty detached entries). Clean total: 114.
- For all 211 paths, the dirty flag from this recount agrees with the `dirty_count` in the dry-run output.

## Comparison with the brief and the planner

The brief (223 total, 95 dirty, 128 clean) does not match this run. The planner's 2026-10-14 scan reported 226 registrations.

| Bucket | Planner 2026-10-14 | This run | Change |
|---|---|---|---|
| primary checkout | 1 | 1 | 0 |
| scratch copy | 5 | 5 | 0 |
| clean-merged | 24 | 5 | -19 |
| dirty-merged | 46 | 45 | -1 |
| dirty-unmerged | 39 | 41 | +2 |
| clean-unmerged | 83 | 86 | +3 |
| detached | 28 | 28 | 0 |
| **Total** | **226** | **211** | **-15** |

- `dev` is at the same SHA as in the planner scan, so the merged-bucket change is not caused by `dev` moving.
- 14 of the 17 planner-named entries checked (substring match on the porcelain output) no longer appear in the registration list. Examples: `review-token-race`, `scope-wave2-mcp`, `ui-waffle-launcher`, `workspace-notes-opt-in`. The cause was not investigated. The planner's named lists should be treated as stale.

## Findings in the existing module

1. `meridian/worktree_hygiene.py` line 123 reads status with `git status --porcelain=v1`, not `git --no-optional-locks status --porcelain`. The dry run therefore does not meet the no-index-lock requirement. The recount above used `--no-optional-locks`.
2. The module does not compute a merged or unmerged status. Its dry-run output has only root, protected, locked, missing, dirty-candidate and removable lists. The bucket counts above come from a separate read-only script, which is not committed.
3. The module reports 90 dirty candidates against 97 dirty registrations in total. The 7 difference are dirty trees the module puts in the protected, locked or repository-root lists.

## Tests

- `pixi run pytest tests/test_worktree_hygiene.py`: 9 passed. No code was changed.
- The full suite (`pixi run test`) was not run, because this change only adds this report.

## Acceptance status

- [x] Dry run executed without `--apply`, output saved outside the repository.
- [x] Every registration counted into exactly one bucket. The buckets sum to 211, not the 226 in the criteria. The 226 was the 2026-10-14 figure.
- [ ] Status and merge checks without index locks. The recount meets this. The module's own dry-run does not (finding 1), so the criterion is only partly met.
- [ ] Brief figures replaced in every group item before any manual item is claimed. Not done. It needs writes to the sprint items, which were not part of this run. Reconciled figures to use: 211 total, 97 dirty, 114 clean.
