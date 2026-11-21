"""Asynchronous background forecasting job runner with per-UID isolation and real progress."""

from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .config import settings
from .firebase_store import build_summary_results, firebase_store
from .forecasting import analyze_material
from .parser import ParsedUpload

logger = logging.getLogger(__name__)
IS_SERVERLESS = bool(os.getenv("VERCEL") or os.getenv("AWS_LAMBDA_FUNCTION_NAME"))


@dataclass
class UploadEntry:
    upload_id: str
    dataset_id: str
    uid: str
    email: Optional[str]
    parsed: ParsedUpload
    storage_path: Optional[str]
    file_size_bytes: int
    created_at: float = field(default_factory=time.time)
    active_job_id: Optional[str] = None


@dataclass
class JobState:
    job_id: str
    upload_id: str
    dataset_id: str
    uid: str
    status: str = "queued"  # queued | forecasting | saving | done | error
    stage: str = "Menunggu antrean proses..."
    done: int = 0
    total: int = 0
    current_material: Optional[Dict[str, str]] = None
    saved_to_firebase: bool = False
    activated: bool = False
    error: Optional[str] = None
    started_at: str = field(default_factory=lambda: dt.datetime.now(dt.timezone.utc).isoformat())
    finished_at: Optional[str] = None
    finished_ts: Optional[float] = None
    summary: Optional[Dict[str, Any]] = None
    results: List[Dict[str, Any]] = field(default_factory=list)

    def to_public_dict(self) -> Dict[str, Any]:
        # Real progress computation:
        # Forecasting accounts for 0..90%, saving to Firebase accounts for 90..100%.
        if self.status == "done":
            percent = 100
        elif self.status == "saving":
            percent = 95
        elif self.status == "error":
            percent = int(round(90 * self.done / self.total)) if self.total else 0
        elif self.total > 0:
            percent = min(90, int(round(90 * self.done / self.total)))
        else:
            percent = 0

        return {
            "job_id": self.job_id,
            "upload_id": self.upload_id,
            "dataset_id": self.dataset_id,
            "status": self.status,
            "stage": self.stage,
            "done": self.done,
            "total": self.total,
            "percent": percent,
            "current_material": self.current_material,
            "saved_to_firebase": self.saved_to_firebase,
            "activated": self.activated,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "summary": self.summary,
        }


