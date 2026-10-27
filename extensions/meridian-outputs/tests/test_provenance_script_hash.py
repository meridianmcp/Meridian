"""record_provenance stores the generating script's content hash (sprint item: script hash recorded in
record_provenance).

Before this change the record kept only the script's name and the output's own content hash, so an edited
generating script left no trace in the record. The record now carries script_content_hash: the SHA-256 of the
script's bytes when the record was written, or None when the script cannot be found.
"""

import hashlib
from pathlib import Path

from meridian_outputs import annotate as AN
from meridian_outputs.annotate import ProvenanceRecord


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_absolute_script_path_hash_is_recorded(tmp_path: Path) -> None:
    script = tmp_path / "make_plot.py"
    script.write_text("print('plot')\n", encoding="utf-8")
    out = tmp_path / "figures" / "plot.txt"
    out.parent.mkdir()
    out.write_text("plot bytes", encoding="utf-8")

    res = AN.record_provenance(str(tmp_path), str(out), generating_script=str(script))

    assert res["script_content_hash"] == _sha(script)
    assert res["content_hash"] != res["script_content_hash"]  # the output's hash is a different thing


def test_relative_script_name_resolves_against_the_outputs_directory(tmp_path: Path) -> None:
    script = tmp_path / "make_table.py"
    script.write_text("print('table')\n", encoding="utf-8")
    out = tmp_path / "table.csv"
    out.write_text("a,b\n1,2\n", encoding="utf-8")

    res = AN.record_provenance(str(tmp_path), str(out), generating_script="make_table.py")

    assert res["script_content_hash"] == _sha(script)


def test_missing_script_leaves_the_hash_none_and_still_writes_the_record(tmp_path: Path) -> None:
    out = tmp_path / "result.txt"
    out.write_text("result", encoding="utf-8")

    res = AN.record_provenance(str(tmp_path), str(out), generating_script="no_such_script.py")

    assert "error" not in res
    assert res["script_content_hash"] is None
    assert res["generating_script"] == "no_such_script.py"
    assert AN.get_provenance(str(tmp_path), str(out))["generating_script"] == "no_such_script.py"


def test_editing_the_script_changes_the_hash_on_re_record_and_not_the_output_hash(tmp_path: Path) -> None:
    script = tmp_path / "make_plot.py"
    script.write_text("v = 1\n", encoding="utf-8")
    out = tmp_path / "plot.txt"
    out.write_text("same output", encoding="utf-8")

    first = AN.record_provenance(str(tmp_path), str(out), generating_script="make_plot.py")
    script.write_text("v = 2\n", encoding="utf-8")
    second = AN.record_provenance(str(tmp_path), str(out), generating_script="make_plot.py")

    assert first["script_content_hash"] != second["script_content_hash"]
    assert second["script_content_hash"] == _sha(script)
    assert first["content_hash"] == second["content_hash"]  # the output did not change


def test_no_generating_script_gives_no_script_hash(tmp_path: Path) -> None:
    out = tmp_path / "bare.txt"
    out.write_text("no script named", encoding="utf-8")

    res = AN.record_provenance(str(tmp_path), str(out))

    assert res["script_content_hash"] is None


def test_the_field_defaults_to_none_for_records_written_before_it_existed() -> None:
    old_shape = ProvenanceRecord(path="legacy.txt")
    assert old_shape.script_content_hash is None
    assert "script_content_hash" in old_shape.to_dict()
