"""Regression tests for the native OMML script census."""
from __future__ import annotations

import io
import zipfile

from meridian_docs.script_census import census_equation_scripts


def _docx_with_scripted_term() -> bytes:
    xml = """<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math" xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"><w:body><w:p w14:paraId="ABCDEF01"><m:oMathPara><m:oMath><m:sSub><m:sSubPr/><m:e><m:r><m:t>ρ</m:t></m:r></m:e><m:sub><m:r><m:t>jℓ</m:t></m:r></m:sub></m:sSub></m:oMath></m:oMathPara></w:p><w:sectPr/></w:body></w:document>"""
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as package:
        package.writestr("word/document.xml", xml)
    return output.getvalue()


def test_census_preserves_native_script_and_locator():
    result = census_equation_scripts(_docx_with_scripted_term())
    assert result["equation_count"] == 1
    assert result["scripted_occurrence_count"] == 1
    assert result["terms"][0]["term"] == "ρ_jℓ"
    assert result["terms"][0]["structures"] == {"sSub": 1}
    assert result["provenance"]["document_mutated"] is False
