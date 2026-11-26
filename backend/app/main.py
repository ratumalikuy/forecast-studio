"""FastAPI application for Dead Stock Analytics Backend.

Preserves all existing frontend bridge endpoints:
- GET  /                        Health check & capabilities
- POST /upload                  Upload & validate .xlsx/.xlsm/.csv, archive original to Firebase Storage
- POST /analyze                 Launch async background forecasting job
- GET  /jobs/{job_id}           Poll real stage-by-stage & per-material job progress
- GET  /materials?full=true     Retrieve analyzed materials (for dataset_id or latest run)

Plus account-scoped dataset & history management endpoints:
- GET    /datasets                        List user's dataset history + activeDatasetId
- POST   /datasets/{dataset_id}/activate  Set active dataset (or 'baseline') atomically
- DELETE /datasets/{dataset_id}           Delete dataset metadata, material docs, & Storage file
- GET    /datasets/{dataset_id}/download-url  Get signed URL to download original file
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import re
import uuid
from typing import Any, Dict, Optional

from fastapi import FastAPI, File, HTTPException, Path, Query, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from . import __version__
from .auth import AuthenticatedUser, CurrentUser
from .config import settings
from .firebase_store import firebase_store
from .jobs import job_manager
from .parser import FileValidationError, parse_demand_file

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

_DATASET_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _validate_dataset_id(dataset_id: str) -> str:
    if not _DATASET_ID_RE.match(dataset_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Format dataset_id tidak valid.",
        )
    return dataset_id


app = FastAPI(
    title="Dead Stock Analytics API",
    version=__version__,
    description="Spare parts intermittent demand forecasting backend (ADI/CV², Croston, SBA, TSB) with Firebase persistence.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials="*" not in settings.cors_origins,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


class AnalyzeRequest(BaseModel):
    upload_id: str = Field(..., min_length=1)
    dataset_name: Optional[str] = Field(default=None, max_length=120)
    activate_on_complete: bool = True


@app.get("/")
def health_check() -> Dict[str, Any]:
    return {
        "status": "ok",
        "service": "dead-stock-analytics-api",
        "version": __version__,
        "environment": settings.environment,
        "auth_required": settings.require_auth,
        "firebase_auth_ready": firebase_store.auth_ready,
        "firestore_ready": firebase_store.firestore_ready,
        "storage_ready": firebase_store.storage_ready,
        "formula_version": "excel-verified-v1",
    }


@app.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    dataset_name: Optional[str] = Query(default=None, max_length=120),
    user: AuthenticatedUser = CurrentUser,
) -> Dict[str, Any]:
    """Receive, validate, and archive an uploaded .xlsx, .xlsm, or .csv demand file."""
    filename = file.filename or ""
    raw_bytes = await file.read()

    if len(raw_bytes) > settings.max_upload_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Ukuran file melebihi batas maksimal {settings.max_upload_mb} MB.",
        )

    try:
        parsed = parse_demand_file(filename, raw_bytes)
    except FileValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Gagal membaca file upload '%s': %s", filename, exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File tidak dapat dibaca. Pastikan file Excel/CSV valid dan tidak terproteksi password.",
        ) from exc

    dataset_id = f"ds_{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    base_label = os.path.splitext(os.path.basename(filename))[0] or dataset_id
    final_name = (dataset_name.strip() if dataset_name and dataset_name.strip() else base_label)[:120]

    # Store original unmodified file in Firebase Storage scoped to users/{uid}/datasets/{datasetId}/
    storage_path: Optional[str] = None
    storage_warning: Optional[str] = None
    if firebase_store.storage_ready:
        try:
            storage_path = firebase_store.upload_original_file(
                uid=user.uid,
                dataset_id=dataset_id,
                filename=filename,
                raw_bytes=raw_bytes,
                content_type=file.content_type or "application/octet-stream",
            )
        except Exception as exc:
            logger.warning("Upload file asli ke Firebase Storage gagal (uid=%s): %s", user.uid, exc)
            storage_warning = "File asli belum berhasil diarsipkan ke Firebase Storage, namun analisis tetap dapat dilanjutkan."

    if storage_warning:
        parsed.report["warnings"].append(storage_warning)

    # Create initial Firestore dataset record + stage parsed series for serverless /analyze
    if firebase_store.firestore_ready:
        try:
            firebase_store.create_dataset_record(
                uid=user.uid,
                email=user.email,
                dataset_id=dataset_id,
                dataset_name=final_name,
                filename=filename,
                file_ext=parsed.file_ext,
                file_size_bytes=len(raw_bytes),
                storage_path=storage_path,
                validation_report=parsed.report,
            )
            firebase_store.save_staged_upload(user.uid, dataset_id, parsed.materials)
        except Exception as exc:
            logger.exception("Gagal menyimpan metadata awal dataset di Firestore: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Gagal menyimpan metadata upload ke Firebase Firestore. Silakan coba lagi.",
            ) from exc

    entry = job_manager.register_upload(
        uid=user.uid,
        email=user.email,
        parsed=parsed,
        storage_path=storage_path,
        file_size_bytes=len(raw_bytes),
        dataset_id=dataset_id,
    )

    return {
        "upload_id": entry.upload_id,
        "dataset_id": entry.dataset_id,
        "dataset_name": final_name,
        "storage_path": storage_path,
        "report": parsed.report,
    }


@app.post("/analyze")
def start_analysis(
    payload: AnalyzeRequest,
    user: AuthenticatedUser = CurrentUser,
) -> Dict[str, Any]:
    """Start an asynchronous forecasting job for a previously validated upload."""
    try:
        job = job_manager.start_job(
            upload_id=payload.upload_id,
            uid=user.uid,
            dataset_name=payload.dataset_name,
            activate_on_complete=payload.activate_on_complete,
        )
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc

    return job.to_public_dict()


@app.get("/jobs/{job_id}")
def get_job_status(
    job_id: str,
    user: AuthenticatedUser = CurrentUser,
) -> Dict[str, Any]:
    """Return real progress and final persistence status of a background job."""
    try:
        job = job_manager.get_job(job_id, user.uid)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc

    return job.to_public_dict()


@app.get("/materials")
def get_materials(
    full: bool = Query(default=True),
    dataset_id: Optional[str] = Query(default=None),
    user: AuthenticatedUser = CurrentUser,
) -> Dict[str, Any]:
    """Return forecasting results for a specific dataset_id or the user's latest completed run."""
    if dataset_id and firebase_store.firestore_ready:
        try:
            items = firebase_store.get_dataset_materials(user.uid, dataset_id)
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
        if not full:
            items = [
                {
                    "stock_code": i["stock_code"],
                    "name": i["result"]["name"],
                    "classification": i["result"]["classification"],
                    "bestMethod": i["result"]["bestMethod"],
                }
                for i in items
            ]
        return {"dataset_id": dataset_id, "count": len(items), "materials": items}

    latest = job_manager.get_latest_results(user.uid)
    if latest:
        items = latest["materials"]
        if not full:
            items = [
                {
                    "stock_code": i["stock_code"],
                    "name": i["result"]["name"],
                    "classification": i["result"]["classification"],
                    "bestMethod": i["result"]["bestMethod"],
                }
                for i in items
            ]
        return {
            "dataset_id": latest["dataset_id"],
            "saved_to_firebase": latest["saved_to_firebase"],
            "count": len(items),
            "materials": items,
        }

    # Fallback: read active dataset from Firestore if backend restarted
    if firebase_store.firestore_ready:
        listing = firebase_store.list_datasets(user.uid)
        active_id = listing.get("activeDatasetId")
        if active_id and active_id != "baseline":
            try:
                items = firebase_store.get_dataset_materials(user.uid, active_id)
                return {"dataset_id": active_id, "count": len(items), "materials": items}
            except Exception:
                pass

    return {"dataset_id": None, "count": 0, "materials": []}


