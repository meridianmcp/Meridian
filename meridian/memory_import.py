"""Claude Code auto-memory -> Meridian importer (sprint item 55d48d69,
design "memory_md_handling" step 2). DRY-RUN BY DEFAULT.

``python -m meridian memory import [--out FILE] [--summary FILE]``
    Scans every ``<claude config>/projects/*/memory/`` directory READ-ONLY,
    parses each ``*.md`` (frontmatter ``name`` / ``description`` / ``type`` --
    top-level or nested under ``metadata:`` -- plus the body) and writes a JSON
    *mapping* proposing one Meridian record per memory file. Nothing is sent
    anywhere and no memory file is modified.

``python -m meridian memory import --apply APPROVED_FILE``
    Writes ONLY the records an owner explicitly approved in a copy of that
    mapping (top-level ``approval.approved: true`` + ``approved_by``, and
    ``approved: true`` on each record), through a Meridian server's REST API.
    Idempotent: every record carries a per-title dedupe tag, and a record whose
    tag (or pinned-decision title) already exists is skipped.

Mapping rules (design):

* ``feedback_*`` -> a PROCESS ``pin_decision`` candidate when the rule is an
  imperative ("Never/Always/Hard rule/Must/Do not ..."), otherwise an
  ``add_note`` with ``category="rule"``; the other form is listed under
  ``alternatives`` so the owner can switch.
* ``reference_*`` -> ``add_note`` ``kind="reference"``.
* ``project_*`` -> ``add_note`` under the memory dir's resolved project id.
* ``user_*`` -> ``add_workspace_note``.
* The frontmatter ``type`` wins over the filename prefix; a disagreement is
  flagged. Files with neither are ``unclassified``.

Every record is tagged ``auto-memory-import`` and deduped by a hash of its
normalized title (and by identical body). Stale facts are FLAGGED, never
copied: a line that recommends an index name the cbm index cleanup deletes
(``project='meridian-repo'`` ...), tunnel-slot-dependent advice, a dated
state snapshot ("as of <date>", ``dev@<sha>``, "~1773 tests"), or
secret-shaped text is removed from the proposed body and listed under
``excluded_lines`` (secret-shaped lines by number and pattern name only --
their text is never reproduced). Record-level warnings cover age, "FIXED /
SUPERSEDED" history, repo paths that no longer exist, and kind mismatches.

Project resolution per memory dir: explicit ``--project SLUG=ID``; else the
slug is decoded back to its on-disk directory (Claude Code slugs replace every
non-alphanumeric character with ``-``; older builds kept ``_``) and that
repo's ``meridian.toml`` ``[project] project_id`` (only that key is read) or
``CLAUDE.local.md`` "Project ID" line is used. Anything else stays
``unresolved``; UUIDs the memory text itself labels as a project id are
recorded as ``candidate_project_ids`` hints only.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol

from .hook_settings_merge import resolve_repo_project_id

SCHEMA = "meridian.memory_import.mapping/v1"
IMPORT_TAG = "auto-memory-import"
DEDUPE_TAG_PREFIX = "amimport-"
KINDS = ("feedback", "reference", "project", "user")
# Point-in-time project state ages fast; durable rules/references slowly.
STALE_AGE_DAYS = 30
STALE_AGE_DAYS_DURABLE = 90
INDEX_FILE = "MEMORY.md"

# Index names the codebase-memory index cleanup deletes (design
# open_questions_for_owner #1 + indexMap "rows per root"): same-root
# duplicates and stale subdir indexes of the Meridian repo, and the old
# lowercase dnabert index. Advice naming one of them is flagged, not copied.
DEFAULT_STALE_INDEX_NAMES: tuple[str, ...] = (
    "meridian-repo",
    "meridian-canonical",
    "meridian-main",
    "C-Users-13144-Documents-Meridian-repository-Meridian",
    "meridian-build",
    "meridian-build-local",
    "meridian-core",
    "meridian-core-current",
    "meridian-package-clean",
    "dnabert-error-correction",
)
# Index names that are ALSO ordinary project / repo / folder names in prose
# (the Meridian project itself is "meridian-build"; "dnabert-error-correction"
# is the repo folder). These are flagged only in an explicit index context
# (``project="X"``, ``name="X"``, "X is indexed", "indexed as X").
AMBIGUOUS_INDEX_NAMES: frozenset[str] = frozenset(
    {"meridian-build", "meridian-core", "dnabert-error-correction"}
)

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_UUID_RE = re.compile(_UUID)
_CANDIDATE_PID_RE = re.compile(r"project[ _-]?id\W{0,6}(?:is\W{0,4})?`?(" + _UUID + r")", re.IGNORECASE)

# A code-intel context on the same line makes an index-name hit an index
# reference (``meridian-build`` is also a repo/folder name in prose).
_CODE_INTEL_CONTEXT_RE = re.compile(
    r"project\s*[=:]|search_graph|search_code|trace_path|get_code_snippet|query_graph"
    r"|get_architecture|index_repository|list_projects|codebase[-_ ]memory|\bindex(?:ed|es)?\b",
    re.IGNORECASE,
)
# Tool families that only exist through the hosted tunnel slots (all of them
# returned 503 in the design session while local stdio code-intel worked).
_TUNNEL_TOOL_RE = re.compile(
    r"mcp__meridian__(?:codebase|extractor|code|fs|serena)__"
    r"|(?<![\w-])meridian(?:-|__)(?:code|extractor|debug|fs)(?![\w-])"
    r"|(?<![\w-])(?:serena-meridian|codebase-memory-meridian)(?![\w-])",
    re.IGNORECASE,
)
# Routing through the tunnel (not a mere mention: "routes/tunnel.py",
# "tunnel restart" and "hosted-tunnel variants which 503" are facts).
_TUNNEL_ROUTING_RE = re.compile(
    r"\btunnel-routed\b|\b(?:via|through|over) (?:the )?(?:hosted )?tunnel\b"
    r"|\btunnel(?:ed)? (?:prefix|prefixes|route|routes|routing)\b",
    re.IGNORECASE,
)
_ADVICE_RE = re.compile(
    r"\b(?:use|prefer|try|call|route|first|instead|fall ?back|always|go through)\b", re.IGNORECASE
)
_DATE_RE = re.compile(r"\b20\d\d-[01]\d-[0-3]\d\b")
_COMMIT_REF_RE = re.compile(
    r"\b(?:origin/)?(?:dev|main|master|prod)@[0-9a-f]{7,40}\b", re.IGNORECASE
)
_VERSION_TAG_RE = re.compile(r"\b(?:tagged|at|is)\s+v\d+\.\d+(?:\.\d+)?\b", re.IGNORECASE)
_TEST_COUNT_RE = re.compile(r"~\s?\d{3,5}\+?\s+tests?\b|\b\d{3,5}\+?\s+(?:tests?\s+)?passing\b", re.IGNORECASE)
_STATE_WORD_RE = re.compile(
    r"\b(?:as of|currently|current(?:ly)? state|right now|at the moment|now live|is live|live at"
    r"|still (?:open|pending|unresolved|blocked|broken|failing)|unresolved|not (?:yet )?started"
    r"|in progress|so far|to date|next step|open items?|pending|remaining)\b",
    re.IGNORECASE,
)
_LIVE_WORD_RE = re.compile(r"\b(?:prod|live|deployed|HEAD|latest)\b", re.IGNORECASE)
_RESOLVED_RE = re.compile(r"\b(?:FIXED|RESOLVED|SUPERSEDED|REFUTED|OBSOLETE)\b")
_RESOLVED_SOFT_RE = re.compile(r"\b(?:superseded|no longer (?:applies|true|relevant|needed))\b", re.IGNORECASE)
_DECISION_IMPERATIVE_RE = re.compile(
    r"""^\W*(?:never|always|hard rule|must|do not|don't|treat|stop)\b""", re.IGNORECASE
)
_REPO_PATH_RE = re.compile(
    r"(?<![\w/:.\\-])((?:\.claude|meridian|tests|scripts|docs|extensions|tools|helpers|src)"
    r"/[\w./-]*?[\w-]\.(?:py|ts|tsx|js|mjs|json|toml|md|ps1|sh|css|ya?ml|txt))\b"
)
_INDEX_LINE_RE = re.compile(r"^\s*[-*]\s*\[([^\]]+)\]\(([^)\s]+)\)\s*(?:[\u2014\u2013-]+\s*(.*))?$")


