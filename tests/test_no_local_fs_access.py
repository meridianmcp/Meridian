"""Static enforcement of workspace decision 0dedff91 (2026-07-12).

ARCHITECTURAL LAW (see meridian/_deps.py::require_local_fs_access):

    Any server-side MCP tool whose dispatch touches a path on the CALLER's own
    local filesystem -- os.walk / os.path.isdir/isfile/exists/abspath / open() /
    os.stat / zipfile.ZipFile on a value derived from the tool's ``args`` -- must
    fail loudly under hosted Meridian (MERIDIAN_HOSTED) instead of silently
    mis-resolving that path against the SERVER's own filesystem, where it can
    never exist. The two are physically different machines; no path-normalization
    or error-message fix can bridge them, so the tool MUST carry a hosted-mode
    guard (an ``if _hosted_mode(): return {...error...}`` early-out, or a
    ``require_local_fs_access(...)`` call, or the inline ``MERIDIAN_HOSTED`` env
    check code_index.py uses).

This was violated three times before the guards existed -- ingest_document
(832d67af), get_document_structure (b43bab91), and search_code_semantic
(90c593d, the reactive fix). search_code_semantic's reactive guard is the model;
THIS file is the PREVENTIVE enforcement layer: it AST-scans the MCP dispatcher
so no new (or refactored) server-side tool can EVER again open/stat/walk a
caller-supplied local path when hosted without a guard, and fails CI loudly if
one appears unguarded.

Design notes (why AST, not regex):
  * A tool's guard may live EITHER in its ``if name == "tool":`` dispatch branch
    in meridian/mcp/handler.py (the ingest_document / get_document_structure /
    get_latex_structure / search_outputs / find_similar_figure pattern) OR
    INSIDE the delegate function the branch hands off to (the
    search_code_semantic pattern -- the guard is in code_index.search_code_semantic
    itself). The scan resolves both.
  * The trigger is a REAL local-filesystem sink call (os.walk/os.path.isdir/...,
    open, zipfile.ZipFile), NOT merely a path-shaped arg name -- read_file /
    patch_file / list_files take a ``path``/``file_path`` arg but resolve it via
    the GitHub HTTP API and never touch the local FS, so they must NOT be flagged
    (avoiding that false positive is the whole point of scanning for the sink,
    not the arg name).
"""
from __future__ import annotations

import ast
import os

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MERIDIAN = os.path.join(_REPO_ROOT, "meridian")
_HANDLER = os.path.join(_MERIDIAN, "mcp", "handler.py")

# Local-filesystem "sink" operations. A dispatch branch (or a delegate it calls)
# that invokes ANY of these on a value derived from the caller's ``args`` is
# reaching the local filesystem and therefore needs a hosted-mode guard.
_FS_OS_PATH_FUNCS = frozenset(
    {"isdir", "isfile", "exists", "abspath", "realpath", "lexists", "getsize"}
)
_FS_OS_FUNCS = frozenset({"walk", "stat", "listdir", "scandir", "lstat"})
# Bare/attribute callables whose *name* alone signals a local-FS read.
_FS_NAMED_CALLS = frozenset({"open"})

# Recognized hosted-mode guard signatures. Presence of ANY of these in the code
# path (branch body or delegate function) satisfies the enforcement rule.
_GUARD_TOKENS = ("_hosted_mode", "require_local_fs_access", "MERIDIAN_HOSTED")

# Delegate modules a handler branch may hand a caller path off to. Maps the
# import-alias used inside handler.py to the module file, so the scan can open
# the delegate and look for the sink + guard there (the search_code_semantic
# case: the branch has no inline guard, the delegate function does).
_DELEGATE_MODULES = {
    "_code_index": os.path.join(_MERIDIAN, "code_index.py"),
    "code_index": os.path.join(_MERIDIAN, "code_index.py"),
    "_outputs_indexer": os.path.join(_MERIDIAN, "outputs_indexer.py"),
    "outputs_indexer": os.path.join(_MERIDIAN, "outputs_indexer.py"),
    "docs_intel": os.path.join(_MERIDIAN, "docs_intel.py"),
    "latex_intel": os.path.join(_MERIDIAN, "latex_intel.py"),
    "doc_ingest": os.path.join(_MERIDIAN, "doc_ingest.py"),
}


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _has_fs_sink(node: ast.AST) -> bool:
    """True if the subtree performs a local-filesystem sink call."""
    for n in ast.walk(node):
        if not isinstance(n, ast.Call):
            continue
        fn = n.func
        # open(...)
        if isinstance(fn, ast.Name) and fn.id in _FS_NAMED_CALLS:
            return True
        if isinstance(fn, ast.Attribute):
            attr = fn.attr
            # os.path.isdir(...) / os.walk(...) / os.stat(...)
            if attr in _FS_OS_PATH_FUNCS or attr in _FS_OS_FUNCS:
                return True
            # zipfile.ZipFile(...) / Path(...).open() / .read_text()/.read_bytes()
            if attr in ("ZipFile", "read_text", "read_bytes"):
                return True
    return False


