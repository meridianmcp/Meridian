#!/usr/bin/env bash
# 31a4a9c8 -- PreToolUse dependency/package install verification guard.
# REFILED (original 23f21820 never shipped).
#
# Threat class: the May 2026 CISA/NSA/Five Eyes joint advisory on AI-coding-agent
# supply-chain attacks -- a malicious or typosquatted package gets installed by an
# autonomous agent via `pip install <name>` / `npm install <name>` / `uvx <name>`
# and its arbitrary setup/postinstall code runs, with no human ever seeing the
# package name before it lands on disk.
#
# This hook fires on Bash tool calls and BLOCKS (exit 2, fail-closed) any
# pip/pip3/py/npm/uvx/pixi/conda/poetry/pipx install invocation that names a
# package NOT already:
#   (a) declared in this repo's own manifests (pyproject.toml dependencies /
#       optional-dependencies, package.json dependencies / devDependencies,
#       pixi.toml [dependencies]/[pypi-dependencies] -- including per-feature
#       tables like [feature.dev.dependencies]) -- i.e. already vetted and
#       checked into the repo, or
#   (b) listed in .claude/hooks/verified_packages.txt -- a durable allowlist an
#       executor (or human) appends to AFTER doing a real registry lookup
#       (PyPI / npmjs.com) confirming the package is the legitimate,
#       actively-maintained project (not a typosquat), or getting explicit
#       human confirmation via request_hitl.
#
# 8fae0e17 -- pixi is THIS repo's actual package manager (see pixi.toml,
# AGENTS.md/CLAUDE.md) but was entirely unrecognized until this fix: `pixi add`
# names a new package exactly like `npm install <name>` does, and `pixi
# install` (no package arg) installs from pixi.lock/pixi.toml -- exactly like
# bare `npm ci` -- so it is always allowed regardless of what's declared.
# conda install / poetry add / pipx install / `py -m pip install` are the same
# shape of install-verb-plus-package-args as the managers above and are
# recognized too while in this code, per that item's own notes.
#
# Manifest-only installs (`pip install -r requirements.txt`, bare `npm install`
# / `npm ci`, `pixi install`, `conda install --file environment.yml`,
# local/editable installs `pip install -e .`) are always allowed -- they
# install already-vetted, already-in-repo dependencies, not a new unknown
# package. Bare `pixi run <task>` (e.g. `pixi run test`) is NOT an install
# command at all and is never matched by the `pixi add|install` pattern below.
#
# Best-effort command parsing (same philosophy as secret_guard.sh /
# worktree_guard.sh): this is not a full shell grammar and can be defeated by a
# determined adversarial prompt (base64-encoded commands, $(...) indirection,
# etc.) -- it is a speed bump for the common case, not a sandbox. Fails OPEN on
# any parse ambiguity outside the specific "unknown package name" match, which
# fails CLOSED by design -- that is the entire point of this guard.
#
# Tolerant JSON extraction (no jq dependency), mirrors secret_guard.sh /
# worktree_guard.sh. Fails OPEN on any parse error.
# NOT hooks.sh (the token-rotation installer).
set -uo pipefail

payload="$(cat 2>/dev/null || true)"
[ -z "$payload" ] && exit 0

