"""Tests for meridian_desktop/log_tail.py: the bounded read, the backup fallback, the no-log and
unreadable states, the redaction pass, and the read-only rule.

Every file here lives under pytest's temp directory. The environment and home directory are
passed in, so no test reads the real MERIDIAN_TUNNEL_LOG or the real home directory. Fake tokens
carry a fake marker and match no live token. Files are written as bytes, so the byte counts do
not change with the platform's newline translation.
"""

import ast
from pathlib import Path

import pytest

from meridian_desktop import log_tail
from meridian_desktop.log_tail import (
    DEFAULT_MAX_BYTES,
    NO_LOG_YET_TEXT,
    PATTERN_NAMES,
    STATUS_NO_LOG_YET,
    STATUS_OK,
    STATUS_UNREADABLE,
    TUNNEL_LOG_ENV,
    UNREADABLE_TEXT,
    read_log_tail,
    read_tunnel_log_tail,
    redact_line,
    tunnel_log_path,
)

REPO = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO / "meridian_desktop" / "log_tail.py"

FAKE_QUERY_TOKEN = "fake-query-0001"
FAKE_BEARER = "sk_meridian_fake-bearer-0002"
FAKE_BARE = "sk_meridian_fake-bare-0003"
SECRETS = (FAKE_QUERY_TOKEN, FAKE_BEARER, FAKE_BARE)

ALLOWED_IMPORT_ROOTS = {"__future__", "collections", "dataclasses", "os", "pathlib", "re", "typing"}
# Attribute names a write, rename, delete, truncate or rotate would need. The module uses none.
WRITE_NAMES = {
    "open",
    "write",
    "write_text",
    "write_bytes",
    "rename",
    "replace",
    "remove",
    "unlink",
    "truncate",
    "touch",
    "mkdir",
    "rmdir",
    "chmod",
    "copy",
    "move",
}


class _CountingFile:
    """Wraps a real binary file and records how many bytes each read returned."""

    def __init__(self, fh, reads):
        self._fh = fh
        self._reads = reads

    def read(self, size=-1):
        data = self._fh.read(size)
        self._reads.append(len(data))
        return data

    def seek(self, offset, whence=0):
        return self._fh.seek(offset, whence)

    def fileno(self):
        return self._fh.fileno()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self._fh.close()


@pytest.fixture
def counted_reads(monkeypatch):
    """Route every file open in log_tail through a counter. The list holds each read's byte count."""
    reads: list[int] = []
    real_open = log_tail._open_for_read

    def counting_open(path):
        return _CountingFile(real_open(path), reads)

    monkeypatch.setattr(log_tail, "_open_for_read", counting_open)
    return reads


def _snapshot(directory: Path) -> dict[str, tuple[bytes, int, int]]:
    """Each file's bytes, size and modification time, keyed by name. A change to any of them shows here."""
    found = {}
    for entry in sorted(directory.iterdir()):
        stat = entry.stat()
        found[entry.name] = (entry.read_bytes(), stat.st_size, stat.st_mtime_ns)
    return found


def test_tunnel_log_tail_is_bounded_and_redacted(tmp_path, counted_reads):
    log = tmp_path / "tunnel.log"
    filler = b"".join(b"line-%07d steady diagnostic text\n" % i for i in range(60000))
    tail = (
        b"2026-10-19T10:00:00.000+00:00 connect ok\n"
        + f"2026-10-19T10:00:01.000+00:00 GET /ws?token={FAKE_QUERY_TOKEN}&slot=fs 401\n".encode()
        + f"2026-10-19T10:00:02.000+00:00 Authorization: Bearer {FAKE_BEARER}\n".encode()
        + f"2026-10-19T10:00:03.000+00:00 key {FAKE_BARE} rejected\n".encode()
        + b"2026-10-19T10:00:04.000+00:00 final marker END-OF-TAIL\n"
    )
    log.write_bytes(filler + tail)
    assert log.stat().st_size > 2 * 1024 * 1024

    assert DEFAULT_MAX_BYTES == 65536
    result = read_tunnel_log_tail(log)

    assert result.status == STATUS_OK
    assert sum(counted_reads) <= DEFAULT_MAX_BYTES
    text = "\n".join(result.lines)
    assert len(text.encode("utf-8")) <= DEFAULT_MAX_BYTES
    assert "END-OF-TAIL" in text
    assert "line-0000000" not in text  # the first line of a 2 MB file is outside the window
    for secret in SECRETS:
        assert secret not in text
    assert "2026-10-19T10:00:01.000+00:00 GET /ws?token=[redacted]&slot=fs 401" in result.lines
    assert "2026-10-19T10:00:02.000+00:00 Authorization: Bearer [redacted]" in result.lines
    assert "2026-10-19T10:00:03.000+00:00 key [redacted] rejected" in result.lines
    for line in result.lines:
        assert redact_line(line) == line


