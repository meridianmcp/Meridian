"""Convert one paper_search result row into a Zotero connector item.

This module is pure: it makes no network call, touches no file, and never talks
to Zotero. The item it returns has the connector shape that
``meridian.zotero_filer.save_items`` accepts, and the caller decides when, or
whether, to send it.

Rows come from the parsers in ``meridian.paper_search``. Each row carries one
source identifier (``arxiv_id``, ``openalex_id``, ``s2_id``, ``pmid`` or
``core_id``), a ``title``, ``authors`` as display-name strings, ``summary``,
``published``, ``url`` and ``doi``. Crossref and CORE rows also carry ``venue``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

_DOI_PREFIX_RE = re.compile(r"^(?:https?://)?(?:dx\.)?doi\.org/", re.IGNORECASE)
_ISO_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")


def _text(value: Any) -> str:
    """Return a string value with its whitespace collapsed, or "" for anything else."""
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())


def _venue(row: Mapping[str, Any]) -> str:
    """Return the journal or venue name a row carries, or "" when it has none."""
    return _text(row.get("venue")) or _text(row.get("journal"))


def _item_type(row: Mapping[str, Any]) -> str:
    """An arXiv row with no journal is a preprint. Every other row is a journal article."""
    if _text(row.get("arxiv_id")) and not _venue(row):
        return "preprint"
    return "journalArticle"


def _date(value: Any) -> str:
    """Keep the calendar date. A timestamp such as 2021-01-02T18:00:00Z becomes 2021-01-02."""
    text = _text(value)
    match = _ISO_DATE_RE.match(text)
    return match.group(1) if match else text


def _creators(authors: Any) -> list[dict[str, str]]:
    """Map author display names to author creators.

    Each name goes in the single-field ``name`` form. Splitting on whitespace would
    mangle organisation names such as "Google DeepMind", and a row carries no
    reliable first and last name split.
    """
    if not isinstance(authors, (list, tuple)):
        return []
    creators: list[dict[str, str]] = []
    for raw in authors:
        name = _text(raw)
        if name:
            creators.append({"creatorType": "author", "name": name})
    return creators


def paper_to_connector_item(row: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return the connector item for one paper_search row, or None when it has no title.

    Absent or empty fields are left out of the item rather than set to "". The
    fields are itemType, title, creators, date, DOI, url, publicationTitle and
    abstractNote. The pdf_url and citation fields are not mapped.
    """
    if not isinstance(row, Mapping):
        return None
    title = _text(row.get("title"))
    if not title:
        return None

    item: dict[str, Any] = {"itemType": _item_type(row), "title": title}

    creators = _creators(row.get("authors"))
    if creators:
        item["creators"] = creators

    date = _date(row.get("published"))
    if date:
        item["date"] = date

    doi = _DOI_PREFIX_RE.sub("", _text(row.get("doi")))
    if doi:
        item["DOI"] = doi

    url = _text(row.get("url"))
    if url:
        item["url"] = url

    venue = _venue(row)
    if venue:
        item["publicationTitle"] = venue

    summary = _text(row.get("summary"))
    if summary:
        item["abstractNote"] = summary

    return item