tool="$(printf '%s' "$payload" | grep -oE '"tool_name"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed -E 's/.*:[[:space:]]*"([^"]*)"/\1/')"
[ -z "$tool" ] && exit 0
[ "$tool" != "Bash" ] && exit 0

cmd="$(printf '%s' "$payload" | grep -oE '"command"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed -E 's/^"command"[[:space:]]*:[[:space:]]*"//; s/"$//')"
[ -z "$cmd" ] && exit 0

SCRIPT_DIR="$(cd "$(dirname "$0")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." >/dev/null 2>&1 && pwd)"
ALLOWLIST="$SCRIPT_DIR/verified_packages.txt"
PYPROJECT="$REPO_ROOT/pyproject.toml"
PACKAGE_JSON="$REPO_ROOT/package.json"
PIXI_TOML="$REPO_ROOT/pixi.toml"

normalize() {
    # Trailing newline is intentional -- callers that accumulate many
    # normalized names into a newline-separated list (the known-package set)
    # rely on it to keep entries separated; $(normalize ...) command
    # substitution strips it back off for single-value callers (is_known).
    printf '%s\n' "$1" | tr '[:upper:]' '[:lower:]' | sed -E 's/[-_.]+/-/g'
}

# Package managers/build tools themselves are always safe to (re)install --
# they are not the attack vector this guard targets.
builtin_known="pip pip3 setuptools wheel pip-tools uv npm npx corepack pixi conda poetry pipx"

# pixi.toml is real TOML (unlike pyproject.toml's array-of-quoted-strings
# dependency lists), so its package names are bare, unquoted keys inside
# specific tables: [dependencies], [pypi-dependencies], and their per-feature
# counterparts [feature.<name>.dependencies] / [feature.<name>.pypi-dependencies]
# (see this repo's own pixi.toml). Track the current [section] and only emit
# keys while inside one of those tables -- otherwise top-level keys like
# `name = "meridian"` under [workspace] would be misread as package names.
extract_pixi_toml_deps() {
    [ -f "$PIXI_TOML" ] || return 0
    local in_dep=0
    local line
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            \[dependencies\]|\[pypi-dependencies\]|\[feature.*.dependencies\]|\[feature.*.pypi-dependencies\])
                in_dep=1
                continue
                ;;
            \[*)
                in_dep=0
                continue
                ;;
        esac
        [ "$in_dep" = "1" ] || continue
        if printf '%s\n' "$line" | grep -qE '^[A-Za-z0-9][A-Za-z0-9_.-]*[[:space:]]*='; then
            printf '%s\n' "$line" | sed -E 's/^([A-Za-z0-9][A-Za-z0-9_.-]*)[[:space:]]*=.*/\1/'
        fi
    done < "$PIXI_TOML"
}

# Build the known-good package set (normalized, one per line): repo manifests
# (already vetted, checked into the repo) + the durable allowlist.
known="$(
    {
        printf '%s\n' $builtin_known
        [ -f "$PYPROJECT" ] && grep -E '^[[:space:]]*"[A-Za-z0-9]' "$PYPROJECT" \
            | sed -E 's/^[[:space:]]*"([A-Za-z0-9][A-Za-z0-9_.-]*).*/\1/'
        [ -f "$PACKAGE_JSON" ] && grep -E '^[[:space:]]*"[^"]+"[[:space:]]*:[[:space:]]*"' "$PACKAGE_JSON" \
            | sed -E 's/^[[:space:]]*"([^"]+)".*/\1/'
        extract_pixi_toml_deps
        [ -f "$ALLOWLIST" ] && grep -vE '^[[:space:]]*(#|$)' "$ALLOWLIST"
    } 2>/dev/null | tr -d '\r' | while IFS= read -r n; do normalize "$n"; done
)"

is_known() {
    local n
    n="$(normalize "$1")"
    [ -z "$n" ] && return 0   # nothing to check -- treat as fine
    printf '%s\n' "$known" | grep -Fxq -- "$n"
}

is_flag() {
    case "$1" in
        -*) return 0 ;;
        *) return 1 ;;
    esac
}

is_local_path() {
    case "$1" in
        .|./*|../*|/*|~*|[A-Za-z]:*) return 0 ;;
        *) return 1 ;;
    esac
}

flagged=""
manager=""

pip_value_flags=" -r --requirement -c --constraint -i --index-url --extra-index-url -f --find-links -t --target --root --prefix -b --build --cache-dir --log --proxy --retries --timeout --trusted-host --python --config-settings "
npm_value_flags=" --registry --scope --tag --save-prefix --workspace -w --prefix --cache --userconfig "
pixi_value_flags=" --manifest-path -f --feature --platform --host --build --git --branch --tag --rev --subdir "
conda_value_flags=" -n --name -p --prefix -c --channel "
poetry_value_flags=" -G --group -E --extras --source --python --platform "
pipx_value_flags=" --index-url --python --spec --pip-args --suffix "

check_pip_segment() {
    local rest="$1"
    # A manifest install via -r/--requirement anywhere on the line is allowed
    # wholesale -- it installs already-vetted, already-in-repo pins.
    case " $rest " in
        *" -r "*|*" --requirement "*) return ;;
    esac
    local skip_next=0
    local tok
    for tok in $rest; do
        if [ "$skip_next" = "1" ]; then skip_next=0; continue; fi
        if is_flag "$tok"; then
            case "$pip_value_flags" in
                *" $tok "*) skip_next=1 ;;
            esac
            continue
        fi
        # Editable/local installs (`-e .`, `pip install .`) are local, not a
        # registry package -- allow.
        if is_local_path "$tok"; then continue; fi
        local pkgname
        pkgname="$(printf '%s' "$tok" | sed -E 's/[<>=!~; ].*//; s/\[.*//')"
        [ -z "$pkgname" ] && continue
        if ! is_known "$pkgname"; then
            flagged="$pkgname"
            return
        fi
    done
}

check_npm_segment() {
    local rest="$1"
    local skip_next=0
    local tok
    for tok in $rest; do
        if [ "$skip_next" = "1" ]; then skip_next=0; continue; fi
        if is_flag "$tok"; then
            case "$npm_value_flags" in
                *" $tok "*) skip_next=1 ;;
            esac
            continue
        fi
        if is_local_path "$tok"; then continue; fi
        local pkgname="$tok"
        case "$pkgname" in
            @*)
                # Scoped package: @scope/name[@version] -- strip a version
                # after the SECOND '@' (the scope itself starts with '@').
                local rest2 scope after name_part
                rest2="${pkgname#@}"
                scope="${rest2%%/*}"
                after="${rest2#*/}"
                name_part="${after%%@*}"
                pkgname="@${scope}/${name_part}"
                ;;
            *)
                pkgname="${pkgname%%@*}"
                ;;
        esac
        [ -z "$pkgname" ] && continue
        if ! is_known "$pkgname"; then
            flagged="$pkgname"
            return
        fi
    done
}

