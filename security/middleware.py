"""
Riot HTTP Security Middleware
=============================

Features
--------
- request ID propagation
- security headers
- body-size guard
- trusted credential extraction
- request timing
- audit integration
"""

from __future__ import annotations

import time
import uuid

from typing import Optional

from fastapi import HTTPException, Request
from starlette.middleware.base import (
    BaseHTTPMiddleware,
)
from starlette.responses import Response

from .audit import AuditLogger
from .policy import (
    SecurityLimits,
    constant_time_equals,
)


class SecurityMiddleware(
    BaseHTTPMiddleware
):
    def __init__(
        self,
        app,
        *,
        audit: Optional[
            AuditLogger
        ] = None,
        limits: Optional[
            SecurityLimits
        ] = None,
        master_secret: Optional[str] = None,
    ) -> None:

        super().__init__(app)

        self.audit = audit
        self.limits = (
            limits
            or SecurityLimits()
        )
        self.master_secret = master_secret

    async def dispatch(
        self,
        request: Request,
        call_next,
    ) -> Response:

        started = time.perf_counter()

        request_id = (
            request.headers.get(
                "X-Request-ID"
            )
            or f"req_{uuid.uuid4().hex}"
        )

        request.state.request_id = (
            request_id
        )

        content_length = request.headers.get(
            "content-length"
        )

        if content_length:
            try:
                body_size = int(
                    content_length
                )
            except ValueError:
                body_size = 0

            if body_size > self.limits.max_request_body_bytes:
                response = Response(
                    content="request body too large",
                    status_code=413,
                )

                return self._secure_response(
                    response,
                    request_id,
                )

        try:
            response = await call_next(
                request
            )

        except HTTPException:
            raise

        except Exception as exc:

            if self.audit:
                self.audit.emit(
                    action="http.request",
                    subject="anonymous",
                    outcome="error",
                    request_id=request_id,
                    source_ip=(
                        request.client.host
                        if request.client
                        else None
                    ),
                    resource=request.url.path,
                    metadata={
                        "method": request.method,
                        "error_type": (
                            type(exc).__name__
                        ),
                    },
                )

            raise

        response = self._secure_response(
            response,
            request_id,
        )

        elapsed_ms = (
            time.perf_counter()
            - started
        ) * 1000.0

        response.headers[
            "X-Response-Time-ms"
        ] = f"{elapsed_ms:.3f}"

        return response

    @staticmethod
    def _secure_response(
        response: Response,
        request_id: str,
    ) -> Response:

        response.headers[
            "X-Request-ID"
        ] = request_id

        response.headers[
            "X-Content-Type-Options"
        ] = "nosniff"

        response.headers[
            "X-Frame-Options"
        ] = "DENY"

        response.headers[
            "Referrer-Policy"
        ] = "strict-origin-when-cross-origin"

        response.headers[
            "Permissions-Policy"
        ] = (
            "camera=(), "
            "microphone=(), "
            "geolocation=()"
        )

        response.headers[
            "Cache-Control"
        ] = "no-store"

        return response

    def validate_master_credential(
        self,
        candidate: Optional[str],
    ) -> bool:

        return constant_time_equals(
            candidate,
            self.master_secret,
        )


__all__ = [
    "SecurityMiddleware",
]
