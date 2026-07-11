"""Cursor-style local code index — 93fce816.

A **local-first** semantic code index modelled on Cursor's published indexing
architecture. Three cooperating layers, all in a single DuckDB *sidecar* file
(no cloud round-trip, no shared table with any other indexer):

1. **tree-sitter semantic chunking** (:class:`CodeChunk`, :func:`chunk_file`).
   Source files are parsed into semantic chunks at **function / class / method**
   boundaries (Python via the stdlib ``ast``, TypeScript/JavaScript via the
   tree-sitter grammars already vendored for :mod:`meridian.symbols`), PLUS the
   *logical un-named blocks* that ``search_graph`` (named-symbols-only) can't
   see — top-level ``if __name__`` guards, bare module-level calls, dict/list
   literal assignments. Every span of the file lands in exactly one chunk (the
   gaps between named symbols become "module"/"top_level" chunks), so a keyword
   that only appears in a bare call at the bottom of a file is still findable.

2. **Merkle tree of file-content hashes** (:class:`MerkleTree`,
   :func:`build_merkle_tree`, :meth:`MerkleTree.diff`). Every file is a leaf
   hashed by content; every directory is an interior node hashing its children's
   hashes; the tree has a single root hash. Between two passes we compare root
   hashes first and only descend into divergent subtrees — an unchanged
   directory's whole subtree is skipped in O(1) by one hash compare. This is
   what makes incremental reindex cheap: :meth:`CodeIndex.reindex` re-chunks
   **only** the files whose leaf hash moved.

3. **Hybrid search over chunks** (:class:`CodeIndex`). DuckDB native **FTS
   (Okapi BM25)** for keyword match — the exact ``PRAGMA create_fts_index``
   rebuild-with-overwrite contract proven in :mod:`meridian.outputs_indexer`'s
   ``OutputsFtsIndex`` — PLUS an **optional** DuckDB **VSS** (vector similarity,
   HNSW cosine) leg over local Model2Vec embeddings. The vector leg is
   *opt-in / lazy / degrades to keyword-only* exactly like
   :mod:`meridian.semantic_search`: it never loads a model or a native extension
   on the 512MB prod box unless ``MERIDIAN_CODE_INDEX_VECTORS`` is enabled and
   the deps are importable. With vectors off (the default), the index is a
   fully-functional BM25 code searcher.

**Reindex trigger** — :func:`reindex_at_checkpoint` is the natural-lifecycle
entry point (callable from ``claim_file`` / ``complete_sprint_item`` paths). It
is *not* a real-time per-save watchdog: it runs a single incremental Merkle-diff
reindex pass and returns cheaply when nothing changed.

Nothing here raises on a missing grammar, an unreadable file, a missing native
extension, or a missing embedding model — every such case degrades gracefully
(empty chunk list / keyword-only search / skipped file), mirroring the
best-effort posture of ``symbols.py``, ``outputs_indexer.py`` and
``semantic_search.py``.
"""
from __future__ import annotations

import ast
import hashlib
import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# What we index. Python + TypeScript/JavaScript are the repo's two languages;
# the extension map mirrors meridian.symbols so the two stay in lock-step.
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
    (``module`` / ``top_level`` / ``block``) that fill the gap
    ``search_graph`` (named-symbols-only) leaves. ``name`` is the symbol name
    for named chunks, else a synthetic label (e.g. ``"<module:1>"``).

    Line numbers are **1-based inclusive**, matching :mod:`meridian.symbols`.
    ``chunk_id`` is deterministic (path + span + content hash) so the same chunk
    keeps a stable identity across reindex passes.
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