# ---------------------------------------------------------------------------
# Paths and slugs
# ---------------------------------------------------------------------------


def default_projects_root(env: Mapping[str, str] | None = None) -> Path:
    """``$CLAUDE_CONFIG_DIR/projects`` when set, else ``~/.claude/projects``."""
    env = os.environ if env is None else env
    cfg = (env.get("CLAUDE_CONFIG_DIR") or "").strip()
    base = Path(cfg) if cfg else Path.home() / ".claude"
    return base / "projects"


def claude_slug(path: str | Path) -> str:
    """Claude Code's project-dir slug: every non-alphanumeric char -> ``-``."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def claude_slug_legacy(path: str | Path) -> str:
    """Older Claude Code builds kept ``_`` in the slug."""
    return re.sub(r"[^A-Za-z0-9_]", "-", str(path))


def _component_slugs(name: str) -> set[str]:
    return {claude_slug(name), claude_slug_legacy(name)}


def decode_slug(slug: str, *, max_dirs: int = 4000) -> Path | None:
    """Recover the on-disk directory a project slug was made from by walking
    the filesystem (read-only ``scandir``), matching one path component at a
    time. Returns ``None`` when no existing directory produces ``slug``."""
    m = re.match(r"^([A-Za-z])--(.*)$", slug)
    if m:
        root = Path(f"{m.group(1).upper()}:/")
        rest = m.group(2)
    elif slug.startswith("-"):
        root = Path("/")
        rest = slug[1:]
    else:
        return None
    if not root.exists():
        return None
    if not rest:
        return root
    budget = [max_dirs]

    def walk(cur: Path, remaining: str) -> Path | None:
        if budget[0] <= 0:
            return None
        budget[0] -= 1
        try:
            names = sorted(
                e.name for e in os.scandir(cur) if e.is_dir(follow_symlinks=False)
            )
        except OSError:
            return None
        partial: list[tuple[int, str, str]] = []
        for name in names:
            for s in _component_slugs(name):
                if remaining == s:
                    return cur / name
                if remaining.startswith(s + "-"):
                    partial.append((len(s), name, remaining[len(s) + 1:]))
        for _length, name, rest_after in sorted(partial, key=lambda t: (-t[0], t[1])):
            found = walk(cur / name, rest_after)
            if found is not None:
                return found
        return None

    return walk(root, rest)


def scan_memory_dirs(projects_root: Path) -> list[Path]:
    """Every ``<projects_root>/<slug>/memory`` directory, sorted. Read-only;
    renamed archives (``memory.imported-<date>``) are not matched."""
    out: list[Path] = []
    try:
        entries = sorted(projects_root.iterdir())
    except OSError:
        return out
    for entry in entries:
        mem = entry / "memory"
        if entry.is_dir() and mem.is_dir():
            out.append(mem)
    return out


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def split_frontmatter(text: str) -> tuple[str | None, str, int]:
    """Return ``(frontmatter_text_or_None, body, body_start_line)`` where
    ``body_start_line`` is the 1-based file line the body begins on."""
    t = text[1:] if text.startswith("\ufeff") else text
    lines = t.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return None, t, 1
    for i in range(1, len(lines)):
        if lines[i].strip() in ("---", "..."):
            return "".join(lines[1:i]), "".join(lines[i + 1:]), i + 2
    return None, t, 1


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.isoformat()
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _unquote(value: str) -> str:
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] == '"':
        try:
            return str(json.loads(v))
        except ValueError:
            return v[1:-1]
    if len(v) >= 2 and v[0] == v[-1] == "'":
        return v[1:-1].replace("''", "'")
    return v


def _parse_frontmatter_minimal(fm: str) -> dict[str, Any]:
    """Fallback for frontmatter PyYAML rejects: ``key: value`` lines, one
    nesting level (``metadata:`` + indented keys), folded ``>``/``|``."""
    out: dict[str, Any] = {}
    parent: dict[str, Any] | None = None
    folded_key: tuple[dict[str, Any], str] | None = None
    for raw in fm.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        if folded_key is not None and indent > 0 and ":" not in line.split(" ", 1)[0]:
            target, key = folded_key
            target[key] = (str(target.get(key) or "") + " " + line).strip()
            continue
        folded_key = None
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        container = out if indent == 0 or parent is None else parent
        if indent == 0:
            parent = None
        if value == "":
            if indent == 0:
                out[key] = {}
                parent = out[key]
            else:
                container[key] = ""
            continue
        if value in (">", ">-", "|", "|-"):
            container[key] = ""
            folded_key = (container, key)
            continue
        container[key] = _unquote(value)
    return out


def parse_frontmatter(fm: str | None) -> tuple[dict[str, Any], str | None]:
    """Return ``(frontmatter_dict, parse_note)``."""
    if fm is None:
        return {}, None
    try:
        import yaml  # noqa: PLC0415 - optional dependency

        loaded = yaml.safe_load(fm)
        if isinstance(loaded, dict):
            return _jsonable(loaded), None
        return {}, "frontmatter is not a mapping"
    except Exception:  # noqa: BLE001 - fall back on any YAML problem
        try:
            return _parse_frontmatter_minimal(fm), "parsed with fallback parser"
        except Exception as exc:  # noqa: BLE001
            return {}, f"unparseable frontmatter: {exc.__class__.__name__}"


def parse_index(memory_dir: Path) -> dict[str, dict[str, str]]:
    """Parse ``MEMORY.md`` link lines -> ``{basename: {title, description}}``."""
    out: dict[str, dict[str, str]] = {}
    try:
        text = (memory_dir / INDEX_FILE).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        m = _INDEX_LINE_RE.match(line)
        if not m:
            continue
        target = m.group(2).split("#", 1)[0]
        if not target.lower().endswith(".md"):
            continue
        out[Path(target).name] = {"title": m.group(1).strip(), "description": (m.group(3) or "").strip()}
    return out


def classify_kind(frontmatter: Mapping[str, Any], filename: str) -> tuple[str, str, str | None]:
    """Return ``(kind, source, mismatch_note)``; frontmatter ``type`` (top
    level or under ``metadata``) wins over the filename prefix."""
    meta = frontmatter.get("metadata") if isinstance(frontmatter.get("metadata"), dict) else {}
    fm_type = frontmatter.get("type") or (meta or {}).get("type")
    fm_type = str(fm_type).strip().lower() if fm_type else None
    lower = filename.lower()
    prefix = next((k for k in KINDS if lower.startswith(k + "_") or lower.startswith(k + "-")), None)
    if prefix is None:
        name = str(frontmatter.get("name") or "").lower()
        prefix_from_name = next(
            (k for k in KINDS if name.startswith(k + "_") or name.startswith(k + "-")), None
        )
    else:
        prefix_from_name = None
    mismatch = None
    if fm_type in KINDS:
        if prefix and prefix != fm_type:
            mismatch = f"frontmatter type '{fm_type}' but filename prefix '{prefix}_'"
        return fm_type, "frontmatter", mismatch
    if prefix:
        return prefix, "filename", None
    if prefix_from_name:
        return prefix_from_name, "name", None
    return "unclassified", "none", None


def normalize_title(title: str) -> str:
    return " ".join(re.sub(r"[^0-9a-z]+", " ", title.casefold()).split())


def title_hash(title: str) -> str:
    return hashlib.sha256(normalize_title(title).encode("utf-8")).hexdigest()[:16]


def _choose_title(fm: Mapping[str, Any], index_entry: Mapping[str, str] | None, stem: str) -> tuple[str, str]:
    if index_entry and index_entry.get("title"):
        return index_entry["title"], "MEMORY.md link text"
    name = str(fm.get("name") or "").strip()
    if name and " " in name:
        return name, "frontmatter name"
    desc = str(fm.get("description") or "").strip()
    if desc:
        first = re.split(r"(?<=[.;])\s|\s[\u2014\u2013]\s", desc, maxsplit=1)[0].strip()
        return (first[:137] + "...") if len(first) > 140 else first, "frontmatter description"
    if name:
        return name, "frontmatter name"
    return stem, "filename"


# ---------------------------------------------------------------------------
# Staleness
# ---------------------------------------------------------------------------


@dataclass
class LineFlag:
    line: int
    kind: str
    reason: str
    text: str | None  # None for secret-shaped lines: never reproduced

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"line": self.line, "kind": self.kind, "reason": self.reason}
        if self.text is not None:
            d["text"] = self.text[:300] + ("..." if len(self.text) > 300 else "")
        return d


@dataclass(frozen=True)
class IndexNamePattern:
    name: str
    token: re.Pattern[str]  # the bare name as a whole token
    strict: re.Pattern[str] | None  # explicit index context, for ambiguous names


def _index_name_patterns(names: Iterable[str]) -> list[IndexNamePattern]:
    out: list[IndexNamePattern] = []
    q = r"[`'\"]?"
    for n in names:
        if not n:
            continue
        e = re.escape(n)
        token = re.compile(r"(?<![\w-])" + e + r"(?![\w-])")
        strict = None
        if n in AMBIGUOUS_INDEX_NAMES:
            strict = re.compile(
                r"(?:\bproject|\bname)\s*=\s*" + q + e + r"(?![\w-])"
                r"|(?<![\w-])" + e + q + r"\s+(?:is\s+(?:now\s+)?|was\s+)?(?:re)?index"
                r"|\bindex(?:ed)?\s+(?:as|named)\s+" + q + e + r"(?![\w-])",
                re.IGNORECASE,
            )
        out.append(IndexNamePattern(n, token, strict))
    return out


def flag_line(text: str, index_patterns: list[IndexNamePattern]) -> tuple[str, str] | None:
    """Classify one body line: ``(kind, reason)`` when it is a stale fact that
    must not be copied, else ``None``. Order: index name, tunnel, dated."""
    has_context = bool(_CODE_INTEL_CONTEXT_RE.search(text))
    hits = [
        p.name
        for p in index_patterns
        if (p.strict.search(text) if p.strict is not None else (has_context and p.token.search(text)))
    ]
    if hits:
        return (
            "stale-index-name",
            "names codebase-memory index "
            + ", ".join(repr(h) for h in hits)
            + ", which the index cleanup deletes; resolve the live index instead",
        )
    tm = _TUNNEL_TOOL_RE.search(text)
    if tm:
        return (
            "tunnel-dependent",
            f"relies on tunnel-slot tool family '{tm.group(0)}' (503 while local stdio code-intel works)",
        )
    if _TUNNEL_ROUTING_RE.search(text) and _ADVICE_RE.search(text):
        return ("tunnel-dependent", "routing advice that depends on the hosted tunnel being up")
    has_date = bool(_DATE_RE.search(text))
    has_commit = bool(_COMMIT_REF_RE.search(text))
    has_version = bool(_VERSION_TAG_RE.search(text))
    has_count = bool(_TEST_COUNT_RE.search(text))
    state = _STATE_WORD_RE.search(text)
    if state and (has_date or has_commit or has_version or has_count):
        return ("dated-state", f"point-in-time state ('{state.group(0)}' + a date/commit/version/count)")
    if has_commit and _LIVE_WORD_RE.search(text):
        return ("dated-state", "deploy/branch head pinned to a specific commit")
    if has_count:
        return ("dated-state", "test-count snapshot")
    return None


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------


@dataclass
class DirInfo:
    slug: str
    source_dir: str
    decoded_root: str | None
    project_id: str | None
    resolution: str
    candidate_project_ids: list[str] = field(default_factory=list)
    file_count: int = 0
    index_entries: int = 0
    dangling_index_entries: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def _parse_overrides(pairs: Iterable[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key.strip() or not value.strip():
            raise ValueError(f"--project expects SLUG_OR_PATH=PROJECT_ID, got {pair!r}")
        k = key.strip()
        if "/" in k or "\\" in k or ":" in k:
            k = claude_slug(k.rstrip("/\\"))
        out[k] = value.strip()
    return out


def resolve_dir_project(
    slug: str,
    *,
    overrides: Mapping[str, str],
    decode: bool = True,
) -> tuple[str | None, str | None, str]:
    """Return ``(decoded_root, project_id, resolution)``."""
    if slug in overrides:
        root = decode_slug(slug) if decode else None
        return (str(root) if root else None), overrides[slug], "explicit --project"
    root = decode_slug(slug) if decode else None
    if root is None:
        return None, None, "unresolved (source directory not found on disk)"
    pid, source = resolve_repo_project_id(root)
    if pid:
        return str(root), pid, source
    return str(root), None, "unresolved (no meridian.toml [project] project_id or CLAUDE.local.md Project ID)"


def _modified(fm: Mapping[str, Any], path: Path) -> _dt.datetime | None:
    meta = fm.get("metadata") if isinstance(fm.get("metadata"), dict) else {}
    raw = (meta or {}).get("modified") or fm.get("modified")
    if raw:
        try:
            value = _dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            return value if value.tzinfo else value.replace(tzinfo=_dt.timezone.utc)
        except ValueError:
            pass
    try:
        return _dt.datetime.fromtimestamp(path.stat().st_mtime, tz=_dt.timezone.utc)
    except OSError:
        return None


def _secret_lines(body: str) -> dict[int, list[str]]:
    """``{body_line_index(0-based): [pattern names]}`` -- names only."""
    try:
        from .secret_redaction import scan  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return {}
    out: dict[int, list[str]] = {}
    for m in scan(body):
        start_line = body.count("\n", 0, m.start)
        end_line = body.count("\n", 0, m.end)
        for ln in range(start_line, end_line + 1):
            out.setdefault(ln, []).append(m.name)
    return out


def _target_for(
    kind: str,
    *,
    title: str,
    body: str,
    project_id: str | None,
    tags: list[str],
    decision_candidate: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    tag_str = ",".join(tags)
    note = {"tool": "add_note", "project_id": project_id, "title": title, "body": body, "tags": tag_str}
    if kind == "feedback":
        rule_note = {**note, "kind": "wiki", "category": "rule", "tags": tag_str}
        decision = {
            "tool": "pin_decision",
            "project_id": project_id,
            "title": title,
            "body": body,
            "category": "PROCESS",
            "tags": tag_str,
        }
        if decision_candidate:
            return decision, [{"tool": "add_note", "category": "rule", "kind": "wiki"}]
        return rule_note, [{"tool": "pin_decision", "category": "PROCESS"}]
    if kind == "reference":
        return {**note, "kind": "reference"}, []
    if kind == "project":
        return {**note, "kind": "wiki"}, [{"tool": "add_workspace_note"}]
    if kind == "user":
        return {"tool": "add_workspace_note", "title": title, "body": body, "tags": tag_str}, []
    return {**note, "kind": "wiki"}, [{"tool": "add_workspace_note"}]


def build_mapping(
    projects_root: Path | None = None,
    *,
    overrides: Mapping[str, str] | None = None,
    stale_index_names: Iterable[str] = DEFAULT_STALE_INDEX_NAMES,
    now: _dt.datetime | None = None,
    decode: bool = True,
) -> dict[str, Any]:
    """Build the dry-run mapping. Reads memory files only; writes nothing."""
    projects_root = Path(projects_root) if projects_root is not None else default_projects_root()
    overrides = dict(overrides or {})
    now = (now or _dt.datetime.now(_dt.timezone.utc)).astimezone(_dt.timezone.utc)
    index_patterns = _index_name_patterns(stale_index_names)
    dirs: list[DirInfo] = []
    records: list[dict[str, Any]] = []
    seen_titles: dict[str, str] = {}
    seen_bodies: dict[str, str] = {}

    for mem_dir in scan_memory_dirs(projects_root):
        slug = mem_dir.parent.name
        md_files = sorted(p for p in mem_dir.glob("*.md") if p.is_file() and p.name != INDEX_FILE)
        index = parse_index(mem_dir)
        info = DirInfo(
            slug=slug,
            source_dir=str(mem_dir),
            decoded_root=None,
            project_id=None,
            resolution="skipped (empty memory dir)",
            file_count=len(md_files),
            index_entries=len(index),
            dangling_index_entries=sorted(n for n in index if not (mem_dir / n).is_file()),
        )
        dirs.append(info)
        if not md_files:
            continue
        root, pid, resolution = resolve_dir_project(slug, overrides=overrides, decode=decode)
        info.decoded_root, info.project_id, info.resolution = root, pid, resolution
        candidates: set[str] = set()

        for path in md_files:
            try:
                raw = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                records.append({"source_file": str(path), "error": f"unreadable: {exc}"})
                continue
            fm_text, body, body_start = split_frontmatter(raw)
            fm, fm_note = parse_frontmatter(fm_text)
            for m in _CANDIDATE_PID_RE.finditer(raw):
                candidates.add(m.group(1).lower())
            kind, kind_source, mismatch = classify_kind(fm, path.name)
            title, title_source = _choose_title(fm, index.get(path.name), path.stem)
            thash = title_hash(title)
            body_norm = body.strip()
            bhash = hashlib.sha256(body_norm.encode("utf-8")).hexdigest()
            record_id = hashlib.sha256(f"{slug}/{path.name}".encode("utf-8")).hexdigest()[:16]

            secret_lines = _secret_lines(body)
            excluded: list[LineFlag] = []
            kept: list[str] = []
            body_lines = body.splitlines()
            for i, line in enumerate(body_lines):
                file_line = body_start + i
                if i in secret_lines:
                    excluded.append(
                        LineFlag(
                            file_line,
                            "secret-shaped",
                            "matches secret pattern(s): " + ", ".join(sorted(set(secret_lines[i]))),
                            None,
                        )
                    )
                    continue
                verdict = flag_line(line, index_patterns) if line.strip() else None
                if verdict:
                    excluded.append(LineFlag(file_line, verdict[0], verdict[1], line.strip()))
                    continue
                kept.append(line)
            kept_text = "\n".join(kept).strip()
            kept_text = re.sub(r"\n{3,}", "\n\n", kept_text)

            warnings: list[dict[str, str]] = []
            if fm_text is None:
                warnings.append({"kind": "no-frontmatter", "reason": "no YAML frontmatter; kind/title inferred"})
            if fm_note:
                warnings.append({"kind": "frontmatter", "reason": fm_note})
            if mismatch:
                warnings.append({"kind": "kind-mismatch", "reason": mismatch})
            modified = _modified(fm, path)
            age_days = (now - modified).days if modified else None
            age_limit = STALE_AGE_DAYS if kind in ("project", "unclassified") else STALE_AGE_DAYS_DURABLE
            if age_days is not None and age_days > age_limit:
                warnings.append(
                    {"kind": "age", "reason": f"last modified {age_days} days ago ({modified.date().isoformat()})"}
                )
            desc = str(fm.get("description") or "")
            head = desc + "\n" + body_norm[:400]
            if _RESOLVED_RE.search(head) or _RESOLVED_SOFT_RE.search(head):
                warnings.append(
                    {
                        "kind": "resolved-or-superseded",
                        "reason": "describes a fixed/superseded/refuted state; import as history or drop",
                    }
                )
            if flag_line(desc, index_patterns):
                warnings.append({"kind": "description-stale", "reason": "the description itself carries a flagged fact"})
            if root and Path(root).is_dir():
                # Only paths plausibly relative to THIS repo (their top-level
                # dir exists here); a thesis memory citing Meridian's
                # meridian/server.py is not a stale path in the thesis repo.
                missing = sorted(
                    {
                        p
                        for p in _REPO_PATH_RE.findall(body)
                        if (Path(root) / p.split("/", 1)[0]).is_dir() and not (Path(root) / p).exists()
                    }
                )
                if missing:
                    warnings.append(
                        {
                            "kind": "missing-paths",
                            "reason": "repo paths not found under "
                            + root
                            + ": "
                            + ", ".join(missing[:8])
                            + (f" (+{len(missing) - 8} more)" if len(missing) > 8 else ""),
                        }
                    )

            decision_candidate = kind == "feedback" and bool(
                _DECISION_IMPERATIVE_RE.search(desc) or _DECISION_IMPERATIVE_RE.search(body_norm[:200])
            )
            tags = [IMPORT_TAG, f"{DEDUPE_TAG_PREFIX}{thash}"]
            tags.append({"feedback": "rule", "reference": "reference", "project": "project-state",
                         "user": "user-profile"}.get(kind, "unclassified"))
            provenance = (
                f"\n\n---\nImported from Claude Code auto-memory `{slug}/memory/{path.name}` "
                f"(sha256 {bhash[:12]}) on {now.date().isoformat()}. Tag: {IMPORT_TAG}."
            )
            body_for_target = (kept_text + provenance) if kept_text else ""
            target, alternatives = _target_for(
                kind,
                title=title,
                body=body_for_target,
                project_id=pid,
                tags=tags,
                decision_candidate=decision_candidate,
            )

            duplicate_of = seen_titles.get(thash) or (seen_bodies.get(bhash) if body_norm else None)
            original_chars = len(body_norm)
            kept_chars = len(kept_text)
            if duplicate_of:
                action = "skip_duplicate"
            elif not kept_text:
                action = "review_only"
            elif target["tool"] != "add_workspace_note" and not pid:
                action = "needs_project"
            elif excluded and kept_chars < 0.5 * max(original_chars, 1):
                action = "review_only"
            elif excluded:
                action = "import_with_exclusions"
            else:
                action = "import"
            if not duplicate_of:
                seen_titles.setdefault(thash, record_id)
                if body_norm:
                    seen_bodies.setdefault(bhash, record_id)

            records.append(
                {
                    "record_id": record_id,
                    "source_dir_slug": slug,
                    "source_file": str(path),
                    "memory_kind": kind,
                    "kind_source": kind_source,
                    "frontmatter": {
                        "name": fm.get("name"),
                        "description": fm.get("description"),
                        "type": fm.get("type") or ((fm.get("metadata") or {}) if isinstance(fm.get("metadata"), dict) else {}).get("type"),
                    },
                    "title": title,
                    "title_source": title_source,
                    "title_hash": thash,
                    "body_sha256": bhash,
                    "body_chars": original_chars,
                    "kept_chars": kept_chars,
                    "modified": modified.isoformat() if modified else None,
                    "age_days": age_days,
                    "project_id": pid,
                    "decision_candidate": decision_candidate,
                    "action": action,
                    "duplicate_of": duplicate_of,
                    "excluded_lines": [f.as_dict() for f in excluded],
                    "warnings": warnings,
                    "target": target,
                    "alternatives": alternatives,
                    "approved": False,
                }
            )
        info.candidate_project_ids = sorted(c for c in candidates if c != (pid or "").lower())

    summary = summarize(records, dirs)
    return {
        "schema": SCHEMA,
        "mode": "dry-run",
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "projects_root": str(projects_root),
        "tag": IMPORT_TAG,
        "stale_index_names": list(stale_index_names),
        "approval": {
            "approved": False,
            "approved_by": None,
            "approved_at": None,
            "instructions": (
                "Owner review: set approved=true on each record to import (edit target.* freely), "
                "then set approval.approved=true and approval.approved_by, save as a NEW file and run "
                "`python -m meridian memory import --apply <that file>`. Records with action "
                "skip_duplicate are never applied."
            ),
        },
        "summary": summary,
        "memory_dirs": [d.as_dict() for d in dirs],
        "records": records,
    }


def summarize(records: list[dict[str, Any]], dirs: list[DirInfo]) -> dict[str, Any]:
    by_kind: dict[str, int] = {}
    by_action: dict[str, int] = {}
    by_tool: dict[str, int] = {}
    by_flag: dict[str, int] = {}
    by_dir: dict[str, dict[str, Any]] = {}
    for d in dirs:
        by_dir[d.slug] = {"project_id": d.project_id, "resolution": d.resolution, "files": d.file_count, "records": 0}
    for r in records:
        if "error" in r:
            by_action["error"] = by_action.get("error", 0) + 1
            continue
        by_kind[r["memory_kind"]] = by_kind.get(r["memory_kind"], 0) + 1
        by_action[r["action"]] = by_action.get(r["action"], 0) + 1
        by_tool[r["target"]["tool"]] = by_tool.get(r["target"]["tool"], 0) + 1
        by_dir.setdefault(r["source_dir_slug"], {"records": 0})["records"] += 1
        for f in r["excluded_lines"]:
            by_flag[f["kind"]] = by_flag.get(f["kind"], 0) + 1
    return {
        "memory_dirs": len(dirs),
        "records": sum(1 for r in records if "error" not in r),
        "errors": sum(1 for r in records if "error" in r),
        "by_kind": dict(sorted(by_kind.items())),
        "by_action": dict(sorted(by_action.items())),
        "by_target_tool": dict(sorted(by_tool.items())),
        "excluded_lines_by_flag": dict(sorted(by_flag.items())),
        "by_memory_dir": by_dir,
    }


def render_summary_markdown(mapping: Mapping[str, Any]) -> str:
    """Short human summary: counts per kind / project / action, and every
    flagged record with its reasons."""
    s = mapping["summary"]
    out = [
        "# Auto-memory import -- dry-run summary",
        "",
        f"Generated {mapping['generated_at']} from `{mapping['projects_root']}` (read-only; nothing was written to Meridian).",
        f"Every proposed record is tagged `{mapping['tag']}` and carries a per-title dedupe tag. "
        "No record is applied until the owner approves it (see `approval.instructions` in the JSON).",
        "",
        f"**{s['records']} memory files** across **{s['memory_dirs']} memory dirs** "
        f"({s['errors']} unreadable).",
        "",
        "## Per kind",
        "",
        "| kind | files |",
        "|---|---|",
    ]
    out += [f"| {k} | {v} |" for k, v in s["by_kind"].items()]
    out += ["", "## Proposed action", "", "| action | records |", "|---|---|"]
    out += [f"| {k} | {v} |" for k, v in s["by_action"].items()]
    out += ["", "| target tool | records |", "|---|---|"]
    out += [f"| {k} | {v} |" for k, v in s["by_target_tool"].items()]
    out += ["", "## Per memory dir / project", "", "| memory dir | files | project | resolution |", "|---|---|---|---|"]
    for d in mapping["memory_dirs"]:
        out.append(
            f"| `{d['slug']}` | {d['file_count']} | {d['project_id'] or 'unresolved'} | {d['resolution']}"
            + (f"; candidate ids in text: {', '.join(d['candidate_project_ids'])}" if d.get("candidate_project_ids") else "")
            + " |"
        )
    out += ["", "## Excluded (stale / unsafe) lines by reason", "", "| flag | lines |", "|---|---|"]
    out += [f"| {k} | {v} |" for k, v in s["excluded_lines_by_flag"].items()] or ["| (none) | 0 |"]
    records = [r for r in mapping["records"] if "error" not in r]

    def _ref(r: Mapping[str, Any]) -> str:
        return (
            f"`{r['source_dir_slug']}/{Path(r['source_file']).name}` "
            f"({r['memory_kind']} -> {r['target']['tool']}, **{r['action']}**"
            + (f", duplicate of {r['duplicate_of']}" if r.get("duplicate_of") else "")
            + ")"
        )

    out += [
        "",
        "## Flagged stale facts (removed from the proposed body, not copied)",
        "",
        "Each line below is left out of `target.body`; the owner can restore it by editing the "
        "approved mapping. Secret-shaped lines are listed by number and pattern only.",
        "",
    ]
    stale = [r for r in records if r["excluded_lines"]]
    if not stale:
        out.append("(none)")
    for r in stale:
        out.append(f"- {_ref(r)}")
        for f in r["excluded_lines"]:
            text = f.get("text")
            snippet = ""
            if text:
                snippet = " -- `" + (text[:140] + ("..." if len(text) > 140 else "")).replace("`", "'") + "`"
            out.append(f"  - L{f['line']} {f['kind']}: {f['reason']}{snippet}")
    out += ["", "## Record-level warnings (content kept; review before approving)", ""]
    warned = [r for r in records if r["warnings"]]
    if not warned:
        out.append("(none)")
    for r in warned:
        out.append(f"- {_ref(r)}")
        for w in r["warnings"]:
            out.append(f"  - {w['kind']}: {w['reason']}")
    blocked = [r for r in records if r["action"] in ("needs_project", "skip_duplicate", "review_only")]
    out += [
        "",
        "## Not importable as proposed",
        "",
        f"- needs_project: {sum(1 for r in blocked if r['action'] == 'needs_project')} records whose memory dir "
        "has no resolved Meridian project (see the table above; pass `--project SLUG=ID` or retarget them).",
        f"- skip_duplicate: {sum(1 for r in blocked if r['action'] == 'skip_duplicate')}",
        f"- review_only (most content was stale): "
        + (", ".join(_ref(r) for r in blocked if r["action"] == "review_only") or "0"),
        "",
    ]
    return "\n".join(out)


# ---------------------------------------------------------------------------
# --apply (owner-approved mapping only)
# ---------------------------------------------------------------------------


class ApplyError(Exception):
    """The approved mapping is missing, malformed or not approved."""


_APPLY_TOOLS = ("add_note", "pin_decision", "add_workspace_note")


def load_approved_mapping(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Validate an owner-approved mapping; return ``(mapping, approved_records)``.
    Refuses: unreadable/malformed JSON, wrong schema, no top-level approval,
    no approver, or zero approved records."""
    try:
        mapping = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ApplyError(f"cannot read approved mapping {path}: {exc}") from exc
    if not isinstance(mapping, dict) or mapping.get("schema") != SCHEMA:
        raise ApplyError(f"{path} is not a {SCHEMA} mapping")
    approval = mapping.get("approval")
    if not isinstance(approval, dict) or approval.get("approved") is not True:
        raise ApplyError("mapping is not approved: set approval.approved=true after reviewing it")
    if not str(approval.get("approved_by") or "").strip():
        raise ApplyError("approval.approved_by is required")
    records = mapping.get("records")
    if not isinstance(records, list):
        raise ApplyError("mapping has no records list")
    approved = [r for r in records if isinstance(r, dict) and r.get("approved") is True]
    if not approved:
        raise ApplyError("no record has approved=true")
    return mapping, approved