@pytest.mark.parametrize("rows", [1, 14, 200, 60000])
def test_tail_reads_at_most_max_bytes_from_the_end_regardless_of_size(tmp_path, counted_reads, rows):
    log = tmp_path / "tunnel.log"
    log.write_bytes(b"".join(b"row-%08d\n" % i for i in range(rows)))

    result = read_tunnel_log_tail(log, max_bytes=512)

    assert result.status == STATUS_OK
    assert sum(counted_reads) <= 512
    assert result.lines[-1] == f"row-{rows - 1:08d}"


def test_tail_reads_backup_when_current_log_is_short(tmp_path, counted_reads):
    log = tmp_path / "tunnel.log"
    backup = tmp_path / "tunnel.log.1"
    backup.write_bytes(b"".join(b"older-%03d\n" % i for i in range(100)))  # 1000 bytes, 10 per line
    log.write_bytes(b"fresh-a\nfresh-b\n")  # 16 bytes

    result = read_tunnel_log_tail(log, max_bytes=60)

    # The current log gives 16 bytes, so the backup gives the last 44 bytes. That window starts
    # inside "older-095", so the partial line is dropped and four whole backup lines remain.
    assert result.lines == ("older-096", "older-097", "older-098", "older-099", "fresh-a", "fresh-b")
    assert sum(counted_reads) <= 60


def test_backup_is_not_read_when_current_log_fills_the_budget(tmp_path, counted_reads):
    log = tmp_path / "tunnel.log"
    backup = tmp_path / "tunnel.log.1"
    backup.write_bytes(b"older-only-in-backup\n" * 50)
    log.write_bytes(b"".join(b"current-%03d\n" % i for i in range(40)))  # 400 bytes

    result = read_tunnel_log_tail(log, max_bytes=100)

    assert result.status == STATUS_OK
    assert result.lines and all(line.startswith("current-") for line in result.lines)
    assert sum(counted_reads) <= 100


def test_read_log_tail_reads_any_log_path_with_its_backup(tmp_path, counted_reads):
    # The same window as test_tail_reads_backup_when_current_log_is_short, under another file name.
    log = tmp_path / "desktop.log"
    backup = tmp_path / "desktop.log.1"
    backup.write_bytes(b"".join(b"older-%03d\n" % i for i in range(100)))
    log.write_bytes(b"fresh-a\nfresh-b\n")

    result = read_log_tail(log, max_bytes=60)

    assert result.status == STATUS_OK
    assert result.path == log
    assert result.lines == ("older-096", "older-097", "older-098", "older-099", "fresh-a", "fresh-b")
    assert sum(counted_reads) <= 60


def test_read_log_tail_missing_path_is_no_log_yet_and_checks_the_budget(tmp_path):
    missing = tmp_path / "desktop.log"

    result = read_log_tail(missing)

    assert result.status == STATUS_NO_LOG_YET
    assert result.message == NO_LOG_YET_TEXT
    assert list(tmp_path.iterdir()) == []
    with pytest.raises(ValueError):
        read_log_tail(missing, max_bytes=0)


def test_tunnel_entry_point_gives_the_same_result_as_the_generic_reader(tmp_path):
    log = tmp_path / "tunnel.log"
    log.write_bytes(b"".join(b"row %03d token=%s\n" % (i, FAKE_QUERY_TOKEN.encode()) for i in range(50)))

    assert read_tunnel_log_tail(log, max_bytes=300) == read_log_tail(log, max_bytes=300)


def test_tail_never_shows_a_fragment_of_a_line(tmp_path):
    log = tmp_path / "tunnel.log"
    rows = [f"row {i:04d} " + "x" * (i % 37) for i in range(400)]
    log.write_bytes(("\n".join(rows) + "\n").encode("utf-8"))

    for budget in range(10, 400, 37):
        lines = read_tunnel_log_tail(log, max_bytes=budget).lines
        assert all(line in rows for line in lines), (budget, lines)
        if lines:
            assert list(lines) == rows[-len(lines):]
            assert sum(len(line.encode("utf-8")) + 1 for line in lines) <= budget


def test_crlf_line_endings_are_stripped(tmp_path):
    log = tmp_path / "tunnel.log"
    log.write_bytes(b"alpha\r\nbeta\r\n")

    result = read_tunnel_log_tail(log)

    assert result.lines == ("alpha", "beta")


