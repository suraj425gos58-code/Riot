"""
Riot Authorization Engine
=========================

Central RBAC authorization boundary.

Never embed role checks directly inside business logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import FrozenSet

from .policy import (
    AuthorizationDecision,
    Principal,
    SecurityRole,
)


@dataclass(frozen=True, slots=True)
class PermissionSet:
    name: str
    allowed_roles: FrozenSet[SecurityRole]


ROLE_PERMISSIONS: dict[str, PermissionSet] = {
    "view_health": PermissionSet(
        "view_health",
        frozenset({
            SecurityRole.USER,
            SecurityRole.PLAYER,
            SecurityRole.OPERATOR,
            SecurityRole.ADMIN,
            SecurityRole.SERVICE,
            SecurityRole.SYSTEM,
        }),
    ),
    "execute_generation": PermissionSet(
        "execute_generation",
        frozenset({
            SecurityRole.PLAYER,
            SecurityRole.OPERATOR,
            SecurityRole.ADMIN,
            SecurityRole.SERVICE,
            SecurityRole.SYSTEM,
        }),
    ),
    "export_build": PermissionSet(
        "export_build",
        frozenset({
            SecurityRole.PLAYER,
            SecurityRole.OPERATOR,
            SecurityRole.ADMIN,
            SecurityRole.SERVICE,
            SecurityRole.SYSTEM,
        }),
    ),
    "self_evolve": PermissionSet(
        "self_evolve",
        frozenset({
            SecurityRole.ADMIN,
            SecurityRole.SYSTEM,
        }),
    ),
    "live_edit": PermissionSet(
        "live_edit",
        frozenset({
            SecurityRole.OPERATOR,
            SecurityRole.ADMIN,
            SecurityRole.SERVICE,
            SecurityRole.SYSTEM,
        }),
    ),
    "multiplayer": PermissionSet(
        "multiplayer",
        frozenset({
            SecurityRole.PLAYER,
            SecurityRole.OPERATOR,
            SecurityRole.ADMIN,
            SecurityRole.SERVICE,
            SecurityRole.SYSTEM,
        }),
    ),
    "economy": PermissionSet(
        "economy",
        frozenset({
            SecurityRole.PLAYER,
            SecurityRole.OPERATOR,
            SecurityRole.ADMIN,
            SecurityRole.SERVICE,
            SecurityRole.SYSTEM,
        }),
    ),
}


class AuthorizationEngine:
    """
    Stateless RBAC evaluator.

    Keep this class pure so it can later be backed by an external policy
    service without changing application code.
    """

    def check(
        self,
        principal: Principal,
        permission: str,
    ) -> AuthorizationDecision:

        if not principal.authenticated:
            return AuthorizationDecision(
                allowed=False,
                reason="principal is not authenticated",
            )

        policy = ROLE_PERMISSIONS.get(
            permission
        )

        if policy is None:
            return AuthorizationDecision(
                allowed=False,
                reason="unknown permission",
            )

        if principal.roles.intersection(
            policy.allowed_roles
        ):
            return AuthorizationDecision(
                allowed=True,
                reason="role authorized",
            )

        return AuthorizationDecision(
            allowed=False,
            reason="insufficient privileges",
        )

    def require(
        self,
        principal: Principal,
        permission: str,
    ) -> None:

        decision = self.check(
            principal,
            permission,
        )

        if not decision.allowed:
            raise PermissionError(
                f"permission denied: "
                f"{permission} "
                f"({decision.reason})"
            )


AUTHORIZER = AuthorizationEngine()


__all__ = [
    "PermissionSet",
    "AuthorizationEngine",
    "AUTHORIZER",
    "ROLE_PERMISSIONS",
]
