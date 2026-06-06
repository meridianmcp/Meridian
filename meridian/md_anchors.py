"""XML-anchor structured auto-update of Meridian's markdown docs.

Markdown files carry HTML-comment anchors::

    <!-- MERIDIAN:ANCHOR:START:devlog -->
    ... section body ...
    <!-- MERIDIAN:ANCHOR:END:devlog -->

Each anchor has a *mode* declared in :data:`ANCHOR_REGISTRY`:

* ``append``  — additive only (insert a line before END). Runs directly; low
  blast radius. Used by session-end DEVLOG logging, ``pin_decision`` and
  ``add_note`` auto-append.
* ``replace`` — rewrites the whole section. ALWAYS goes through a HITL approval
  (see ``update_md_section`` in server.py); never written unattended.

A file may host both kinds (e.g. ROADMAP.md). The wrong operation against the
wrong anchor is rejected, so an auto-append can never clobber curated prose and
an unattended replace can never land without a human approving the diff.

All writes are:
* **hosted-tier no-ops** — the hosted process multiplexes tenants and must never
  touch a repo-root .md (mirrors ``goal_md.py``).
* **atomic** — tmp -> ``os.replace``, with the Windows "file locked by the live
  server" ``PermissionError`` swallowed.
* **content-guarded** — only TECHNICAL / ARCHITECTURAL / PRODUCT material may
  carry into a committed .md (honors the no-sensitive-strategy rule).

The module also tracks a process-global *touched-set* of files it has written,
drained at checkpoint/session-end by ``git_md.commit_touched_md`` for a single
pathspec-scoped commit. The touched-set is only ever populated in single-tenant
local mode (hosted skips every write), so the global is safe.
"""
from __future__ import annotations

import difflib
import hashlib
import os
from pathlib import Path

# --------------------------------------------------------------------------
# Registry & policy
# --------------------------------------------------------------------------

#: (filename, anchor_name) -> "append" | "replace"
#:
#: ROADMAP.md / DECISIONS.md / DEVLOG.md are append-only by project rule (humans
#: own them; never rewrite existing prose) so they carry ONLY append anchors.
#: Section *replacement* via update_md_section is therefore limited to the agent
#: templates CLAUDE.md and AGENTS.md.
ANCHOR_REGISTRY: dict[tuple[str, str], str] = {
    ("DEVLOG.md", "devlog"): "append",            # session-end summaries
    ("DECISIONS.md", "decisions-log"): "append",  # pin_decision (committable only)
    ("ROADMAP.md", "roadmap-notes"): "append",    # add_note(tag=roadmap, committable)
    ("CLAUDE.md", "claude-body"): "replace",      # update_md_section (HITL)
    ("AGENTS.md", "agents-body"): "replace",      # update_md_section (HITL)
}

#: Files this module is ever allowed to write. README.md is deliberately absent
#: — it is manual-only and both code paths reject it.
ALLOWED_MD_FILES: frozenset[str] = frozenset(f for (f, _a) in ANCHOR_REGISTRY)

#: Decision/note categories permitted into a committed .md. Anything else
#: (STRATEGIC / COMPETITIVE / BUSINESS / unknown / None) is blocked, fail-closed.
COMMITTABLE_CATEGORIES: frozenset[str] = frozenset(
    {"TECHNICAL", "ARCHITECTURAL", "PRODUCT"}
)


class AnchorError(Exception):
    """Anchor markers are malformed or the operation is illegal for the mode."""


class AnchorMissing(AnchorError):
    """The requested anchor is not present in the file."""


class AnchorAmbiguous(AnchorError):
    """The anchor name appears more than once — refuse to guess."""


# --------------------------------------------------------------------------
# Environment / roots
# --------------------------------------------------------------------------


def md_root() -> Path:
    """Directory that holds the repo's markdown docs.

    Reads ``MERIDIAN_MD_ROOT`` at call time so tests can redirect every write
    into a temp dir (the suite would otherwise mutate and commit the real
    repo docs — there is no override on server.py's ``_REPO_ROOT``).
    """
    override = os.environ.get("MERIDIAN_MD_ROOT")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent


def _hosted_mode() -> bool:
    """v2.3 — .md auto-update is dev-tier only; skip every write when hosted."""
    return os.environ.get("MERIDIAN_HOSTED", "").lower() in ("1", "true", "yes")