def _validate_target(record: Mapping[str, Any]) -> str | None:
    target = record.get("target")
    if not isinstance(target, dict):
        return "record has no target"
    if record.get("action") == "skip_duplicate":
        return "duplicate record (action skip_duplicate)"
    tool = target.get("tool")
    if tool not in _APPLY_TOOLS:
        return f"unsupported target tool {tool!r}"
    if not str(target.get("title") or "").strip() or not str(target.get("body") or "").strip():
        return "target title/body empty"
    if tool != "add_workspace_note" and not _UUID_RE.fullmatch(str(target.get("project_id") or "")):
        return "target.project_id missing or not a UUID"
    try:
        from .secret_redaction import scan  # noqa: PLC0415

        if scan(str(target.get("title") or "") + "\n" + str(target.get("body") or "")):
            return "target contains secret-shaped text"
    except ImportError:
        pass
    return None


def _dedupe_tag(record: Mapping[str, Any]) -> str:
    return f"{DEDUPE_TAG_PREFIX}{record.get('title_hash') or title_hash(str(record['target']['title']))}"


class MemoryWriter(Protocol):
    def exists(self, record: Mapping[str, Any]) -> bool: ...
    def create(self, record: Mapping[str, Any]) -> Mapping[str, Any]: ...


class HttpMeridianWriter:
    """Writes through a Meridian server's REST API (the same routes the
    dashboard uses). The bearer token, when set, is read from the
    environment and only ever sent as a header."""

    def __init__(self, base_url: str, token: str | None = None, timeout: float = 20.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.timeout = timeout

    def _request(self, method: str, path: str, body: Any = None, query: Mapping[str, str] | None = None) -> Any:
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self._token:
            req.add_header("Authorization", f"Bearer {self._token}")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 - owner-supplied URL
            raw = resp.read()
        return json.loads(raw) if raw else None

    def exists(self, record: Mapping[str, Any]) -> bool:
        target = record["target"]
        tag = _dedupe_tag(record)
        if target["tool"] == "add_note":
            rows = self._request("GET", f"/projects/{target['project_id']}/notes", query={"tag": tag})
            return bool(rows)
        if target["tool"] == "add_workspace_note":
            rows = self._request("GET", "/workspace/notes", query={"tag": tag})
            return bool(rows)
        rows = self._request("GET", f"/projects/{target['project_id']}/decisions-pinned") or []
        return any(str(r.get("title")) == str(target["title"]) for r in rows if isinstance(r, dict))

    def create(self, record: Mapping[str, Any]) -> Mapping[str, Any]:
        target = record["target"]
        tags = str(target.get("tags") or "")
        if target["tool"] == "add_note":
            payload = {"title": target["title"], "body": target["body"], "tags": tags}
            if target.get("kind"):
                payload["kind"] = target["kind"]
            return self._request("POST", f"/projects/{target['project_id']}/notes", payload) or {}
        if target["tool"] == "add_workspace_note":
            return self._request("POST", "/workspace/notes", {"title": target["title"], "body": target["body"], "tags": tags}) or {}
        payload = {
            "title": target["title"],
            "body": target["body"],
            "category": target.get("category") or "PROCESS",
            "priority": target.get("priority") or "normal",
        }
        return self._request("POST", f"/projects/{target['project_id']}/decisions-pinned", payload) or {}


def apply_mapping(records: Iterable[Mapping[str, Any]], writer: MemoryWriter) -> dict[str, list[dict[str, Any]]]:
    """Apply already-approved ``records`` through ``writer``. Idempotent via
    ``writer.exists``. Never raises for a single record; errors are reported."""
    report: dict[str, list[dict[str, Any]]] = {"applied": [], "skipped_existing": [], "rejected": [], "errors": []}
    for record in records:
        rid = str(record.get("record_id"))
        problem = _validate_target(record)
        if problem:
            report["rejected"].append({"record_id": rid, "reason": problem})
            continue
        try:
            if writer.exists(record):
                report["skipped_existing"].append({"record_id": rid})
                continue
            created = writer.create(record)
            report["applied"].append({"record_id": rid, "id": (created or {}).get("id")})
        except (urllib.error.URLError, OSError, ValueError) as exc:
            report["errors"].append({"record_id": rid, "error": f"{exc.__class__.__name__}: {exc}"})
    return report


# ---------------------------------------------------------------------------
# CLI: python -m meridian memory import ...
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="meridian memory", description="Claude Code auto-memory -> Meridian.")
    sub = parser.add_subparsers(dest="command", required=True)
    imp = sub.add_parser("import", help="Dry-run (default) or --apply an owner-approved mapping.")
    imp.add_argument("--out", default="memory_import_mapping.json",
                     help="Where to write the dry-run JSON mapping (default: ./memory_import_mapping.json).")
    imp.add_argument("--summary", default=None, help="Also write a short Markdown summary here.")
    imp.add_argument("--projects-root", default=None,
                     help="Claude Code projects dir (default: $CLAUDE_CONFIG_DIR/projects or ~/.claude/projects).")
    imp.add_argument("--project", action="append", default=[], metavar="SLUG_OR_PATH=PROJECT_ID",
                     help="Pin a memory dir (slug or repo path) to a Meridian project id. Repeatable.")
    imp.add_argument("--stale-index", action="append", default=None, metavar="NAME",
                     help="Index name to flag as deleted (repeatable; replaces the built-in list).")
    imp.add_argument("--apply", default=None, metavar="APPROVED_FILE",
                     help="Apply ONLY the owner-approved records in this mapping file.")
    imp.add_argument("--url", default=None, help="Meridian server for --apply (default: $MERIDIAN_URL or http://localhost:7878).")
    return parser


