"""Firebase Admin SDK persistence layer (Auth, Firestore, Firebase Storage).

Architecture:
- users/{uid}
    profile, settings, activeDatasetId, activeDatasetVersion, activeDatasetUpdatedAt
- users/{uid}/datasets/{datasetId}
    metadata, uploadInformation, status, forecastingConfiguration, summaryResults, timestamps
- users/{uid}/datasets/{datasetId}/materials/{stockCode}
    Full per-material forecasting result, metrics, evaluation, gridSearch, flat-encoded series
- Storage path:
    users/{uid}/datasets/{datasetId}/original/{sanitized_filename}

Chunking each material into its own subcollection document guarantees we never
exceed Firestore's 1 MiB document size limit even for 100-500+ materials.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from .config import settings

logger = logging.getLogger(__name__)

try:
    import firebase_admin
    from firebase_admin import auth as fb_auth
    from firebase_admin import credentials as fb_credentials
    from firebase_admin import firestore as fb_firestore
    from firebase_admin import storage as fb_storage
except ImportError:  # pragma: no cover
    firebase_admin = None
    fb_auth = None
    fb_credentials = None
    fb_firestore = None
    fb_storage = None


class TokenExpiredError(Exception):
    """Raised when a Firebase ID token has expired."""


def _sanitize_filename(name: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(name or "dataset.xlsx"))
    return clean[:120] or "dataset.xlsx"


def _safe_doc_id(stock_code: str) -> str:
    """Ensure a valid Firestore document ID (no '/', '.', '..', or '__reserved__')."""
    clean = re.sub(r"[/\\]", "_", str(stock_code).strip())
    if not clean or clean in {".", ".."} or (clean.startswith("__") and clean.endswith("__")):
        clean = f"mat_{clean}"
    return clean[:300]


def _to_utc_datetime(val: Any) -> Optional[dt.datetime]:
    if not isinstance(val, dt.datetime):
        return None
    if val.tzinfo is None:
        return val.replace(tzinfo=dt.timezone.utc)
    return val.astimezone(dt.timezone.utc)


def _encode_pairs(pairs: List[List[Any]]) -> List[str]:
    """Flatten [[month, qty], ...] -> ['YYYY-MM|qty', ...] (Firestore forbids nested arrays)."""
    return [f"{p[0]}|{p[1]}" for p in pairs]


def _decode_pairs(flat: Optional[List[Any]]) -> List[List[Any]]:
    out: List[List[Any]] = []
    for item in flat or []:
        if isinstance(item, str) and "|" in item:
            m, q = item.split("|", 1)
            out.append([m, float(q)])
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            out.append([str(item[0]), float(item[1])])
    return out


def _serialize_material_doc(stock_code: str, item: Dict[str, Any]) -> Dict[str, Any]:
    res = item["result"]
    ev = res.get("evaluation") or {}
    # Flatten eval trainRange/testRange cleanly (1D lists are allowed in Firestore; nested lists are not)
    eval_copy = dict(ev)
    if "rows" in eval_copy and isinstance(eval_copy["rows"], list):
        # Array of maps is valid in Firestore
        eval_copy["rows"] = [dict(r) for r in eval_copy["rows"]]

    return {
        "stockCode": stock_code,
        "name": res["name"],
        "classification": res["classification"],
        "adi": res["adi"],
        "cv2": res["cv2"],
        "range": res["range"],
        "alphaC": res["alphaC"],
        "alphaS": res["alphaS"],
        "betaT": res["betaT"],
        "bestMethod": res["bestMethod"],
        "bestMse": res.get("bestMse"),
        "bestMape": res.get("bestMape"),
        "selectionBasis": res["selectionBasis"],
        "source": res["source"],
        "formulaVersion": res.get("formulaVersion", "excel-verified-v1"),
        "gridStep": res.get("gridStep", 0.0001),
        "horizonMonths": res["horizonMonths"],
        "nextForecast": res.get("nextForecast"),
        "interval": res.get("interval"),
        "nextMonth": res.get("nextMonth"),
        "nextQty": res.get("nextQty"),
        "metrics": res["metrics"],
        "evaluation": eval_copy,
        "gridSearch": item.get("grid_search"),
        "gridSearchTrain": item.get("grid_search_train"),
        "data": _encode_pairs(res.get("data") or []),
        "predictions": _encode_pairs(res.get("predictions") or []),
        "custom": True,
    }


def _deserialize_material_doc(doc_data: Dict[str, Any]) -> Dict[str, Any]:
    stock_code = doc_data["stockCode"]
    result = {
        "stockCode": stock_code,
        "name": doc_data["name"],
        "classification": doc_data["classification"],
        "adi": doc_data.get("adi"),
        "cv2": doc_data.get("cv2"),
        "range": doc_data.get("range", ""),
        "alphaC": doc_data["alphaC"],
        "alphaS": doc_data["alphaS"],
        "betaT": doc_data["betaT"],
        "bestMethod": doc_data["bestMethod"],
        "bestMse": doc_data.get("bestMse"),
        "bestMape": doc_data.get("bestMape"),
        "selectionBasis": doc_data.get("selectionBasis", "mase_in_sample"),
        "source": doc_data.get("source", "backend_python"),
        "formulaVersion": doc_data.get("formulaVersion", "excel-verified-v1"),
        "gridStep": doc_data.get("gridStep", 0.0001),
        "horizonMonths": doc_data["horizonMonths"],
        "nextForecast": doc_data.get("nextForecast"),
        "interval": doc_data.get("interval"),
        "nextMonth": doc_data.get("nextMonth"),
        "nextQty": doc_data.get("nextQty"),
        "metrics": doc_data["metrics"],
        "data": _decode_pairs(doc_data.get("data")),
        "predictions": _decode_pairs(doc_data.get("predictions")),
        "evaluation": doc_data.get("evaluation"),
        "custom": True,
    }
    return {
        "stock_code": stock_code,
        "result": result,
        "grid_search": doc_data.get("gridSearch"),
        "grid_search_train": doc_data.get("gridSearchTrain"),
    }


def build_summary_results(analyzed_items: List[Dict[str, Any]]) -> Dict[str, Any]:
    cls_counts = {"Lumpy": 0, "Intermittent": 0, "Erratic": 0, "Smooth": 0}
    method_counts = {"Croston": 0, "SBA": 0, "TSB": 0}
    with_forecast = 0
    eval_tt = 0
    eval_ld = 0
    events_by_year: Dict[str, int] = {}
    material_index: List[Dict[str, Any]] = []

    for item in analyzed_items:
        r = item["result"]
        c = r["classification"]
        if c in cls_counts:
            cls_counts[c] += 1
        bm = r["bestMethod"]
        if bm in method_counts:
            method_counts[bm] += 1
        preds = r.get("predictions") or []
        if preds:
            with_forecast += 1
            for p in preds:
                yr = str(p[0])[:4]
                events_by_year[yr] = events_by_year.get(yr, 0) + 1
        ev = r.get("evaluation") or {}
        if ev.get("mode") == "traintest":
            eval_tt += 1
        else:
            eval_ld += 1

        material_index.append({
            "code": item["stock_code"],
            "name": r["name"],
            "classification": c,
            "adi": r.get("adi"),
            "cv2": r.get("cv2"),
            "bestMethod": bm,
            "mase": (r["metrics"].get(bm) or {}).get("mase"),
            "evalMode": ev.get("mode", "lastdemand"),
            "dataMonths": len(r.get("data") or []),
            "predictionCount": len(preds),
        })

    max_index = 500
    return {
        "totalMaterials": len(analyzed_items),
        "classificationCounts": cls_counts,
        "bestMethodCounts": method_counts,
        "materialsWithForecast": with_forecast,
        "evalTrainTestCount": eval_tt,
        "evalLastDemandCount": eval_ld,
        "forecastEventsByYear": events_by_year,
        "materialIndex": material_index[:max_index],
        "materialIndexTruncated": len(material_index) > max_index,
    }


class FirebaseStore:
    """Thread-safe wrapper around Firebase Admin Auth, Firestore, and Storage."""

    TokenExpiredError = TokenExpiredError

    def __init__(self) -> None:
        self._app = None
        self._db = None
        self._bucket = None
        self.init_error: Optional[str] = None
        self._initialize()

    def _load_credential(self):
        raw_json = settings.firebase_service_account_json
        if raw_json:
            text = raw_json.strip()
            if not text.startswith("{"):
                # Support base64-encoded service account JSON in env var
                text = base64.b64decode(text).decode("utf-8")
            info = json.loads(text, strict=False)
            # Fix double-escaped newlines common when pasting private_key into Render env UI
            if isinstance(info.get("private_key"), str):
                info["private_key"] = info["private_key"].replace("\\n", "\n")
            return fb_credentials.Certificate(info)

        cred_path = settings.google_application_credentials
        if cred_path and os.path.isfile(cred_path):
            return fb_credentials.Certificate(cred_path)

        return None

    def _initialize(self) -> None:
        if firebase_admin is None:
            self.init_error = "firebase-admin tidak terpasang."
            return
        created_app = None
        try:
            if firebase_admin._apps:
                self._app = firebase_admin.get_app()
            else:
                cred = self._load_credential()
                options: Dict[str, Any] = {"projectId": settings.firebase_project_id}
                if settings.firebase_storage_bucket:
                    options["storageBucket"] = settings.firebase_storage_bucket
                if cred is not None:
                    created_app = firebase_admin.initialize_app(cred, options)
                else:
                    # Attempt Application Default Credentials (e.g., Cloud Run)
                    created_app = firebase_admin.initialize_app(options=options)
                self._app = created_app

            self._db = fb_firestore.client(app=self._app)
            if settings.firebase_storage_bucket:
                try:
                    self._bucket = fb_storage.bucket(settings.firebase_storage_bucket, app=self._app)
                except Exception as exc:
                    logger.warning("Firebase Storage bucket belum aktif: %s", exc)
                    self._bucket = None
        except Exception as exc:
            self.init_error = str(exc)
            logger.warning("Firebase Admin SDK tidak terinisialisasi: %s", exc)
            if created_app is not None:
                try:
                    firebase_admin.delete_app(created_app)
                except Exception:
                    pass
            self._app = None
            self._db = None
            self._bucket = None

    @property
    def auth_ready(self) -> bool:
        return self._app is not None and fb_auth is not None

    @property
    def firestore_ready(self) -> bool:
        return self._db is not None

    @property
    def storage_ready(self) -> bool:
        return self._bucket is not None

    def verify_id_token(self, id_token: str) -> Dict[str, Any]:
        if not self.auth_ready:
            raise RuntimeError("Firebase Admin Auth belum aktif.")
        try:
            return fb_auth.verify_id_token(id_token, app=self._app, check_revoked=False)
        except fb_auth.ExpiredIdTokenError as exc:
            raise TokenExpiredError("Token kedaluwarsa") from exc

    def _user_ref(self, uid: str):
        return self._db.collection("users").document(uid)

    def _dataset_ref(self, uid: str, dataset_id: str):
        return self._user_ref(uid).collection("datasets").document(dataset_id)

    def upload_original_file(
        self,
        uid: str,
        dataset_id: str,
        filename: str,
        raw_bytes: bytes,
        content_type: str,
    ) -> Optional[str]:
        """Store original uploaded Excel/CSV file under users/{uid}/datasets/{datasetId}/original/."""
        if not self.storage_ready:
            return None
        safe_name = _sanitize_filename(filename)
        storage_path = f"users/{uid}/datasets/{dataset_id}/original/{safe_name}"
        blob = self._bucket.blob(storage_path)
        blob.upload_from_string(raw_bytes, content_type=content_type or "application/octet-stream")
        return storage_path

    def create_dataset_record(
        self,
        uid: str,
        email: Optional[str],
        dataset_id: str,
        dataset_name: str,
        filename: str,
        file_ext: str,
        file_size_bytes: int,
        storage_path: Optional[str],
        validation_report: Dict[str, Any],
    ) -> None:
        """Create initial dataset metadata document in Firestore (status='uploaded')."""
        if not self.firestore_ready:
            return
        now = dt.datetime.now(dt.timezone.utc)
        user_ref = self._user_ref(uid)
        user_ref.set(
            {
                "profile": {"email": email or "", "lastSeenAt": now},
                "updatedAt": now,
            },
            merge=True,
        )

        self._dataset_ref(uid, dataset_id).set(
            {
                "datasetId": dataset_id,
                "uid": uid,
                "name": dataset_name,
                "originalFilename": filename,
                "fileType": file_ext,
                "fileSizeBytes": file_size_bytes,
                "storagePath": storage_path,
                "status": "uploaded",
                "errorMessage": None,
                "materialCount": validation_report.get("n_materials", 0),
                "periodMin": validation_report.get("period_min", ""),
                "periodMax": validation_report.get("period_max", ""),
                "validationReport": validation_report,
                "forecastingConfig": {
                    "methods": ["Croston", "SBA", "TSB"],
                    "gridMin": 0.0001,
                    "gridMax": 1.0,
                    "gridStep": 0.0001,
                    "objective": "in_sample_one_step_mse",
                    "selectionMetric": "mase",
                    "evalRuleThresholdMonths": 60,
                    "formulaVersion": "excel-verified-v1",
                },
                "uploadedAt": now,
                "completedAt": None,
                "updatedAt": now,
            }
        )

    def mark_dataset_processing(self, uid: str, dataset_id: str) -> None:
        if not self.firestore_ready:
            return
        now = dt.datetime.now(dt.timezone.utc)
        self._dataset_ref(uid, dataset_id).set(
            {"status": "processing", "errorMessage": None, "updatedAt": now},
            merge=True,
        )

    def mark_dataset_failed(self, uid: str, dataset_id: str, error_message: str) -> None:
        """Mark dataset failed WITHOUT touching users/{uid}.activeDatasetId."""
        if not self.firestore_ready:
            return
        now = dt.datetime.now(dt.timezone.utc)
        self._dataset_ref(uid, dataset_id).set(
            {"status": "failed", "errorMessage": error_message[:1000], "updatedAt": now},
            merge=True,
        )

    def save_completed_dataset(
        self,
        uid: str,
        dataset_id: str,
        analyzed_items: List[Dict[str, Any]],
        activate: bool = True,
    ) -> Tuple[bool, Dict[str, Any]]:
        """Persist all material results in chunked batches, mark dataset completed,
        and atomically promote activeDatasetId via Firestore transaction."""
        summary = build_summary_results(analyzed_items)
        if not self.firestore_ready:
            return False, summary

        ds_ref = self._dataset_ref(uid, dataset_id)
        materials_col = ds_ref.collection("materials")
        now = dt.datetime.now(dt.timezone.utc)

        # Write in Firestore batches (max 400 writes per batch to stay comfortably under 500 limit)
        batch_size = 350
        for start in range(0, len(analyzed_items), batch_size):
            batch = self._db.batch()
            for item in analyzed_items[start : start + batch_size]:
                code = item["stock_code"]
                doc_payload = _serialize_material_doc(code, item)
                doc_payload["updatedAt"] = now
                batch.set(materials_col.document(_safe_doc_id(code)), doc_payload)
            batch.commit()

        # Commit dataset completion document AFTER all material documents succeed
        ds_ref.set(
            {
                "status": "completed",
                "errorMessage": None,
                "materialCount": len(analyzed_items),
                "summaryResults": summary,
                "completedAt": now,
                "updatedAt": now,
            },
            merge=True,
        )

        # Concurrency-safe activation using Firestore Transaction (latest completed upload wins)
        if activate:
            user_ref = self._user_ref(uid)
            transaction = self._db.transaction()

            @fb_firestore.transactional
            def _activate_tx(tx):
                snap = user_ref.get(transaction=tx)
                cur = snap.to_dict() if snap.exists else {}
                cur_updated = _to_utc_datetime(cur.get("activeDatasetUpdatedAt"))
                cur_version = int(cur.get("activeDatasetVersion", 0) or 0)

                # Only promote if no newer completed dataset was activated concurrently
                if cur_updated is None or now >= cur_updated:
                    tx.set(
                        user_ref,
                        {
                            "activeDatasetId": dataset_id,
                            "activeDatasetVersion": cur_version + 1,
                            "activeDatasetUpdatedAt": now,
                            "updatedAt": now,
                        },
                        merge=True,
                    )

            _activate_tx(transaction)

        return True, summary

    def list_datasets(self, uid: str) -> Dict[str, Any]:
        if not self.firestore_ready:
            return {"activeDatasetId": "baseline", "datasets": []}

        user_snap = self._user_ref(uid).get()
        user_data = user_snap.to_dict() if user_snap.exists else {}
        active_id = user_data.get("activeDatasetId") or "baseline"

        snaps = (
            self._user_ref(uid)
            .collection("datasets")
            .order_by("uploadedAt", direction=fb_firestore.Query.DESCENDING)
            .stream()
        )
        datasets: List[Dict[str, Any]] = []
        for s in snaps:
            d = s.to_dict() or {}
            d["datasetId"] = d.get("datasetId") or s.id
            d["isActive"] = d["datasetId"] == active_id
            for ts_key in ("uploadedAt", "completedAt", "updatedAt"):
                if hasattr(d.get(ts_key), "isoformat"):
                    d[ts_key] = d[ts_key].isoformat()
            datasets.append(d)

        return {"activeDatasetId": active_id, "datasets": datasets}

    def get_dataset_materials(self, uid: str, dataset_id: str) -> List[Dict[str, Any]]:
        if not self.firestore_ready:
            return []
        ds_snap = self._dataset_ref(uid, dataset_id).get()
        if not ds_snap.exists:
            raise KeyError("Dataset tidak ditemukan.")
        ds_data = ds_snap.to_dict() or {}
        if ds_data.get("uid") and ds_data.get("uid") != uid:
            raise PermissionError("Akses ditolak.")

        mat_snaps = self._dataset_ref(uid, dataset_id).collection("materials").stream()
        items = [_deserialize_material_doc(s.to_dict()) for s in mat_snaps if s.exists]
        items.sort(key=lambda x: x["stock_code"])
        return items

    def activate_dataset(self, uid: str, dataset_id: str) -> Dict[str, Any]:
        if not self.firestore_ready:
            raise RuntimeError("Firestore belum aktif di backend.")

        now = dt.datetime.now(dt.timezone.utc)
        if dataset_id != "baseline":
            ds_snap = self._dataset_ref(uid, dataset_id).get()
            if not ds_snap.exists:
                raise KeyError("Dataset tidak ditemukan.")
            ds_data = ds_snap.to_dict() or {}
            if ds_data.get("uid") != uid:
                raise PermissionError("Anda tidak memiliki izin mengakses dataset ini.")
            if ds_data.get("status") != "completed":
                raise ValueError("Hanya dataset dengan status selesai (completed) yang dapat diaktifkan.")

        user_ref = self._user_ref(uid)
        transaction = self._db.transaction()

        @fb_firestore.transactional
        def _set_active(tx):
            snap = user_ref.get(transaction=tx)
            cur = snap.to_dict() if snap.exists else {}
            ver = int(cur.get("activeDatasetVersion", 0) or 0) + 1
            tx.set(
                user_ref,
                {
                    "activeDatasetId": dataset_id,
                    "activeDatasetVersion": ver,
                    "activeDatasetUpdatedAt": now,
                    "updatedAt": now,
                },
                merge=True,
            )
            return ver

        new_ver = _set_active(transaction)
        return {"activeDatasetId": dataset_id, "activeDatasetVersion": new_ver}

    def delete_dataset(self, uid: str, dataset_id: str) -> Dict[str, Any]:
        if dataset_id == "baseline":
            raise ValueError("Baseline awal (8 material Excel) tidak dapat dihapus.")
        if not self.firestore_ready:
            raise RuntimeError("Firestore belum aktif di backend.")

        ds_ref = self._dataset_ref(uid, dataset_id)
        ds_snap = ds_ref.get()
        if not ds_snap.exists:
            raise KeyError("Dataset tidak ditemukan.")
        ds_data = ds_snap.to_dict() or {}
        if ds_data.get("uid") != uid:
            raise PermissionError("Anda tidak memiliki izin menghapus dataset ini.")

        # 1. Delete Storage file if present
        storage_path = ds_data.get("storagePath")
        storage_deleted = False
        if storage_path and self.storage_ready:
            try:
                blob = self._bucket.blob(storage_path)
                if blob.exists():
                    blob.delete()
                storage_deleted = True
            except Exception as exc:
                logger.warning("Gagal menghapus file Storage %s: %s", storage_path, exc)

        # 2. Delete all material subcollection documents in batches
        mat_docs = list(ds_ref.collection("materials").stream())
        for start in range(0, len(mat_docs), 350):
            batch = self._db.batch()
            for doc in mat_docs[start : start + 350]:
                batch.delete(doc.reference)
            batch.commit()

        # 3. Delete dataset metadata document
        ds_ref.delete()

        # 4. If deleted dataset was active, safely fall back to latest remaining completed dataset or baseline
        user_ref = self._user_ref(uid)
        user_snap = user_ref.get()
        user_data = user_snap.to_dict() if user_snap.exists else {}
        new_active = user_data.get("activeDatasetId") or "baseline"
        if new_active == dataset_id:
            remaining = (
                user_ref.collection("datasets")
                .order_by("uploadedAt", direction=fb_firestore.Query.DESCENDING)
                .stream()
            )
            fallback_id = "baseline"
            for r in remaining:
                rd = r.to_dict() or {}
                if rd.get("status") == "completed":
                    fallback_id = rd.get("datasetId") or r.id
                    break
            now = dt.datetime.now(dt.timezone.utc)
            ver = int(user_data.get("activeDatasetVersion", 0) or 0) + 1
            user_ref.set(
                {
                    "activeDatasetId": fallback_id,
                    "activeDatasetVersion": ver,
                    "activeDatasetUpdatedAt": now,
                    "updatedAt": now,
                },
                merge=True,
            )
            new_active = fallback_id

        return {
            "deletedDatasetId": dataset_id,
            "deletedMaterials": len(mat_docs),
            "storageDeleted": storage_deleted,
            "activeDatasetId": new_active,
        }

    def generate_download_url(self, uid: str, dataset_id: str) -> Dict[str, Any]:
        if not self.firestore_ready or not self.storage_ready:
            raise RuntimeError("Firebase Storage belum dikonfigurasi di backend.")
        ds_snap = self._dataset_ref(uid, dataset_id).get()
        if not ds_snap.exists:
            raise KeyError("Dataset tidak ditemukan.")
        ds_data = ds_snap.to_dict() or {}
        if ds_data.get("uid") != uid:
            raise PermissionError("Akses ditolak.")
        storage_path = ds_data.get("storagePath")
        if not storage_path:
            raise KeyError("File asli tidak tersedia di Firebase Storage untuk dataset ini.")

        blob = self._bucket.blob(storage_path)
        if not blob.exists():
            raise KeyError("File asli sudah tidak ditemukan di Firebase Storage.")

        url = blob.generate_signed_url(
            version="v4",
            expiration=dt.timedelta(minutes=15),
            method="GET",
            response_disposition=f'attachment; filename="{_sanitize_filename(ds_data.get("originalFilename", "dataset.xlsx"))}"',
        )
        return {
            "downloadUrl": url,
            "filename": ds_data.get("originalFilename"),
            "expiresInSeconds": 900,
        }


firebase_store = FirebaseStore()