def is_committable_category(category: str | None) -> bool:
    """True only for categories allowed in a committed .md. None/unknown -> False."""
    return (category or "").strip().upper() in COMMITTABLE_CATEGORIES


# --------------------------------------------------------------------------
# Anchor markers & pure parsing
# --------------------------------------------------------------------------


def start_marker(name: str) -> str:
    return f"<!-- MERIDIAN:ANCHOR:START:{name} -->"


def end_marker(name: str) -> str:
    return f"<!-- MERIDIAN:ANCHOR:END:{name} -->"


def _anchor_span(text: str, name: str) -> tuple[int, int] | None:
    """Return ``(inner_start, inner_end)`` char offsets of the body between the
    START and END markers, or ``None`` when neither marker is present.

    ``inner_start`` is just after the START marker's line; ``inner_end`` is the
    start of the END marker's line, so ``text[inner_start:inner_end]`` is the
    replaceable/appendable body (including its trailing newline).

    Raises :class:`AnchorAmbiguous` on duplicate markers and :class:`AnchorError`
    when exactly one of START/END is present (malformed) or they are out of order.
    """
    sm, em = start_marker(name), end_marker(name)
    sc, ec = text.count(sm), text.count(em)
    if sc == 0 and ec == 0:
        return None
    if sc > 1 or ec > 1:
        raise AnchorAmbiguous(f"anchor {name!r} appears more than once")
    if sc != 1 or ec != 1:
        raise AnchorError(f"anchor {name!r} is malformed (missing START or END)")
    s = text.index(sm)
    e = text.index(em)
    if e < s:
        raise AnchorError(f"anchor {name!r} END precedes START")
    nl = text.find("\n", s)
    inner_start = len(text) if nl == -1 else nl + 1
    line_start = text.rfind("\n", 0, e)
    inner_end = 0 if line_start == -1 else line_start + 1
    if inner_end < inner_start:
        inner_end = inner_start
    return (inner_start, inner_end)


def find_anchor(text: str, name: str) -> tuple[int, int] | None:
    """Public wrapper around :func:`_anchor_span`."""
    return _anchor_span(text, name)


def get_anchor_body(text: str, name: str) -> str | None:
    span = _anchor_span(text, name)
    if span is None:
        return None
    return text[span[0]:span[1]]


def replace_anchor(text: str, name: str, new_body: str) -> str:
    """Return ``text`` with the named anchor's body replaced. Raises
    :class:`AnchorMissing` if the anchor is absent (caller must not silently
    create on a replace — a missing anchor means the doc drifted)."""
    span = _anchor_span(text, name)
    if span is None:
        raise AnchorMissing(f"anchor {name!r} not found")
    block = new_body.rstrip("\n") + "\n"
    return text[:span[0]] + block + text[span[1]:]


def append_to_anchor(text: str, name: str, line: str) -> str:
    """Return ``text`` with ``line`` inserted just before the anchor's END
    marker. Idempotent: if a line with identical stripped content already lives
    in the body, the text is returned unchanged. Raises :class:`AnchorMissing`
    if the anchor is absent (callers create the anchor first)."""
    span = _anchor_span(text, name)
    if span is None:
        raise AnchorMissing(f"anchor {name!r} not found")
    body = text[span[0]:span[1]]
    target = line.strip()
    if any(existing.strip() == target for existing in body.splitlines()):
        return text
    new_line = line.rstrip("\n") + "\n"
    if body and not body.endswith("\n"):
        body += "\n"
    return text[:span[0]] + body + new_line + text[span[1]:]


def _fresh_anchor_block(name: str) -> str:
    return f"{start_marker(name)}\n\n{end_marker(name)}\n"


def _ensure_anchor(text: str, name: str, *, file: str) -> str:
    """Return ``text`` guaranteed to contain the named anchor, creating an empty
    block at EOF when absent. Used by append paths only."""
    if _anchor_span(text, name) is not None:
        return text
    if not text:
        text = f"# {Path(file).stem}\n\n"
    if not text.endswith("\n"):
        text += "\n"
    return text + "\n" + _fresh_anchor_block(name)


