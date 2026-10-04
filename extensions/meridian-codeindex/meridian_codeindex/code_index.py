"""Cursor-style local code index (extracted from Meridian, 2b2433ca / 93fce816).

A **local-first** semantic code index modelled on Cursor's published indexing
architecture. Three cooperating layers, all in a single DuckDB *sidecar* file
(no cloud round-trip, no shared table with any other indexer):

1. **tree-sitter semantic chunking** (:class:`CodeChunk`, :func:`chunk_file`).
   Source files are parsed into semantic chunks at **function / class / method**
   boundaries (Python via the stdlib ``ast``, TypeScript/JavaScript via the
   ``tree-sitter`` grammars, when installed), PLUS the *logical un-named blocks*
   that a named-symbols-only index can't see — top-level ``if __name__`` guards,
   bare module-level calls, dict/list literal assignments. Every span of the
   file lands in exactly one chunk (the gaps between named symbols become
   "module"/"top_level" chunks), so a keyword that only appears in a bare call
   at the bottom of a file is still findable.

2. **Merkle tree of file-content hashes** (:class:`MerkleTree`,
   :func:`build_merkle_tree`, :meth:`MerkleTree.diff`). Every file is a leaf
   hashed by content; every directory is an interior node hashing its children's
   hashes; the tree has a single root hash. Between two passes we compare root
   hashes first and only descend into divergent subtrees — an unchanged
   directory's whole subtree is skipped in O(1) by one hash compare. This is
   what makes incremental reindex cheap: :meth:`CodeIndex.reindex` re-chunks
   **only** the files whose leaf hash moved.

3. **Hybrid search over chunks** (:class:`CodeIndex`). DuckDB native **FTS
   (Okapi BM25)** for keyword match — a ``PRAGMA create_fts_index``
   rebuild-with-overwrite index — PLUS an **optional** DuckDB **VSS** (vector
   similarity, HNSW cosine) leg over local Model2Vec embeddings. The vector leg
   is *opt-in / lazy / degrades to keyword-only*: it never loads a model or a
   native extension unless ``MERIDIAN_CODE_INDEX_VECTORS`` is enabled and the
   deps are importable. With vectors off (the default), the index is a
   fully-functional BM25 code searcher.

**Reindex trigger** — :func:`reindex_at_checkpoint` is the natural-lifecycle
entry point (callable from any host application's own lifecycle hooks — e.g. a
file-save event or a task-completion checkpoint). It is *not* a real-time
per-save watchdog: it runs a single incremental Merkle-diff reindex pass and
returns cheaply when nothing changed.

Nothing here raises on a missing grammar, an unreadable file, a missing native
extension, or a missing embedding model — every such case degrades gracefully
(empty chunk list / keyword-only search / skipped file).

This module has **zero dependency on any host application** — no Meridian
imports, no LSP, no Serena, no codebase-memory-mcp. It is a standalone,
installable BM25 code index usable as a library (``from meridian_codeindex
import CodeIndex, search_code_semantic``) or via the ``meridian-codeindex``
CLI (see :mod:`meridian_codeindex.cli`). A caller that wants to layer its own
deployment-specific policy (auth, hosted-vs-local guards, etc.) on top should
do so in its own thin wrapper around :func:`search_code_semantic`.
"""
from __future__ import annotations

import ast
import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Iterable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .vector_index import IndexMetadata

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# What we index. Python + TypeScript/JavaScript are the two languages this
# index understands out of the box.
# ---------------------------------------------------------------------------

_EXT_LANG: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
}

# Directories never worth indexing (vendored / build / VCS / caches). A source
# tree walk prunes these so a node_modules or .git never bloats the index.
_SKIP_DIRS: frozenset[str] = frozenset({
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".venv", "venv", "env", ".pixi", "dist",
    "build", ".next", ".turbo", "coverage", ".tox", "site-packages",
    ".claude", "htmlcov", ".idea", ".vscode",
})

# Cap the content stored per chunk so a pathological generated file can't blow
# the index up; plenty for BM25 term coverage + embedding.
_MAX_CHUNK_CHARS = 20_000


def detect_language(file_path: str) -> str | None:
    """Language key for a path's extension, or ``None`` if we don't index it."""
    _, ext = os.path.splitext((file_path or "").lower())
    return _EXT_LANG.get(ext)


def is_indexable(file_path: str) -> bool:
    """Whether a path is a source file this index chunks (by extension)."""
    return detect_language(file_path) is not None


# ===========================================================================
# 1. tree-sitter / ast semantic chunking
# ===========================================================================

@dataclass
class CodeChunk:
    """One semantic chunk of a source file.

    ``kind`` is the semantic category — ``function`` / ``class`` / ``method``
    for named symbols, or one of the *un-named-block* kinds
    (``module`` / ``top_level`` / ``block``) that fill the gap a
    named-symbols-only index leaves. ``name`` is the symbol name for named
    chunks, else a synthetic label (e.g. ``"<module:1>"``).

    Line numbers are **1-based inclusive**. ``chunk_id`` is deterministic
    (path + span + content hash) so the same chunk keeps a stable identity
    across reindex passes.
    """

    path: str
    language: str
    kind: str
    name: str
    line_start: int
    line_end: int
    content: str
    content_hash: str = ""
    chunk_id: str = ""

    def __post_init__(self) -> None:
        if not self.content_hash:
            self.content_hash = hashlib.sha256(
                self.content.encode("utf-8", "replace")
            ).hexdigest()
        if not self.chunk_id:
            raw = f"{self.path}:{self.line_start}-{self.line_end}:{self.content_hash}"
            self.chunk_id = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _slice_lines(lines: list[str], start_1: int, end_1: int) -> str:
    """Join source ``lines`` (0-based list) for the 1-based inclusive span."""
    start = max(1, start_1)
    end = min(len(lines), end_1)
    if end < start:
        return ""
    return "\n".join(lines[start - 1:end])[:_MAX_CHUNK_CHARS]


# -- Python (stdlib ast, exact — no third-party dep) -------------------------