check_uvx_segment() {
    local rest="$1"
    local skip_next=0
    local from_next=0
    local pkg=""
    local tok
    for tok in $rest; do
        if [ "$from_next" = "1" ]; then
            pkg="$tok"
            from_next=0
            continue
        fi
        if [ "$skip_next" = "1" ]; then skip_next=0; continue; fi
        if is_flag "$tok"; then
            case "$tok" in
                --from) from_next=1 ;;
                --python|--index|--index-url|--with) skip_next=1 ;;
            esac
            continue
        fi
        if [ -z "$pkg" ]; then pkg="$tok"; fi
    done
    [ -z "$pkg" ] && return
    local pkgname
    pkgname="$(printf '%s' "$pkg" | sed -E 's/[<>=!~; ].*//; s/\[.*//; s/@.*//')"
    [ -z "$pkgname" ] && return
    if ! is_known "$pkgname"; then
        flagged="$pkgname"
    fi
}

check_pixi_segment() {
    # `pixi add` takes one or more positional package specs, e.g.
    # `pixi add numpy pandas==2.2 --feature dev`. There is no manifest-file
    # flag to bypass wholesale (unlike pip's -r) -- every named package is
    # checked, same as npm install/add.
    local rest="$1"
    local skip_next=0
    local tok
    for tok in $rest; do
        if [ "$skip_next" = "1" ]; then skip_next=0; continue; fi
        if is_flag "$tok"; then
            case "$pixi_value_flags" in
                *" $tok "*) skip_next=1 ;;
            esac
            continue
        fi
        if is_local_path "$tok"; then continue; fi
        local pkgname
        pkgname="$(printf '%s' "$tok" | sed -E 's/[<>=!~; ].*//; s/\[.*//')"
        [ -z "$pkgname" ] && continue
        if ! is_known "$pkgname"; then
            flagged="$pkgname"
            return
        fi
    done
}

check_conda_segment() {
    local rest="$1"
    # `conda install --file environment.yml` installs from an already-vetted
    # manifest file, not a new named package -- allow wholesale, same as
    # pip's -r/--requirement bypass.
    case " $rest " in
        *" --file "*) return ;;
    esac
    local skip_next=0
    local tok
    for tok in $rest; do
        if [ "$skip_next" = "1" ]; then skip_next=0; continue; fi
        if is_flag "$tok"; then
            case "$conda_value_flags" in
                *" $tok "*) skip_next=1 ;;
            esac
            continue
        fi
        if is_local_path "$tok"; then continue; fi
        local pkgname
        pkgname="$(printf '%s' "$tok" | sed -E 's/[<>=!~; ].*//; s/\[.*//')"
        [ -z "$pkgname" ] && continue
        if ! is_known "$pkgname"; then
            flagged="$pkgname"
            return
        fi
    done
}

check_poetry_segment() {
    # `poetry add` takes one or more positional specs, e.g.
    # `poetry add requests@^2.31 --group dev`.
    local rest="$1"
    local skip_next=0
    local tok
    for tok in $rest; do
        if [ "$skip_next" = "1" ]; then skip_next=0; continue; fi
        if is_flag "$tok"; then
            case "$poetry_value_flags" in
                *" $tok "*) skip_next=1 ;;
            esac
            continue
        fi
        if is_local_path "$tok"; then continue; fi
        local pkgname
        pkgname="$(printf '%s' "$tok" | sed -E 's/[<>=!~; ].*//; s/\[.*//; s/@.*//')"
        [ -z "$pkgname" ] && continue
        if ! is_known "$pkgname"; then
            flagged="$pkgname"
            return
        fi
    done
}

