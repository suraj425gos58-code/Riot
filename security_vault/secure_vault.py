"""
Riot Secure Secret Vault
========================

Production-grade encrypted secret storage foundation.

Features
--------
- Fernet authenticated encryption
- MultiFernet key rotation
- Atomic file persistence
- File permissions hardening
- Secret metadata/fingerprints
- Strict secret-name validation
- Thread-safe operations
- Versioned envelope
- Fail-closed configuration
- No plaintext secret logging

Environment
-----------
RIOT_VAULT_KEYRING=
    comma-separated Fernet keys, newest key first.

NEXUS_MASTER_KEY=
    legacy compatibility fallback.

RIOT_ALLOW_EPHEMERAL_VAULT_KEY=
    explicit development-only ephemeral key switch.
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import tempfile
import threading
import time

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from cryptography.fernet import Fernet, InvalidToken, MultiFernet


_SECRET_NAME_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
)

MAX_SECRET_NAME_LENGTH = 128
MAX_SECRET_VALUE_BYTES = 2 * 1024 * 1024


class VaultError(RuntimeError):
    """Base exception for vault failures."""


class VaultConfigurationError(VaultError):
    """Raised when vault configuration is unsafe or incomplete."""


class VaultIntegrityError(VaultError):
    """Raised when encrypted vault data cannot be authenticated."""


class SecretNotFoundError(VaultError):
    """Raised when requested secret does not exist."""


class SecretValidationError(VaultError):
    """Raised when a secret name/value is invalid."""


@dataclass(frozen=True, slots=True)
class SecretMetadata:
    name: str
    fingerprint: str
    version: int
    updated_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "fingerprint": self.fingerprint,
            "version": self.version,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class VaultSnapshot:
    format_version: int
    key_count: int
    keyring_size: int
    created_at: float
    updated_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_version": self.format_version,
            "key_count": self.key_count,
            "keyring_size": self.keyring_size,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class SecureSecretVault:
    """
    Thread-safe encrypted secret vault.

    Important
    ---------
    The vault never writes plaintext secrets to disk.

    The encrypted envelope itself contains secret names + values, and the
    entire payload is authenticated by Fernet/MultiFernet.
    """

    FORMAT_VERSION = 2

    def __init__(
        self,
        path: str | Path = "security_vault/secure_keys.json",
        *,
        keyring: Optional[Iterable[str | bytes]] = None,
        create_parent: bool = True,
        strict_permissions: bool = True,
    ) -> None:
        self.path = Path(path).expanduser().resolve()

        if create_parent:
            self.path.parent.mkdir(parents=True, exist_ok=True)

        self._lock = threading.RLock()
        self._strict_permissions = strict_permissions

        self._keyring = self._resolve_keyring(keyring)
        self._cipher = MultiFernet(
            [Fernet(key) for key in self._keyring]
        )

        self._created_at = time.time()
        self._updated_at = self._created_at

        self._secrets: dict[str, str] = self._load_store()

        self._harden_path_permissions()

    # ============================================================
    # Configuration
    # ============================================================

    @classmethod
    def from_environment(
        cls,
        path: str | Path = "security_vault/secure_keys.json",
    ) -> "SecureSecretVault":
        return cls(path)

    # ============================================================
    # Validation
    # ============================================================

    @staticmethod
    def validate_secret_name(name: str) -> str:
        value = str(name or "").strip()

        if not value:
            raise SecretValidationError(
                "secret name cannot be empty"
            )

        if len(value) > MAX_SECRET_NAME_LENGTH:
            raise SecretValidationError(
                "secret name exceeds maximum length"
            )

        if not _SECRET_NAME_RE.fullmatch(value):
            raise SecretValidationError(
                "invalid secret name"
            )

        return value

    @staticmethod
    def validate_secret_value(value: str) -> str:
        if not isinstance(value, str):
            raise SecretValidationError(
                "secret value must be a string"
            )

        if not value:
            raise SecretValidationError(
                "secret value cannot be empty"
            )

        size = len(value.encode("utf-8"))

        if size > MAX_SECRET_VALUE_BYTES:
            raise SecretValidationError(
                "secret value exceeds maximum allowed size"
            )

        return value

    @staticmethod
    def validate_key(value: str | bytes) -> bytes:
        try:
            raw = (
                value.encode("ascii")
                if isinstance(value, str)
                else bytes(value)
            )
        except Exception as exc:
            raise VaultConfigurationError(
                "invalid vault key encoding"
            ) from exc

        try:
            Fernet(raw)
        except Exception as exc:
            raise VaultConfigurationError(
                "invalid Fernet key"
            ) from exc

        return raw

    # ============================================================
    # Keyring
    # ============================================================

    def _resolve_keyring(
        self,
        explicit_keyring: Optional[Iterable[str | bytes]],
    ) -> list[bytes]:

        if explicit_keyring is not None:
            keys = [
                self.validate_key(key)
                for key in explicit_keyring
            ]

            if not keys:
                raise VaultConfigurationError(
                    "explicit vault keyring is empty"
                )

            return keys

        raw_ring = os.getenv(
            "RIOT_VAULT_KEYRING",
            "",
        ).strip()

        if raw_ring:
            keys = [
                self.validate_key(item.strip())
                for item in raw_ring.split(",")
                if item.strip()
            ]

            if not keys:
                raise VaultConfigurationError(
                    "RIOT_VAULT_KEYRING contains no valid keys"
                )

            return keys

        legacy_key = os.getenv(
            "NEXUS_MASTER_KEY",
            "",
        ).strip()

        if legacy_key:
            return [
                self.validate_key(legacy_key)
            ]

        allow_ephemeral = os.getenv(
            "RIOT_ALLOW_EPHEMERAL_VAULT_KEY",
            "false",
        ).strip().lower()

        if allow_ephemeral in {
            "1",
            "true",
            "yes",
            "on",
        }:
            return [
                Fernet.generate_key()
            ]

        raise VaultConfigurationError(
            "No vault key configured. "
            "Set RIOT_VAULT_KEYRING or NEXUS_MASTER_KEY. "
            "For development only, explicitly enable "
            "RIOT_ALLOW_EPHEMERAL_VAULT_KEY=true."
        )

    # ============================================================
    # Persistence
    # ============================================================

    def _load_store(self) -> dict[str, str]:
        if not self.path.exists():
            return {}

        try:
            ciphertext = self.path.read_bytes()

            if not ciphertext:
                return {}

            plaintext = self._cipher.decrypt(ciphertext)

            envelope = json.loads(
                plaintext.decode("utf-8")
            )

            version = envelope.get(
                "format_version"
            )

            if version != self.FORMAT_VERSION:
                raise VaultIntegrityError(
                    f"unsupported vault format version: {version!r}"
                )

            secrets_payload = envelope.get(
                "secrets"
            )

            if not isinstance(secrets_payload, dict):
                raise VaultIntegrityError(
                    "vault secrets payload must be an object"
                )

            validated: dict[str, str] = {}

            for raw_name, raw_value in secrets_payload.items():
                name = self.validate_secret_name(
                    str(raw_name)
                )

                value = self.validate_secret_value(
                    str(raw_value)
                )

                validated[name] = value

            self._created_at = float(
                envelope.get(
                    "created_at",
                    time.time(),
                )
            )

            self._updated_at = float(
                envelope.get(
                    "updated_at",
                    time.time(),
                )
            )

            return validated

        except InvalidToken as exc:
            raise VaultIntegrityError(
                "vault authentication failed"
            ) from exc

        except json.JSONDecodeError as exc:
            raise VaultIntegrityError(
                "vault payload is not valid JSON"
            ) from exc

        except OSError as exc:
            raise VaultIntegrityError(
                "vault could not be read"
            ) from exc

    def _persist(self) -> None:
        self.path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        now = time.time()

        envelope = {
            "format_version": self.FORMAT_VERSION,
            "created_at": self._created_at,
            "updated_at": now,
            "secrets": dict(self._secrets),
        }

        plaintext = json.dumps(
            envelope,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")

        ciphertext = self._cipher.encrypt(
            plaintext
        )

        fd, tmp_path = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=str(self.path.parent),
        )

        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(ciphertext)
                handle.flush()
                os.fsync(handle.fileno())

            if self._strict_permissions:
                try:
                    os.chmod(
                        tmp_path,
                        0o600,
                    )
                except OSError:
                    pass

            os.replace(
                tmp_path,
                self.path,
            )

            self._updated_at = now

            self._harden_path_permissions()

        finally:
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass

    def _harden_path_permissions(self) -> None:
        if not self.path.exists():
            return

        if not self._strict_permissions:
            return

        try:
            os.chmod(
                self.path,
                0o600,
            )
        except OSError:
            pass

    # ============================================================
    # Secret Operations
    # ============================================================

    def set_secret(
        self,
        name: str,
        value: str,
    ) -> SecretMetadata:

        name = self.validate_secret_name(name)
        value = self.validate_secret_value(value)

        with self._lock:
            self._secrets[name] = value
            self._persist()

            return self._metadata_for(
                name
            )

    def get_secret(
        self,
        name: str,
    ) -> str:

        name = self.validate_secret_name(name)

        with self._lock:
            value = self._secrets.get(name)

            if value is None:
                raise SecretNotFoundError(
                    f"secret not found: {name}"
                )

            return value

    def get_secret_or_none(
        self,
        name: str,
    ) -> Optional[str]:

        try:
            return self.get_secret(name)
        except SecretNotFoundError:
            return None

    def delete_secret(
        self,
        name: str,
    ) -> bool:

        name = self.validate_secret_name(name)

        with self._lock:
            if name not in self._secrets:
                return False

            del self._secrets[name]
            self._persist()

            return True

    def contains(
        self,
        name: str,
    ) -> bool:

        name = self.validate_secret_name(name)

        with self._lock:
            return name in self._secrets

    def count(self) -> int:
        with self._lock:
            return len(self._secrets)

    # ============================================================
    # Metadata
    # ============================================================

    def _metadata_for(
        self,
        name: str,
    ) -> SecretMetadata:

        value = self._secrets[name]

        return SecretMetadata(
            name=name,
            fingerprint=self.fingerprint(
                value
            ),
            version=self.FORMAT_VERSION,
            updated_at=self._updated_at,
        )

    def metadata(
        self,
        name: str,
    ) -> SecretMetadata:

        name = self.validate_secret_name(name)

        with self._lock:
            if name not in self._secrets:
                raise SecretNotFoundError(
                    f"secret not found: {name}"
                )

            return self._metadata_for(
                name
            )

    def list_metadata(
        self,
    ) -> list[SecretMetadata]:

        with self._lock:
            return [
                self._metadata_for(name)
                for name in sorted(
                    self._secrets
                )
            ]

    @staticmethod
    def fingerprint(
        value: str,
    ) -> str:

        return sha256(
            value.encode("utf-8")
        ).hexdigest()[:24]

    def snapshot(self) -> VaultSnapshot:
        with self._lock:
            return VaultSnapshot(
                format_version=self.FORMAT_VERSION,
                key_count=len(
                    self._secrets
                ),
                keyring_size=len(
                    self._keyring
                ),
                created_at=self._created_at,
                updated_at=self._updated_at,
            )

    # ============================================================
    # Rotation
    # ============================================================

    def rotate(
        self,
        new_key: Optional[str | bytes] = None,
        *,
        retain_previous_keys: bool = True,
    ) -> str:
        """
        Rotate encryption key.

        The newest key becomes primary.
        Existing secrets are decrypted/re-encrypted through the new cipher.
        """

        with self._lock:
            primary = (
                self.validate_key(new_key)
                if new_key is not None
                else Fernet.generate_key()
            )

            old_keyring = list(
                self._keyring
            )

            if retain_previous_keys:
                new_keyring = [
                    primary,
                    *old_keyring,
                ]
            else:
                new_keyring = [
                    primary,
                ]

            old_cipher = self._cipher
            old_ring = self._keyring

            self._keyring = new_keyring

            self._cipher = MultiFernet(
                [
                    Fernet(key)
                    for key in new_keyring
                ]
            )

            try:
                self._persist()
            except Exception:
                self._cipher = old_cipher
                self._keyring = old_ring
                raise

            return primary.decode("ascii")

    # ============================================================
    # Destructive operation
    # ============================================================

    def obliterate(
        self,
        *,
        persist: bool = True,
    ) -> None:

        with self._lock:
            self._secrets.clear()

            if persist:
                self._persist()


__all__ = [
    "SecureSecretVault",
    "SecretMetadata",
    "VaultSnapshot",
    "VaultError",
    "VaultConfigurationError",
    "VaultIntegrityError",
    "SecretNotFoundError",
    "SecretValidationError",
]
