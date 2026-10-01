"""Canonical identity for a finding's source (fe0b0331).

``save_finding`` / ``capture_research_finding`` store the provenance URL of a
finding in ``project_notes.source``.  The same paper is routinely captured
under several spellings of that URL -- ``https://doi.org/10.1145/X`` vs
``https://dl.acm.org/doi/10.1145/x``, ``arxiv.org/abs/2301.12345v1`` vs
``arxiv.org/pdf/2301.12345v3`` -- so an exact string match cannot detect an
in-project duplicate.  :func:`finding_identity` reduces a source string to one
canonical key so two spellings of the same work compare equal:

* **DOI**   -> ``doi:<case-folded doi>``  (DOIs are case-insensitive)
* **arXiv** -> ``arxiv:<id>``             (the ``vN`` version suffix is dropped)
* **PMID**  -> ``pmid:<digits>``
* anything else that is an http(s) URL -> ``url:<host><path>[?<query>]``
  (scheme, ``www.``, fragment, trailing slash, default port and tracking
  parameters are ignored; remaining query parameters are sorted)

A source that is empty or is not recognisably a URL / scholarly identifier
(free text, a bare file path, ...) has **no** identity: :func:`finding_identity`
returns ``None`` and the caller must never treat it as a duplicate.  The design
is deliberately conservative -- a false *merge* silently swallows a distinct
finding, a false *miss* merely leaves a duplicate that already exists today.

Pure and dependency-free (stdlib only) so it is trivially unit-testable and safe
to import from both the SQLite and Postgres code paths.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

__all__ = ["finding_identity"]

# arXiv identifier: new style (YYMM.NNNN[N]) or old style (archive[.XX]/YYMMNNN).
_ARXIV_ID = r"(?P<id>\d{4}\.\d{4,5}|[a-z][a-z\-]*(?:\.[a-z]{2})?/\d{7})(?:v\d+)?"
_ARXIV_HOST_RE = re.compile(
    r"^(?:ar5iv\.org|(?:[a-z0-9\-]+\.)*arxiv\.org)$", re.IGNORECASE
)
_ARXIV_PATH_RE = re.compile(
    r"^/(?:abs|pdf|html|format|src|ps)/" + _ARXIV_ID + r"(?:\.pdf)?/?$",
    re.IGNORECASE,
)
_ARXIV_PREFIX_RE = re.compile(
    r"^\s*arxiv\s*:\s*" + _ARXIV_ID + r"\s*$", re.IGNORECASE
)
# DataCite arXiv DOIs: 10.48550/arXiv.<id>.
_ARXIV_DOI_RE = re.compile(
    r"^10\.48550/arxiv\." + _ARXIV_ID + r"$", re.IGNORECASE
)

_PMID_PREFIX_RE = re.compile(r"^\s*pmid\s*[:#]?\s*(?P<id>\d{1,9})\s*$", re.IGNORECASE)
_PUBMED_HOST_RE = re.compile(r"^pubmed\.ncbi\.nlm\.nih\.gov$", re.IGNORECASE)
_NCBI_HOST_RE = re.compile(r"^(?:www\.)?ncbi\.nlm\.nih\.gov$", re.IGNORECASE)
_EPMC_HOST_RE = re.compile(r"^(?:www\.)?europepmc\.org$", re.IGNORECASE)

# A DOI: registrant "10.NNNN(N..)" / suffix.  The lookbehind stops the match
# starting mid-token (``v10.2000/x``, ``1.10.2000/x``).  ``?``/``#``/``&`` end a
# DOI inside a URL (query / fragment); whitespace and quotes always do.
_DOI_RE = re.compile(
    r"(?<![A-Za-z0-9.])(?P<doi>10\.\d{4,9}/[^\s\"'<>?#&]+)", re.IGNORECASE
)
# A DOI given bare, ``doi:``-prefixed, or as a scheme-less doi.org address.
_DOI_PREFIX_RE = re.compile(
    r"^\s*(?:doi\s*[:=]\s*|(?:dx\.)?doi\.org/)?(?P<rest>10\.\d{4,9}/\S+)\s*$",
    re.IGNORECASE,
)
_DOI_HOST_RE = re.compile(r"^(?:[a-z0-9\-]+\.)*doi\.org$", re.IGNORECASE)
# Publisher landing-page decorations that trail a DOI in the URL path.
_DOI_TRAILING_SEGMENT_RE = re.compile(
    r"/(?:pdf|epdf|full|fulltext|abstract|abs)$", re.IGNORECASE
)

_TRACKING_PARAM_RE = re.compile(
    r"^(?:utm_.*|fbclid|gclid|dclid|msclkid|mc_cid|mc_eid|igshid|ref_src|ref_url|"
    r"_hsenc|_hsmi|hsctatracking)$",
    re.IGNORECASE,
)


def _strip_trailing_punct(doi: str) -> str:
    """Drop sentence punctuation that trails a DOI copied out of prose.

    A closing ``)`` is kept when it balances a ``(`` inside the DOI (legacy
    SICI-style DOIs end in one).
    """
    while doi:
        last = doi[-1]
        if last in ".,;:'\"]}>":
            doi = doi[:-1]
        elif last == ")" and doi.count(")") > doi.count("("):
            doi = doi[:-1]
        else:
            break
    return doi


def _arxiv_key(match: "re.Match[str]") -> str:
    return f"arxiv:{match.group('id').casefold()}"


def _doi_key(raw_doi: str, *, from_url: bool) -> str | None:
    """Canonical key for an already percent-decoded DOI, or ``None``."""
    doi = _strip_trailing_punct(raw_doi.strip())
    if from_url:
        doi = _DOI_TRAILING_SEGMENT_RE.sub("", doi)
    doi = doi.casefold()
    if not doi or "/" not in doi:
        return None
    arxiv = _ARXIV_DOI_RE.match(doi)
    if arxiv:  # the arXiv-issued DOI is the same work as its arxiv.org URL
        return _arxiv_key(arxiv)
    return f"doi:{doi}"


def _normalise_url(raw: str) -> str | None:
    text = raw.strip()
    if text.lower().startswith("www."):
        text = "https://" + text
    try:
        parts = urlsplit(text)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not host:
        return None
    if host.startswith("www."):
        host = host[4:]
    netloc = host
    if port and port not in (80, 443):
        netloc = f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", unquote(parts.path)).rstrip("/")
    query_pairs = sorted(
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not _TRACKING_PARAM_RE.match(k)
    )
    key = f"url:{netloc}{path}"
    if query_pairs:
        key += "?" + urlencode(query_pairs)
    return key


def finding_identity(source: str | None) -> str | None:
    """Return the canonical identity key for ``source``, or ``None``.

    See the module docstring for the key grammar.  ``None`` means "no stable
    identity" -- never a duplicate of anything.
    """
    if not isinstance(source, str):
        return None
    text = source.strip()
    if not text:
        return None

    # -- explicit, prefixed identifiers (no URL) ---------------------------
    m = _ARXIV_PREFIX_RE.match(text)
    if m:
        return _arxiv_key(m)
    m = _PMID_PREFIX_RE.match(text)
    if m:
        return f"pmid:{int(m.group('id'))}"
    m = _DOI_PREFIX_RE.match(text)
    if m:
        return _doi_key(unquote(m.group("rest")), from_url=False)

    # -- URLs ---------------------------------------------------------------
    candidate = text
    if candidate.lower().startswith("www."):
        candidate = "https://" + candidate
    try:
        parts = urlsplit(candidate)
        host = (parts.hostname or "").lower()
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not host:
        return None  # free text / file path: no identity, never a duplicate

    if _ARXIV_HOST_RE.match(host):
        m = _ARXIV_PATH_RE.match(unquote(parts.path))
        if m:
            return _arxiv_key(m)

    if _PUBMED_HOST_RE.match(host):
        m = re.match(r"^/(?P<id>\d{1,9})/?$", parts.path)
        if m:
            return f"pmid:{int(m.group('id'))}"
    if _NCBI_HOST_RE.match(host):
        m = re.match(r"^/pubmed/(?P<id>\d{1,9})/?$", parts.path, re.IGNORECASE)
        if m:
            return f"pmid:{int(m.group('id'))}"
    if _EPMC_HOST_RE.match(host):
        m = re.match(r"^/(?:article|abstract)/med/(?P<id>\d{1,9})/?$", parts.path, re.IGNORECASE)
        if m:
            return f"pmid:{int(m.group('id'))}"

    # A DOI in the path or query (doi.org, dl.acm.org/doi/..., link.springer.com/
    # article/..., ?doi=...).  doi.org paths are DOI-only, so the decorative
    # trailing-segment strip is skipped there.
    haystack = unquote(parts.path + ("?" + parts.query if parts.query else ""))
    m = _DOI_RE.search(haystack)
    if m:
        key = _doi_key(m.group("doi"), from_url=not _DOI_HOST_RE.match(host))
        if key:
            return key

    return _normalise_url(candidate)