class JobManager:
    """Manages per-UID uploads, async forecasting jobs, and latest session results."""

    def __init__(self, max_workers: int = 2) -> None:
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="forecast-worker")
        self._uploads: Dict[str, UploadEntry] = {}
        self._jobs: Dict[str, JobState] = {}
        self._latest_results_by_uid: Dict[str, Dict[str, Any]] = {}

    def register_upload(
        self,
        uid: str,
        email: Optional[str],
        parsed: ParsedUpload,
        storage_path: Optional[str],
        file_size_bytes: int,
        dataset_id: Optional[str] = None,
    ) -> UploadEntry:
        ds_id = dataset_id or f"ds_{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
        # Use dataset_id as upload_id so stateless serverless instances can recover staged uploads from Firestore
        upload_id = ds_id
        entry = UploadEntry(
            upload_id=upload_id,
            dataset_id=ds_id,
            uid=uid,
            email=email,
            parsed=parsed,
            storage_path=storage_path,
            file_size_bytes=file_size_bytes,
        )
        with self._lock:
            self._prune_old_uploads_locked()
            self._uploads[upload_id] = entry
        return entry

    def _prune_old_uploads_locked(self, ttl_seconds: int = 3600) -> None:
        cutoff = time.time() - ttl_seconds
        expired = [k for k, v in self._uploads.items() if v.created_at < cutoff]
        for k in expired:
            self._uploads.pop(k, None)

    def _prune_old_jobs_locked(self, ttl_seconds: int = 1800) -> None:
        cutoff = time.time() - ttl_seconds
        expired = [
            k for k, j in self._jobs.items()
            if j.finished_ts is not None and j.finished_ts < cutoff
        ]
        for k in expired:
            self._jobs.pop(k, None)

    def get_upload(self, upload_id: str, uid: str) -> UploadEntry:
        with self._lock:
            entry = self._uploads.get(upload_id)
        if not entry:
            raise KeyError("Upload ID tidak ditemukan atau sudah kedaluwarsa. Silakan unggah ulang file.")
        if entry.uid != uid:
            raise PermissionError("Anda tidak memiliki izin mengakses upload ini.")
        return entry

    def start_job(
        self,
        upload_id: str,
        uid: str,
        dataset_name: Optional[str] = None,
        activate_on_complete: bool = True,
    ) -> JobState:
        with self._lock:
            self._prune_old_jobs_locked()
            entry = self._uploads.get(upload_id)

        # Serverless recovery: if /upload landed on another instance, load staged materials from Firestore
        if entry is None and firebase_store.firestore_ready:
            staged = firebase_store.load_staged_upload(uid, upload_id)
            if staged:
                entry = UploadEntry(
                    upload_id=upload_id,
                    dataset_id=upload_id,
                    uid=uid,
                    email=None,
                    parsed=ParsedUpload(filename="", file_ext="", materials=staged, report={}),
                    storage_path=None,
                    file_size_bytes=0,
                )

        if entry is None:
            raise KeyError("Upload ID tidak ditemukan atau sudah kedaluwarsa. Silakan unggah ulang file.")
        if entry.uid != uid:
            raise PermissionError("Anda tidak memiliki izin mengakses upload ini.")

        with self._lock:
            # Idempotency guard: prevent duplicate concurrent jobs for the same upload_id
            if entry.active_job_id and entry.active_job_id in self._jobs:
                existing = self._jobs[entry.active_job_id]
                if existing.status in ("queued", "forecasting", "saving", "done"):
                    return existing

            job_id = entry.dataset_id if IS_SERVERLESS else uuid.uuid4().hex
            total = len(entry.parsed.materials)
            job = JobState(
                job_id=job_id,
                upload_id=upload_id,
                dataset_id=entry.dataset_id,
                uid=uid,
                status="queued",
                stage="Menyiapkan proses klasifikasi & forecasting...",
                done=0,
                total=total,
            )
            entry.active_job_id = job_id
            self._jobs[job_id] = job

        # Update dataset name in Firestore if user provided a custom label
        if dataset_name and firebase_store.firestore_ready:
            try:
                firebase_store._dataset_ref(uid, entry.dataset_id).set(
                    {"name": dataset_name.strip()[:120]},
                    merge=True,
                )
            except Exception as exc:
                logger.warning("Gagal memperbarui nama dataset di Firestore: %s", exc)

        if IS_SERVERLESS:
            # On Vercel Serverless (Fluid Compute up to 300s), run inline so the runtime
            # does not freeze a background thread before Firestore writes complete.
            self._run_job(job, entry, activate_on_complete)
        else:
            self._executor.submit(self._run_job, job, entry, activate_on_complete)
        return job

    def get_job(self, job_id: str, uid: str) -> JobState:
        with self._lock:
            job = self._jobs.get(job_id)
        if job:
            if job.uid != uid:
                raise PermissionError("Anda tidak memiliki izin melihat status job ini.")
            return job

        # Serverless fallback: check Firestore dataset status
        if firebase_store.firestore_ready:
            ds = firebase_store.get_dataset_status(uid, job_id)
            if ds:
                st_raw = ds.get("status", "processing")
                mapped = "done" if st_raw == "completed" else ("error" if st_raw == "failed" else "forecasting")
                total = int(ds.get("materialCount", 0) or 0)
                return JobState(
                    job_id=job_id,
                    upload_id=job_id,
                    dataset_id=job_id,
                    uid=uid,
                    status=mapped,
                    stage="Selesai — tersimpan permanen ke Firebase." if mapped == "done" else (ds.get("errorMessage") or "Memproses..."),
                    done=total if mapped == "done" else 0,
                    total=total,
                    saved_to_firebase=(mapped == "done"),
                    activated=(mapped == "done"),
                    error=ds.get("errorMessage"),
                    summary=ds.get("summaryResults"),
                )

        raise KeyError("Job ID tidak ditemukan.")

    def get_latest_results(self, uid: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._latest_results_by_uid.get(uid)

    def _run_job(self, job: JobState, entry: UploadEntry, activate_on_complete: bool) -> None:
        uid = entry.uid
        dataset_id = entry.dataset_id
        materials = entry.parsed.materials
        total = len(materials)

        try:
            firebase_store.mark_dataset_processing(uid, dataset_id)
            analyzed: List[Dict[str, Any]] = []

            for idx, (code, payload) in enumerate(materials.items(), start=1):
                with self._lock:
                    job.status = "forecasting"
                    job.current_material = {"code": code, "name": payload["name"]}
                    job.stage = (
                        f"Klasifikasi ADI/CV², grid search α/β & forecasting "
                        f"({idx}/{total}: {code})"
                    )

                item = analyze_material(code, payload["name"], payload["series"])
                analyzed.append(item)

                with self._lock:
                    job.done = idx

            # Persist permanently to Firebase BEFORE marking job done
            with self._lock:
                job.status = "saving"
                job.current_material = None
                job.stage = f"Menyimpan {total} hasil material secara permanen ke Firebase..."

            saved = False
            if firebase_store.firestore_ready:
                saved, summary = firebase_store.save_completed_dataset(
                    uid=uid,
                    dataset_id=dataset_id,
                    analyzed_items=analyzed,
                    activate=activate_on_complete,
                )
                firebase_store.delete_staged_upload(uid, dataset_id)
            else:
                summary = build_summary_results(analyzed)
                if settings.is_production:
                    raise RuntimeError(
                        "Penyimpanan Firebase Firestore belum aktif di server production."
                    )

            with self._lock:
                job.results = [] if saved else analyzed
                job.summary = summary
                job.saved_to_firebase = saved
                job.activated = bool(saved and activate_on_complete)
                job.status = "done"
                job.stage = (
                    "Selesai — seluruh hasil telah tersimpan permanen ke Firebase."
                    if saved
                    else "Selesai (mode lokal tanpa Firestore)."
                )
                job.finished_at = dt.datetime.now(dt.timezone.utc).isoformat()
                job.finished_ts = time.time()
                self._latest_results_by_uid[uid] = {
                    "dataset_id": dataset_id,
                    "upload_id": entry.upload_id,
                    "materials": analyzed,
                    "summary": summary,
                    "saved_to_firebase": saved,
                }
                # Free upload memory after successful run
                self._uploads.pop(entry.upload_id, None)

        except Exception as exc:
            logger.exception("Forecasting job %s gagal untuk uid=%s: %s", job.job_id, uid, exc)
            err_msg = str(exc) or "Terjadi kesalahan internal saat memproses forecasting."
            try:
                firebase_store.mark_dataset_failed(uid, dataset_id, err_msg)
            except Exception as fb_exc:
                logger.error("Gagal menandai dataset failed di Firestore: %s", fb_exc)

            with self._lock:
                job.status = "error"
                job.current_material = None
                job.saved_to_firebase = False
                job.activated = False
                job.error = err_msg
                job.stage = "Proses gagal — dataset aktif sebelumnya tetap dipertahankan."
                job.finished_at = dt.datetime.now(dt.timezone.utc).isoformat()
                job.finished_ts = time.time()
                entry.active_job_id = None


job_manager = JobManager(max_workers=settings.max_concurrent_jobs)
