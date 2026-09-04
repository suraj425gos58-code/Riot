"""
Riot Security Policy
====================

Centralized security primitives.

Responsibilities
----------------
- Identity validation
- Credential comparison
- Principal model
- RBAC role checks
- request ID validation
- security limits
- SSRF-aware host classification
"""

from __future__ import annotations

import hmac
import ipaddress
import re

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional


IDENTITY_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
)

REQUEST_ID_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$"
)

MAX_METADATA_KEYS = 128
MAX_METADATA_STRING = 4096


class SecurityPolicyError(ValueError):
    """Security policy validation failure."""


class SecurityRole(str, Enum):
    USER = "user"
    PLAYER = "player"
    OPERATOR = "operator"
    ADMIN = "admin"
    SERVICE = "service"
    SYSTEM = "system"


@dataclass(frozen=True, slots=True)
class SecurityLimits:
    max_request_body_bytes: int = 16 * 1024 * 1024
    max_prompt_chars: int = 2_000_000
    max_context_keys: int = 512
    max_websocket_message_bytes: int = 1 * 1024 * 1024
    max_player_id_length: int = 128
    max_game_id_length: int = 128


@dataclass(frozen=True, slots=True)
class Principal:
    subject: str
    roles: frozenset[SecurityRole] = frozenset()
    authenticated: bool = True
    metadata: Mapping[str, Any] = field(
        default_factory=dict
    )

    def has_role(
        self,
        role: SecurityRole,
    ) -> bool:
        return role in self.roles

    def has_any_role(
        self,
        *roles: SecurityRole,
    ) -> bool:
        return any(
            role in self.roles
            for role in roles
        )

    def is_privileged(self) -> bool:
        return self.has_any_role(
            SecurityRole.OPERATOR,
            SecurityRole.ADMIN,
            SecurityRole.SERVICE,
            SecurityRole.SYSTEM,
        )


@dataclass(frozen=True, slots=True)
class AuthorizationDecision:
    allowed: bool
    reason: str


def safe_identity(
    value: str,
    field: str = "identity",
) -> str:
    normalized = str(
        value or ""
    ).strip()

    if not normalized:
        raise SecurityPolicyError(
            f"{field} cannot be empty"
        )

    if not IDENTITY_RE.fullmatch(
        normalized
    ):
        raise SecurityPolicyError(
            f"invalid {field}"
        )

    return normalized


def safe_player_id(
    value: str,
) -> str:
    return safe_identity(
        value,
        "player_id",
    )


def safe_game_id(
    value: str,
) -> str:
    return safe_identity(
        value,
        "game_id",
    )


def safe_task_id(
    value: str,
) -> str:
    return safe_identity(
        value,
        "task_id",
    )


def safe_request_id(
    value: str,
) -> str:
    normalized = str(
        value or ""
    ).strip()

    if not REQUEST_ID_RE.fullmatch(
        normalized
    ):
        raise SecurityPolicyError(
            "invalid request_id"
        )

    return normalized


def constant_time_equals(
    candidate: Optional[str],
    expected: Optional[str],
) -> bool:

    if not candidate or not expected:
        return False

    return hmac.compare_digest(
        str(candidate),
        str(expected),
    )


def sanitize_metadata(
    metadata: Optional[Mapping[str, Any]],
) -> dict[str, Any]:

    if not metadata:
        return {}

    if len(metadata) > MAX_METADATA_KEYS:
        raise SecurityPolicyError(
            "metadata contains too many keys"
        )

    result: dict[str, Any] = {}

    for key, value in metadata.items():

        normalized_key = str(
            key
        ).strip()

        if not normalized_key:
            raise SecurityPolicyError(
                "metadata contains empty key"
            )

        normalized_value = _sanitize_value(
            value
        )

        result[
            normalized_key
        ] = normalized_value

    return result


def _sanitize_value(
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
        ) and len(value) > MAX_METADATA_STRING:
            return value[
                :MAX_METADATA_STRING
            ]

        return value

    if isinstance(value, Mapping):
        return sanitize_metadata(
            value
        )

    if isinstance(
        value,
        (list, tuple),
    ):
        return [
            _sanitize_value(
                item
            )
            for item in value[
                :1024
            ]
        ]

    return str(value)[
        :MAX_METADATA_STRING
    ]


def ip_is_private(
    host: str,
) -> bool:

    try:
        address = ipaddress.ip_address(
            host
        )
    except ValueError:
        return False

    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


__all__ = [
    "SecurityPolicyError",
    "SecurityRole",
    "SecurityLimits",
    "Principal",
    "AuthorizationDecision",
    "safe_identity",
    "safe_player_id",
    "safe_game_id",
    "safe_task_id",
    "safe_request_id",
    "constant_time_equals",
    "sanitize_metadata",
    "ip_is_private",
]
