# Publishing meridian-plugin-base to PyPI

This document is the concrete follow-up plan that must be executed before the
`meridian[docs]` modular-extras architecture can work end-to-end.

## Why this is blocked today

`uvx --from <local-path> meridian-docs` installs `meridian-docs` into an isolated
virtualenv from the local path. That env has NO access to the Meridian monorepo
filesystem, so `[tool.uv.sources]` path-based dependencies like:

```toml
# This does NOT work in a uvx isolated env:
[tool.uv.sources]
meridian-plugin-base = { path = "../../packages/meridian-plugin-base" }
```

...fail at runtime with an ImportError even if they install without error, because
the path is relative to the monorepo root which is NOT inside the isolated env.

This was confirmed live on 2026-07-14 (see the header comment in
`extensions/meridian-docs/meridian_docs/_vendored_content_tree.py`).

The only reliable fix is to publish `meridian-plugin-base` to a RESOLVABLE INDEX
so plugins can declare a normal `dependencies = ["meridian-plugin-base>=0.1"]`
that `pip`/`uv` resolves from PyPI (not from a local path).

## Step-by-step publishing procedure

### 1. Set the version in pyproject.toml

The current version is `0.1.0`. Follow PEP 440 semver:
- Patch (0.1.0 -> 0.1.1): bug-fix or docs-only change.
- Minor (0.1.0 -> 0.2.0): new public API surface added (new module/function).
- Major (0.1.0 -> 1.0.0): breaking API change (rename/remove existing public symbol).

Before tagging, set the version in `packages/meridian-plugin-base/pyproject.toml`
to match the tag you will push.

### 2. Set up PyPI Trusted Publisher (one-time, requires PyPI account)

PyPI Trusted Publisher lets GitHub Actions publish without a stored API key.
The Meridian repo's note `reference_release_pipeline.md` records that trusted-publisher
setup for `meridian-server` is PENDING. The same setup is needed for `meridian-plugin-base`.

Steps (PyPI web UI, done by the repo owner):
1. Log in to https://pypi.org
2. Go to https://pypi.org/manage/account/publishing/
3. Click "Add a new pending publisher"
4. Fill in:
   - PyPI project name: `meridian-plugin-base`
   - GitHub repository owner: `meridianmcp` (or your GitHub username)
   - GitHub repository name: `meridian`
   - Workflow filename: `publish-plugin-base.yml` (the workflow file in step 3)
   - Environment name: `pypi` (must match the `environment:` in the workflow)
5. Save. This creates a "pending publisher" — the package does NOT need to exist
   on PyPI yet; the first push from the workflow creates it.

### 3. Add the GitHub Actions publish workflow

Create `.github/workflows/publish-plugin-base.yml` in the monorepo:

```yaml
name: Publish meridian-plugin-base to PyPI

on:
  push:
    tags:
      - "plugin-base-v*"   # e.g. plugin-base-v0.1.0

jobs:
  publish:
    runs-on: ubuntu-latest
    environment: pypi
    permissions:
      id-token: write   # OIDC token for trusted publisher
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Install build
        run: pip install build
      - name: Build wheel + sdist
        run: python -m build packages/meridian-plugin-base
      - name: Publish to PyPI
        uses: pypa/gh-action-pypi-publish@release/v1
        with:
          packages-dir: packages/meridian-plugin-base/dist/
```

Trigger: push a tag `plugin-base-v0.1.0` (distinct from the main `vX.Y.Z` tag
so the plugin-base and main-server releases are independent).

### 4. Wire meridian-docs to the published package

Once `meridian-plugin-base==0.1.0` is on PyPI:

**a. Update `extensions/meridian-docs/pyproject.toml`:**
```toml
[project]
dependencies = [
    "mcp>=1.0",
    "meridian-plugin-base>=0.1",    # <-- add this
]
```

**b. Delete the vendored copy:**
```
rm extensions/meridian-docs/meridian_docs/_vendored_content_tree.py
```

**c. Update imports in `local_ingest.py`:**
```python
# Before:
from meridian_docs._vendored_content_tree import document_content_tree
# After:
from meridian_plugin_base.ooxml import document_content_tree
```

**d. Update imports in `docs_intel.py`** (the `index_docx_structure` function):
```python
# Before:
from ._vendored_content_tree import document_content_tree
# After:
from meridian_plugin_base.ooxml import document_content_tree
```

**e. Update `meridian_docs/__init__.py`** to reflect the new dependency:
```python
# Remove/update the note about vendoring:
# "until the parser is fully extracted, keep this copy in sync with the source..."
```

### 5. Decide the fate of packages/docparse

`packages/docparse/docparse/docs_intel.py` contains a more feature-rich version of
`document_content_tree` (with citation marker extraction, structural_parser ABC, LaTeX
support via latex_intel). Two options:

**Option A (recommended): keep docparse as the extended server-side layer**
- `packages/docparse` keeps its full-featured `docs_intel` (cite markers, latex, etc.)
- `packages/meridian-plugin-base` is the minimal stdlib-only subset for plugins
- `meridian/docs_intel.py` (the compat shim) keeps importing from `docparse`
- The two `document_content_tree` implementations diverge by design: the plugin-base
  version is minimal/stable; the docparse version has server-side extras

**Option B: merge into meridian-plugin-base**
- Move all of docparse's `docs_intel` content into `meridian_plugin_base.ooxml`
- Deprecate `packages/docparse`; make it a thin re-export from `meridian_plugin_base`
- Requires a more careful version bump (the cite-marker and latex features are not
  wanted in the minimal plugin base)

Recommendation: Option A for now. Revisit when a third plugin (beyond meridian-docs)
also needs the citation/LaTeX features.

### 6. Future plugins (meridian-research, meridian-figma, ...)

Each future plugin that needs to call back to the hosted server adds:
```toml
dependencies = ["mcp>=1.0", "meridian-plugin-base>=0.1"]
```
and imports:
```python
from meridian_plugin_base.ingest_client import call_mcp_tool
```

No more per-plugin urllib copy-paste.

## Summary of what blocks the publish today

1. **PyPI trusted-publisher not set up** for `meridian-plugin-base` (nor for
   `meridian-server`, per `reference_release_pipeline.md`). Requires PyPI account access
   (repo owner action, not an automated agent action).
2. **No `publish-plugin-base.yml` workflow exists yet** (step 3 above creates it).
3. **No `plugin-base-v*` tag has been pushed** (step 3 trigger).

None of these require code changes — they are account/configuration actions.
