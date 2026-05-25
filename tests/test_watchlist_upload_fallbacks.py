from __future__ import annotations

import builtins
import io
import zipfile
from datetime import datetime, timezone

from admin.blueprints import config as config_bp
from services import ugly_mugs_sync_service


def _build_minimal_xlsx_bytes() -> bytes:
    workbook_xml = """<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
          xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>
    <sheet name="Sheet1" sheetId="1" r:id="rId1" />
  </sheets>
</workbook>"""
    workbook_rels_xml = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1"
      Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"
      Target="worksheets/sheet1.xml" />
</Relationships>"""
    sheet_xml = """<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData>
    <row r="1">
      <c r="A1" t="inlineStr"><is><t>Phone</t></is></c>
      <c r="C1" t="inlineStr"><is><t>Recency</t></is></c>
      <c r="D1" t="inlineStr"><is><t>Reports</t></is></c>
    </row>
    <row r="2">
      <c r="A2" t="inlineStr"><is><t>0412345678</t></is></c>
      <c r="C2"><v>1</v></c>
      <c r="D2"><v>3</v></c>
    </row>
  </sheetData>
</worksheet>"""

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/workbook.xml", workbook_xml)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels_xml)
        archive.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    return output.getvalue()


def test_extract_watchlist_rows_from_xlsx_bytes_reads_columns_a_c_d():
    rows = config_bp._extract_watchlist_rows_from_xlsx_bytes(_build_minimal_xlsx_bytes())
    assert ("0412345678", "1", "3") in rows


def test_write_export_workbook_falls_back_to_csv_when_openpyxl_missing(monkeypatch, tmp_path):
    real_import = builtins.__import__

    def _fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "openpyxl":
            raise ImportError("openpyxl not installed")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _fake_import)

    export_path = tmp_path / "ugly_mugs_export.xlsx"
    result_path = ugly_mugs_sync_service._write_export_workbook(
        export_path=str(export_path),
        unique_numbers={
            "61400111222": {"raw": "0400 111 222", "first_page": 1, "occurrences": 2},
        },
        failed_pages=[],
        started_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
        start_page=1,
        total_pages=2,
    )

    assert result_path.endswith(".csv")
    assert (tmp_path / "ugly_mugs_export.csv").exists()