check_pipx_segment() {
    # `pipx install <spec>` names exactly one tool, same shape as uvx.
    local rest="$1"
    local skip_next=0
    local pkg=""
    local tok
    for tok in $rest; do
        if [ "$skip_next" = "1" ]; then skip_next=0; continue; fi
        if is_flag "$tok"; then
            case "$pipx_value_flags" in
                *" $tok "*) skip_next=1 ;;
            esac
            continue
        fi
        if [ -z "$pkg" ]; then pkg="$tok"; fi
    done
    [ -z "$pkg" ] && return
    local pkgname
    pkgname="$(printf '%s' "$pkg" | sed -E 's/[<>=!~; ].*//; s/\[.*//; s/@.*//')"
    [ -z "$pkgname" ] && return
    if ! is_known "$pkgname"; then
        flagged="$pkgname"
    fi
}

# Split on &&, ||, ; into segments so each sub-command is inspected independently.
segments="$(printf '%s' "$cmd" | sed -E 's/&&|\|\||;/\n/g')"

while IFS= read -r seg; do
    [ -n "$flagged" ] && break
    seg_trim="$(printf '%s' "$seg" | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//')"
    [ -z "$seg_trim" ] && continue

    if printf '%s' "$seg_trim" | grep -qE '^((python3?|py)[[:space:]]+-m[[:space:]]+)?pip3?[[:space:]]+install([[:space:]]|$)'; then
        manager="pip"
        rest="$(printf '%s' "$seg_trim" | sed -E 's/^((python3?|py)[[:space:]]+-m[[:space:]]+)?pip3?[[:space:]]+install[[:space:]]*//')"
        check_pip_segment "$rest"
    elif printf '%s' "$seg_trim" | grep -qE '^npm[[:space:]]+(install|i|add|ci)([[:space:]]|$)'; then
        manager="npm"
        # `npm ci` installs strictly from the lockfile -- never a new package.
        case "$seg_trim" in
            npm\ ci*) ;;
            *)
                rest="$(printf '%s' "$seg_trim" | sed -E 's/^npm[[:space:]]+(install|i|add|ci)[[:space:]]*//')"
                check_npm_segment "$rest"
                ;;
        esac
    elif printf '%s' "$seg_trim" | grep -qE '^uvx([[:space:]]|$)'; then
        manager="uvx"
        rest="$(printf '%s' "$seg_trim" | sed -E 's/^uvx[[:space:]]*//')"
        check_uvx_segment "$rest"
    elif printf '%s' "$seg_trim" | grep -qE '^pixi[[:space:]]+add([[:space:]]|$)'; then
        manager="pixi"
        rest="$(printf '%s' "$seg_trim" | sed -E 's/^pixi[[:space:]]+add[[:space:]]*//')"
        check_pixi_segment "$rest"
    elif printf '%s' "$seg_trim" | grep -qE '^pixi[[:space:]]+install([[:space:]]|$)'; then
        # `pixi install` (no package arg -- packages come from pixi.add above)
        # installs from pixi.lock/pixi.toml, same as bare `npm ci`. Note this
        # is checked AFTER `pixi add` above so it never masks that pattern,
        # and it deliberately does NOT match `pixi run ...` (e.g. `pixi run
        # test`), which is not an install command at all.
        manager="pixi"
    elif printf '%s' "$seg_trim" | grep -qE '^conda[[:space:]]+install([[:space:]]|$)'; then
        manager="conda"
        rest="$(printf '%s' "$seg_trim" | sed -E 's/^conda[[:space:]]+install[[:space:]]*//')"
        check_conda_segment "$rest"
    elif printf '%s' "$seg_trim" | grep -qE '^poetry[[:space:]]+add([[:space:]]|$)'; then
        manager="poetry"
        rest="$(printf '%s' "$seg_trim" | sed -E 's/^poetry[[:space:]]+add[[:space:]]*//')"
        check_poetry_segment "$rest"
    elif printf '%s' "$seg_trim" | grep -qE '^pipx[[:space:]]+install([[:space:]]|$)'; then
        manager="pipx"
        rest="$(printf '%s' "$seg_trim" | sed -E 's/^pipx[[:space:]]+install[[:space:]]*//')"
        check_pipx_segment "$rest"
    fi
done <<EOF
$segments
EOF

if [ -n "$flagged" ]; then
    echo "Meridian dependency-install guard (31a4a9c8): BLOCKED $manager install of unverified package '$flagged'. Per the May 2026 CISA/NSA/Five Eyes supply-chain advisory on malicious AI-agent package installs, an unknown package must be verified BEFORE install. Do ONE of: (1) look '$flagged' up on the official registry (PyPI: https://pypi.org/pypi/$flagged/json or https://www.npmjs.com/package/$flagged) to confirm it is the real, actively-maintained project -- not a typosquat -- then append the exact name to .claude/hooks/verified_packages.txt and retry; or (2) call request_hitl(project_id, question) for explicit human confirmation, then add it to the allowlist once approved. Packages already declared in pyproject.toml/package.json/pixi.toml are pre-approved and never blocked." >&2
    exit 2
fi

exit 0
