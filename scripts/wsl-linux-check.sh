#!/usr/bin/env bash
# wsl-linux-check.sh -- sync + verify Meridian's dedicated WSL Linux checkout
# (sprint item a70e6a33)
#
# Purpose
# -------
# This is NOT a duplicate of CI's own Linux runners -- test-core/test-postgres
# already run the full suite on ubuntu-latest in GitHub Actions on every push.
# What this script exists for is INTERACTIVE/manual verification of Linux
# desktop-integration work that a headless CI runner can't meaningfully
# exercise: tray icons (pystray backend availability), `.desktop` file
# validity, `systemd --user` units, and similar real-OS-integration checks.
# See docs/ or CONTRIBUTING.md's "Local Linux verification via WSL" section
# for the two-distro setup this assumes.
#
# Usage (from a Windows shell, at the repo root)
# -----------------------------------------------
#   wsl.exe -d Ubuntu-20.04 -- bash scripts/wsl-linux-check.sh
#   wsl.exe -d Ubuntu-20.04 -- bash scripts/wsl-linux-check.sh tests/test_install_linux_launcher.py
#   wsl.exe -d Ubuntu-20.04 -- bash scripts/wsl-linux-check.sh -k tray
#
# Any positional args are passed straight through to
# `scripts/run_tests.py` (the repo's pytest wrapper) as a targeted test
# selection -- omit them to just sync + `pixi install` with no test run.
#
# Can also be run directly from inside a WSL/Linux shell that already has
# this checkout, e.g. `bash scripts/wsl-linux-check.sh`.
#
# What this actually does, and why it needs to exist at all
# -----------------------------------------------------------
# The dedicated checkout this targets (default `~/meridian-test`, see
# pinned decision b6a8f317 -- fresh WSL Ubuntu-20.04, `pixi install` ->
# `python -m meridian` -> /health 200, zero Windows-only workarounds
# needed) is a `--depth 1` shallow clone that only tracks `main`. That
# clone shape causes two real, previously-undiscovered-until-you-hit-them
# pieces of friction, both confirmed live on 2026-09-24 and both fixed
# below rather than worked around ad hoc every time:
#
# 1. SHALLOW CLONE DOESN'T TRACK dev. A `--depth 1` clone's
#    `remote.origin.fetch` refspec only maps the branch it cloned (main):
#      +refs/heads/main:refs/remotes/origin/main
#    So `git fetch origin dev --depth 1` DOES pull dev's history down into
#    FETCH_HEAD, but does NOT create or update a `refs/remotes/origin/dev`
#    ref -- there is no refspec telling fetch to put it there. A plain
#    `git checkout -b x origin/dev` therefore fails outright ("unknown
#    revision or path not in the working tree"), and the only way to
#    actually land on dev's HEAD is `git checkout -B x FETCH_HEAD` -- a
#    workaround that has to be rediscovered/re-run from scratch every
#    single time, since it never fixes the underlying refspec.
#
#    The fix here (step 2 below) is `git remote set-branches --add origin
#    dev`, which appends a REAL, PERSISTENT refspec for dev to
#    remote.origin.fetch. Confirmed live: after that one-time call, the
#    very next `git fetch origin dev` creates a normal `origin/dev`
#    tracking ref, and every fetch/checkout after that (including a human
#    running plain `git fetch`/`git pull` by hand afterwards, with no
#    knowledge of this script) just works -- the FETCH_HEAD dance is never
#    needed again for this checkout.
#
#    Confirmed NOT idempotent on its own: running `git remote set-branches
#    --add origin dev` a second time appends a SECOND, identical
#    `+refs/heads/dev:refs/remotes/origin/dev` line to remote.origin.fetch
#    rather than being a no-op. Step 2 below guards against that
#    explicitly so re-running this script repeatedly (the normal use
#    case) never accumulates duplicate refspec lines.
#
# 2. pixi IS NOT ON PATH UNDER `bash -lc` (i.e. under exactly how
#    `wsl.exe -d <distro> -- bash script.sh` invokes this script).
#    pixi's installer appends `export PATH="$HOME/.pixi/bin:$PATH"` to
#    `~/.bashrc`. But Ubuntu's default `~/.bashrc` opens with:
#      case $- in *i*) ;; *) return;; esac
#    which returns immediately for a NON-interactive shell -- before ever
#    reaching that export. `bash -lc "cmd"` (a login shell, no `-i`) is
#    non-interactive, so on this exact distro `pixi --version` reported
#    "command not found" even though `~/.bashrc` genuinely has the PATH
#    export -- it's just never sourced for this invocation shape. Fixed
#    below by setting PATH explicitly rather than trusting shell rc files.
#
# Safety
# ------
# This script will NOT silently discard uncommitted local changes in the
# checkout -- it refuses (non-zero exit, clear message) unless you pass
# MERIDIAN_WSL_FORCE=1. The checkout is meant to be disposable/
# verification-only, but a script shouldn't assume that on your behalf.
#
# Env var overrides (all optional)
# ---------------------------------
#   MERIDIAN_WSL_CHECKOUT      Checkout dir (default: $HOME/meridian-test)
#   MERIDIAN_WSL_REPO_URL      Remote to clone if the checkout doesn't exist
#                              yet (default: the real Meridian repo)
#   MERIDIAN_WSL_BRANCH        Remote branch to sync to (default: dev)
#   MERIDIAN_WSL_LOCAL_BRANCH  Local branch name to land on (default: wsl-check)
#   MERIDIAN_WSL_FORCE=1       Discard uncommitted local changes instead of
#                              refusing to proceed
#   MERIDIAN_WSL_SKIP_INSTALL=1  Skip the `pixi install` step (used by this
#                              script's own test suite, tests/test_wsl_linux_check.py,
#                              against throwaway fixture repos that have no
#                              pixi.toml at all)
#   MERIDIAN_WSL_TEST_SUBSET   Same effect as passing test args positionally;
#                              only used when no positional args are given.