def test_missing_log_is_no_log_yet_not_an_error(tmp_path):
    log = tmp_path / "tunnel.log"

    result = read_tunnel_log_tail(log)

    assert result.status == STATUS_NO_LOG_YET == "no_log_yet"
    assert result.lines == ()
    assert result.message == NO_LOG_YET_TEXT
    assert result.path == log
    assert list(tmp_path.iterdir()) == []  # nothing was created

    absent_dir = tmp_path / "absent" / "tunnel.log"
    assert read_tunnel_log_tail(absent_dir).status == STATUS_NO_LOG_YET
    assert not (tmp_path / "absent").exists()


def test_empty_existing_log_is_ok_with_no_lines(tmp_path):
    log = tmp_path / "tunnel.log"
    log.write_bytes(b"")

    result = read_tunnel_log_tail(log)

    assert result.status == STATUS_OK
    assert result.lines == ()
    assert result.message == ""


def test_unreadable_log_is_a_status_not_an_exception(tmp_path):
    log = tmp_path / "tunnel.log"
    log.mkdir()  # a directory cannot be read as a log

    result = read_tunnel_log_tail(log)

    assert result.status == STATUS_UNREADABLE
    assert result.lines == ()
    assert result.message == UNREADABLE_TEXT


def test_tail_never_writes_truncates_or_rotates(tmp_path):
    log = tmp_path / "tunnel.log"
    backup = tmp_path / "tunnel.log.1"
    log.write_bytes(b"".join(b"row %d\n" % i for i in range(5000)))
    backup.write_bytes(b"older\n" * 2000)
    before = _snapshot(tmp_path)

    for budget in (1, 100, DEFAULT_MAX_BYTES, 10**7):
        read_tunnel_log_tail(log, max_bytes=budget)

    assert _snapshot(tmp_path) == before


def test_log_tail_module_has_no_write_or_rotate_calls():
    tree = ast.parse(MODULE_PATH.read_bytes(), filename=str(MODULE_PATH))
    attribute_names = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert not attribute_names & WRITE_NAMES

    opens = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open"
    ]
    assert len(opens) == 1, "the module must open files in one place only"
    mode = opens[0].args[1] if len(opens[0].args) > 1 else None
    assert isinstance(mode, ast.Constant) and mode.value == "rb"


def test_redaction_pattern_set_is_pinned_and_covers_three_shapes():
    assert PATTERN_NAMES == ("token-param", "bearer", "meridian-token")

    assert redact_line(f"GET /ws?token={FAKE_QUERY_TOKEN}") == "GET /ws?token=[redacted]"
    assert redact_line(f"x&token={FAKE_QUERY_TOKEN}&y=1") == "x&token=[redacted]&y=1"
    assert redact_line(f"Authorization: Bearer {FAKE_BEARER}") == "Authorization: Bearer [redacted]"
    assert redact_line(f"bearer  {FAKE_BEARER}") == "bearer  [redacted]"
    assert redact_line(f"key {FAKE_BARE} rejected") == "key [redacted] rejected"
    assert redact_line(f"sk_meridian_{FAKE_BARE}") == "[redacted]"


def test_redaction_leaves_ordinary_text_unchanged():
    for line in (
        "tokens per second: 5",
        "connect ok on slot fs",
        "bearer-less header value",
        "the word Bearer",
    ):
        assert redact_line(line) == line


def test_tunnel_log_path_honours_env_override_and_defaults_under_home(tmp_path):
    override = tmp_path / "custom" / "tunnel-test.log"
    home = tmp_path / "home"

    assert tunnel_log_path(env={TUNNEL_LOG_ENV: str(override)}, home=home) == override
    assert tunnel_log_path(env={TUNNEL_LOG_ENV: "   "}, home=home) == home / ".meridian" / "tunnel.log"
    assert tunnel_log_path(env={}, home=home) == home / ".meridian" / "tunnel.log"

    log = tmp_path / "from-env.log"
    log.write_bytes(b"from the override\n")
    assert read_tunnel_log_tail(env={TUNNEL_LOG_ENV: str(log)}).lines == ("from the override",)
    assert read_tunnel_log_tail(env={}, home=home).status == STATUS_NO_LOG_YET


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "100"])
def test_tail_rejects_a_budget_that_is_not_a_positive_integer(tmp_path, bad):
    with pytest.raises(ValueError):
        read_tunnel_log_tail(tmp_path / "tunnel.log", max_bytes=bad)


def test_log_tail_imports_no_qt_and_no_network_module():
    tree = ast.parse(MODULE_PATH.read_bytes(), filename=str(MODULE_PATH))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= ALLOWED_IMPORT_ROOTS
    assert "PySide6" not in imported