def cli_main(argv: list[str] | None = None, *, stdout=None, stderr=None, writer: MemoryWriter | None = None) -> int:
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    args = build_parser().parse_args(argv)
    if args.apply:
        try:
            _mapping, approved = load_approved_mapping(Path(args.apply))
        except ApplyError as exc:
            print(f"error: {exc}", file=stderr)
            return 1
        if writer is None:
            url = args.url or os.environ.get("MERIDIAN_URL") or "http://localhost:7878"
            token = os.environ.get("MERIDIAN_API_KEY") or os.environ.get("BEARER_TOKEN") or None
            writer = HttpMeridianWriter(url, token)
        report = apply_mapping(approved, writer)
        stdout.write(json.dumps({k: len(v) for k, v in report.items()}) + "\n")
        for key in ("rejected", "errors"):
            for row in report[key]:
                stdout.write(f"  {key}: {row}\n")
        return 1 if report["errors"] else 0
    try:
        overrides = _parse_overrides(args.project)
    except ValueError as exc:
        print(f"error: {exc}", file=stderr)
        return 2
    stale = tuple(args.stale_index) if args.stale_index else DEFAULT_STALE_INDEX_NAMES
    root = Path(args.projects_root) if args.projects_root else default_projects_root()
    mapping = build_mapping(root, overrides=overrides, stale_index_names=stale)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(mapping, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if args.summary:
        summary_path = Path(args.summary)
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(render_summary_markdown(mapping), encoding="utf-8")
    s = mapping["summary"]
    stdout.write(
        f"[dry-run] {s['records']} memory files in {s['memory_dirs']} dirs -> {out}\n"
        f"  by kind: {s['by_kind']}\n  by action: {s['by_action']}\n"
        f"  excluded lines: {s['excluded_lines_by_flag']}\n"
        "  nothing was written to Meridian; approve records in a copy of the mapping, then --apply it.\n"
    )
    return 0