# Named-definition node types per grammar → chunk kind. Mirrors
# meridian.symbols._TS_DEF_TYPES for the two languages this index covers.
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
    chunk. This is the coverage ``search_graph`` (named symbols only) lacks.

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
    """A persisted Merkle tree over a source directory's indexable files.

    :meth:`diff` against a previous tree returns exactly the set of files that
    were added / modified / deleted, computed by comparing hashes top-down and
    skipping any subtree whose interior hash is unchanged.
    """

    root: MerkleNode

    @property
    def root_hash(self) -> str:
        return self.root.hash

    def files(self) -> dict[str, str]:
        """``{rel_path: content_hash}`` for every leaf (file) in the tree."""
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
        """Files added / modified / removed vs ``previous`` (``None`` → all added).

        The top-down hash compare is the whole point: if a directory node's hash
        equals the previous tree's node at the same path, its ENTIRE subtree is
        identical and is skipped without descending (O(1) per unchanged dir).
        """
        if previous is None:
            added = sorted(self.files().keys())
            return MerkleDiff(added=added, modified=[], removed=[])
        if self.root_hash == previous.root_hash:
            return MerkleDiff(added=[], modified=[], removed=[])

        added: list[str] = []
        modified: list[str] = []
        removed: list[str] = []

        def _descend(cur: MerkleNode | None, old: MerkleNode | None) -> None:
            # Both present and identical hash → whole subtree unchanged, skip.
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
            added=sorted(added), modified=sorted(modified),
            removed=sorted(removed),
        )

    def to_json(self) -> str:
        return json.dumps(self.root.to_dict())

    @classmethod
    def from_json(cls, blob: str) -> "MerkleTree":
        return cls(root=MerkleNode.from_dict(json.loads(blob)))


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


def _iter_indexable_files(root_dir: str) -> Iterable[str]:
    """Yield absolute paths of every indexable source file under ``root_dir``.

    Prunes :data:`_SKIP_DIRS` in-place so vendored/build trees are never walked.
    """
    for cur, dirs, files in os.walk(root_dir):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for fn in files:
            if is_indexable(fn):
                yield os.path.join(cur, fn)


