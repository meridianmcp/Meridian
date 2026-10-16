"""Tests for meridian.paper_zotero_item.

Each source row below mirrors the shape its parser in meridian.paper_search
returns. Nothing here touches the network or Zotero: the no-network test
replaces the socket entry points with functions that fail the test if called,
and the save_items round trip uses a fake transport.
"""

from __future__ import annotations

import copy
import json
import socket

import pytest

from meridian import paper_zotero_item as pzi
from meridian import zotero_filer as zf

ARXIV_ROW = {
    "arxiv_id": "2101.00001",
    "title": "Sparse   Attention\nRevisited",
    "authors": ["Jane Smith", "Google DeepMind"],
    "summary": "We study  sparse attention.",
    "published": "2021-01-02T18:00:00Z",
    "updated": "2021-01-03T00:00:00Z",
    "url": "https://arxiv.org/abs/2101.00001",
    "pdf_url": "https://arxiv.org/pdf/2101.00001",
}

OPENALEX_ROW = {
    "openalex_id": "W2741809807",
    "title": "Graph Methods for Citation Data",
    "authors": ["Ada Lovelace"],
    "summary": "Citation graphs.",
    "published": "2020-05-10",
    "updated": "2020-06-01",
    "url": "https://www.example-journal.org/articles/1",
    "pdf_url": "",
    "doi": "https://doi.org/10.1000/openalex.1",
}

S2_ROW = {
    "s2_id": "9f2c1",
    "title": "Retrieval for Long Documents",
    "authors": ["Grace Hopper", "Alan Turing"],
    "summary": "",
    "published": "2019-07-01",
    "updated": "",
    "url": "https://www.semanticscholar.org/paper/9f2c1",
    "pdf_url": "",
    "citation_count": 12,
    "doi": "10.5555/s2.1",
    "tldr": "Short.",
}

PUBMED_ROW = {
    "pmid": "31234567",
    "title": "Clinical Signals in Records",
    "authors": ["Mary Curie"],
    "summary": "Records study.",
    "published": "2018-03-04",
    "updated": "",
    "url": "https://pubmed.ncbi.nlm.nih.gov/31234567/",
    "pdf_url": "",
    "doi": "10.1000/pm.1",
}

CROSSREF_ROW = {
    "doi": "10.1000/cr.1",
    "title": "Crossref Study",
    "authors": ["Linus Pauling"],
    "summary": "Crossref abstract.",
    "published": "2017-11-20",
    "updated": "2017-12-01",
    "url": "https://doi.org/10.1000/cr.1",
    "pdf_url": "",
    "venue": "Journal of Crossref Things",
    "issn": ["1234-5678"],
    "publisher": "Pub Co",
    "type": "journal-article",
    "citation_count": 3,
}

CORE_ROW = {
    "core_id": "987654",
    "title": "CORE Aggregated Work",
    "authors": ["Rosalind Franklin"],
    "summary": "",
    "published": "2016-02-29T00:00:00",
    "updated": "2016-03-01",
    "url": "https://core.ac.uk/display/987654",
    "pdf_url": "https://core.ac.uk/download/987654.pdf",
    "doi": "",
    "venue": "CORE Venue",
    "publisher": "",
    "has_full_text": True,
}

# Field names the connector item may use. Taken from the fixtures in
# tests/test_zotero_filer.py; no other names are sent.
CONNECTOR_FIELDS = {
    "itemType", "title", "creators", "date", "DOI", "url", "publicationTitle", "abstractNote",
}

SOURCE_CASES = [
    (
        "arxiv",
        ARXIV_ROW,
        {
            "itemType": "preprint",
            "title": "Sparse Attention Revisited",
            "creators": [
                {"creatorType": "author", "name": "Jane Smith"},
                {"creatorType": "author", "name": "Google DeepMind"},
            ],
            "date": "2021-01-02",
            "url": "https://arxiv.org/abs/2101.00001",
            "abstractNote": "We study sparse attention.",
        },
    ),
    (
        "openalex",
        OPENALEX_ROW,
        {
            "itemType": "journalArticle",
            "title": "Graph Methods for Citation Data",
            "creators": [{"creatorType": "author", "name": "Ada Lovelace"}],
            "date": "2020-05-10",
            "DOI": "10.1000/openalex.1",
            "url": "https://www.example-journal.org/articles/1",
            "abstractNote": "Citation graphs.",
        },
    ),
    (
        "semantic_scholar",
        S2_ROW,
        {
            "itemType": "journalArticle",
            "title": "Retrieval for Long Documents",
            "creators": [
                {"creatorType": "author", "name": "Grace Hopper"},
                {"creatorType": "author", "name": "Alan Turing"},
            ],
            "date": "2019-07-01",
            "DOI": "10.5555/s2.1",
            "url": "https://www.semanticscholar.org/paper/9f2c1",
        },
    ),
    (
        "pubmed",
        PUBMED_ROW,
        {
            "itemType": "journalArticle",
            "title": "Clinical Signals in Records",
            "creators": [{"creatorType": "author", "name": "Mary Curie"}],
            "date": "2018-03-04",
            "DOI": "10.1000/pm.1",
            "url": "https://pubmed.ncbi.nlm.nih.gov/31234567/",
            "abstractNote": "Records study.",
        },
    ),
    (
        "crossref",
        CROSSREF_ROW,
        {
            "itemType": "journalArticle",
            "title": "Crossref Study",
            "creators": [{"creatorType": "author", "name": "Linus Pauling"}],
            "date": "2017-11-20",
            "DOI": "10.1000/cr.1",
            "url": "https://doi.org/10.1000/cr.1",
            "publicationTitle": "Journal of Crossref Things",
            "abstractNote": "Crossref abstract.",
        },
    ),
    (
        "core",
        CORE_ROW,
        {
            "itemType": "journalArticle",
            "title": "CORE Aggregated Work",
            "creators": [{"creatorType": "author", "name": "Rosalind Franklin"}],
            "date": "2016-02-29",
            "url": "https://core.ac.uk/display/987654",
            "publicationTitle": "CORE Venue",
        },
    ),
]


