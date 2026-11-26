"""Firebase Authentication ID Token verification middleware."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, Header, HTTPException, status

from .config import settings
from .firebase_store import firebase_store

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AuthenticatedUser:
    uid: str
    email: Optional[str] = None
    name: Optional[str] = None


def verify_firebase_user(
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
) -> AuthenticatedUser:
    """Verify Firebase ID Token from `Authorization: Bearer <token>` header.

    In production (or whenever REQUIRE_AUTH=true), every protected endpoint
    strictly requires a valid, non-expired Firebase ID Token.
    """
    if not settings.require_auth:
        # Local development / automated unit testing bypass ONLY when explicitly configured
        if settings.is_production:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="REQUIRE_AUTH tidak boleh dinonaktifkan di environment production.",
            )
        return AuthenticatedUser(uid="dev-local-user", email="dev@localhost", name="Local Dev")

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token autentikasi Firebase tidak ditemukan. Silakan login kembali.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    id_token = authorization[len("Bearer "):].strip()
    if not id_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token autentikasi Firebase kosong. Silakan login kembali.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not firebase_store.auth_ready:
        logger.error("Firebase Admin SDK belum siap untuk memverifikasi token.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Layanan autentikasi backend belum terkonfigurasi dengan Firebase Admin SDK.",
        )

    try:
        decoded = firebase_store.verify_id_token(id_token)
    except firebase_store.TokenExpiredError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sesi login Firebase telah kedaluwarsa. Silakan login kembali.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except firebase_store.TokenRevokedError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sesi akun Anda telah dicabut. Silakan login kembali.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except Exception as exc:
        logger.warning("Verifikasi Firebase ID Token gagal: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token autentikasi Firebase tidak valid atau telah dicabut.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    uid = decoded.get("uid")
    if not uid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token Firebase tidak memiliki UID pengguna yang valid.",
        )

    return AuthenticatedUser(
        uid=uid,
        email=decoded.get("email"),
        name=decoded.get("name"),
    )


CurrentUser = Depends(verify_firebase_user)