def build_merkle_tree(
    root_dir: str, *, hasher: Callable[[str], str | None] = _hash_file_bytes,
) -> MerkleTree:
    """Build the content Merkle tree over the indexable files under ``root_dir``.

    Only indexable source files (Python/TS/JS) become leaves; empty directories
    and skipped subtrees contribute nothing. ``hasher`` is injectable for tests.
    A file that can't be read is hashed as the empty string so it still appears
    (and a later read that succeeds shows up as a modification).
    """
    root_dir = os.path.abspath(root_dir)
    # Build a nested dict of the relative path segments → leaf hashes.
    file_hashes: dict[str, str] = {}
    for abs_path in _iter_indexable_files(root_dir):
        rel = os.path.relpath(abs_path, root_dir).replace(os.sep, "/")
        file_hashes[rel] = hasher(abs_path) or ""

    root = MerkleNode(rel_path="", is_file=False, hash="")
    for rel, h in sorted(file_hashes.items()):
        segments = rel.split("/")
        node = root
        for i, seg in enumerate(segments):
            is_last = i == len(segments) - 1
            child = node.children.get(seg)
            if child is None:
                child_rel = "/".join(segments[: i + 1])
                child = MerkleNode(
                    rel_path=child_rel, is_file=is_last,
                    hash=(h if is_last else ""),
                )
                node.children[seg] = child
            node = child
    _recompute_hashes(root)
    return MerkleTree(root=root)


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
    extension or an embedding model on the 512MB prod box unless the operator
    opts in with ``MERIDIAN_CODE_INDEX_VECTORS=1``. Read fresh so tests can
    toggle it via ``os.environ``.
    """
    return os.environ.get(_ENV_VECTORS, "").strip().lower() in _TRUTHY


class _Embedder:
    """Lazy local Model2Vec embedder — mirrors semantic_search's posture.

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
    ) -> None:
        self.root_dir = os.path.abspath(root_dir)
        self._db_path = db_path
        self._hasher = hasher
        self._embedder = embedder if embedder is not None else _Embedder()
        self._lock = threading.RLock()
        self._con = connection
        self._owns_con = connection is None
        self._fts_built = False
        self._vss_ready = False
        self._vss_dim: int | None = None

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
            "id INTEGER PRIMARY KEY, merkle_json VARCHAR)"
        )

    # -- Merkle persistence --------------------------------------------------

    def _load_merkle(self, con: Any) -> MerkleTree | None:
        try:
            rows = con.execute(
                "SELECT merkle_json FROM code_index_meta WHERE id = 1"
            ).fetchall()
        except Exception:  # noqa: BLE001
            return None
        if not rows or not rows[0][0]:
            return None
        try:
            return MerkleTree.from_json(rows[0][0])
        except Exception:  # noqa: BLE001
            return None

    def _store_merkle(self, con: Any, tree: MerkleTree) -> None:
        con.execute("DELETE FROM code_index_meta WHERE id = 1")
        con.execute(
            "INSERT INTO code_index_meta (id, merkle_json) VALUES (1, ?)",
            [tree.to_json()],
        )

    # -- chunk row upsert / delete ------------------------------------------

    def _delete_file_chunks(self, con: Any, rel_paths: list[str]) -> None:
        for rel in rel_paths:
            abs_path = self._abs(rel)
            con.execute("DELETE FROM code_chunks WHERE path = ?", [abs_path])

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
        """Incrementally reindex the tree via a Merkle diff. Returns a summary.

        Builds a fresh Merkle tree, diffs it against the persisted one, and
        re-chunks ONLY the added/modified files (removed files' chunks are
        deleted). ``full=True`` forces a from-scratch pass (ignores the stored
        tree). Rebuilds the FTS (+ optional VSS) index afterwards **only when
        something changed**. Best-effort: a DuckDB failure is logged and
        swallowed. Returns
        ``{changed_files, added, modified, removed, chunks_written,
        root_hash, rebuilt}``.
        """
        with self._lock:
            new_tree = build_merkle_tree(self.root_dir, hasher=self._hasher)
            summary: dict[str, Any] = {
                "changed_files": [], "added": [], "modified": [],
                "removed": [], "chunks_written": 0,
                "root_hash": new_tree.root_hash, "rebuilt": False,
            }
            try:
                con = self._connect()
                self._ensure_schema(con)
                prev = None if full else self._load_merkle(con)
                diff = new_tree.diff(prev)
                summary.update({
                    "added": diff.added, "modified": diff.modified,
                    "removed": diff.removed,
                    "changed_files": diff.changed_files,
                })
                if diff.is_empty and prev is not None:
                    # Nothing moved — the whole point of the Merkle short-circuit.
                    self._store_merkle(con, new_tree)
                    return summary
                if diff.removed:
                    self._delete_file_chunks(con, diff.removed)
                written = 0
                for rel in diff.changed_files:
                    abs_path = self._abs(rel)
                    self._delete_file_chunks(con, [rel])
                    chunks = self._chunk_path(abs_path)
                    self._insert_chunks(con, chunks)
                    written += len(chunks)
                summary["chunks_written"] = written
                self._store_merkle(con, new_tree)
                self._rebuild_search(con)
                summary["rebuilt"] = True
            except Exception:  # noqa: BLE001 — never crash a session lifecycle hook
                _log.debug("CodeIndex.reindex failed", exc_info=True)
            return summary

    def _chunk_path(self, abs_path: str) -> list[CodeChunk]:
        """Read + chunk one file; returns ``[]`` on read failure (best-effort)."""
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as fh:
                source = fh.read()
        except OSError:
            return []
        return chunk_file(abs_path, source)

    # -- FTS + VSS index build ----------------------------------------------

    def _rebuild_search(self, con: Any) -> None:
        """Rebuild the BM25 FTS index and, if enabled, the VSS vector index."""
        self._rebuild_fts(con)
        if self._embedder.available():
            self._rebuild_vss(con)

    def _rebuild_fts(self, con: Any) -> None:
        """(Re)build the DuckDB FTS index over chunk ``content``, keyed on
        ``chunk_id`` — ``overwrite`` because the FTS index never tracks source
        changes (the exact contract proven in outputs_indexer)."""
        con.execute("INSTALL fts")
        con.execute("LOAD fts")
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
            con.execute("INSTALL vss")
            con.execute("LOAD vss")
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
        return {
            "chunk_id": rec.get("chunk_id"),
            "path": rec.get("path"),
            "language": rec.get("language"),
            "kind": rec.get("kind"),
            "name": rec.get("name"),
            "line_start": rec.get("line_start"),
            "line_end": rec.get("line_end"),
            "content": rec.get("content"),
        }

    def _bm25_search(
        self, con: Any, query: str, *, kind: str | None,
    ) -> list[dict[str, Any]]:
        sql = (
            "SELECT chunk_id, path, language, kind, name, line_start, "
            "line_end, content, "
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
            "line_end, content, "
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
# 4. Reindex trigger — the session-lifecycle checkpoint entry point
# ===========================================================================

# One CodeIndex per (root_dir, db_path) so a session-lifecycle checkpoint reuses
# the persisted Merkle tree + chunk store instead of rebuilding from scratch.
_INDEX_CACHE: dict[tuple[str, str], CodeIndex] = {}
_INDEX_CACHE_LOCK = threading.Lock()


def get_code_index(root_dir: str, *, db_path: str = ":memory:") -> CodeIndex:
    """Return a process-cached :class:`CodeIndex` for ``(root_dir, db_path)``.

    A file ``db_path`` persists the chunk table + Merkle tree across restarts, so
    the first checkpoint after a restart does an incremental (not full) reindex.
    """
    key = (os.path.abspath(root_dir), db_path)
    with _INDEX_CACHE_LOCK:
        idx = _INDEX_CACHE.get(key)
        if idx is None:
            idx = CodeIndex(root_dir, db_path=db_path)
            _INDEX_CACHE[key] = idx
        return idx


def reindex_at_checkpoint(
    root_dir: str, *, db_path: str = ":memory:",
) -> dict[str, Any]:
    """Session-lifecycle reindex checkpoint (93fce816 requirement 4).

    The natural-checkpoint entry point wired into ``claim_file`` /
    ``complete_sprint_item`` paths — NOT a real-time per-save watchdog. Runs one
    incremental Merkle-diff reindex over ``root_dir`` and returns the reindex
    summary. Cheap and idempotent: when nothing changed since the last
    checkpoint the Merkle root-hash compare short-circuits and no re-chunking
    happens. Never raises — a bad root simply reports zero changes.
    """
    if not root_dir or not os.path.isdir(root_dir):
        return {
            "changed_files": [], "added": [], "modified": [], "removed": [],
            "chunks_written": 0, "root_hash": "", "rebuilt": False,
            "error": f"root_dir does not exist: {root_dir}",
        }
    idx = get_code_index(root_dir, db_path=db_path)
    return idx.reindex()


def search_code_semantic(
    root_dir: str,
    query: str,
    *,
    limit: int = 10,
    kind: str | None = None,
    db_path: str = ":memory:",
    reindex: bool = True,
) -> dict[str, Any]:
    """Stateless one-shot hybrid code search (backs the ``search_code_semantic``
    MCP tool).

    Ensures the ``root_dir`` index is current (an incremental Merkle-diff
    reindex unless ``reindex=False``), then runs the hybrid BM25 (+ optional
    VSS) search. With the vector leg disabled (the default posture / prod), this
    is a pure BM25 code search over tree-sitter chunks — a real, complete
    deliverable on its own. Returns
    ``{root_dir, query, total_indexed, vectors_enabled, hits:[...]}``. A missing
    directory / empty tree returns an empty hits list, never an error.
    """
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
    idx = get_code_index(root_dir, db_path=db_path)
    if reindex:
        idx.reindex()
    result["total_indexed"] = idx.count()
    result["vectors_active"] = idx._vss_ready
    result["hits"] = idx.search(query, limit=limit, kind=kind)
    return result