@app.get("/datasets")
def list_user_datasets(user: AuthenticatedUser = CurrentUser) -> Dict[str, Any]:
    """List all historical datasets belonging to the authenticated Firebase UID."""
    if not firebase_store.firestore_ready:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Firestore belum terkonfigurasi di backend.",
        )
    return firebase_store.list_datasets(user.uid)


@app.post("/datasets/{dataset_id}/activate")
def activate_user_dataset(
    dataset_id: str = Path(..., min_length=1, max_length=128),
    user: AuthenticatedUser = CurrentUser,
) -> Dict[str, Any]:
    """Switch the authenticated user's active dataset (or 'baseline') atomically."""
    _validate_dataset_id(dataset_id)
    try:
        return firebase_store.activate_dataset(user.uid, dataset_id)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@app.delete("/datasets/{dataset_id}")
def delete_user_dataset(
    dataset_id: str = Path(..., min_length=1, max_length=128),
    user: AuthenticatedUser = CurrentUser,
) -> Dict[str, Any]:
    """Delete a user's dataset metadata, all material subcollection docs, and original Storage file."""
    _validate_dataset_id(dataset_id)
    try:
        return firebase_store.delete_dataset(user.uid, dataset_id)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@app.get("/datasets/{dataset_id}/download-url")
def get_dataset_download_url(
    dataset_id: str = Path(..., min_length=1, max_length=128),
    user: AuthenticatedUser = CurrentUser,
) -> Dict[str, Any]:
    """Generate a temporary signed URL to download the original Excel/CSV from Firebase Storage."""
    _validate_dataset_id(dataset_id)
    try:
        return firebase_store.generate_download_url(user.uid, dataset_id)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
