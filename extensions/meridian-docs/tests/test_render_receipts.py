"""Tests for render_gate.py's durable render receipts (1e6150ef / MDE-7 P1).

Covers:
  - _pdf_receipt_fields: hash/size/page-count computed from produced PDF
    bytes, never raises on an unreadable path.
  - _field_refresh_status: the <w:updateFields> settings.xml inspection.
  - check_render_capability: backend_order recorded on every result status;
    a "rendered" result's detail carries attempts.
  - render_with_receipt: durable receipt shape, visual_qa never defaults to
    "verified" on backend-conversion success alone, persistence survives a
    fresh read, process_identity only for the word-com backend.
  - check_release_render_gate: fresh/stale/content-changed/failed-status
    rejection, the audited degraded override path, visually_verified stays
    a strictly separate signal from release_ready.
  - _word_com_process_worker emits a "version" message (isolated/child-
    process path), mirroring the in-thread path's own owned["version"].
"""
from __future__ import annotations

import io
import sys
import types
import zipfile
from typing import Any, Callable

import pytest

from meridian_docs import render_gate, server


def _write_dummy_docx(tmp_path, name: str = "doc.docx") -> str:
    path = tmp_path / name
    path.write_bytes(b"not a real docx -- these tests never parse this content")
    return str(path)