def _python_symbol_spans(source: str) -> list[tuple[str, str, int, int]]:
    """``[(kind, name, start, end)]`` for top-level defs + methods (Python).

    Covers ``function`` / ``async function`` / ``class`` and one level of
    methods (``Class.method``). Decorator lines are folded into the span so a
    decorator can't fall into a neighbouring chunk. Returns ``[]`` on a syntax
    error (caller then treats the whole file as one module chunk).
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []

    def _span(node: ast.AST) -> tuple[int, int]:
        start = getattr(node, "lineno", 1)
        end = getattr(node, "end_lineno", None) or start
        for dec in getattr(node, "decorator_list", []) or []:
            start = min(start, getattr(dec, "lineno", start))
        return start, end

    out: list[tuple[str, str, int, int]] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            cs, ce = _span(node)
            out.append(("class", node.name, cs, ce))
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    ms, me = _span(child)
                    out.append(("method", f"{node.name}.{child.name}", ms, me))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fs, fe = _span(node)
            out.append(("function", node.name, fs, fe))
    return out


# -- tree-sitter (TypeScript / JavaScript) -----------------------------------

# Named-definition node types per grammar → chunk kind.
_TS_DEF_TYPES: dict[str, dict[str, str]] = {
    "javascript": {
        "function_declaration": "function",
        "generator_function_declaration": "function",
        "class_declaration": "class",
        "method_definition": "method",
    },
    "typescript": {
        "function_declaration": "function",
        "generator_function_declaration": "function",
        "class_declaration": "class",
        "method_definition": "method",
        "interface_declaration": "interface",
        "abstract_class_declaration": "class",
        "enum_declaration": "enum",
    },
}
_TS_DEF_TYPES["tsx"] = _TS_DEF_TYPES["typescript"]

_PARSER_CACHE: dict[str, Any] = {}


def _get_ts_parser(language: str) -> Any:
    """Lazily build + cache a tree-sitter Parser, or ``None`` if unavailable.

    Only the two grammars this index needs (javascript, typescript/tsx) are
    wired; anything else — or a missing tree-sitter — returns ``None`` so the
    caller falls back to a single whole-file chunk instead of crashing.
    """
    if language in _PARSER_CACHE:
        return _PARSER_CACHE[language]
    parser = None
    try:
        from tree_sitter import Language, Parser  # noqa: PLC0415
        if language == "javascript":
            import tree_sitter_javascript as ts_mod  # noqa: PLC0415
            lang_capsule = ts_mod.language()
        elif language == "typescript":
            import tree_sitter_typescript as ts_mod  # noqa: PLC0415
            lang_capsule = ts_mod.language_typescript()
        elif language == "tsx":
            import tree_sitter_typescript as ts_mod  # noqa: PLC0415
            lang_capsule = ts_mod.language_tsx()
        else:
            lang_capsule = None
        if lang_capsule is not None:
            parser = Parser(Language(lang_capsule))
    except Exception:  # noqa: BLE001 — missing grammar → whole-file fallback
        parser = None
    _PARSER_CACHE[language] = parser
    return parser


def _ts_node_name(node: Any, source_bytes: bytes) -> str | None:
    """Best-effort symbol name for a tree-sitter definition node."""
    name_node = node.child_by_field_name("name")
    if name_node is None:
        for child in node.named_children:
            if "identifier" in child.type:
                name_node = child
                break
    if name_node is None:
        return None
    return source_bytes[name_node.start_byte:name_node.end_byte].decode(
        "utf-8", "replace"
    )


def _ts_symbol_spans(language: str, source: str) -> list[tuple[str, str, int, int]]:
    """``[(kind, name, start, end)]`` for named TS/JS defs, or ``[]``.

    Only TOP-LEVEL classes/functions and one level of methods inside a class
    are emitted (a method's span nests inside its class's span; the chunker
    resolves the overlap by keeping the outermost owner — see :func:`chunk_file`).
    Returns ``[]`` when tree-sitter/grammar is unavailable so the file becomes a
    single module chunk.
    """
    parser = _get_ts_parser(language)
    def_types = _TS_DEF_TYPES.get(language, {})
    if parser is None or not def_types:
        return []
    source_bytes = source.encode("utf-8")
    try:
        tree = parser.parse(source_bytes)
    except Exception:  # noqa: BLE001
        return []
    out: list[tuple[str, str, int, int]] = []

    def _walk(node: Any, class_name: str | None) -> None:
        sym_type = def_types.get(node.type)
        emitted_here: str | None = None
        if sym_type:
            name = _ts_node_name(node, source_bytes)
            if name:
                if sym_type == "method" and class_name:
                    name = f"{class_name}.{name}"
                out.append((
                    sym_type, name,
                    node.start_point[0] + 1, node.end_point[0] + 1,
                ))
                if sym_type == "class":
                    emitted_here = name
        next_class = emitted_here or class_name
        for child in node.children:
            _walk(child, next_class)

    _walk(tree.root_node, None)
    return out


def _symbol_spans(language: str, source: str) -> list[tuple[str, str, int, int]]:
    """Dispatch to the ast (Python) or tree-sitter (TS/JS) span extractor."""
    if language == "python":
        return _python_symbol_spans(source)
    return _ts_symbol_spans(language, source)


def _gap_kind(language: str) -> str:
    """Chunk kind for the un-named spans between named symbols."""
    return "module"


def chunk_file(file_path: str, source: str) -> list[CodeChunk]:
    """Parse ``source`` into ordered, non-overlapping :class:`CodeChunk` s.

    Named symbols (function/class/method/interface/enum) become their own
    chunks; the source *between* named symbols — top-level statements, bare
    calls, ``if __name__`` guards, dict/list literals, imports — is grouped into
    ``module`` chunks so every non-blank line of the file lives in exactly one
    chunk. This is coverage a named-symbols-only index lacks.

    Overlap handling: a method's span nests inside its class span. We keep the
    OUTERMOST owner (the class) as one chunk AND emit the method as its own
    finer chunk, but we never let the un-named-gap logic double-count the lines a
    named chunk already owns. Returns a single whole-file ``module`` chunk when
    the language has no parser / the file doesn't parse, and ``[]`` for an
    unsupported extension or empty source.
    """
    language = detect_language(file_path)
    if language is None or not source.strip():
        return []
    lines = source.splitlines()
    n_lines = len(lines)

    spans = _symbol_spans(language, source)
    chunks: list[CodeChunk] = []

    # Emit every named symbol as its own chunk.
    for kind, name, start, end in spans:
        content = _slice_lines(lines, start, end)
        if not content.strip():
            continue
        chunks.append(CodeChunk(
            path=file_path, language=language, kind=kind, name=name,
            line_start=start, line_end=min(end, n_lines), content=content,
        ))

    # Compute the set of lines covered by the TOP-LEVEL named symbols only
    # (not nested methods — their lines are already inside the class span). We
    # take the union of the widest spans so the "gap" chunks are the genuinely
    # un-named top-level regions.
    top_spans = _top_level_spans(spans)
    covered = _covered_lines(top_spans, n_lines)

    # Group the uncovered lines into contiguous "module" gap chunks.
    for gs, ge in _contiguous_gaps(covered, n_lines):
        content = _slice_lines(lines, gs, ge)
        if not content.strip():
            continue
        chunks.append(CodeChunk(
            path=file_path, language=language, kind=_gap_kind(language),
            name=f"<module:{gs}>", line_start=gs, line_end=ge, content=content,
        ))

    if not chunks:
        # Nothing parsed (no symbols, all-comment file, or parser missing) —
        # index the whole file as one module chunk so it's never invisible.
        content = source[:_MAX_CHUNK_CHARS]
        chunks.append(CodeChunk(
            path=file_path, language=language, kind="module",
            name="<module:1>", line_start=1, line_end=n_lines, content=content,
        ))

    chunks.sort(key=lambda c: (c.line_start, c.line_end))
    return chunks


def _top_level_spans(
    spans: list[tuple[str, str, int, int]]
) -> list[tuple[int, int]]:
    """The (start, end) of the OUTERMOST named symbols only.

    A method span is discarded when it is fully enclosed by a class span; the
    remaining spans are the top-level owners whose lines the gap logic must not
    re-emit.
    """
    raw = sorted(((s, e) for _k, _n, s, e in spans), key=lambda p: (p[0], -p[1]))
    top: list[tuple[int, int]] = []
    for s, e in raw:
        if top and s >= top[-1][0] and e <= top[-1][1]:
            continue  # nested inside the previous top-level span
        top.append((s, e))
    return top


def _covered_lines(top_spans: list[tuple[int, int]], n_lines: int) -> set[int]:
    """Set of 1-based line numbers owned by a top-level named symbol."""
    covered: set[int] = set()
    for s, e in top_spans:
        for ln in range(max(1, s), min(n_lines, e) + 1):
            covered.add(ln)
    return covered


def _contiguous_gaps(
    covered: set[int], n_lines: int
) -> list[tuple[int, int]]:
    """Contiguous runs of uncovered 1-based line numbers as (start, end) spans."""
    gaps: list[tuple[int, int]] = []
    run_start: int | None = None
    for ln in range(1, n_lines + 1):
        if ln in covered:
            if run_start is not None:
                gaps.append((run_start, ln - 1))
                run_start = None
        elif run_start is None:
            run_start = ln
    if run_start is not None:
        gaps.append((run_start, n_lines))
    return gaps


# ===========================================================================
# 2. Merkle tree of file-content hashes
# ===========================================================================

@dataclass
class MerkleNode:
    """One node of the content Merkle tree.

    A **leaf** (``is_file=True``) hashes a file's bytes; an **interior** node
    hashes the sorted ``(name, child_hash)`` pairs of its children. ``rel_path``
    is POSIX-normalised and relative to the tree root so the structure is
    portable across OSes.
    """

    rel_path: str
    is_file: bool
    hash: str
    children: dict[str, "MerkleNode"] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rel_path": self.rel_path,
            "is_file": self.is_file,
            "hash": self.hash,
            "children": {k: v.to_dict() for k, v in self.children.items()},
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MerkleNode":
        return cls(
            rel_path=d["rel_path"],
            is_file=bool(d["is_file"]),
            hash=d["hash"],
            children={
                k: cls.from_dict(v) for k, v in (d.get("children") or {}).items()
            },
        )


@dataclass
class MerkleTree:
    """A Merkle tree over a source scope and its indexable files."""

    root: MerkleNode
    scan_info: dict[str, Any] | None = None

    @property
    def root_hash(self) -> str:
        return self.root.hash

    def files(self) -> dict[str, str]:
        """Return relative paths and content hashes for all leaves."""
        out: dict[str, str] = {}

        def _walk(node: MerkleNode) -> None:
            if node.is_file:
                out[node.rel_path] = node.hash
            else:
                for child in node.children.values():
                    _walk(child)

        _walk(self.root)
        return out

    def diff(self, previous: "MerkleTree | None") -> "MerkleDiff":
        """Return files added, modified or removed against a prior tree."""
        if previous is not None:
            current_scope = (self.scan_info or {}).get("scope_id")
            previous_scope = (previous.scan_info or {}).get("scope_id")
            if current_scope and previous_scope and current_scope != previous_scope:
                previous = None
        if previous is None:
            return MerkleDiff(added=sorted(self.files()), modified=[], removed=[])
        if self.root_hash == previous.root_hash:
            return MerkleDiff(added=[], modified=[], removed=[])

        added: list[str] = []
        modified: list[str] = []
        removed: list[str] = []

        def _descend(cur: MerkleNode | None, old: MerkleNode | None) -> None:
            if cur is not None and old is not None and cur.hash == old.hash:
                return
            if cur is not None and cur.is_file:
                if old is None or not old.is_file:
                    added.append(cur.rel_path)
                elif old.hash != cur.hash:
                    modified.append(cur.rel_path)
                return
            if cur is None and old is not None and old.is_file:
                removed.append(old.rel_path)
                return
            cur_children = cur.children if cur is not None else {}
            old_children = old.children if old is not None else {}
            for name in set(cur_children) | set(old_children):
                _descend(cur_children.get(name), old_children.get(name))

        _descend(self.root, previous.root)
        return MerkleDiff(
            added=sorted(added),
            modified=sorted(modified),
            removed=sorted(removed),
        )

    def to_json(self) -> str:
        return json.dumps(
            {"root": self.root.to_dict(), "scan_info": self.scan_info or {}},
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, blob: str) -> "MerkleTree":
        payload = json.loads(blob)
        if isinstance(payload, dict) and isinstance(payload.get("root"), dict):
            return cls(
                root=MerkleNode.from_dict(payload["root"]),
                scan_info=payload.get("scan_info") or None,
            )
        return cls(root=MerkleNode.from_dict(payload))


@dataclass
class MerkleDiff:
    """The set-difference between two Merkle passes."""

    added: list[str]
    modified: list[str]
    removed: list[str]

    @property
    def changed_files(self) -> list[str]:
        """Files that need (re)chunking — added + modified (not removed)."""
        return sorted(set(self.added) | set(self.modified))

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.modified or self.removed)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _hash_file_bytes(path: str) -> str | None:
    """SHA-256 of a file's bytes, streamed. ``None`` if unreadable."""
    try:
        h = hashlib.sha256()
        with open(path, "rb", buffering=0) as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None



def _run_bounded_nul_list(
    command: list[str],
    *,
    max_entries: int,
    max_bytes: int,
    deadline: float,
) -> tuple[list[bytes], str | None]:
    """Read a NUL-delimited subprocess stream with strict count/byte/time bounds."""
    import queue
    import subprocess
    import threading

    if max_entries < 1 or max_bytes < 1:
        return [], "file_list_budget_exceeded"
    try:
        proc = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
    except OSError:
        return [], "tracked_file_list_unavailable"

    chunks: queue.Queue[bytes | object] = queue.Queue(maxsize=4)
    finished = object()
    reader_errors: list[OSError] = []

    def publish(value: bytes | object) -> None:
        while True:
            try:
                chunks.put(value, timeout=0.05)
                return
            except queue.Full:
                continue

    def read_stdout() -> None:
        try:
            if proc.stdout is not None:
                while True:
                    chunk = proc.stdout.read(65536)
                    if not chunk:
                        break
                    publish(chunk)
        except OSError as exc:
            reader_errors.append(exc)
        finally:
            publish(finished)

    reader = threading.Thread(target=read_stdout, name="meridian-git-path-reader", daemon=True)
    reader.start()
    paths: list[bytes] = []
    buffered = bytearray()
    total_bytes = 0
    reason: str | None = None

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            reason = "time_budget_exceeded"
            break
        try:
            chunk = chunks.get(timeout=min(0.1, remaining))
        except queue.Empty:
            continue
        if chunk is finished:
            break
        assert isinstance(chunk, bytes)
        if total_bytes + len(chunk) > max_bytes:
            reason = "file_list_byte_budget_exceeded"
            break
        total_bytes += len(chunk)
        buffered.extend(chunk)
        while True:
            try:
                end = buffered.index(0)
            except ValueError:
                break
            path = bytes(buffered[:end])
            del buffered[: end + 1]
            if not path:
                continue
            if len(paths) >= max_entries:
                reason = "file_list_entry_budget_exceeded"
                break
            paths.append(path)
        if reason:
            break

    if reason is None:
        try:
            remaining = max(0.001, deadline - time.monotonic())
            return_code = proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            reason = "time_budget_exceeded"
        else:
            if reader_errors:
                reason = "tracked_file_list_unavailable"
            elif return_code != 0:
                reason = "tracked_file_list_unavailable"
            elif buffered:
                reason = "tracked_file_list_malformed"

    if reason is not None and proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass
    if reason is not None:
        # Drain while the reader exits so a full queue cannot strand its thread.
        while reader.is_alive():
            try:
                chunks.get(timeout=0.05)
            except queue.Empty:
                pass
    reader.join(timeout=0.5)
    try:
        proc.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
        proc.wait()
    if proc.stdout is not None:
        try:
            proc.stdout.close()
        except OSError:
            pass
    return paths, reason