def build_diff(file: str, anchor: str, new_body: str) -> str:
    """Unified diff between the anchor's current body and the proposed body.
    Reads the on-disk file under :func:`md_root`; missing file/anchor -> old is
    empty. Pure-ish (read-only); used to populate the HITL diff preview."""
    try:
        text = (md_root() / file).read_text(encoding="utf-8")
    except OSError:
        text = ""
    try:
        old = get_anchor_body(text, anchor) or ""
    except AnchorError:
        old = ""
    diff = difflib.unified_diff(
        old.splitlines(keepends=True),
        (new_body.rstrip("\n") + "\n").splitlines(keepends=True),
        fromfile=f"{file}:{anchor} (current)",
        tofile=f"{file}:{anchor} (proposed)",
    )
    return "".join(diff)


def anchor_content_hash(file: str, anchor: str) -> str | None:
    """Stable hash of the anchor's current body, for stale-approval detection.
    Returns ``None`` if the file/anchor can't be read. Git-independent (works
    even outside a repo)."""
    try:
        text = (md_root() / file).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        body = get_anchor_body(text, anchor)
    except AnchorError:
        return None
    if body is None:
        return None
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def assert_replace_target(file: str, anchor: str) -> None:
    """Raise ``ValueError`` unless ``(file, anchor)`` is a registered *replace*
    anchor. Rejects unknown files (incl. README.md), unknown anchors, and
    append-only anchors."""
    if file not in ALLOWED_MD_FILES:
        raise ValueError(f"{file!r} is not a code-writable markdown file")
    mode = ANCHOR_REGISTRY.get((file, anchor))
    if mode is None:
        raise ValueError(f"unknown anchor {anchor!r} in {file!r}")
    if mode != "replace":
        raise ValueError(
            f"anchor {anchor!r} in {file!r} is append-only; "
            f"use the auto-append path, not a section replace"
        )


# --------------------------------------------------------------------------
# Atomic write & touched-set
# --------------------------------------------------------------------------

_TOUCHED: set[Path] = set()


def _atomic_write(path: Path, content: str) -> bool:
    """Write ``content`` to ``path`` atomically (tmp -> os.replace). Returns True
    if the file was written, False if a Windows lock blocked the rename (the live
    server holds the file). Records the path in the touched-set on success."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    try:
        os.replace(tmp, path)
    except PermissionError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    _TOUCHED.add(path.resolve())
    return True


def drain_touched() -> list[Path]:
    """Return the touched files and clear the set (called at checkpoint)."""
    paths = sorted(_TOUCHED)
    _TOUCHED.clear()
    return paths


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


# --------------------------------------------------------------------------
# High-level apply ops (the only writers)
# --------------------------------------------------------------------------


async def apply_append(
    file: str, anchor: str, line: str, *, category: str | None = None
) -> Path | None:
    """Append ``line`` to an *append* anchor, creating the anchor (and file) if
    needed. No-op (returns ``None``) when hosted, when ``category`` is provided
    but not committable (defense-in-depth — callers also guard), or when nothing
    changed. Returns the written path otherwise.

    ``category=None`` means "no category context" (e.g. a session summary) and is
    allowed; the category gate is for decision/note content whose caller passes
    an explicit category.
    """
    if _hosted_mode():
        return None
    if ANCHOR_REGISTRY.get((file, anchor)) != "append":
        raise ValueError(f"anchor {anchor!r} in {file!r} is not an append anchor")
    if category is not None and not is_committable_category(category):
        return None
    path = md_root() / file
    original = _read(path)
    text = _ensure_anchor(original, anchor, file=file)
    text = append_to_anchor(text, anchor, line)
    if text == original:
        return None
    return path if _atomic_write(path, text) else None


async def apply_replace(file: str, anchor: str, content: str) -> Path | None:
    """Replace a *replace* anchor's body (HITL-approved path only). Raises
    ``ValueError`` if not a replace target, :class:`AnchorMissing` if the file or
    anchor is absent (drift since drafting). No-op when hosted or unchanged."""
    if _hosted_mode():
        return None
    assert_replace_target(file, anchor)
    path = md_root() / file
    original = _read(path)
    if not original:
        raise AnchorMissing(f"{file!r} does not exist; cannot replace section")
    text = replace_anchor(original, anchor, content)
    if text == original:
        return None
    return path if _atomic_write(path, text) else None