_SETTINGS_XML_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<w:settings xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
{body}
</w:settings>
"""


def _write_docx_with_settings(tmp_path, settings_body: str | None, name: str = "doc.docx") -> str:
    path = tmp_path / name
    with zipfile.ZipFile(str(path), "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", "<w:document/>")
        if settings_body is not None:
            archive.writestr("word/settings.xml", _SETTINGS_XML_TEMPLATE.format(body=settings_body))
    return str(path)


def _fake_backend(
    name: str,
    *,
    available: bool = True,
    reason: str | None = None,
    render: Callable[[str], dict[str, Any]] | None = None,
) -> render_gate.RenderBackend:
    def _unavailable_reason() -> str | None:
        return None if available else reason

    return render_gate.RenderBackend(
        name=name,
        unavailable_reason=_unavailable_reason,
        render=render if render is not None else (lambda path: {}),
    )


# ---------------------------------------------------------------------------
# _pdf_receipt_fields
# ---------------------------------------------------------------------------

class TestPdfReceiptFields:
    def test_unreadable_path_yields_none_fields(self, tmp_path) -> None:
        missing = str(tmp_path / "nope.pdf")
        fields = render_gate._pdf_receipt_fields(missing)
        assert fields == {"pdf_sha256": None, "pdf_size_bytes": None, "page_count": None}

    def test_computes_hash_size_and_page_count(self, tmp_path) -> None:
        pdf_path = tmp_path / "out.pdf"
        content = (
            b"%PDF-1.4\n"
            b"1 0 obj<< /Type /Catalog >>endobj\n"
            b"2 0 obj<< /Type /Pages /Count 2 >>endobj\n"
            b"3 0 obj<< /Type /Page >>endobj\n"
            b"4 0 obj<< /Type /Page >>endobj\n"
        )
        pdf_path.write_bytes(content)

        fields = render_gate._pdf_receipt_fields(str(pdf_path))

        assert fields["page_count"] == 2  # /Type /Pages must not be counted as a page
        assert fields["pdf_size_bytes"] == len(content)
        import hashlib
        assert fields["pdf_sha256"] == hashlib.sha256(content).hexdigest()

    def test_no_page_markers_is_none_not_zero(self, tmp_path) -> None:
        pdf_path = tmp_path / "out.pdf"
        pdf_path.write_bytes(b"%PDF-1.4\nnothing page-shaped here")
        fields = render_gate._pdf_receipt_fields(str(pdf_path))
        assert fields["page_count"] is None


# ---------------------------------------------------------------------------
# _field_refresh_status
# ---------------------------------------------------------------------------

class TestFieldRefreshStatus:
    def test_no_settings_xml_is_unknown(self, tmp_path) -> None:
        path = _write_docx_with_settings(tmp_path, None)
        raw = open(path, "rb").read()
        assert render_gate._field_refresh_status(raw) == "unknown"

    def test_malformed_settings_xml_is_unknown(self, tmp_path) -> None:
        path = tmp_path / "doc.docx"
        with zipfile.ZipFile(str(path), "w") as archive:
            archive.writestr("word/settings.xml", "<not-valid-xml")
        raw = open(path, "rb").read()
        assert render_gate._field_refresh_status(raw) == "unknown"

    def test_update_fields_present_no_val_is_auto_update(self, tmp_path) -> None:
        path = _write_docx_with_settings(tmp_path, '<w:updateFields/>')
        raw = open(path, "rb").read()
        assert render_gate._field_refresh_status(raw) == "will_auto_update"

    def test_update_fields_explicit_true_is_auto_update(self, tmp_path) -> None:
        path = _write_docx_with_settings(tmp_path, '<w:updateFields w:val="true"/>')
        raw = open(path, "rb").read()
        assert render_gate._field_refresh_status(raw) == "will_auto_update"

    def test_update_fields_explicit_false_is_not_configured(self, tmp_path) -> None:
        path = _write_docx_with_settings(tmp_path, '<w:updateFields w:val="false"/>')
        raw = open(path, "rb").read()
        assert render_gate._field_refresh_status(raw) == "not_configured"

    def test_no_update_fields_element_is_not_configured(self, tmp_path) -> None:
        path = _write_docx_with_settings(tmp_path, "<w:zoom w:percent=\"100\"/>")
        raw = open(path, "rb").read()
        assert render_gate._field_refresh_status(raw) == "not_configured"


# ---------------------------------------------------------------------------
# check_render_capability: backend_order + attempts-on-success
# ---------------------------------------------------------------------------

class TestBackendOrderRecorded:
    def test_backend_order_recorded_on_rendered_result(self, tmp_path) -> None:
        docx_path = _write_dummy_docx(tmp_path)
        a = _fake_backend("backend-a", available=False, reason="nope")
        b = _fake_backend("backend-b", available=True, render=lambda p: {"ok": True})

        result = render_gate.check_render_capability(docx_path, backends=[a, b])

        assert result["status"] == render_gate.RENDERED
        assert result["backend_order"] == ["backend-a", "backend-b"]
        assert result["detail"]["attempts"] == 1

    def test_backend_order_recorded_on_unavailable_result(self, tmp_path) -> None:
        docx_path = _write_dummy_docx(tmp_path)
        a = _fake_backend("backend-a", available=False, reason="nope")

        result = render_gate.check_render_capability(docx_path, backends=[a])

        assert result["status"] == render_gate.UNAVAILABLE_WITH_REASON
        assert result["backend_order"] == ["backend-a"]

    def test_backend_order_recorded_on_failed_result(self, tmp_path) -> None:
        docx_path = _write_dummy_docx(tmp_path)

        def _boom(path: str) -> dict[str, Any]:
            raise render_gate.RenderCapabilityError("broken")

        a = _fake_backend("backend-a", available=True, render=_boom)
        result = render_gate.check_render_capability(docx_path, backends=[a])

        assert result["status"] == render_gate.FAILED
        assert result["backend_order"] == ["backend-a"]

    def test_backend_order_recorded_even_on_missing_file(self, tmp_path) -> None:
        missing = str(tmp_path / "nope.docx")
        a = _fake_backend("backend-a")
        result = render_gate.check_render_capability(missing, backends=[a])
        assert result["status"] == render_gate.FAILED
        assert result["backend_order"] == ["backend-a"]

    def test_configurable_order_changes_which_backend_is_recorded_first(self, tmp_path) -> None:
        docx_path = _write_dummy_docx(tmp_path)
        x = _fake_backend("x", render=lambda p: {})
        y = _fake_backend("y", render=lambda p: {})

        result_xy = render_gate.check_render_capability(docx_path, backends=[x, y])
        result_yx = render_gate.check_render_capability(docx_path, backends=[y, x])

        assert result_xy["backend_order"] == ["x", "y"]
        assert result_xy["backend"] == "x"
        assert result_yx["backend_order"] == ["y", "x"]
        assert result_yx["backend"] == "y"


# ---------------------------------------------------------------------------
# render_with_receipt
# ---------------------------------------------------------------------------

class TestRenderWithReceipt:
    def test_rendered_receipt_shape_and_visual_qa_defaults_not_reviewed(self, tmp_path) -> None:
        docx_path = _write_dummy_docx(tmp_path)
        backend = _fake_backend(
            "fake-ok",
            render=lambda p: {
                "converted_via": "fake-ok", "pdf_sha256": "abc", "pdf_size_bytes": 10,
                "page_count": 3, "pid": None, "backend_version": "9.9",
            },
        )

        receipt = render_gate.render_with_receipt(docx_path, backends=[backend])

        assert receipt["status"] == render_gate.RENDERED
        assert receipt["backend"] == "fake-ok"
        assert receipt["backend_order"] == ["fake-ok"]
        assert receipt["backend_version"] == "9.9"
        assert receipt["pdf_sha256"] == "abc"
        assert receipt["page_count"] == 3
        assert receipt["attempts"] == 1
        assert receipt["timed_out"] is False
        assert receipt["source_docx_sha256"]  # computed from the real docx bytes
        # The critical distinction this item exists for: backend conversion
        # success is NEVER, by itself, visual verification.
        assert receipt["visual_qa"]["status"] == "not_reviewed"
        assert "not a human" in receipt["visual_qa"]["note"] or "visual QA" in receipt["visual_qa"]["note"]
        assert receipt["kind"] == "render"
        assert receipt["receipt_id"]
        assert receipt["created_at"]
        assert receipt["created_at_epoch"] > 0

    def test_explicit_visual_qa_is_used_verbatim(self, tmp_path) -> None:
        docx_path = _write_dummy_docx(tmp_path)
        backend = _fake_backend("fake-ok", render=lambda p: {})
        receipt = render_gate.render_with_receipt(
            docx_path, backends=[backend],
            visual_qa={"status": "verified", "reviewer": "alice"},
        )
        assert receipt["visual_qa"] == {"status": "verified", "reviewer": "alice"}

    def test_failed_receipt_has_no_pdf_fields_and_not_applicable_visual_qa(self, tmp_path) -> None:
        docx_path = _write_dummy_docx(tmp_path)

        def _boom(path: str) -> dict[str, Any]:
            raise render_gate.RenderCapabilityError(
                "broken", error_class=render_gate.CORRUPTION_ERROR,
            )

        backend = _fake_backend("fake-broken", render=_boom)
        receipt = render_gate.render_with_receipt(docx_path, backends=[backend])

        assert receipt["status"] == render_gate.FAILED
        assert receipt["pdf_sha256"] is None
        assert receipt["page_count"] is None
        assert receipt["error_class"] == render_gate.CORRUPTION_ERROR
        assert receipt["visual_qa"]["status"] == "not_applicable"

    def test_unavailable_receipt_status(self, tmp_path) -> None:
        docx_path = _write_dummy_docx(tmp_path)
        backend = _fake_backend("fake-missing", available=False, reason="not installed")
        receipt = render_gate.render_with_receipt(docx_path, backends=[backend])
        assert receipt["status"] == render_gate.UNAVAILABLE_WITH_REASON
        assert receipt["visual_qa"]["status"] == "not_applicable"

    def test_process_identity_only_recorded_for_word_com_backend(self, tmp_path) -> None:
        docx_path = _write_dummy_docx(tmp_path)
        word_backend = render_gate.RenderBackend(
            name=render_gate._WORD_COM_BACKEND.name,
            unavailable_reason=lambda: None,
            render=lambda p: {"pid": 4242},
        )
        receipt = render_gate.render_with_receipt(docx_path, backends=[word_backend])
        assert receipt["process_identity"] == {"pid": 4242, "owned": True}

    def test_process_identity_none_for_non_word_backend(self, tmp_path) -> None:
        docx_path = _write_dummy_docx(tmp_path)
        backend = _fake_backend("libreoffice-soffice", render=lambda p: {"pid": None})
        receipt = render_gate.render_with_receipt(docx_path, backends=[backend])
        assert receipt["process_identity"] is None

    def test_receipt_persists_and_survives_a_fresh_read(self, tmp_path) -> None:
        docx_path = _write_dummy_docx(tmp_path)
        receipts_path = str(tmp_path / "receipts.json")
        backend = _fake_backend("fake-ok", render=lambda p: {"pdf_sha256": "xyz"})

        receipt = render_gate.render_with_receipt(
            docx_path, backends=[backend], receipts_path=receipts_path,
        )

        # A totally independent read (as if the process restarted, and the
        # backend's own tempdir is long gone) still finds the receipt.
        reloaded = render_gate.list_render_receipts(receipts_path, docx_path=docx_path)
        assert len(reloaded) == 1
        assert reloaded[0]["receipt_id"] == receipt["receipt_id"]
        assert reloaded[0]["pdf_sha256"] == "xyz"

    def test_no_receipts_path_does_not_persist(self, tmp_path) -> None:
        docx_path = _write_dummy_docx(tmp_path)
        backend = _fake_backend("fake-ok", render=lambda p: {})
        render_gate.render_with_receipt(docx_path, backends=[backend])
        # Nothing to check directly (no path was given) -- this test's real
        # assertion is simply that no receipts_path is required for the call
        # to succeed and return a full receipt dict.

    def test_check_result_reuse_skips_a_fresh_render(self, tmp_path) -> None:
        docx_path = _write_dummy_docx(tmp_path)
        calls: list[str] = []

        def _boom(p: str) -> dict[str, Any]:
            calls.append(p)
            raise AssertionError("must not render again when check_result is supplied")

        backend = _fake_backend("fake-ok", render=_boom)
        precomputed = {
            "status": render_gate.RENDERED, "backend": "fake-ok",
            "backend_order": ["fake-ok"],
            "detail": {"pdf_sha256": "precomputed-hash", "attempts": 1},
        }

        receipt = render_gate.render_with_receipt(
            docx_path, backends=[backend], check_result=precomputed,
        )

        assert calls == []
        assert receipt["status"] == render_gate.RENDERED
        assert receipt["pdf_sha256"] == "precomputed-hash"
        assert receipt["backend_order"] == ["fake-ok"]
        assert receipt["duration_seconds"] == 0.0

    def test_multiple_receipts_accumulate_newest_first(self, tmp_path) -> None:
        docx_path = _write_dummy_docx(tmp_path)
        receipts_path = str(tmp_path / "receipts.json")
        backend = _fake_backend("fake-ok", render=lambda p: {})

        first = render_gate.render_with_receipt(docx_path, backends=[backend], receipts_path=receipts_path)
        second = render_gate.render_with_receipt(docx_path, backends=[backend], receipts_path=receipts_path)

        rows = render_gate.list_render_receipts(receipts_path, docx_path=docx_path)
        assert len(rows) == 2
        assert rows[0]["created_at_epoch"] >= rows[1]["created_at_epoch"]
        assert {r["receipt_id"] for r in rows} == {first["receipt_id"], second["receipt_id"]}


# ---------------------------------------------------------------------------
# check_release_render_gate
# ---------------------------------------------------------------------------

class TestCheckReleaseRenderGate:
    def test_missing_docx_path(self, tmp_path) -> None:
        result = render_gate.check_release_render_gate("", str(tmp_path / "r.json"))
        assert result["release_ready"] is False
        assert "docx_path" in result["reason"]

    def test_missing_receipts_path(self, tmp_path) -> None:
        docx_path = _write_dummy_docx(tmp_path)
        result = render_gate.check_release_render_gate(docx_path, "")
        assert result["release_ready"] is False
        assert "receipts_path" in result["reason"]

    def test_no_receipt_on_file_refuses(self, tmp_path) -> None:
        docx_path = _write_dummy_docx(tmp_path)
        receipts_path = str(tmp_path / "receipts.json")
        result = render_gate.check_release_render_gate(docx_path, receipts_path)
        assert result["release_ready"] is False
        assert result["degraded"] is False
        assert "no render receipt" in result["reason"]

    def test_fresh_rendered_receipt_passes(self, tmp_path) -> None:
        docx_path = _write_dummy_docx(tmp_path)
        receipts_path = str(tmp_path / "receipts.json")
        backend = _fake_backend("fake-ok", render=lambda p: {})
        render_gate.render_with_receipt(docx_path, backends=[backend], receipts_path=receipts_path)

        result = render_gate.check_release_render_gate(docx_path, receipts_path)

        assert result["release_ready"] is True
        assert result["degraded"] is False
        assert result["matched_receipt"] is not None
        # backend-conversion success alone is never "visually verified".
        assert result["visually_verified"] is False

    def test_visually_verified_true_when_visual_qa_verified(self, tmp_path) -> None:
        docx_path = _write_dummy_docx(tmp_path)
        receipts_path = str(tmp_path / "receipts.json")
        backend = _fake_backend("fake-ok", render=lambda p: {})
        render_gate.render_with_receipt(
            docx_path, backends=[backend], receipts_path=receipts_path,
            visual_qa={"status": "verified", "reviewer": "alice"},
        )

        result = render_gate.check_release_render_gate(docx_path, receipts_path)
        assert result["release_ready"] is True
        assert result["visually_verified"] is True

    def test_failed_receipt_never_satisfies_the_gate(self, tmp_path) -> None:
        docx_path = _write_dummy_docx(tmp_path)
        receipts_path = str(tmp_path / "receipts.json")

        def _boom(p: str) -> dict[str, Any]:
            raise render_gate.RenderCapabilityError("nope")

        backend = _fake_backend("fake-broken", render=_boom)
        render_gate.render_with_receipt(docx_path, backends=[backend], receipts_path=receipts_path)

        result = render_gate.check_release_render_gate(docx_path, receipts_path)
        assert result["release_ready"] is False

    def test_stale_receipt_refuses(self, tmp_path) -> None:
        docx_path = _write_dummy_docx(tmp_path)
        receipts_path = str(tmp_path / "receipts.json")
        backend = _fake_backend("fake-ok", render=lambda p: {})
        render_gate.render_with_receipt(docx_path, backends=[backend], receipts_path=receipts_path)

        far_future = render_gate.time.time() + 999999
        result = render_gate.check_release_render_gate(
            docx_path, receipts_path, max_age_seconds=3600.0, now=far_future,
        )
        assert result["release_ready"] is False
        assert "FRESH" in result["reason"] or "fresh" in result["reason"].lower()

    def test_content_changed_since_receipt_refuses(self, tmp_path) -> None:
        docx_path = _write_dummy_docx(tmp_path)
        receipts_path = str(tmp_path / "receipts.json")
        backend = _fake_backend("fake-ok", render=lambda p: {})
        render_gate.render_with_receipt(docx_path, backends=[backend], receipts_path=receipts_path)

        # Mutate the document after the receipt was taken.
        with open(docx_path, "ab") as fh:
            fh.write(b"more bytes -- content has changed")

        result = render_gate.check_release_render_gate(docx_path, receipts_path)
        assert result["release_ready"] is False

    def test_degraded_override_without_reason_is_refused(self, tmp_path) -> None:
        docx_path = _write_dummy_docx(tmp_path)
        receipts_path = str(tmp_path / "receipts.json")
        result = render_gate.check_release_render_gate(
            docx_path, receipts_path, allow_degraded_override=True,
        )
        assert result["release_ready"] is False
        assert "unaudited" in result["reason"]

    def test_degraded_override_with_reason_is_audited_and_persisted(self, tmp_path) -> None:
        docx_path = _write_dummy_docx(tmp_path)
        receipts_path = str(tmp_path / "receipts.json")

        result = render_gate.check_release_render_gate(
            docx_path, receipts_path, allow_degraded_override=True,
            override_reason="render backend unavailable in this environment, human confirmed visually in Word",
            override_by="alice",
        )

        assert result["release_ready"] is True
        assert result["degraded"] is True
        assert result["visually_verified"] is False
        assert result["override"]["override_reason"].startswith("render backend unavailable")
        assert result["override"]["override_by"] == "alice"
        assert result["override"]["kind"] == "degraded_override"

        # The override itself is durably recorded in the same ledger.
        all_receipts = render_gate.list_render_receipts(receipts_path, docx_path=docx_path)
        assert any(r["kind"] == "degraded_override" for r in all_receipts)

    def test_override_receipt_never_counts_as_a_fresh_render_for_a_later_call(self, tmp_path) -> None:
        docx_path = _write_dummy_docx(tmp_path)
        receipts_path = str(tmp_path / "receipts.json")

        render_gate.check_release_render_gate(
            docx_path, receipts_path, allow_degraded_override=True,
            override_reason="one-time human sign-off",
        )

        # A later call WITHOUT the override flag must not silently treat the
        # prior override as if it were a real, fresh "rendered" receipt.
        result = render_gate.check_release_render_gate(docx_path, receipts_path)
        assert result["release_ready"] is False


# ---------------------------------------------------------------------------
# _word_com_process_worker emits a "version" message (isolated/child-process
# path) -- called directly, single-process, with fake win32com injected, so
# this needs neither real pywin32 nor real multiprocessing.
# ---------------------------------------------------------------------------

class _FakeQueue:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def put(self, item: dict[str, Any]) -> None:
        self.items.append(item)


class _FakeWordDocumentForWorker:
    def __init__(self, on_save) -> None:
        self._on_save = on_save

    def SaveAs(self, path, FileFormat):
        self._on_save(path)

    def Close(self, save_changes):
        pass


class _FakeWordApplicationForWorker:
    def __init__(self, hwnd: int, open_document) -> None:
        self.Hwnd = hwnd
        self.Visible = None
        self.DisplayAlerts = None
        self.Version = "16.0"
        self._open_document = open_document

    class _Documents:
        def __init__(self, outer):
            self._outer = outer

        def Open(self, path, ReadOnly=True, **kwargs):
            return self._outer._open_document(path)

    @property
    def Documents(self):
        return self._Documents(self)

    def Quit(self):
        pass


def _install_fake_win32com_for_worker(monkeypatch, open_document, hwnd=7777):
    fake_client = types.ModuleType("win32com.client")
    fake_client.DispatchEx = lambda prog_id: _FakeWordApplicationForWorker(hwnd, open_document)

    fake_win32com = types.ModuleType("win32com")
    fake_win32com.client = fake_client

    fake_win32process = types.ModuleType("win32process")
    fake_win32process.GetWindowThreadProcessId = lambda hwnd_arg: (0, hwnd_arg)

    fake_pythoncom = types.ModuleType("pythoncom")
    fake_pythoncom.CoInitialize = lambda: None
    fake_pythoncom.CoUninitialize = lambda: None

    monkeypatch.setitem(sys.modules, "win32com", fake_win32com)
    monkeypatch.setitem(sys.modules, "win32com.client", fake_client)
    monkeypatch.setitem(sys.modules, "win32process", fake_win32process)
    monkeypatch.setitem(sys.modules, "pythoncom", fake_pythoncom)


def test_word_com_process_worker_emits_pid_version_and_result(tmp_path, monkeypatch):
    docx_path = _write_dummy_docx(tmp_path)
    pdf_path = str(tmp_path / "out.pdf")

    def _open_document(path):
        def _on_save(save_path):
            with open(save_path, "wb") as fh:
                fh.write(b"%PDF-1.4 fake")
        return _FakeWordDocumentForWorker(_on_save)

    _install_fake_win32com_for_worker(monkeypatch, _open_document)
    fake_queue = _FakeQueue()

    render_gate._word_com_process_worker(docx_path, pdf_path, fake_queue)

    kinds = [item["kind"] for item in fake_queue.items]
    assert "pid" in kinds
    assert "version" in kinds
    assert kinds[-1] == "result"
    version_item = next(item for item in fake_queue.items if item["kind"] == "version")
    assert version_item["version"] == "16.0"
    result_item = fake_queue.items[-1]
    assert result_item["ok"] is True


def test_word_com_process_worker_reports_failure_never_raises(tmp_path, monkeypatch):
    docx_path = _write_dummy_docx(tmp_path)
    pdf_path = str(tmp_path / "out.pdf")

    def _open_document(path):
        raise RuntimeError("Word blew up")

    _install_fake_win32com_for_worker(monkeypatch, _open_document)
    fake_queue = _FakeQueue()

    render_gate._word_com_process_worker(docx_path, pdf_path, fake_queue)

    result_item = fake_queue.items[-1]
    assert result_item["kind"] == "result"
    assert result_item["ok"] is False
    assert "Word blew up" in result_item["error"]


# ---------------------------------------------------------------------------
# server.py MCP tool wiring
# ---------------------------------------------------------------------------

class TestServerToolWiring:
    def test_render_with_receipt_is_registered_and_delegates(self, tmp_path, monkeypatch) -> None:
        import inspect

        assert callable(server.render_with_receipt)
        sig = inspect.signature(server.render_with_receipt)
        assert list(sig.parameters) == ["docx_path", "receipts_path", "max_retries", "visual_qa"]

        sentinel = {"status": "rendered", "receipt_id": "sentinel"}
        seen: dict[str, Any] = {}

        def _fake(path, **kwargs):
            seen["path"] = path
            seen.update(kwargs)
            return sentinel

        monkeypatch.setattr(render_gate, "render_with_receipt", _fake)
        result = server.render_with_receipt("doc.docx", receipts_path="r.json")

        assert result is sentinel
        assert seen["path"] == "doc.docx"
        assert seen["receipts_path"] == "r.json"

    def test_list_render_receipts_is_registered_and_delegates(self, monkeypatch) -> None:
        import inspect

        assert callable(server.list_render_receipts)
        sig = inspect.signature(server.list_render_receipts)
        assert list(sig.parameters) == ["receipts_path", "docx_path"]

        sentinel = [{"receipt_id": "one"}]
        monkeypatch.setattr(render_gate, "list_render_receipts", lambda *a, **k: sentinel)
        assert server.list_render_receipts("r.json") is sentinel

    def test_check_release_render_gate_is_registered_and_delegates(self, monkeypatch) -> None:
        import inspect

        assert callable(server.check_release_render_gate)
        sig = inspect.signature(server.check_release_render_gate)
        assert list(sig.parameters) == [
            "docx_path", "receipts_path", "max_age_seconds",
            "allow_degraded_override", "override_reason", "override_by",
        ]

        sentinel = {"release_ready": True}
        seen: dict[str, Any] = {}

        def _fake(docx_path, receipts_path, **kwargs):
            seen["docx_path"] = docx_path
            seen["receipts_path"] = receipts_path
            seen.update(kwargs)
            return sentinel

        monkeypatch.setattr(render_gate, "check_release_render_gate", _fake)
        result = server.check_release_render_gate(
            "doc.docx", "r.json", allow_degraded_override=True, override_reason="human ok",
        )

        assert result is sentinel
        assert seen["docx_path"] == "doc.docx"
        assert seen["allow_degraded_override"] is True
        assert seen["override_reason"] == "human ok"
