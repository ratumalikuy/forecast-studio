"""Dynamic Excel (.xlsx, .xlsm) and CSV Demand File Parser & Validator.

Reads an arbitrary number of materials dynamically from uploaded files without
modifying the original source bytes, validates schema/dates/quantities/period
consistency, aggregates duplicate (material, month) transactions by summing,
and zero-fills intermediate months within each material's historical span.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import math
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import openpyxl

from .forecasting import index_to_month, month_to_index

ALLOWED_EXTENSIONS = {".xlsx", ".xlsm", ".csv"}

_CODE_ALIASES = {
    "kode", "stock_code", "stock code", "stockcode", "material_code",
    "material code", "kode_material", "kode material", "sku", "part_number",
    "part number", "item_code", "item code", "code",
}
_NAME_ALIASES = {
    "nama", "nama_material", "nama material", "material", "material_name",
    "material name", "description", "deskripsi", "item_name", "item name", "name",
}
_MONTH_ALIASES = {
    "bulan", "periode", "period", "month", "date", "tanggal", "tgl", "ym", "year_month",
}
_QTY_ALIASES = {
    "qty", "quantity", "demand", "iss", "issue", "kuantitas", "jumlah", "xt", "value", "pemakaian",
}

_MONTH_NAME_MAP = {
    "jan": 1, "januari": 1, "january": 1,
    "feb": 2, "februari": 2, "february": 2,
    "mar": 3, "maret": 3, "march": 3,
    "apr": 4, "april": 4,
    "mei": 5, "may": 5,
    "jun": 6, "juni": 6, "june": 6,
    "jul": 7, "juli": 7, "july": 7,
    "ags": 8, "agu": 8, "agustus": 8, "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "okt": 10, "oktober": 10, "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "des": 12, "desember": 12, "dec": 12, "december": 12,
}

_RE_YYYY_MM = re.compile(r"^(\d{4})[-/](\d{1,2})$")
_RE_YYYYMM = re.compile(r"^(\d{4})(0[1-9]|1[0-2])$")
_RE_YYYY_MM_DD = re.compile(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:[T\s].*)?$")
_RE_MM_YYYY = re.compile(r"^(\d{1,2})[-/](\d{4})$")
_RE_DD_MM_YYYY = re.compile(r"^(\d{1,2})[-/](\d{1,2})[-/](\d{4})$")
_RE_MON_YYYY = re.compile(r"^([A-Za-z]+)\s*[-/]?\s*(\d{4})$")
_RE_YYYY_MON = re.compile(r"^(\d{4})\s*[-/]?\s*([A-Za-z]+)$")
_RE_UNSAFE_CODE = re.compile(r"[<>&\"'`/\\]")


class FileValidationError(ValueError):
    """Raised when an uploaded file cannot be parsed or fails structural checks."""


@dataclass
class ParsedUpload:
    filename: str
    file_ext: str
    materials: Dict[str, Dict[str, Any]]  # code -> {"name": str, "series": [(month, qty), ...]}
    report: Dict[str, Any]


def _normalize_header(raw: Any) -> str:
    if raw is None:
        return ""
    text = str(raw).strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text


def _detect_columns(headers: List[Any]) -> Dict[str, int]:
    norm = [_normalize_header(h) for h in headers]
    mapping: Dict[str, int] = {}

    for idx, h in enumerate(norm):
        if "code" not in mapping and h in _CODE_ALIASES:
            mapping["code"] = idx
        elif "name" not in mapping and h in _NAME_ALIASES:
            mapping["name"] = idx
        elif "month" not in mapping and h in _MONTH_ALIASES:
            mapping["month"] = idx
        elif "qty" not in mapping and h in _QTY_ALIASES:
            mapping["qty"] = idx

    missing = [req for req in ("code", "month", "qty") if req not in mapping]
    if missing:
        expected = {
            "code": "kode / stock_code / kode_material",
            "month": "bulan / periode / month / date",
            "qty": "qty / demand / iss / kuantitas",
        }
        missing_desc = ", ".join(f"{k} ({expected[k]})" for k in missing)
        found_desc = ", ".join(h for h in norm if h) or "(kosong)"
        raise FileValidationError(
            f"Struktur kolom tidak sesuai. Kolom wajib tidak ditemukan: {missing_desc}. "
            f"Kolom terdeteksi: {found_desc}."
        )
    return mapping


def _parse_code(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    if isinstance(raw, float):
        if not math.isfinite(raw):
            return None
        if raw.is_integer():
            code = str(int(raw))
        else:
            code = str(raw)
    else:
        code = str(raw).strip()
    if not code or _RE_UNSAFE_CODE.search(code):
        return None
    return code


def _parse_month(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    if isinstance(raw, (dt.datetime, dt.date)):
        return f"{raw.year:04d}-{raw.month:02d}"

    # Support numeric YYYYMM (e.g., 202001) and Excel serial date numbers (1970..2100)
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        if not math.isfinite(float(raw)):
            return None
        ival = int(round(float(raw)))
        if 190001 <= ival <= 210012:
            y, mo = divmod(ival, 100)
            if 1900 <= y <= 2100 and 1 <= mo <= 12:
                return f"{y:04d}-{mo:02d}"
        if 25569 <= ival <= 73050:
            d = dt.date(1899, 12, 30) + dt.timedelta(days=ival)
            return f"{d.year:04d}-{d.month:02d}"
        return None

    text = str(raw).strip()
    if not text:
        return None

    m = _RE_YYYYMM.match(text)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}"

    m = _RE_YYYY_MM.match(text)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        if 1900 <= y <= 2100 and 1 <= mo <= 12:
            return f"{y:04d}-{mo:02d}"
        return None

    m = _RE_YYYY_MM_DD.match(text)
    if m:
        y, mo, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1900 <= y <= 2100 and 1 <= mo <= 12 and 1 <= day <= 31:
            return f"{y:04d}-{mo:02d}"
        return None

    m = _RE_MM_YYYY.match(text)
    if m:
        mo, y = int(m.group(1)), int(m.group(2))
        if 1900 <= y <= 2100 and 1 <= mo <= 12:
            return f"{y:04d}-{mo:02d}"
        return None

    m = _RE_DD_MM_YYYY.match(text)
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1900 <= y <= 2100:
            # Prefer DD/MM/YYYY (Indonesian convention), fallback MM/DD/YYYY if b > 12
            mo = b if 1 <= b <= 12 else (a if 1 <= a <= 12 else 0)
            if 1 <= mo <= 12:
                return f"{y:04d}-{mo:02d}"
        return None

    m = _RE_MON_YYYY.match(text)
    if m:
        mo = _MONTH_NAME_MAP.get(m.group(1).lower())
        y = int(m.group(2))
        if mo and 1900 <= y <= 2100:
            return f"{y:04d}-{mo:02d}"
        return None

    m = _RE_YYYY_MON.match(text)
    if m:
        y = int(m.group(1))
        mo = _MONTH_NAME_MAP.get(m.group(2).lower())
        if mo and 1900 <= y <= 2100:
            return f"{y:04d}-{mo:02d}"
        return None

    return None


def _parse_qty(raw: Any) -> Optional[float]:
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        val = float(raw)
        return val if math.isfinite(val) and val >= 0.0 else None

    text = str(raw).strip()
    if not text:
        return None

    # Handle comma decimal vs thousand separator carefully
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            # e.g. 1.250,50 -> 1250.50
            text = text.replace(".", "").replace(",", ".")
        else:
            # e.g. 1,250.50 -> 1250.50
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")

    try:
        val = float(text)
    except ValueError:
        return None

    if not math.isfinite(val) or val < 0.0:
        return None
    return val


def _read_csv_rows(raw_bytes: bytes) -> Tuple[List[Any], List[List[Any]], str]:
    decoded: Optional[str] = None
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            decoded = raw_bytes.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if decoded is None:
        raise FileValidationError("Encoding file CSV tidak dapat dibaca.")

    sample = decoded[:4096]
    delimiter = ","
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        delimiter = dialect.delimiter
    except csv.Error:
        if sample.count(";") > sample.count(","):
            delimiter = ";"

    reader = csv.reader(io.StringIO(decoded), delimiter=delimiter)
    all_rows = [row for row in reader if any(str(c).strip() for c in row)]
    if not all_rows:
        raise FileValidationError("File CSV kosong.")
    return all_rows[0], all_rows[1:], f"csv (delimiter '{delimiter}')"


def _read_excel_rows(raw_bytes: bytes, ext: str) -> Tuple[List[Any], List[List[Any]], str]:
    try:
        wb = openpyxl.load_workbook(io.BytesIO(raw_bytes), data_only=True, read_only=True)
    except Exception as exc:
        raise FileValidationError(f"File Excel tidak valid atau rusak: {exc}") from exc

    try:
        chosen_sheet = None
        chosen_headers: Optional[List[Any]] = None
        chosen_rows: List[List[Any]] = []

        # Inspect sheets & scan first 10 non-empty rows to find the real header row
        # (handles Excel sheets that have report title rows above the table header)
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            non_empty_rows: List[List[Any]] = []
            for row in ws.iter_rows(values_only=True):
                vals = list(row)
                if any(v is not None and str(v).strip() != "" for v in vals):
                    non_empty_rows.append(vals)

            if not non_empty_rows:
                continue

            found_idx: Optional[int] = None
            for idx in range(min(10, len(non_empty_rows))):
                try:
                    _detect_columns(non_empty_rows[idx])
                    found_idx = idx
                    break
                except FileValidationError:
                    continue

            if found_idx is not None:
                chosen_sheet = sheet_name
                chosen_headers = non_empty_rows[found_idx]
                chosen_rows = non_empty_rows[found_idx + 1 :]
                break

            if chosen_headers is None:
                chosen_sheet = sheet_name
                chosen_headers = non_empty_rows[0]
                chosen_rows = non_empty_rows[1:]

        if chosen_headers is None:
            raise FileValidationError("File Excel tidak memiliki baris header atau data.")

        return chosen_headers, chosen_rows, f"{ext.lstrip('.')} (sheet: {chosen_sheet})"
    finally:
        wb.close()


def parse_demand_file(filename: str, raw_bytes: bytes) -> ParsedUpload:
    """Parse and validate an uploaded .xlsx, .xlsm, or .csv demand dataset."""
    if not filename:
        raise FileValidationError("Nama file tidak ditemukan.")

    lower = filename.lower()
    ext = ""
    for candidate in ALLOWED_EXTENSIONS:
        if lower.endswith(candidate):
            ext = candidate
            break
    if not ext:
        raise FileValidationError(
            "Format file tidak didukung. Gunakan file .xlsx, .xlsm, atau .csv."
        )

    if not raw_bytes:
        raise FileValidationError("File yang diunggah kosong (0 byte).")

    if ext == ".csv":
        headers, rows, source_desc = _read_csv_rows(raw_bytes)
    else:
        headers, rows, source_desc = _read_excel_rows(raw_bytes, ext)

    col_map = _detect_columns(headers)
    idx_code = col_map["code"]
    idx_name = col_map.get("name")
    idx_month = col_map["month"]
    idx_qty = col_map["qty"]

    grouped: Dict[str, Dict[str, Any]] = {}
    rows_total = len(rows)
    rows_valid = 0
    rejected_bad_code_or_month = 0
    rejected_bad_qty = 0
    duplicates_summed = 0

    for row in rows:
        raw_code = row[idx_code] if idx_code < len(row) else None
        raw_name = row[idx_name] if (idx_name is not None and idx_name < len(row)) else None
        raw_month = row[idx_month] if idx_month < len(row) else None
        raw_qty = row[idx_qty] if idx_qty < len(row) else None

        code = _parse_code(raw_code)
        month = _parse_month(raw_month)
        if not code or not month:
            rejected_bad_code_or_month += 1
            continue

        qty = _parse_qty(raw_qty)
        if qty is None:
            rejected_bad_qty += 1
            continue

        rows_valid += 1
        name_clean = str(raw_name).strip() if raw_name is not None and str(raw_name).strip() else code
        if code not in grouped:
            grouped[code] = {"name": name_clean, "month_map": {}}
        elif grouped[code]["name"] == code and name_clean != code:
            grouped[code]["name"] = name_clean

        month_map = grouped[code]["month_map"]
        if month in month_map:
            duplicates_summed += 1
            month_map[month] += qty
        else:
            month_map[month] = qty

    materials: Dict[str, Dict[str, Any]] = {}
    skipped_materials: List[Dict[str, str]] = []
    months_filled_zero = 0
    period_spans: List[Tuple[str, str, int]] = []
    warnings: List[str] = []

    for code, entry in grouped.items():
        sorted_months = sorted(entry["month_map"].keys())
        start_idx = month_to_index(sorted_months[0])
        end_idx = month_to_index(sorted_months[-1])
        series: List[Tuple[str, float]] = []
        filled_here = 0

        for idx in range(start_idx, end_idx + 1):
            m_str = index_to_month(idx)
            if m_str in entry["month_map"]:
                series.append((m_str, float(entry["month_map"][m_str])))
            else:
                series.append((m_str, 0.0))
                filled_here += 1

        if len(series) < 6:
            skipped_materials.append({"code": code, "reason": f"periode < 6 bulan ({len(series)} bulan)"})
            continue
        if all(v == 0.0 for _, v in series):
            skipped_materials.append({"code": code, "reason": "seluruh nilai demand = 0"})
            continue

        months_filled_zero += filled_here
        materials[code] = {"name": entry["name"], "series": series}
        period_spans.append((series[0][0], series[-1][0], len(series)))

    if not materials:
        raise FileValidationError(
            "Tidak ada material valid yang dapat diproses. Pastikan minimal 6 bulan data per material, "
            "format bulan valid (mis. YYYY-MM), dan minimal ada satu nilai demand > 0."
        )

    period_min = min(s[0] for s in period_spans)
    period_max = max(s[1] for s in period_spans)
    unique_starts = sorted({s[0] for s in period_spans})
    unique_ends = sorted({s[1] for s in period_spans})

    if rejected_bad_code_or_month > 0 or rejected_bad_qty > 0:
        warnings.append(
            f"Terdapat {rejected_bad_code_or_month + rejected_bad_qty} baris tidak valid yang dilewati "
            f"(kode/bulan: {rejected_bad_code_or_month}, qty: {rejected_bad_qty})."
        )
    if duplicates_summed > 0:
        warnings.append(
            f"Terdapat {duplicates_summed} transaksi duplikat pada material & bulan yang sama; "
            "nilai diagregasi dengan penjumlahan sesuai aturan pengolahan data."
        )
    if months_filled_zero > 0:
        warnings.append(
            f"{months_filled_zero} bulan tanpa transaksi di antara periode awal-akhir diisi 0 "
            "(zero demand dipertahankan untuk analisis intermittent demand)."
        )
    if len(unique_starts) > 1 or len(unique_ends) > 1:
        warnings.append(
            f"Rentang periode antar material bervariasi (awal: {unique_starts[0]}..{unique_starts[-1]}, "
            f"akhir: {unique_ends[0]}..{unique_ends[-1]}). Evaluasi tetap diterapkan per material sesuai aturan (>60 vs <=60 bulan)."
        )
    if skipped_materials:
        codes_preview = ", ".join(item["code"] for item in skipped_materials[:10])
        suffix = f" (+{len(skipped_materials) - 10} lainnya)" if len(skipped_materials) > 10 else ""
        warnings.append(
            f"{len(skipped_materials)} material dilewati karena data tidak cukup (<6 bulan atau semua nol): {codes_preview}{suffix}."
        )

    eval_tt_count = sum(1 for m in materials.values() if len(m["series"]) > 60)
    eval_ld_count = len(materials) - eval_tt_count

    report = {
        "filename": filename,
        "source": source_desc,
        "file_ext": ext.lstrip("."),
        "rows_total": rows_total,
        "rows_valid": rows_valid,
        "rejected_bad_code_or_month": rejected_bad_code_or_month,
        "rejected_bad_qty": rejected_bad_qty,
        "duplicates_summed": duplicates_summed,
        "months_filled_zero": months_filled_zero,
        "skipped_materials": [item["code"] for item in skipped_materials],
        "skipped_details": skipped_materials,
        "n_materials": len(materials),
        "material_codes_preview": list(materials.keys())[:20],
        "period_min": period_min,
        "period_max": period_max,
        "eval_traintest_count": eval_tt_count,
        "eval_lastdemand_count": eval_ld_count,
        "warnings": warnings,
    }

    return ParsedUpload(
        filename=filename,
        file_ext=ext.lstrip("."),
        materials=materials,
        report=report,
    )