set -euo pipefail

# See gotcha #2 above: do not remove this assuming ~/.bashrc will do it --
# it will not, under this exact non-interactive invocation shape.
export PATH="$HOME/.pixi/bin:$PATH"

CHECKOUT_DIR="${MERIDIAN_WSL_CHECKOUT:-$HOME/meridian-test}"
REPO_URL="${MERIDIAN_WSL_REPO_URL:-https://github.com/meridianmcp/Meridian.git}"
TARGET_BRANCH="${MERIDIAN_WSL_BRANCH:-dev}"
LOCAL_BRANCH="${MERIDIAN_WSL_LOCAL_BRANCH:-wsl-check}"
FORCE="${MERIDIAN_WSL_FORCE:-0}"

if [ "$#" -gt 0 ]; then
  TEST_ARGS=("$@")
elif [ -n "${MERIDIAN_WSL_TEST_SUBSET:-}" ]; then
  # shellcheck disable=SC2206  # intentional word-splitting of a simple arg string
  TEST_ARGS=($MERIDIAN_WSL_TEST_SUBSET)
else
  TEST_ARGS=()
fi

echo "== Meridian WSL Linux verification loop =="
echo "distro       : $(lsb_release -ds 2>/dev/null || uname -a)"
echo "checkout dir : $CHECKOUT_DIR"
echo "target branch: origin/$TARGET_BRANCH"
echo ""

# -- 1. clone if this is a first run for this checkout dir -------------------
if [ ! -d "$CHECKOUT_DIR/.git" ]; then
  echo "-> no checkout found at $CHECKOUT_DIR, cloning (shallow, default branch only)..."
  git clone --depth 1 "$REPO_URL" "$CHECKOUT_DIR"
fi

cd "$CHECKOUT_DIR"

# -- 2. refuse to clobber uncommitted local work, unless explicitly forced ---
if [ -n "$(git status --porcelain)" ]; then
  if [ "$FORCE" != "1" ]; then
    echo "error: $CHECKOUT_DIR has uncommitted local changes." >&2
    echo "This script does not silently discard local work. Commit or stash" >&2
    echo "them first, or re-run with MERIDIAN_WSL_FORCE=1 to discard them and" >&2
    echo "force-sync to origin/$TARGET_BRANCH anyway." >&2
    exit 1
  fi
  echo "-> MERIDIAN_WSL_FORCE=1: discarding uncommitted local changes in $CHECKOUT_DIR"
  git reset --hard HEAD
  git clean -fd
fi

# -- 3. widen the shallow clone's tracked-branch set to include $TARGET_BRANCH
# See gotcha #1 above for why this step exists at all. Guarded so re-running
# this script never appends a duplicate refspec line.
if ! git config --get-all remote.origin.fetch 2>/dev/null | grep -qF "refs/heads/${TARGET_BRANCH}:refs/remotes/origin/${TARGET_BRANCH}"; then
  echo "-> widening shallow clone to also track '$TARGET_BRANCH'..."
  git remote set-branches --add origin "$TARGET_BRANCH"
fi

echo "-> fetching origin/$TARGET_BRANCH (depth 1)..."
git fetch origin "$TARGET_BRANCH" --depth 1

# -- 4. land on current $TARGET_BRANCH HEAD -----------------------------------
if git rev-parse --verify -q "origin/${TARGET_BRANCH}" >/dev/null; then
  git checkout -B "$LOCAL_BRANCH" "origin/${TARGET_BRANCH}"
else
  # Belt-and-suspenders: should not happen after step 3, but if some git
  # version/config quirk still leaves origin/<branch> missing, FETCH_HEAD
  # (set by the fetch above regardless) still gets us to the right commit --
  # this is the original ad hoc workaround, kept only as a last-resort fallback.
  echo "-> origin/${TARGET_BRANCH} ref still missing after widening; falling back to FETCH_HEAD" >&2
  git checkout -B "$LOCAL_BRANCH" FETCH_HEAD
fi

echo ""
echo "-> now at $(git rev-parse --short HEAD) ($(git log -1 --format=%s))"
echo ""

# -- 5. install ---------------------------------------------------------------
if [ "${MERIDIAN_WSL_SKIP_INSTALL:-0}" != "1" ]; then
  echo "-> pixi install..."
  pixi install
else
  echo "-> MERIDIAN_WSL_SKIP_INSTALL=1: skipping pixi install"
fi

# -- 6. optional targeted test subset -----------------------------------------
if [ "${#TEST_ARGS[@]}" -gt 0 ]; then
  echo ""
  echo "-> running targeted test subset: ${TEST_ARGS[*]}"
  pixi run python scripts/run_tests.py "${TEST_ARGS[@]}" -q --tb=short
else
  echo ""
  echo "-> no test subset requested -- pass args through, e.g.:"
  echo "   bash scripts/wsl-linux-check.sh tests/test_install_linux_launcher.py"
fi

echo ""
echo "== done =="