def _iter_indexable_files(
    root_dir: str,
    *,
    scan_info: dict[str, Any] | None = None,
    allow_broad_root: bool = False,
    max_files: int = 20_000,
    max_file_bytes: int = 4_000_000,
    max_total_bytes: int = 512_000_000,
    max_seconds: float = 30.0,
) -> Iterable[str]:
    """Yield a bounded, deterministic view of one canonical source root.

    Git scope enumeration itself is bounded in elapsed time, path count, and
    bytes before paths are materialized. Git projects default to tracked files,
    then a bounded check-ignore pass removes ignored tracked source. Small
    non-Git projects use a bounded walk. Broad non-project roots are refused
    unless explicitly opted in. scan_info records truthful completeness.
    """
    import subprocess

    info = scan_info if scan_info is not None else {}
    root = normalize_root_dir(root_dir)
    excluded = _SKIP_DIRS | {
        ".codex", ".serena", ".cache", "cache", "caches",
        "OneDrive", "Dropbox", "Google Drive", "iCloud Drive", "iCloudDrive",
    }
    excluded_folded = {name.casefold() for name in excluded}
    scope_id = hashlib.sha256(
        os.path.normcase(root).encode("utf-8", "surrogatepass")
    ).hexdigest()
    info.update({
        "canonical_root": root,
        "scope_id": scope_id,
        "repo_root": None,
        "is_git_worktree": False,
        "git_common_dir": None,
        "scope_mode": "bounded_walk",
        "scan_complete": True,
        "scan_reason": None,
        "indexed_file_count": 0,
        "listed_path_count": 0,
        "total_bytes": 0,
        "excluded_paths": sorted(excluded, key=str.casefold),
        "allow_broad_root": bool(allow_broad_root),
    })
    started = time.monotonic()
    if not root or not os.path.isdir(root):
        info.update(
            scan_complete=False, scan_reason="root_missing", scope_mode="unavailable"
        )
        return

    def mark_partial(reason: str) -> None:
        info["scan_complete"] = False
        if info["scan_reason"] is None:
            info["scan_reason"] = reason

    def inside_root(path: str) -> bool:
        try:
            return os.path.commonpath([root, os.path.realpath(path)]) == root
        except (OSError, ValueError):
            return False

    probe_timeout = max(0.25, min(3.0, max_seconds))
    try:
        git_root_result = subprocess.run(
            ["git", "-C", root, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=probe_timeout,
        )
    except subprocess.TimeoutExpired:
        mark_partial("git_scope_probe_timed_out")
        info["scope_mode"] = "unavailable"
        return

    if git_root_result.returncode == 0 and git_root_result.stdout.strip():
        repo_root = normalize_root_dir(git_root_result.stdout.strip())
        info["repo_root"] = repo_root
        git_marker = os.path.join(repo_root, ".git")
        info["is_git_worktree"] = os.path.isfile(git_marker)
        try:
            common_result = subprocess.run(
                ["git", "-C", root, "rev-parse", "--git-common-dir"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=max(0.25, min(3.0, max_seconds)),
            )
        except subprocess.TimeoutExpired:
            mark_partial("git_identity_probe_timed_out")
            info["scope_mode"] = "unavailable"
            return
        if common_result.returncode != 0 or not common_result.stdout.strip():
            mark_partial("git_identity_probe_failed")
            info["scope_mode"] = "unavailable"
            return
        common = common_result.stdout.strip()
        if not os.path.isabs(common):
            common = os.path.join(repo_root, common)
        info["git_common_dir"] = os.path.realpath(common)
        info["scope_mode"] = "git_tracked"

        # A default proportional cap prevents a huge non-source file list from
        # bypassing max_files before source filtering. The byte cap bounds both
        # the subprocess reader and the materialized Python path list.
        max_listed_paths = max(1024, min(100_000, max_files * 8))
        max_listed_bytes = max(
            8 * 1024 * 1024, min(64 * 1024 * 1024, max_listed_paths * 256)
        )
        deadline = started + max_seconds
        tracked_bytes, list_error = _run_bounded_nul_list(
            [
                "git", "-C", root, "ls-files", "--cached", "--full-name", "-z",
                "--", ".",
            ],
            max_entries=max_listed_paths,
            max_bytes=max_listed_bytes,
            deadline=deadline,
        )
        info["listed_path_count"] = len(tracked_bytes)
        if list_error:
            mark_partial(list_error)
            return
        tracked_paths = sorted({
            os.fsdecode(part).replace("\\", "/") for part in tracked_bytes if part
        })

        candidate_paths: list[str] = []
        for rel_repo_path in tracked_paths:
            parts = rel_repo_path.split("/")
            if any(part.casefold() in excluded_folded for part in parts):
                continue
            if not is_indexable(rel_repo_path):
                continue
            abs_path = os.path.join(repo_root, *parts)
            if inside_root(abs_path) and os.path.isfile(abs_path):
                candidate_paths.append(rel_repo_path)

        ignored_paths: set[str] = set()
        if candidate_paths:
            remaining = max_seconds - (time.monotonic() - started)
            if remaining <= 0:
                mark_partial("time_budget_exceeded")
                return
            try:
                ignored_result = subprocess.run(
                    [
                        "git", "-C", repo_root, "check-ignore", "--no-index",
                        "--stdin", "-z",
                    ],
                    input=b"\0".join(os.fsencode(path) for path in candidate_paths) + b"\0",
                    capture_output=True,
                    check=False,
                    timeout=remaining,
                )
            except subprocess.TimeoutExpired:
                mark_partial("time_budget_exceeded")
                return
            if ignored_result.returncode not in (0, 1):
                mark_partial("ignored_path_filter_unavailable")
                return
            ignored_paths = {
                os.fsdecode(part).replace("\\", "/")
                for part in ignored_result.stdout.split(b"\0") if part
            }

        yielded = 0
        for rel_repo_path in candidate_paths:
            if time.monotonic() - started > max_seconds:
                mark_partial("time_budget_exceeded")
                break
            if yielded >= max_files:
                mark_partial("file_count_budget_exceeded")
                break
            if rel_repo_path in ignored_paths:
                continue
            parts = rel_repo_path.split("/")
            abs_path = os.path.join(repo_root, *parts)
            try:
                size = os.path.getsize(abs_path)
            except OSError:
                mark_partial("file_stat_failed")
                continue
            if size > max_file_bytes:
                mark_partial("file_size_budget_exceeded")
                continue
            if int(info["total_bytes"]) + size > max_total_bytes:
                mark_partial("total_size_budget_exceeded")
                break
            rel_to_root = os.path.relpath(abs_path, root).replace(os.sep, "/")
            if rel_to_root in (".", "..") or rel_to_root.startswith("../"):
                continue
            yielded += 1
            info["indexed_file_count"] = yielded
            info["total_bytes"] = int(info["total_bytes"]) + size
            yield abs_path
        return

    if os.path.exists(os.path.join(root, ".git")):
        mark_partial("git_identity_unavailable")
        info["scope_mode"] = "unavailable"
        return

    project_markers = (
        "pyproject.toml", "package.json", "Cargo.toml", "go.mod",
        "pom.xml", "build.gradle", "requirements.txt", "setup.py",
        "CMakeLists.txt", "Makefile",
    )
    has_project_marker = any(os.path.isfile(os.path.join(root, name)) for name in project_markers)
    home = normalize_root_dir(os.path.expanduser("~"))
    drive, _ = os.path.splitdrive(root)
    drive_root = drive + os.sep if drive else ""
    broad_roots = {
        home,
        normalize_root_dir(os.path.dirname(home)),
        normalize_root_dir(drive_root),
    }
    top_entries = []
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                top_entries.append(entry.name)
                if len(top_entries) > 128:
                    break
    except OSError:
        mark_partial("root_listing_failed")
        info["scope_mode"] = "unavailable"
        return
    if not allow_broad_root and not has_project_marker and (
        root in broad_roots or len(top_entries) > 128
    ):
        mark_partial("broad_root_refused")
        info["scope_mode"] = "refused_broad_root"
        return

    yielded = 0
    for current, dirs, files in os.walk(root, followlinks=False):
        if time.monotonic() - started > max_seconds:
            mark_partial("time_budget_exceeded")
            break
        dirs[:] = sorted(
            d for d in dirs
            if d.casefold() not in excluded_folded
            and not os.path.islink(os.path.join(current, d))
        )
        for filename in sorted(files):
            if not is_indexable(filename):
                continue
            if time.monotonic() - started > max_seconds:
                mark_partial("time_budget_exceeded")
                break
            if yielded >= max_files:
                mark_partial("file_count_budget_exceeded")
                break
            abs_path = os.path.join(current, filename)
            if not inside_root(abs_path):
                continue
            try:
                size = os.path.getsize(abs_path)
            except OSError:
                mark_partial("file_stat_failed")
                continue
            if size > max_file_bytes:
                mark_partial("file_size_budget_exceeded")
                continue
            if int(info["total_bytes"]) + size > max_total_bytes:
                mark_partial("total_size_budget_exceeded")
                break
            yielded += 1
            info["indexed_file_count"] = yielded
            info["total_bytes"] = int(info["total_bytes"]) + size
            yield abs_path
        if not info["scan_complete"]:
            break


def build_merkle_tree(
    root_dir: str,
    *,
    hasher: Callable[[str], str | None] = _hash_file_bytes,
    allow_broad_root: bool = False,
    max_files: int = 20_000,
    max_file_bytes: int = 4_000_000,
    max_total_bytes: int = 512_000_000,
    max_seconds: float = 30.0,
) -> MerkleTree:
    """Build a content tree plus explicit scope and completeness metadata."""
    root_dir = normalize_root_dir(root_dir)
    scan_info: dict[str, Any] = {}
    file_hashes: dict[str, str] = {}
    unreadable = 0
    for abs_path in _iter_indexable_files(
        root_dir,
        scan_info=scan_info,
        allow_broad_root=allow_broad_root,
        max_files=max_files,
        max_file_bytes=max_file_bytes,
        max_total_bytes=max_total_bytes,
        max_seconds=max_seconds,
    ):
        rel = os.path.relpath(abs_path, root_dir).replace(os.sep, "/")
        try:
            digest = hasher(abs_path)
        except Exception:  # noqa: BLE001 — unreadable hashes must fail closed
            digest = None
        if digest is None:
            unreadable += 1
            scan_info["scan_complete"] = False
            if scan_info.get("scan_reason") is None:
                scan_info["scan_reason"] = "file_hash_failed"
            continue
        file_hashes[rel] = digest

    if unreadable:
        scan_info["unreadable_file_count"] = unreadable
    root = MerkleNode(rel_path="", is_file=False, hash="")
    for rel, content_hash in sorted(file_hashes.items()):
        segments = rel.split("/")
        node = root
        for i, segment in enumerate(segments):
            is_last = i == len(segments) - 1
            child = node.children.get(segment)
            if child is None:
                child_rel = "/".join(segments[: i + 1])
                child = MerkleNode(
                    rel_path=child_rel,
                    is_file=is_last,
                    hash=content_hash if is_last else "",
                )
                node.children[segment] = child
            node = child
    _recompute_hashes(root)
    return MerkleTree(root=root, scan_info=scan_info)


def _recompute_hashes(node: MerkleNode) -> str:
    """Post-order: an interior node's hash = hash of its children's (name,hash)."""
    if node.is_file:
        return node.hash
    h = hashlib.sha256()
    for name in sorted(node.children):
        child = node.children[name]
        child_hash = _recompute_hashes(child)
        h.update(name.encode("utf-8"))
        h.update(b"\x00")
        h.update(child_hash.encode("utf-8"))
        h.update(b"\x00")
    node.hash = h.hexdigest()
    return node.hash


# ===========================================================================
# 3 + 5. Hybrid BM25 (+ optional VSS) search over chunks — DuckDB sidecar
# ===========================================================================

_ENV_VECTORS = "MERIDIAN_CODE_INDEX_VECTORS"
_EMBED_MODEL_NAME = "minishlab/potion-base-8M"
_TRUTHY = {"1", "true", "yes", "on", "y", "t"}


def _vectors_enabled() -> bool:
    """Whether the optional vector (VSS + embedding) leg is switched on.

    OFF by default — the base index is BM25-only, so nothing loads a native
    extension or an embedding model unless the caller opts in with
    ``MERIDIAN_CODE_INDEX_VECTORS=1``. Read fresh so tests can toggle it via
    ``os.environ``.
    """
    return os.environ.get(_ENV_VECTORS, "").strip().lower() in _TRUTHY


def _model2vec_version() -> str | None:
    """Best-effort ``model2vec`` package version, or ``None`` if unimportable.

    Used as the "embedding version" half of :meth:`CodeIndex.describe_vector_index`
    (e1475682) — ``_EMBED_MODEL_NAME`` alone identifies *which* pretrained
    model, this identifies *which build of the encoder* produced the
    vectors, matching ``IndexMetadata.embedding_version``'s intent.
    """
    try:
        import model2vec  # noqa: PLC0415

        return getattr(model2vec, "__version__", None)
    except Exception:  # noqa: BLE001
        return None


class _Embedder:
    """Lazy local Model2Vec embedder.

    Never imports ``model2vec`` at construction; loads the static model on first
    real use and caches it. Returns ``None`` from :meth:`embed` whenever the
    vector leg is disabled, the package is missing, or the load/encode fails, so
    every caller degrades to keyword-only. Not thread-safe by design — the
    :class:`CodeIndex` lock serialises all access.
    """

    def __init__(self, model_name: str = _EMBED_MODEL_NAME) -> None:
        self._model_name = model_name
        self._model: Any = None
        self._import_ok: bool | None = None
        self._dim: int | None = None

    @property
    def model_name(self) -> str:
        """The embedding model identifier this embedder would use.

        e631d54f — a stable, tracked "embedding model/version" marker,
        independent of whether the model has actually been loaded. Compared
        against the model name persisted alongside the LAST successfully
        built vector index (:class:`CodeIndex`'s ``code_index_meta`` row) to
        detect a model upgrade even when no source file changed — see
        :meth:`CodeIndex.reindex`'s deterministic-invalidation branch.
        """
        return self._model_name

    def available(self) -> bool:
        """True only when the vector leg is enabled AND model2vec is importable."""
        if not _vectors_enabled():
            return False
        if self._import_ok is None:
            try:
                import model2vec  # noqa: F401,PLC0415

                self._import_ok = True
            except Exception:  # noqa: BLE001
                self._import_ok = False
        return bool(self._import_ok)

    def _ensure_model(self) -> Any:
        if not self.available():
            return None
        if self._model is not None:
            return self._model
        try:
            from model2vec import StaticModel  # noqa: PLC0415

            self._model = StaticModel.from_pretrained(self._model_name)
        except Exception:  # noqa: BLE001 — load failure → keyword-only
            _log.warning("code_index: embed model load failed", exc_info=True)
            self._model = None
        return self._model

    @property
    def dim(self) -> int | None:
        return self._dim

    def embed(self, texts: list[str]) -> Any:
        """Embed ``texts`` → ``list[list[float]]``, or ``None`` if unavailable."""
        if not texts:
            return None
        model = self._ensure_model()
        if model is None:
            return None
        try:
            import numpy as np  # noqa: PLC0415

            capped = [(t or "")[:_MAX_CHUNK_CHARS] for t in texts]
            vecs = np.asarray(model.encode(capped), dtype="float32")
            if vecs.ndim != 2:
                return None
            self._dim = int(vecs.shape[1])
            return [row.tolist() for row in vecs]
        except Exception:  # noqa: BLE001 — encode failure → keyword-only
            _log.warning("code_index: embed failed", exc_info=True)
            return None


# ---------------------------------------------------------------------------
# Explicit embedding-freshness / convergence state (item e631d54f, follow-up
# to the outputs-side answer in 6af1518d).
# ---------------------------------------------------------------------------
#
# CodeIndex.reindex() is a single synchronous full pass over the Merkle diff
# (never deadline-bound / resumable the way OutputsFtsIndex's walk is), so a
# BM25/keyword result from this index is either fully current (the last
# reindex() call completed) or reflects whatever the last successful pass
# saw — there is no "partial keyword walk" state to track here. The genuine
# staleness risk is narrower and specific to the OPTIONAL vector leg: an
# embedding model upgrade, a load/encode failure, or chunks written by a
# targeted registration whose vectors haven't been (re)computed yet can all
# leave the vector leg silently behind the keyword leg. This dataclass makes
# that risk explicit instead of leaving a caller to infer it from
# ``_vss_ready`` alone.

@dataclass(frozen=True)
class IndexConvergenceState:
    """A structured freshness and scope snapshot for one code index."""

    root_dir: str
    source_fingerprint: str | None
    index_revision: int
    embedding_model: str | None
    configured_embedding_model: str | None
    vectors_enabled: bool
    vectors_ready: bool
    total_chunks: int
    pending_embedding_count: int
    last_checkpoint_at: float | None
    degraded: bool
    converged: bool
    canonical_root: str | None = None
    scope_id: str | None = None
    is_git_worktree: bool | None = None
    git_common_dir: str | None = None
    scope_mode: str | None = None
    scan_complete: bool = False
    scan_reason: str | None = None
    indexed_file_count: int = 0
    total_bytes: int = 0
    excluded_paths: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CodeIndex:
    """The local code index — Merkle-driven incremental chunk store + hybrid search.

    Owns a single DuckDB *sidecar* connection (``:memory:`` by default; a file
    path persists the chunk table + Merkle tree across process restarts). The
    ``code_chunks`` table holds one row per :class:`CodeChunk`; a one-row
    ``code_index_meta`` table persists the serialized Merkle tree so an
    incremental :meth:`reindex` can diff against the last pass.

    Search is hybrid: :meth:`search` runs BM25 over an FTS index rebuilt with
    ``overwrite`` on every reindex (the DuckDB FTS index does not track source
    changes), and — when the vector leg is enabled and the embedding model
    loads — fuses it with a DuckDB VSS (HNSW cosine) nearest-neighbour query via
    Reciprocal Rank Fusion. With vectors off, ``search`` is pure BM25.

    ``connection`` / ``embedder`` / ``hasher`` are injectable for tests.
    Thread-safe: every DB op holds an internal ``RLock``.
    """

    _COLUMNS = (
        "chunk_id", "path", "language", "kind", "name",
        "line_start", "line_end", "content", "content_hash",
    )

    def __init__(
        self,
        root_dir: str,
        *,
        db_path: str = ":memory:",
        connection: Any = None,
        embedder: _Embedder | None = None,
        hasher: Callable[[str], str | None] = _hash_file_bytes,
        allow_broad_root: bool = False,
    ) -> None:
        canonical_root = normalize_root_dir(root_dir)
        self.root_dir = canonical_root or os.path.realpath(os.path.abspath(root_dir))
        scope_id = hashlib.sha256(
            os.path.normcase(self.root_dir).encode("utf-8", "surrogatepass")
        ).hexdigest()
        git_common_dir = None
        is_git_worktree = False
        git_marker = os.path.join(self.root_dir, ".git")
        if os.path.isdir(git_marker):
            git_common_dir = os.path.realpath(git_marker)
        elif os.path.isfile(git_marker):
            try:
                with open(git_marker, "r", encoding="utf-8", errors="replace") as stream:
                    first_line = stream.readline().strip()
                if first_line.lower().startswith("gitdir:"):
                    git_dir = first_line.split(":", 1)[1].strip()
                    if not os.path.isabs(git_dir):
                        git_dir = os.path.join(self.root_dir, git_dir)
                    git_dir = os.path.realpath(git_dir)
                    is_git_worktree = True
                    parent = os.path.dirname(git_dir)
                    git_common_dir = (
                        os.path.dirname(parent)
                        if os.path.basename(parent).casefold() == "worktrees"
                        else git_dir
                    )
            except OSError:
                pass
        excluded = sorted(
            _SKIP_DIRS | {
                ".codex", ".serena", ".cache", "cache", "caches",
                "OneDrive", "Dropbox", "Google Drive", "iCloud Drive", "iCloudDrive",
            },
            key=str.casefold,
        )
        self._allow_broad_root = bool(allow_broad_root)
        cache_policy = "broad" if self._allow_broad_root else "default"
        cache_scope_id = hashlib.sha256(
            f"{scope_id}:{cache_policy}".encode("utf-8")
        ).hexdigest()
        self._root_identity = {
            "canonical_root": self.root_dir,
            "scope_id": scope_id,
            "cache_scope_id": cache_scope_id,
            "cache_policy": cache_policy,
            "is_git_worktree": is_git_worktree,
            "git_common_dir": git_common_dir,
            "excluded_paths": excluded,
        }
        if connection is None and db_path not in ("", ":memory:"):
            base_path = os.path.abspath(os.path.expanduser(db_path))
            stem, suffix = os.path.splitext(base_path)
            suffix = suffix or ".duckdb"
            self._db_path = (
                f"{stem}.root-{scope_id[:16]}.{cache_policy}{suffix}"
            )
        else:
            self._db_path = db_path
        self._hasher = hasher
        self._embedder = embedder if embedder is not None else _Embedder()
        self._lock = threading.RLock()
        self._con = connection
        self._owns_con = connection is None
        self._fts_built = False
        self._vss_ready = False
        self._vss_dim: int | None = None
        self._index_revision: int = 0

    # -- connection / schema -------------------------------------------------

    def _connect(self) -> Any:
        if self._con is None:
            import duckdb  # noqa: PLC0415

            self._con = duckdb.connect(self._db_path)
        return self._con

    def _ensure_schema(self, con: Any) -> None:
        con.execute(
            "CREATE TABLE IF NOT EXISTS code_chunks ("
            "chunk_id VARCHAR PRIMARY KEY, path VARCHAR, language VARCHAR, "
            "kind VARCHAR, name VARCHAR, line_start INTEGER, line_end INTEGER, "
            "content VARCHAR, content_hash VARCHAR)"
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS code_index_meta ("
            "id INTEGER PRIMARY KEY, merkle_json VARCHAR, "
            "embedding_model VARCHAR, index_revision INTEGER, "
            "last_checkpoint_at DOUBLE, scope_info_json VARCHAR)"
        )
        for col, coltype in (
            ("embedding_model", "VARCHAR"),
            ("index_revision", "INTEGER"),
            ("last_checkpoint_at", "DOUBLE"),
            ("scope_info_json", "VARCHAR"),
        ):
            try:
                con.execute(
                    f"ALTER TABLE code_index_meta ADD COLUMN IF NOT EXISTS {col} {coltype}"
                )
            except Exception:  # noqa: BLE001
                pass

    # -- meta persistence (Merkle tree + embedding freshness state) ---------

    _META_DEFAULTS: dict[str, Any] = {
        "merkle": None, "embedding_model": None,
        "index_revision": 0, "last_checkpoint_at": None,
    }

    def _load_meta(self, con: Any) -> dict[str, Any]:
        """Load the last complete tree and latest explicit scope state."""
        defaults = dict(self._META_DEFAULTS)
        defaults.setdefault("scan_info", None)
        try:
            rows = con.execute(
                "SELECT merkle_json, embedding_model, index_revision, "
                "last_checkpoint_at, scope_info_json "
                "FROM code_index_meta WHERE id = 1"
            ).fetchall()
        except Exception:  # noqa: BLE001
            return defaults
        if not rows:
            return defaults
        merkle_json, embedding_model, index_revision, last_checkpoint_at, scope_json = rows[0]
        merkle: MerkleTree | None = None
        if merkle_json:
            try:
                merkle = MerkleTree.from_json(merkle_json)
            except Exception:  # noqa: BLE001
                merkle = None
        scan_info = None
        if scope_json:
            try:
                scan_info = json.loads(scope_json)
            except (TypeError, ValueError):
                scan_info = None
        if scan_info is None and merkle is not None:
            scan_info = merkle.scan_info
        return {
            "merkle": merkle,
            "embedding_model": embedding_model,
            "index_revision": int(index_revision) if index_revision is not None else 0,
            "last_checkpoint_at": last_checkpoint_at,
            "scan_info": scan_info,
        }

    def _store_meta(
        self,
        con: Any,
        *,
        tree: MerkleTree | None,
        embedding_model: str | None,
        index_revision: int,
        last_checkpoint_at: float | None,
        scan_info: dict[str, Any] | None = None,
    ) -> None:
        state = scan_info or (tree.scan_info if tree is not None else None) or {
            **self._root_identity,
            "scope_mode": "unknown",
            "scan_complete": False,
            "scan_reason": "scope_state_unavailable",
            "indexed_file_count": 0,
            "total_bytes": 0,
        }
        con.execute(
            "INSERT OR REPLACE INTO code_index_meta "
            "(id, merkle_json, embedding_model, index_revision, "
            "last_checkpoint_at, scope_info_json) VALUES (1, ?, ?, ?, ?, ?)",
            [
                tree.to_json() if tree is not None else None,
                embedding_model,
                index_revision,
                last_checkpoint_at,
                json.dumps(state, sort_keys=True),
            ],
        )

    # -- chunk row upsert / delete ------------------------------------------

    def _delete_file_chunks(self, con: Any, rel_paths: list[str]) -> None:
        for rel in rel_paths:
            abs_path = self._abs(rel)
            con.execute("DELETE FROM code_chunks WHERE path = ?", [abs_path])

    def _delete_paths_outside_keep_set(
        self, con: Any, scope_root: str, keep_paths: Iterable[str],
    ) -> int:
        """Delete cached paths under ``scope_root`` absent from a complete scan."""
        normalized_root = os.path.normcase(os.path.abspath(scope_root))
        keep = {
            os.path.normcase(os.path.abspath(path))
            for path in keep_paths
        }
        rows = con.execute("SELECT DISTINCT path FROM code_chunks").fetchall()
        deleted = 0
        for row in rows:
            path = row[0]
            if not isinstance(path, str):
                continue
            normalized_path = os.path.normcase(os.path.abspath(path))
            try:
                within_scope = os.path.commonpath(
                    [normalized_root, normalized_path]
                ) == normalized_root
            except (OSError, ValueError):
                within_scope = False
            if within_scope and normalized_path not in keep:
                con.execute("DELETE FROM code_chunks WHERE path = ?", [path])
                deleted += 1
        return deleted

    def _insert_chunks(self, con: Any, chunks: list[CodeChunk]) -> None:
        for c in chunks:
            con.execute(
                "INSERT OR REPLACE INTO code_chunks VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    c.chunk_id, c.path, c.language, c.kind, c.name,
                    c.line_start, c.line_end, c.content, c.content_hash,
                ],
            )

    def _abs(self, rel_path: str) -> str:
        return os.path.join(self.root_dir, rel_path.replace("/", os.sep))

    # -- (re)index -----------------------------------------------------------

    def reindex(self, *, full: bool = False) -> dict[str, Any]:
        """Reindex one bounded scope and report incomplete scans truthfully."""
        with self._lock:
            new_tree = build_merkle_tree(
                self.root_dir,
                hasher=self._hasher,
                allow_broad_root=self._allow_broad_root,
            )
            scan_info = new_tree.scan_info or {}
            summary: dict[str, Any] = {
                "changed_files": [],
                "added": [],
                "modified": [],
                "removed": [],
                "chunks_written": 0,
                "root_hash": new_tree.root_hash,
                "rebuilt": False,
                **scan_info,
                "partial": not bool(scan_info.get("scan_complete")),
            }
            try:
                con = self._connect()
                self._ensure_schema(con)
                meta = self._load_meta(con)
                revision = meta["index_revision"]
                if not scan_info.get("scan_complete", False):
                    # Keep the last complete Merkle baseline and all indexed
                    # rows intact. A bounded/failed pass must never make
                    # unseen files look deleted or the old index look current.
                    self._store_meta(
                        con,
                        tree=meta["merkle"],
                        embedding_model=meta["embedding_model"],
                        index_revision=revision,
                        last_checkpoint_at=meta["last_checkpoint_at"],
                        scan_info=scan_info,
                    )
                    self._index_revision = revision
                    summary["error"] = scan_info.get("scan_reason") or "incomplete_scope"
                    return summary

                prev = None if full else meta["merkle"]
                diff = new_tree.diff(prev)
                summary.update({
                    "added": diff.added,
                    "modified": diff.modified,
                    "removed": diff.removed,
                    "changed_files": diff.changed_files,
                })
                configured_model = (
                    self._embedder.model_name if self._embedder.available() else None
                )
                if diff.is_empty and prev is not None:
                    if configured_model and configured_model != meta["embedding_model"]:
                        self._prepare_search_extensions(con)
                        search_state = self._search_feature_state()
                        con.execute("BEGIN TRANSACTION")
                        try:
                            self._rebuild_vss(con)
                            if self._vss_ready:
                                revision += 1
                                self._store_meta(
                                    con,
                                    tree=new_tree,
                                    embedding_model=configured_model,
                                    index_revision=revision,
                                    last_checkpoint_at=time.time(),
                                    scan_info=scan_info,
                                )
                            con.execute("COMMIT")
                        except Exception:
                            self._restore_search_feature_state(con, search_state)
                            raise
                        if self._vss_ready:
                            self._index_revision = revision
                            summary["rebuilt"] = True
                    else:
                        self._store_meta(
                            con,
                            tree=new_tree,
                            embedding_model=meta["embedding_model"],
                            index_revision=revision,
                            last_checkpoint_at=time.time(),
                            scan_info=scan_info,
                        )
                        self._index_revision = revision
                    return summary

                file_hashes = new_tree.files()
                prepared: dict[str, list[CodeChunk]] = {}
                for rel in diff.changed_files:
                    chunks, error = self._chunk_snapshot(
                        self._abs(rel), expected_hash=file_hashes.get(rel),
                    )
                    if error is not None:
                        scan_info["scan_complete"] = False
                        scan_info["scan_reason"] = error
                        summary.update(scan_info)
                        summary["partial"] = True
                        summary["error"] = error
                        self._store_meta(
                            con,
                            tree=meta["merkle"],
                            embedding_model=meta["embedding_model"],
                            index_revision=revision,
                            last_checkpoint_at=meta["last_checkpoint_at"],
                            scan_info=scan_info,
                        )
                        self._index_revision = revision
                        return summary
                    assert chunks is not None
                    prepared[rel] = chunks

                self._prepare_search_extensions(con)
                search_state = self._search_feature_state()
                con.execute("BEGIN TRANSACTION")
                try:
                    if full:
                        self._delete_paths_outside_keep_set(
                            con,
                            self.root_dir,
                            (self._abs(rel) for rel in file_hashes),
                        )
                    elif diff.removed:
                        self._delete_file_chunks(con, diff.removed)

                    written = 0
                    for rel, chunks in prepared.items():
                        self._delete_file_chunks(con, [rel])
                        self._insert_chunks(con, chunks)
                        written += len(chunks)
                    summary["chunks_written"] = written
                    self._rebuild_search(con)
                    revision += 1
                    new_embedding_model = (
                        configured_model if self._vss_ready else meta["embedding_model"]
                    )
                    self._store_meta(
                        con,
                        tree=new_tree,
                        embedding_model=new_embedding_model,
                        index_revision=revision,
                        last_checkpoint_at=time.time(),
                        scan_info=scan_info,
                    )
                    con.execute("COMMIT")
                except Exception:
                    self._restore_search_feature_state(con, search_state)
                    raise
                self._index_revision = revision
                summary["rebuilt"] = True
            except Exception:  # noqa: BLE001
                _log.debug("CodeIndex.reindex failed", exc_info=True)
                summary["error"] = "index_write_failed"
                summary["partial"] = True
            return summary

    def _chunk_path(self, abs_path: str) -> list[CodeChunk]:
        """Read + chunk one file; returns ``[]`` on read failure (best-effort)."""
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as fh:
                source = fh.read()
        except OSError:
            return []
        return chunk_file(abs_path, source)

    def _chunk_snapshot(
        self, abs_path: str, *, expected_hash: str | None = None,
    ) -> tuple[list[CodeChunk] | None, str | None]:
        """Read a bounded source snapshot and optionally bind it to its Merkle hash.

        The tree scan and chunk read are separate filesystem operations.  A
        source may be removed, become unreadable, or change between them, so a
        caller publishing Merkle-backed changes must verify the exact bytes it
        will chunk before deleting any existing rows.
        """
        max_bytes = 4_000_000
        try:
            with open(abs_path, "rb") as stream:
                raw = stream.read(max_bytes + 1)
        except OSError:
            return None, "file_read_failed"
        if len(raw) > max_bytes:
            return None, "file_size_budget_exceeded"
        content_hash = hashlib.sha256(raw).hexdigest()
        if expected_hash is not None and content_hash != expected_hash:
            return None, "file_changed_during_chunk"
        source = raw.decode("utf-8", errors="replace")
        return chunk_file(abs_path, source), None

    def _prepare_search_extensions(self, con: Any) -> None:
        """Load DuckDB search extensions before opening a write transaction."""
        con.execute("INSTALL fts")
        con.execute("LOAD fts")
        if self._embedder.available():
            try:
                con.execute("INSTALL vss")
                con.execute("LOAD vss")
            except Exception:  # noqa: BLE001 — VSS is an optional search leg
                _log.debug("CodeIndex could not load VSS", exc_info=True)

    def _search_feature_state(self) -> tuple[bool, bool, int | None]:
        """Capture instance flags that mirror transactional search objects."""
        return self._fts_built, self._vss_ready, self._vss_dim

    def _restore_search_feature_state(
        self, con: Any, state: tuple[bool, bool, int | None],
    ) -> None:
        """Roll back DB work and keep instance flags aligned with its outcome."""
        try:
            con.execute("ROLLBACK")
        except Exception:  # noqa: BLE001 — transaction outcome is now uncertain
            # The connection may have lost the transaction while retaining
            # partially changed search objects. Force callers to avoid both
            # accelerators until the next successful rebuild.
            self._fts_built = False
            self._vss_ready = False
            self._vss_dim = None
            return
        self._fts_built, self._vss_ready, self._vss_dim = state

    # -- FTS + VSS index build ----------------------------------------------

    def _rebuild_search(self, con: Any) -> None:
        """Rebuild the BM25 FTS index and, if enabled, the VSS vector index."""
        self._rebuild_fts(con)
        if self._embedder.available():
            self._rebuild_vss(con)

    def _rebuild_fts(self, con: Any) -> None:
        """(Re)build the DuckDB FTS index over chunk ``content``, keyed on
        ``chunk_id`` — ``overwrite`` because the FTS index never tracks source
        changes."""
        con.execute(
            "PRAGMA create_fts_index("
            "'code_chunks', 'chunk_id', 'content', "
            "stemmer = 'porter', stopwords = 'none', overwrite = 1)"
        )
        self._fts_built = True

    def _rebuild_vss(self, con: Any) -> None:
        """Embed every chunk + (re)build a DuckDB VSS HNSW cosine index.

        Adds an ``embedding FLOAT[dim]`` column, populates it from the local
        Model2Vec embedder, and builds an HNSW index for cosine NN queries. Any
        failure (extension missing, model unavailable) leaves ``_vss_ready``
        False so :meth:`search` cleanly falls back to BM25-only."""
        self._vss_ready = False
        try:
            rows = con.execute(
                "SELECT chunk_id, content FROM code_chunks"
            ).fetchall()
        except Exception:  # noqa: BLE001
            return
        if not rows:
            return
        texts = [r[1] or "" for r in rows]
        vectors = self._embedder.embed(texts)
        if not vectors:
            return
        dim = len(vectors[0]) if vectors else 0
        if dim <= 0:
            return
        try:
            con.execute("SET hnsw_enable_experimental_persistence = true")
            con.execute("DROP INDEX IF EXISTS code_chunks_vec_idx")
            # Recreate the embedding column with the right fixed dimension.
            con.execute("ALTER TABLE code_chunks DROP COLUMN IF EXISTS embedding")
            con.execute(f"ALTER TABLE code_chunks ADD COLUMN embedding FLOAT[{dim}]")
            for (chunk_id, _content), vec in zip(rows, vectors):
                con.execute(
                    "UPDATE code_chunks SET embedding = ? WHERE chunk_id = ?",
                    [vec, chunk_id],
                )
            con.execute(
                "CREATE INDEX code_chunks_vec_idx ON code_chunks "
                "USING HNSW (embedding) WITH (metric = 'cosine')"
            )
            self._vss_ready = True
            self._vss_dim = dim
        except Exception:  # noqa: BLE001 — VSS unavailable → BM25-only
            _log.debug("CodeIndex._rebuild_vss failed", exc_info=True)
            self._vss_ready = False

    # -- search --------------------------------------------------------------

    def search(
        self, query: str, *, limit: int = 10, kind: str | None = None,
    ) -> list[dict[str, Any]]:
        """Hybrid BM25 (+ optional VSS) search over the chunk store.

        Runs a BM25 query; when the vector leg is ready, ALSO runs a VSS cosine
        NN query over the query's embedding and fuses the two rankings with
        Reciprocal Rank Fusion. Optional ``kind`` filters to one chunk category
        (e.g. ``"function"``). Each hit is a dict with the chunk fields + a
        fused ``score`` and the component ``bm25`` / ``vector_rank``. Best-effort:
        an empty index or a query error returns ``[]``, never raises.
        """
        q = (query or "").strip()
        if not q:
            return []
        with self._lock:
            try:
                con = self._connect()
                self._ensure_schema(con)
                if not self._fts_built:
                    self._rebuild_fts(con)
                bm25_hits = self._bm25_search(con, q, kind=kind)
                vec_hits = (
                    self._vss_search(con, q, kind=kind)
                    if self._vss_ready else []
                )
            except Exception:  # noqa: BLE001
                _log.debug("CodeIndex.search failed", exc_info=True)
                return []
        fused = _reciprocal_rank_fusion(bm25_hits, vec_hits)
        return fused[: max(1, int(limit))]

    def _row_to_hit(self, columns: list[str], row: tuple) -> dict[str, Any]:
        rec = dict(zip(columns, row))
        chunk_id = rec.get("chunk_id")
        return {
            "chunk_id": chunk_id,
            "path": rec.get("path"),
            "language": rec.get("language"),
            "kind": rec.get("kind"),
            "name": rec.get("name"),
            "line_start": rec.get("line_start"),
            "line_end": rec.get("line_end"),
            "content": rec.get("content"),
            # -- shared BM25-first + Model2Vec retrieval contract (5044d8eb) --
            # Additive-only fields conforming to the common hit schema shared
            # across code/docs/outputs/planning search (see
            # meridian.retrieval_contract.RETRIEVAL_HIT_FIELDS). This module
            # is intentionally zero-Meridian-dependency (see the module
            # docstring's "zero dependency on any host application"), so
            # these are a plain dict literal here -- matching the contract's
            # field NAMES/shape by convention, never importing
            # meridian.retrieval_contract. Every existing flat key above is
            # unchanged; nothing here removes or renames a field a caller
            # may already depend on.
            "id": chunk_id,
            "source": "code_index",
            "content_hash": rec.get("content_hash"),
            "structure": {
                "path": rec.get("path"),
                "language": rec.get("language"),
                "kind": rec.get("kind"),
                "name": rec.get("name"),
                "line_start": rec.get("line_start"),
                "line_end": rec.get("line_end"),
            },
        }

    def _bm25_search(
        self, con: Any, query: str, *, kind: str | None,
    ) -> list[dict[str, Any]]:
        sql = (
            "SELECT chunk_id, path, language, kind, name, line_start, "
            "line_end, content, content_hash, "
            "fts_main_code_chunks.match_bm25(chunk_id, ?) AS bm25 "
            "FROM code_chunks"
        )
        params: list[Any] = [query]
        if kind:
            sql += " WHERE kind = ?"
            params.append(kind)
        relation = con.execute(sql, params)
        columns = [c[0] for c in relation.description]
        hits: list[dict[str, Any]] = []
        for row in relation.fetchall():
            rec = dict(zip(columns, row))
            bm25 = rec.get("bm25")
            if bm25 is None:
                continue
            hit = self._row_to_hit(columns, row)
            hit["bm25"] = float(bm25)
            # -- shared retrieval contract score fields (5044d8eb) --
            # This is the BM25-FIRST leg: lexical_score is the raw BM25
            # score; semantic_score is None (no Model2Vec signal has run at
            # this stage -- that's a caller-layered second-stage rerank over
            # THIS leg's bounded candidate set, e.g.
            # meridian.semantic_search.rank_confident, matching the item
            # title "BM25-first plus Model2Vec second-stage"); fused_score
            # falls back to the lexical score alone (mirrors
            # meridian.semantic_search.score_confidence's own "no other
            # signal -> fuse to whichever one score exists" rule). The
            # existing VSS + Reciprocal-Rank-Fusion leg
            # (_vss_search/_reciprocal_rank_fusion) is a DIFFERENT,
            # independently-useful architecture -- a full ANN search fused
            # by rank position, not a rerank of THIS leg's candidates -- and
            # is deliberately left untouched here.
            hit["lexical_score"] = hit["bm25"]
            hit["semantic_score"] = None
            hit["fused_score"] = hit["bm25"]
            # BM25 has no partial/stale state of its own to track (see
            # CodeIndex.get_convergence_state: "the BM25 leg alone has no
            # partial/stale state to track") -- freshness here is always
            # "current". codeindex does not track per-hit provenance yet
            # (that is the "provenance-rich outputs" stage of this same
            # staged rollout per the sprint item's own notes), so
            # provenance_status is the explicit "not_tracked" sentinel,
            # never a fabricated verdict.
            hit["freshness"] = "current"
            hit["provenance_status"] = "not_tracked"
            hits.append(hit)
        hits.sort(key=lambda h: h["bm25"], reverse=True)
        return hits

    def _vss_search(
        self, con: Any, query: str, *, kind: str | None,
    ) -> list[dict[str, Any]]:
        vecs = self._embedder.embed([query])
        if not vecs:
            return []
        qvec = vecs[0]
        dim = self._vss_dim or len(qvec)
        sql = (
            "SELECT chunk_id, path, language, kind, name, line_start, "
            "line_end, content, content_hash, "
            f"array_cosine_distance(embedding, ?::FLOAT[{dim}]) AS dist "
            "FROM code_chunks WHERE embedding IS NOT NULL"
        )
        params: list[Any] = [qvec]
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY dist LIMIT 50"
        try:
            relation = con.execute(sql, params)
        except Exception:  # noqa: BLE001
            return []
        columns = [c[0] for c in relation.description]
        hits: list[dict[str, Any]] = []
        for row in relation.fetchall():
            rec = dict(zip(columns, row))
            dist = rec.get("dist")
            if dist is None:
                continue
            hit = self._row_to_hit(columns, row)
            hit["distance"] = float(dist)
            hits.append(hit)
        return hits

    # -- lifecycle -----------------------------------------------------------

    def count(self) -> int:
        """Number of chunks currently in the index."""
        with self._lock:
            try:
                con = self._connect()
                self._ensure_schema(con)
                return int(
                    con.execute("SELECT COUNT(*) FROM code_chunks").fetchone()[0]
                )
            except Exception:  # noqa: BLE001
                return 0

    # -- explicit embedding-freshness / convergence state (e631d54f) --------

    def get_convergence_state(self) -> "IndexConvergenceState":
        """Return scope identity and freshness for this exact index."""
        with self._lock:
            try:
                con = self._connect()
                self._ensure_schema(con)
                meta = self._load_meta(con)
                total = int(con.execute("SELECT COUNT(*) FROM code_chunks").fetchone()[0])
            except Exception:  # noqa: BLE001
                _log.debug("CodeIndex.get_convergence_state failed", exc_info=True)
                return IndexConvergenceState(
                    root_dir=self.root_dir,
                    source_fingerprint=None,
                    index_revision=0,
                    embedding_model=None,
                    configured_embedding_model=None,
                    vectors_enabled=self._embedder.available(),
                    vectors_ready=False,
                    total_chunks=0,
                    pending_embedding_count=0,
                    last_checkpoint_at=None,
                    degraded=True,
                    converged=False,
                    **self._root_identity,
                    scope_mode="unavailable",
                    scan_complete=False,
                    scan_reason="metadata_unavailable",
                )
            vectors_enabled = self._embedder.available()
            configured_model = self._embedder.model_name if vectors_enabled else None
            pending = 0
            if vectors_enabled and total:
                try:
                    pending = int(
                        con.execute(
                            "SELECT COUNT(*) FROM code_chunks WHERE embedding IS NULL"
                        ).fetchone()[0]
                    )
                except Exception:  # noqa: BLE001
                    pending = total
            model_mismatch = bool(
                vectors_enabled and configured_model and meta["embedding_model"]
                and meta["embedding_model"] != configured_model
            )
            scan_info = meta.get("scan_info") or {
                **self._root_identity,
                "scope_mode": "not_scanned",
                "scan_complete": False,
                "scan_reason": "not_scanned",
                "indexed_file_count": 0,
                "total_bytes": 0,
            }
            scan_complete = bool(scan_info.get("scan_complete"))
            vector_degraded = bool(
                vectors_enabled and (
                    not self._vss_ready or pending > 0 or model_mismatch
                    or meta["embedding_model"] is None
                )
            )
            degraded = bool(not scan_complete or vector_degraded)
            merkle = meta["merkle"]
            identity = {
                key: scan_info.get(key, self._root_identity.get(key))
                for key in (
                    "canonical_root", "scope_id", "is_git_worktree",
                    "git_common_dir", "excluded_paths",
                )
            }
            return IndexConvergenceState(
                root_dir=self.root_dir,
                source_fingerprint=merkle.root_hash if merkle is not None else None,
                index_revision=meta["index_revision"],
                embedding_model=meta["embedding_model"],
                configured_embedding_model=configured_model,
                vectors_enabled=vectors_enabled,
                vectors_ready=bool(self._vss_ready),
                total_chunks=total,
                pending_embedding_count=pending,
                last_checkpoint_at=meta["last_checkpoint_at"],
                degraded=degraded,
                converged=not degraded,
                **identity,
                scope_mode=scan_info.get("scope_mode"),
                scan_complete=scan_complete,
                scan_reason=scan_info.get("scan_reason"),
                indexed_file_count=int(scan_info.get("indexed_file_count") or 0),
                total_bytes=int(scan_info.get("total_bytes") or 0),
            )

    # -- targeted registration after provenance writes (e631d54f) -----------

    def index_paths(
        self, paths: list[str], *, prune_root: str | None = None,
    ) -> dict[str, Any]:
        """Index an explicit allowlist, optionally reconciling a complete subtree."""
        with self._lock:
            targets: list[tuple[str, str]] = []
            skipped = 0
            canonical_prune_root = normalize_root_dir(prune_root) if prune_root else None
            if canonical_prune_root:
                try:
                    if os.path.commonpath(
                        [self.root_dir, canonical_prune_root]
                    ) != self.root_dir:
                        return {
                            "canonical_root": self.root_dir,
                            "scope_id": self._root_identity["scope_id"],
                            "scope_mode": "explicit_allowlist",
                            "allowlisted_paths": [],
                            "indexed": 0,
                            "skipped": len(paths or []),
                            "paths": [],
                            "error": "prune_scope_outside_root",
                        }
                except (OSError, ValueError):
                    return {
                        "canonical_root": self.root_dir,
                        "scope_id": self._root_identity["scope_id"],
                        "scope_mode": "explicit_allowlist",
                        "allowlisted_paths": [],
                        "indexed": 0,
                        "skipped": len(paths or []),
                        "paths": [],
                        "error": "prune_scope_outside_root",
                    }
            for path in paths or []:
                if not path:
                    skipped += 1
                    continue
                candidate = path if os.path.isabs(path) else self._abs(path)
                abs_path = os.path.realpath(os.path.abspath(candidate))
                try:
                    if os.path.commonpath([self.root_dir, abs_path]) != self.root_dir:
                        skipped += 1
                        continue
                except ValueError:
                    skipped += 1
                    continue
                if not is_indexable(abs_path) or not os.path.isfile(abs_path):
                    skipped += 1
                    continue
                rel = os.path.relpath(abs_path, self.root_dir).replace(os.sep, "/")
                targets.append((abs_path, rel))
            base = {
                "canonical_root": self.root_dir,
                "scope_id": self._root_identity["scope_id"],
                "scope_mode": "explicit_allowlist",
                "allowlisted_paths": sorted(rel for _, rel in targets),
            }
            if canonical_prune_root:
                normalized_scope = os.path.normcase(
                    os.path.abspath(canonical_prune_root)
                )
                outside_scope = False
                for abs_path, _rel in targets:
                    normalized_path = os.path.normcase(os.path.abspath(abs_path))
                    try:
                        if os.path.commonpath(
                            [normalized_scope, normalized_path]
                        ) != normalized_scope:
                            outside_scope = True
                            break
                    except (OSError, ValueError):
                        outside_scope = True
                        break
                if outside_scope:
                    return {
                        **base,
                        "indexed": 0,
                        "skipped": skipped + len(targets),
                        "paths": [],
                        "error": "path_outside_prune_scope",
                    }
            if canonical_prune_root and skipped:
                return {
                    **base,
                    "indexed": 0,
                    "skipped": skipped,
                    "paths": [],
                    "error": "subtree_path_changed",
                }
            if not targets and not canonical_prune_root:
                return {**base, "indexed": 0, "skipped": skipped, "paths": []}
            try:
                con = self._connect()
                self._ensure_schema(con)
                prepared: list[tuple[str, str, list[CodeChunk]]] = []
                for abs_path, rel in targets:
                    chunks, error = self._chunk_snapshot(abs_path)
                    if error is not None:
                        return {
                            **base,
                            "indexed": 0,
                            "skipped": skipped + 1,
                            "paths": [],
                            "error": error,
                        }
                    assert chunks is not None
                    prepared.append((abs_path, rel, chunks))

                self._prepare_search_extensions(con)
                search_state = self._search_feature_state()
                con.execute("BEGIN TRANSACTION")
                try:
                    deleted = 0
                    if canonical_prune_root:
                        deleted = self._delete_paths_outside_keep_set(
                            con,
                            canonical_prune_root,
                            (abs_path for abs_path, _, _ in prepared),
                        )
                    for _abs_path, rel, chunks in prepared:
                        self._delete_file_chunks(con, [rel])
                        self._insert_chunks(con, chunks)
                    changed = bool(prepared or deleted)
                    meta = self._load_meta(con)
                    revision = meta["index_revision"]
                    if changed:
                        self._rebuild_search(con)
                        if meta["merkle"] is not None:
                            revision += 1
                            configured_model = (
                                self._embedder.model_name if self._embedder.available() else None
                            )
                            new_model = (
                                configured_model if self._vss_ready else meta["embedding_model"]
                            )
                            self._store_meta(
                                con,
                                tree=meta["merkle"],
                                embedding_model=new_model,
                                index_revision=revision,
                                last_checkpoint_at=time.time(),
                                scan_info=meta.get("scan_info"),
                            )
                    con.execute("COMMIT")
                except Exception:
                    self._restore_search_feature_state(con, search_state)
                    raise
                self._index_revision = revision
                return {
                    **base,
                    "indexed": len(prepared),
                    "skipped": skipped,
                    "paths": [rel for _, rel, _ in prepared],
                }
            except Exception:  # noqa: BLE001
                _log.debug("CodeIndex.index_paths failed", exc_info=True)
                return {
                    **base,
                    "indexed": 0,
                    "skipped": len(paths or []),
                    "paths": [],
                    "error": "index_write_failed",
                }

    def describe_vector_index(self) -> "IndexMetadata":
        """Backend-neutral metadata snapshot of this index's optional VSS leg
        (e1475682) — see :mod:`meridian_codeindex.vector_index.IndexMetadata`.

        A read-only view of the state :meth:`_rebuild_vss` already tracks
        (``_vss_ready`` / ``_vss_dim`` / the embedder) — CodeIndex keeps
        managing its own hybrid search path unchanged; this only exposes
        that state in the shared contract's shape so a caller can persist or
        compare it via ``meridian.db.vector_index_state`` /
        :func:`meridian_codeindex.vector_index.compare_candidates` without
        CodeIndex importing that module on its hot (search) path.
        """
        from .vector_index import IndexMetadata  # local import — keep this an
        # optional, cold-path integration; code_index.py's search hot path
        # never imports vector_index.py.

        return IndexMetadata(
            backend="duckdb_vss" if self._vss_ready else "bm25_lexical",
            embedding_model=_EMBED_MODEL_NAME if self._embedder.available() else None,
            embedding_version=_model2vec_version() if self._embedder.available() else None,
            dimension=self._vss_dim,
            source_fingerprint=None,
            project_id=None,
            scope=self.root_dir,
            record_count=self.count(),
        )

    def close(self) -> None:
        """Close the owned DuckDB connection (no-op for an injected one)."""
        with self._lock:
            if self._owns_con and self._con is not None:
                try:
                    self._con.close()
                except Exception:  # noqa: BLE001
                    _log.debug("CodeIndex.close failed", exc_info=True)
            if self._owns_con:
                self._con = None
                self._fts_built = False
                self._vss_ready = False


def _reciprocal_rank_fusion(
    bm25_hits: list[dict[str, Any]],
    vec_hits: list[dict[str, Any]],
    *,
    k: int = 60,
) -> list[dict[str, Any]]:
    """Fuse two ranked hit lists by Reciprocal Rank Fusion (RRF).

    RRF score for a chunk = sum over each list it appears in of ``1/(k+rank)``
    (rank 1-based). It needs no score calibration between the (unbounded) BM25
    scores and the (0..2) cosine distances — the industry-standard way to fuse a
    keyword and a vector ranking. When ``vec_hits`` is empty this is a stable
    pass-through of the BM25 order. Each output hit carries the fused ``score``
    plus whichever of ``bm25`` / ``vector_rank`` applied.
    """
    fused: dict[str, dict[str, Any]] = {}

    def _merge(hits: list[dict[str, Any]], field_name: str) -> None:
        for rank, hit in enumerate(hits, start=1):
            cid = hit.get("chunk_id")
            if cid is None:
                continue
            entry = fused.get(cid)
            if entry is None:
                entry = dict(hit)
                entry["score"] = 0.0
                fused[cid] = entry
            entry["score"] += 1.0 / (k + rank)
            entry[field_name] = rank
            # Carry the component signal onto the fused entry.
            if "bm25" in hit and "bm25" not in entry:
                entry["bm25"] = hit["bm25"]

    _merge(bm25_hits, "bm25_rank")
    _merge(vec_hits, "vector_rank")
    out = list(fused.values())
    out.sort(key=lambda h: h["score"], reverse=True)
    return out


# ===========================================================================
# 4. Reindex trigger — the natural lifecycle-checkpoint entry point
# ===========================================================================

# One CodeIndex per (root_dir, db_path) so a lifecycle checkpoint reuses the
# persisted Merkle tree + chunk store instead of rebuilding from scratch.
_INDEX_CACHE: dict[tuple[str, str], CodeIndex] = {}
_INDEX_CACHE_LOCK = threading.Lock()


def normalize_root_dir(root_dir: str | None) -> str:
    """Normalize a caller-supplied root into one symlink-resolved identity.

    Shell/JSON quoting, home/environment references and relative segments are
    normalized before any filesystem check. realpath collapses symlink aliases
    so cache keys and containment checks identify the same tree.
    """
    if not root_dir:
        return ""
    value = str(root_dir).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1].strip()
    if not value:
        return ""
    value = os.path.expanduser(os.path.expandvars(value))
    return os.path.realpath(os.path.abspath(value))


def get_code_index(
    root_dir: str,
    *,
    db_path: str = ":memory:",
    allow_broad_root: bool = False,
) -> CodeIndex:
    """Return a cached index isolated by canonical root and scope policy."""
    canonical_root = normalize_root_dir(root_dir)
    resolved_root = canonical_root or os.path.realpath(os.path.abspath(root_dir))
    key = (resolved_root, db_path, bool(allow_broad_root))
    with _INDEX_CACHE_LOCK:
        idx = _INDEX_CACHE.get(key)
        if idx is None:
            idx = CodeIndex(
                resolved_root,
                db_path=db_path,
                allow_broad_root=allow_broad_root,
            )
            _INDEX_CACHE[key] = idx
        return idx


def reindex_at_checkpoint(
    root_dir: str, *, db_path: str = ":memory:",
) -> dict[str, Any]:
    """Lifecycle-checkpoint reindex entry point (93fce816 requirement 4).

    A natural-checkpoint entry point a host application can wire into its own
    file-save / task-completion hooks — NOT a real-time per-save watchdog. Runs
    one incremental Merkle-diff reindex over ``root_dir`` and returns the
    reindex summary. Cheap and idempotent: when nothing changed since the last
    checkpoint the Merkle root-hash compare short-circuits and no re-chunking
    happens. Never raises — a bad root simply reports zero changes.
    """
    # a0cf71ef — normalize (unquote / expanduser / abspath) before the isdir
    # check so a valid local dir handed to us in a quoted or ~-prefixed shape is
    # accepted, and "does not exist" is returned ONLY when it truly is not a dir.
    root_dir = normalize_root_dir(root_dir)
    if not root_dir or not os.path.isdir(root_dir):
        return {
            "changed_files": [], "added": [], "modified": [], "removed": [],
            "chunks_written": 0, "root_hash": "", "rebuilt": False,
            "error": f"root_dir does not exist: {root_dir}",
        }
    idx = get_code_index(root_dir, db_path=db_path)
    return idx.reindex()


def register_priority_path(
    root_dir: str, path: str, *, db_path: str = ":memory:",
) -> dict[str, Any]:
    """Provenance-triggered targeted registration — module-level convenience
    wrapper around :meth:`CodeIndex.index_paths`, mirroring
    ``meridian_outputs.outputs_local.register_priority_path`` (item 6af1518d
    requirement 3) on the code-index side.

    Intended caller: right after a provenance write for ``path`` (e.g. a
    generator script just wrote/overwrote it, or
    ``meridian_outputs.annotate.record_provenance`` just recorded it) so it
    becomes searchable via :func:`search_code_semantic` immediately instead
    of waiting for the next :func:`reindex_at_checkpoint` pass over the whole
    ``root_dir``. Uses the SAME process-cached :class:`CodeIndex` instance
    every other function in this module keys off of via
    :func:`get_code_index`. Never raises — a missing/invalid ``root_dir``
    reports zero indexed rather than erroring.
    """
    root_dir = normalize_root_dir(root_dir)
    if not root_dir or not os.path.isdir(root_dir):
        return {
            "indexed": 0, "skipped": 1, "paths": [],
            "error": f"root_dir does not exist: {root_dir}",
        }
    idx = get_code_index(root_dir, db_path=db_path)
    return idx.index_paths([path])


def search_code_semantic(
    root_dir: str,
    query: str,
    *,
    limit: int = 10,
    kind: str | None = None,
    db_path: str = ":memory:",
    reindex: bool = True,
    allow_broad_root: bool = False,
) -> dict[str, Any]:
    """Search one local root and return its explicit scope/convergence state."""
    root_dir = normalize_root_dir(root_dir)
    result: dict[str, Any] = {
        "root_dir": root_dir,
        "query": query,
        "hits": [],
        "total_indexed": 0,
        "vectors_enabled": _vectors_enabled(),
    }
    if not query or not str(query).strip():
        result["error"] = "query is required"
        return result
    if not root_dir or not os.path.isdir(root_dir):
        result["error"] = f"root_dir does not exist: {root_dir}"
        return result
    idx = get_code_index(
        root_dir,
        db_path=db_path,
        allow_broad_root=allow_broad_root,
    )
    reindex_result = idx.reindex() if reindex else None
    result["total_indexed"] = idx.count()
    result["vectors_active"] = idx._vss_ready
    result["hits"] = idx.search(query, limit=limit, kind=kind)
    convergence = idx.get_convergence_state().to_dict()
    result["convergence"] = convergence
    result["scope"] = {
        "canonical_root": convergence["canonical_root"],
        "scope_id": convergence["scope_id"],
        "is_git_worktree": convergence["is_git_worktree"],
        "git_common_dir": convergence["git_common_dir"],
        "scope_mode": convergence["scope_mode"],
        "scan_complete": convergence["scan_complete"],
        "scan_reason": convergence["scan_reason"],
        "indexed_file_count": convergence["indexed_file_count"],
        "total_bytes": convergence["total_bytes"],
        "excluded_paths": convergence["excluded_paths"],
    }
    if reindex_result and reindex_result.get("partial"):
        result["partial"] = True
        result["scan_reason"] = reindex_result.get("scan_reason")
        if reindex_result.get("error"):
            result["error"] = reindex_result["error"]
    result["degraded"] = convergence["degraded"]
    return result
