"""docparse — deterministic OOXML (.docx) + LaTeX structural parsing.

Meridian's **detachable** document-intelligence sub-package (d45c2cc8): a
self-contained, standalone-installable parser (``pip install -e ./packages/docparse``)
that lives inside the Meridian monorepo but has **no dependency on the rest of
Meridian** — pure stdlib. Import the submodules directly:

    from docparse.docs_intel import document_outline, document_content_tree
    from docparse.latex_intel import analyze_latex
"""
from __future__ import annotations

from . import docs_intel, latex_intel  # noqa: F401 — make submodules importable

__version__ = "0.1.0"
__all__ = ["docs_intel", "latex_intel"]