@pytest.mark.parametrize(("source", "row", "expected"), SOURCE_CASES, ids=[c[0] for c in SOURCE_CASES])
def test_each_source_row_maps_to_its_connector_item(source, row, expected):
    assert pzi.paper_to_connector_item(row) == expected


@pytest.mark.parametrize(("source", "row", "expected"), SOURCE_CASES, ids=[c[0] for c in SOURCE_CASES])
def test_connector_item_uses_only_connector_field_names(source, row, expected):
    item = pzi.paper_to_connector_item(row)
    assert set(item) <= CONNECTOR_FIELDS
    assert all(c["creatorType"] == "author" for c in item.get("creators", []))


def test_arxiv_row_with_a_journal_is_a_journal_article():
    row = dict(ARXIV_ROW, venue="Journal of Preprints Made Real")
    item = pzi.paper_to_connector_item(row)
    assert item["itemType"] == "journalArticle"
    assert item["publicationTitle"] == "Journal of Preprints Made Real"


def test_arxiv_row_with_blank_venue_is_still_a_preprint():
    row = dict(ARXIV_ROW, venue="   ", journal="")
    assert pzi.paper_to_connector_item(row)["itemType"] == "preprint"


def test_journal_key_is_accepted_as_the_venue():
    row = {"openalex_id": "W1", "title": "T", "journal": "J. Things"}
    item = pzi.paper_to_connector_item(row)
    assert item == {"itemType": "journalArticle", "title": "T", "publicationTitle": "J. Things"}


def test_absent_and_empty_fields_are_omitted_not_blanked():
    row = {
        "title": "Only A Title",
        "authors": ["", "   "],
        "published": "",
        "doi": "",
        "url": "",
        "venue": "",
        "summary": "",
    }
    assert pzi.paper_to_connector_item(row) == {"itemType": "journalArticle", "title": "Only A Title"}


@pytest.mark.parametrize(
    "row",
    [{}, {"title": ""}, {"title": "   \n"}, {"title": None}, {"title": 42}],
    ids=["missing", "empty", "whitespace", "none", "non-string"],
)
def test_row_without_a_usable_title_returns_none(row):
    assert pzi.paper_to_connector_item(row) is None


@pytest.mark.parametrize("row", [None, "a string", ["a", "list"]], ids=["none", "str", "list"])
def test_non_mapping_row_returns_none(row):
    assert pzi.paper_to_connector_item(row) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://doi.org/10.1/a", "10.1/a"),
        ("http://dx.doi.org/10.1/a", "10.1/a"),
        ("doi.org/10.1/a", "10.1/a"),
        ("10.1/a", "10.1/a"),
    ],
)
def test_doi_loses_its_resolver_prefix(raw, expected):
    item = pzi.paper_to_connector_item({"title": "T", "doi": raw})
    assert item["DOI"] == expected


@pytest.mark.parametrize(
    ("published", "expected"),
    [
        ("2016-02-29T00:00:00", "2016-02-29"),
        ("2021-01-02T18:00:00Z", "2021-01-02"),
        ("2016", "2016"),
        ("March 2016", "March 2016"),
    ],
)
def test_date_keeps_the_calendar_date_and_passes_other_text_through(published, expected):
    assert pzi.paper_to_connector_item({"title": "T", "published": published})["date"] == expected


def test_authors_that_are_not_a_list_produce_no_creators():
    assert "creators" not in pzi.paper_to_connector_item({"title": "T", "authors": "Jane Smith"})
    assert "creators" not in pzi.paper_to_connector_item({"title": "T", "authors": None})


def test_non_string_field_values_are_ignored():
    row = {"title": "T", "summary": 5, "url": ["x"], "doi": None, "venue": {"a": 1}, "published": 2020}
    assert pzi.paper_to_connector_item(row) == {"itemType": "journalArticle", "title": "T"}


def test_conversion_does_not_mutate_the_input_row():
    row = copy.deepcopy(ARXIV_ROW)
    pzi.paper_to_connector_item(row)
    assert row == ARXIV_ROW


def test_conversion_makes_no_network_or_zotero_call(monkeypatch):
    attempts = []

    def refuse(*args, **kwargs):
        attempts.append((args, kwargs))
        raise AssertionError("network or Zotero call attempted")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(zf, "_send", refuse)
    monkeypatch.setattr(zf, "save_items", refuse)

    for _source, row, _expected in SOURCE_CASES:
        pzi.paper_to_connector_item(row)
    pzi.paper_to_connector_item({"title": ""})

    assert attempts == []


def test_item_passes_through_save_items_unchanged():
    sent = []

    def transport(method, url, body, headers):
        sent.append({"method": method, "url": url, "body": body})
        return 201, "{}"

    item = pzi.paper_to_connector_item(ARXIV_ROW)
    result = zf.save_items([item], transport=transport)

    assert result == {"status": "written", "http_status": 201}
    assert len(sent) == 1
    assert sent[0]["method"] == "POST"
    assert sent[0]["url"] == zf.CONNECTOR + "/saveItems"
    assert json.loads(sent[0]["body"])["items"] == [item]