def _has_guard(node: ast.AST) -> bool:
    """True if the subtree contains any recognized hosted-mode guard token."""
    src = ast.dump(node)
    return any(tok in src for tok in _GUARD_TOKENS)


def _tool_name_from_test(test: ast.AST) -> str | None:
    """Return the tool name for an ``if name == "tool":`` guard, else None."""
    if not isinstance(test, ast.Compare):
        return None
    if len(test.ops) != 1 or not isinstance(test.ops[0], ast.Eq):
        return None
    left, right = test.left, test.comparators[0]

    def _is_name_var(x: ast.AST) -> bool:
        return isinstance(x, ast.Name) and x.id == "name"

    def _const_str(x: ast.AST) -> str | None:
        return x.value if isinstance(x, ast.Constant) and isinstance(x.value, str) else None

    if _is_name_var(left):
        return _const_str(right)
    if _is_name_var(right):
        return _const_str(left)
    return None


def _iter_dispatch_branches(tree: ast.AST):
    """Yield (tool_name, if_node) for every ``if name == "tool":`` branch."""
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            tool = _tool_name_from_test(node.test)
            if tool is not None:
                yield tool, node


def _delegate_targets(branch: ast.If) -> set[str]:
    """Names of module.function delegates called in the branch, e.g.
    ``_code_index.search_code_semantic`` handed a caller path off to a module
    listed in ``_DELEGATE_MODULES`` (function passed by reference OR called)."""
    targets: set[str] = set()
    for n in ast.walk(branch):
        # run_in_bulkhead(_code_index.search_code_semantic, ...) — passed by ref
        # AND direct calls _outputs_indexer.search_outputs(...).
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name):
            mod = n.value.id
            if mod in _DELEGATE_MODULES:
                targets.add(f"{mod}.{n.attr}")
    return targets


def _delegate_function_guarded(delegate: str) -> bool | None:
    """For ``mod.func``, open the delegate module and check whether ``func``
    itself contains BOTH a filesystem sink and a hosted guard. Returns True if
    guarded, False if it has a sink but no guard, None if the function couldn't
    be resolved / has no FS sink (i.e. this delegate is not the sink)."""
    mod_alias, _, func_name = delegate.partition(".")
    path = _DELEGATE_MODULES.get(mod_alias)
    if not path or not os.path.exists(path):
        return None
    tree = ast.parse(_read(path))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            if not _has_fs_sink(node):
                return None
            return _has_guard(node)
    return None


def _collect_violations() -> list[str]:
    """Return a list of human-readable violation strings (empty == compliant)."""
    src = _read(_HANDLER)
    tree = ast.parse(src)
    violations: list[str] = []
    # Some tool names recur across dispatch groups (nested github block reuses
    # the `if name == "..."` form). Dedupe by (tool, lineno) — each physical
    # branch is checked once.
    seen: set[tuple[str, int]] = set()
    for tool, branch in _iter_dispatch_branches(tree):
        key = (tool, branch.lineno)
        if key in seen:
            continue
        seen.add(key)

        # Only branches that actually reference the caller's args are candidates
        # (every real dispatch branch does; this just skips structural noise).
        branch_src = ast.dump(branch)
        if "args" not in branch_src:
            continue

        branch_has_sink = _has_fs_sink(branch)

        # Resolve delegates: does the branch hand a caller path off to a known
        # heavy-FS module function, and is THAT function's guard the enforcer?
        delegate_sink = False
        delegate_guarded = False
        for delegate in _delegate_targets(branch):
            g = _delegate_function_guarded(delegate)
            if g is None:
                continue
            delegate_sink = True
            if g:
                delegate_guarded = True

        if not branch_has_sink and not delegate_sink:
            # This tool never touches the local filesystem (e.g. read_file /
            # patch_file go through the GitHub API). Nothing to enforce.
            continue

        # A local-FS tool. It is compliant iff a guard is reachable either in its
        # own dispatch branch or inside the delegate that owns the sink.
        if _has_guard(branch) or delegate_guarded:
            continue

        where = f"handler.py:{branch.lineno}"
        detail = "inline sink" if branch_has_sink else "delegate sink"
        violations.append(
            f"MCP tool {tool!r} ({where}, {detail}) reaches a caller's local "
            f"filesystem server-side but has NO hosted-mode guard "
            f"(decision 0dedff91). Add an `if _hosted_mode(): return {{...}}` "
            f"early-out (or require_local_fs_access) so it fails honestly on "
            f"hosted Meridian instead of mis-resolving the path against the "
            f"server's own filesystem."
        )
    return violations


