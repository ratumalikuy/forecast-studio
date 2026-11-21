"""Dynamic scale tests for 8, 20, 50, and 100 materials."""

import io
import csv
import pytest

from app.forecasting import analyze_material, index_to_month, month_to_index
from app.parser import parse_demand_file


def _generate_csv_bytes(n_materials: int, months: int = 48) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["kode", "nama", "bulan", "qty"])
    start_idx = month_to_index("2020-01")
    for m_i in range(n_materials):
        code = f"MAT{m_i + 1:04d}"
        name = f"Material Dinamis {m_i + 1}"
        # Alternate sparse intermittent & lumpy patterns
        period = 5 + (m_i % 7)
        for t in range(months):
            mo = index_to_month(start_idx + t)
            qty = 0
            if t > 2 and t % period == 0:
                qty = (m_i % 4 + 1) * (10 if (t // period) % 2 == 0 else 2)
            writer.writerow([code, name, mo, qty])
    return buf.getvalue().encode("utf-8")


@pytest.mark.parametrize("n_materials", [8, 20, 50, 100])
def test_dynamic_material_counts(n_materials: int) -> None:
    raw = _generate_csv_bytes(n_materials=n_materials, months=48)
    parsed = parse_demand_file(f"dataset_{n_materials}.csv", raw)

    assert parsed.report["n_materials"] == n_materials
    assert len(parsed.materials) == n_materials

    results = []
    for code, entry in parsed.materials.items():
        analyzed = analyze_material(code, entry["name"], entry["series"])
        results.append(analyzed)
        assert analyzed["result"]["classification"] in {"Lumpy", "Intermittent", "Erratic", "Smooth"}
        assert analyzed["result"]["bestMethod"] in {"Croston", "SBA", "TSB"}
        assert analyzed["result"]["adi"] is not None
        assert analyzed["result"]["cv2"] is not None

    assert len(results) == n_materials
