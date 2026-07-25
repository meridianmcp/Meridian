# meridian-codeindex

Standalone local BM25 code index — extracted from Meridian (2b2433ca).

Runs **fully locally** against a real source tree on your machine. No cloud
round-trip, no external binary, no LSP, and — deliberately — **no dependency
on Serena or codebase-memory-mcp**: this package exists precisely as an
independent fallback layer for when either of those is unavailable or
misbehaving. It has no dependency on Meridian either; Meridian is just one
caller of it (see `meridian/code_index.py` in the parent repo, a thin
compatibility shim over this package).

Three cooperating layers, all in a single DuckDB sidecar file:

1. **tree-sitter / ast semantic chunking** — source files are parsed into
   chunks at function/class/method boundaries (Python via stdlib `ast`,
   TypeScript/JavaScript via `tree-sitter`, when the `treesitter` extra is
   installed), plus the unnamed blocks a named-symbols-only index would miss
   (module-level `if __name__` guards, bare calls, dict/list literals).
2. **Merkle tree of file-content hashes** — incremental reindex re-chunks only
   the files whose leaf hash actually moved; an unchanged subtree is skipped
   in O(1) by one hash compare.
3. **DuckDB FTS (Okapi BM25)** hybrid search, with an optional local-vector
   leg (Model2Vec + DuckDB VSS, fused via Reciprocal Rank Fusion) that only
   activates when `MERIDIAN_CODE_INDEX_VECTORS=1` and the `vectors` extra is
   installed. With vectors off (the default), this is a complete, real BM25
   code searcher on its own.

## Install

```bash
pip install -e ./extensions/meridian-codeindex
# or, with TypeScript/JavaScript chunking + the optional vector leg:
pip install -e "./extensions/meridian-codeindex[treesitter,vectors]"
```

## CLI

```bash
meridian-codeindex /path/to/repo "parse the auth token and refresh it"
```

```
query: 'parse the auth token and refresh it'  root_dir: /path/to/repo  indexed: 214 chunks
  1. [0.0328] /path/to/repo/auth/tokens.py:12-18  function refresh_token
  2. [0.0210] /path/to/repo/auth/tokens.py:1-10  module <module:1>
  ...
```

Flags: `--limit N`, `--kind function|class|method|module|...`,
`--db-path PATH` (persist the sidecar across runs instead of `:memory:`),
`--no-reindex` (search the existing sidecar as-is).

## Library

```python
from meridian_codeindex import CodeIndex, search_code_semantic

# one-shot, stateless
result = search_code_semantic("/path/to/repo", "parse the auth token")
for hit in result["hits"]:
    print(hit["path"], hit["line_start"], hit["kind"], hit["name"])

# or own the index yourself for repeated incremental reindex + search
idx = CodeIndex("/path/to/repo", db_path="/path/to/repo/.codeindex.duckdb")
idx.reindex()
hits = idx.search("parse the auth token", limit=5)
idx.close()
```

## Tests

```bash
pytest extensions/meridian-codeindex/tests -q
```