def test_handler_module_parses():
    """Sanity: the dispatcher AST-parses (the scan below relies on it)."""
    ast.parse(_read(_HANDLER))
    assert os.path.exists(_HANDLER)


def test_no_unguarded_local_fs_mcp_tool():
    """Every server-side MCP tool that touches a caller's local filesystem has a
    hosted-mode guard (workspace decision 0dedff91). This is the preventive
    enforcement layer for search_code_semantic's reactive guard (90c593d)."""
    violations = _collect_violations()
    assert not violations, (
        "Unguarded local-filesystem MCP tool(s) detected — decision 0dedff91:\n  - "
        + "\n  - ".join(violations)
    )


def test_scanner_detects_a_planted_violation():
    """Meta-test: prove the scanner actually FAILS on an unguarded FS tool, so a
    future refactor can't neuter it into a silent pass. We synthesize a dispatch
    branch that opens a caller path with no guard and assert it is flagged."""
    snippet = (
        "def _h(name, args):\n"
        "    if name == 'planted_bad_tool':\n"
        "        p = args.get('root_dir')\n"
        "        return open(p).read()\n"
    )
    tree = ast.parse(snippet)
    flagged = False
    for tool, branch in _iter_dispatch_branches(tree):
        if tool == "planted_bad_tool":
            assert _has_fs_sink(branch), "sink detector missed open() on a caller path"
            assert not _has_guard(branch), "planted tool should have no guard"
            flagged = True
    assert flagged, "scanner failed to identify the planted dispatch branch"


def test_scanner_ignores_a_guarded_tool():
    """Meta-test: a branch that opens a caller path BUT carries a hosted guard is
    NOT a violation (guard recognition works)."""
    snippet = (
        "def _h(name, args):\n"
        "    if name == 'planted_good_tool':\n"
        "        if _hosted_mode():\n"
        "            return {'error': 'hosted'}\n"
        "        p = args.get('root_dir')\n"
        "        return open(p).read()\n"
    )
    tree = ast.parse(snippet)
    for tool, branch in _iter_dispatch_branches(tree):
        if tool == "planted_good_tool":
            assert _has_fs_sink(branch)
            assert _has_guard(branch)


def test_scanner_ignores_github_api_path_tools():
    """Meta-test: read_file/patch_file/list_files take a ``path``/``file_path``
    arg but resolve it via the GitHub HTTP API (no local FS). The arg name alone
    must NOT trigger a violation — only a real FS sink does."""
    snippet = (
        "async def _h(name, args, http):\n"
        "    if name == 'read_file':\n"
        "        path = args.get('path', '')\n"
        "        r = await http.get(f'https://api.github.com/{path}')\n"
        "        return r.json()\n"
    )
    tree = ast.parse(snippet)
    for tool, branch in _iter_dispatch_branches(tree):
        if tool == "read_file":
            assert not _has_fs_sink(branch), (
                "GitHub-API path tool must not register as a local-FS sink"
            )


@pytest.mark.parametrize(
    "tool",
    [
        "ingest_document",
        "get_document_structure",
        "get_latex_structure",
        "search_code_semantic",
        "search_outputs",
    ],
)
def test_known_local_fs_tools_are_recognized_and_guarded(tool):
    """Regression anchor: the tools we KNOW read a caller's local path are (a)
    detected by the scanner as local-FS tools and (b) currently guarded. If a
    refactor drops one of these guards, this fails with a pointed message."""
    src = _read(_HANDLER)
    tree = ast.parse(src)
    branches = {t: b for t, b in _iter_dispatch_branches(tree)}
    assert tool in branches, f"dispatch branch for {tool!r} not found in handler.py"
    branch = branches[tool]

    delegate_guarded = False
    for delegate in _delegate_targets(branch):
        g = _delegate_function_guarded(delegate)
        if g:
            delegate_guarded = True

    # The load-bearing invariant: each of these known local-FS tools carries a
    # hosted-mode guard, whether inline in its dispatch branch (ingest_document /
    # get_document_structure / get_latex_structure / search_outputs) or inside
    # the delegate function it hands the caller path off to (search_code_semantic,
    # whose guard lives in code_index.search_code_semantic). Note we assert the
    # GUARD, not sink-detection: the FS sink for some tools sits several call
    # hops deep (e.g. ingest_document -> db.ingest_document -> doc_ingest.
    # extract_text) or behind a bare-imported delegate, which a single-file
    # static scan intentionally does not chase. The guard's presence is the rule.
    assert _has_guard(branch) or delegate_guarded, (
        f"{tool!r} touches a caller's local filesystem but its hosted-mode guard "
        f"is gone (decision 0dedff91). Restore the guard."
    )
