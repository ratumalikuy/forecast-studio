"""API security, UID isolation, and async job lifecycle tests."""

import time
from fastapi.testclient import TestClient

from app.auth import AuthenticatedUser, verify_firebase_user
from app.main import app


def _sample_csv() -> bytes:
    lines = ["kode,nama,bulan,qty"]
    for m in range(1, 13):
        qty = 5 if m in (3, 7, 11) else 0
        lines.append(f"001234,Test Valve,2020-{m:02d},{qty}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def test_health_check_public_and_security_headers() -> None:
    client = TestClient(app)
    res = client.get("/")
    assert res.status_code == 200
    assert res.headers.get("cache-control") == "no-store, private"
    assert res.headers.get("x-content-type-options") == "nosniff"
    assert res.headers.get("x-frame-options") == "DENY"
    body = res.json()
    assert body["status"] == "ok"
    assert body["formula_version"] == "excel-verified-v1"


def test_protected_endpoints_require_auth() -> None:
    app.dependency_overrides.clear()
    client = TestClient(app)
    for method, path in (
        ("GET", "/materials"),
        ("GET", "/datasets"),
        ("POST", "/datasets/ds_test/activate"),
        ("DELETE", "/datasets/ds_test"),
        ("GET", "/datasets/ds_test/download-url"),
    ):
        res = client.request(method, path)
        assert res.status_code == 401, f"{method} {path} expected 401, got {res.status_code}"


def test_uid_isolation_on_uploads_and_jobs() -> None:
    user_a = AuthenticatedUser(uid="uid-account-a", email="a@example.com")
    user_b = AuthenticatedUser(uid="uid-account-b", email="b@example.com")

    app.dependency_overrides[verify_firebase_user] = lambda: user_a
    client = TestClient(app)

    up = client.post(
        "/upload",
        files={"file": ("abcd.csv", _sample_csv(), "text/csv")},
    )
    assert up.status_code == 200
    upload_id = up.json()["upload_id"]

    # Account B MUST NOT be able to analyze Account A's upload_id
    app.dependency_overrides[verify_firebase_user] = lambda: user_b
    forbidden_analyze = client.post("/analyze", json={"upload_id": upload_id})
    assert forbidden_analyze.status_code == 403

    # Account A runs analysis
    app.dependency_overrides[verify_firebase_user] = lambda: user_a
    job_res = client.post("/analyze", json={"upload_id": upload_id})
    assert job_res.status_code == 200
    job_id = job_res.json()["job_id"]

    # Account B MUST NOT be able to poll Account A's job
    app.dependency_overrides[verify_firebase_user] = lambda: user_b
    forbidden_job = client.get(f"/jobs/{job_id}")
    assert forbidden_job.status_code == 403

    # Account A polls until done
    app.dependency_overrides[verify_firebase_user] = lambda: user_a
    for _ in range(50):
        st = client.get(f"/jobs/{job_id}").json()
        if st["status"] in ("done", "error"):
            break
        time.sleep(0.05)
    assert st["status"] == "done"
    assert st["done"] == 1
    assert st["total"] == 1

    mats = client.get("/materials?full=true").json()
    assert mats["count"] == 1
    assert mats["materials"][0]["stock_code"] == "001234"

    app.dependency_overrides.clear()
