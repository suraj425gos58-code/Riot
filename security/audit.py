"""
Riot Security Audit
===================

Structured security event pipeline.

Never put secrets/tokens/passwords into metadata.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional


logger = logging.getLogger(
    "Riot.SecurityAudit"
)


SENSITIVE_KEY_WORDS = {
    "password",
    "passwd",
    "token",
    "secret",
    "api_key",
    "apikey",
    "authorization",
    "private_key",
    "master_pin",
}


@dataclass(frozen=True, slots=True)
class AuditEvent:
    event_id: str
    action: str
    subject: str
    outcome: str
    timestamp: float = field(
        default_factory=time.time
    )
    request_id: Optional[str] = None
    source_ip: Optional[str] = None
    resource: Optional[str] = None
    metadata: Mapping[str, Any] = field(
        default_factory=dict
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "action": self.action,
            "subject": self.subject,
            "outcome": self.outcome,
            "timestamp": self.timestamp,
            "request_id": self.request_id,
            "source_ip": self.source_ip,
            "resource": self.resource,
            "metadata": _sanitize_metadata(
                self.metadata
            ),
        }


def _sanitize_metadata(
    metadata: Mapping[str, Any],
) -> dict[str, Any]:

    result: dict[str, Any] = {}

    for key, value in metadata.items():

        normalized = str(
            key
        ).lower()

        if any(
            sensitive in normalized
            for sensitive
            in SENSITIVE_KEY_WORDS
        ):
            result[
                str(key)
            ] = "[REDACTED]"
            continue

        result[
            str(key)
        ] = _safe_value(
            value
        )

    return result


def _safe_value(
    value: Any,
) -> Any:

    if value is None:
        return None

    if isinstance(
        value,
        (str, int, float, bool),
    ):
        if isinstance(
            value,
            str,
        ):
            if len(value) > 4096:
                return value[:4096]
        return value

    if isinstance(value, Mapping):
        return _sanitize_metadata(
            value
        )

    if isinstance(
        value,
        (list, tuple),
    ):
        return [
            _safe_value(
                item
            )
            for item in value[
                :256
            ]
        ]

    return str(value)[:4096]


class AuditLogger:
    """
    Thread-safe structured security audit logger.

    Supports:
    - standard logger
    - optional JSONL persistence
    - event fingerprints
    """

    def __init__(
        self,
        *,
        file_path: str | Path | None = None,
    ) -> None:

        self.file_path = (
            Path(file_path)
            .expanduser()
            .resolve()
            if file_path
            else None
        )

        self._lock = threading.RLock()

        if self.file_path:
            self.file_path.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

    def emit(
        self,
        *,
        action: str,
        subject: str,
        outcome: str,
        request_id: Optional[str] = None,
        source_ip: Optional[str] = None,
        resource: Optional[str] = None,
        metadata: Optional[
            Mapping[str, Any]
        ] = None,
    ) -> AuditEvent:

        event = AuditEvent(
            event_id=uuid.uuid4().hex,
            action=action,
            subject=subject,
            outcome=outcome,
            request_id=request_id,
            source_ip=source_ip,
            resource=resource,
            metadata=metadata or {},
        )

        payload = event.to_dict()

        payload[
            "fingerprint"
        ] = hashlib.sha256(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode(
                "utf-8"
            )
        ).hexdigest()

        with self._lock:

            logger.info(
                "security_audit %s",
                json.dumps(
                    payload,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )

            if self.file_path:
                with self.file_path.open(
                    "a",
                    encoding="utf-8",
                ) as handle:
                    handle.write(
                        json.dumps(
                            payload,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    )
                    handle.write("\n")

        return event


__all__ = [
    "AuditEvent",
    "AuditLogger",
]
