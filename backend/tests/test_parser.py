"""Unit tests for Excel (.xlsx/.xlsm) and CSV parsing, aggregation, and validation."""

import io
import openpyxl
import pytest

from app.parser import FileValidationError, parse_demand_file


def _build_xlsx(headers, rows) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(headers)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_xlsx_duplicate_aggregation_and_zero_fill() -> None:
    raw = _build_xlsx(
        ["Stock Code", "Material Name", "Periode", "ISS"],
        [
            ["001001", "Bearing X", "2020-01", 0],
            ["001001", "Bearing X", "2020-03", 5],
            ["001001", "Bearing X", "2020-03", 7],  # duplicate -> sum = 12
            ["001001", "Bearing X", "2020-06", 10],
            ["001001", "Bearing X", "2020-08", 4],
        ],
    )
    parsed = parse_demand_file("sample.xlsx", raw)
    assert parsed.report["n_materials"] == 1
    assert parsed.report["duplicates_summed"] == 1
    assert parsed.report["months_filled_zero"] == 4  # Feb, Apr, May, Jul
    series = dict(parsed.materials["001001"]["series"])
    assert series["2020-02"] == 0.0
    assert series["2020-03"] == 12.0
    assert series["2020-08"] == 4.0


def test_xlsm_extension_accepted() -> None:
    raw = _build_xlsx(
        ["kode", "nama", "bulan", "qty"],
        [
            ["M01", "Valve", "2020-01", 1],
            ["M01", "Valve", "2020-06", 3],
        ],
    )
    parsed = parse_demand_file("macro_file.xlsm", raw)
    assert parsed.report["n_materials"] == 1
    assert parsed.file_ext == "xlsm"


def test_invalid_columns_rejected() -> None:
    raw = b"foo,bar,baz\n1,2,3\n"
    with pytest.raises(FileValidationError):
        parse_demand_file("bad.csv", raw)


def test_invalid_rows_counted_and_skipped() -> None:
    raw = (
        "kode,nama,bulan,qty\n"
        "A01,Part A,2020-01,2\n"
        "A01,Part A,bad-date,5\n"
        "A01,Part A,2020-03,-4\n"
        "A01,Part A,2020-06,8\n"
    ).encode("utf-8")
    parsed = parse_demand_file("mixed.csv", raw)
    assert parsed.report["rejected_bad_code_or_month"] == 1
    assert parsed.report["rejected_bad_qty"] == 1
    assert parsed.report["n_materials"] == 1


def test_excel_title_rows_and_serial_dates_supported() -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["LAPORAN PERMINTAAN MATERIAL 2020"])
    ws.append([])
    ws.append(["kode", "nama", "bulan", "qty"])
    ws.append(["009999", "Gasket", 43831, 0])       # 2020-01-01 Excel serial
    ws.append(["009999", "Gasket", 202003, 5])      # YYYYMM numeric
    ws.append(["009999", "Gasket", "2020-06", 7])
    buf = io.BytesIO()
    wb.save(buf)

    parsed = parse_demand_file("WITH_TITLE.XLSX", buf.getvalue())
    assert parsed.report["n_materials"] == 1
    series = dict(parsed.materials["009999"]["series"])
    assert series["2020-01"] == 0.0
    assert series["2020-03"] == 5.0
    assert series["2020-06"] == 7.0

