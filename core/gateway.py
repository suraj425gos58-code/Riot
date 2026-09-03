"""
Riot Dynamic API Gateway
========================

Provider-agnostic routing and execution boundary for Riot/God Node.

Design rules
------------
* Provider identity is data, not code.
* Model identity is data, not code.
* Authentication is configuration-driven.
* Payload/response mapping is configuration-driven.
* ProviderRegistry is the canonical provider source of truth.
* ProviderExecutor is the single retry/circuit-breaker authority.
* Gateway performs provider failover, not duplicate per-provider retries.
* Runtime metrics are kept separate from provider configuration.
* Shared HTTP connection pool is reused.
* Model discovery uses the existing TTL cache.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import math
import os
import random
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

import aiohttp
from fastapi import APIRouter

# ---------------------------------------------------------------------------
# Compatibility bootstrap.
# provider_sdk.py currently imports its sibling modules as top-level modules.
# Map those names to the package modules before importing provider_sdk.
# ---------------------------------------------------------------------------
from god_brain import connection_pool as _connection_pool_module
from god_brain import circuit_breaker as _circuit_breaker_module

sys.modules.setdefault("connection_pool", _connection_pool_module)
sys.modules.setdefault("circuit_breaker", _circuit_breaker_module)

from god_brain.connection_pool import HTTP_CLIENT, MODEL_CACHE
from god_brain.circuit_breaker import (
    CIRCUIT_REGISTRY,
    PROVIDER_EXECUTOR,
)
from god_brain.provider_sdk import (
    AuthenticationConfig,
    AuthenticationType,
    PayloadMapping,
    ProviderAdapter,
    ProviderConfiguration,
    ProviderProtocol,
    ProviderResponse,
    PROVIDER_REGISTRY,
    PromptRequest,
    ResponseMapping,
)


# ============================================================================
# LOGGING / CONSTANTS
# ============================================================================

logger = logging.getLogger("Riot.DynamicGateway")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        )
    )
    logger.addHandler(handler)

logger.setLevel(
    os.getenv("RIOT_GATEWAY_LOG_LEVEL", "INFO").upper()
)

DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_MAX_FAILOVERS = 3
DEFAULT_GLOBAL_CONCURRENCY = 64
DEFAULT_PROVIDER_CONCURRENCY = 8
MAX_PROMPT_LENGTH = 2_000_000
MAX_RESPONSE_BYTES = 32 * 1024 * 1024

RETRYABLE_HTTP_CODES = frozenset(
    {408, 425, 429, 500, 502, 503, 504}
)

AUTH_FAILURE_CODES = frozenset({401, 403})


# ============================================================================
# ERRORS
# ============================================================================

class GatewayError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        provider: Optional[str] = None,
        status: Optional[int] = None,
        retryable: bool = False,
        category: str = "gateway",
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.status = status
        self.retryable = retryable
        self.category = category


class GatewayValidationError(GatewayError):
    pass


class ProviderUnavailableError(GatewayError):
    pass


class ProviderExecutionError(GatewayError):
    pass


class DynamicConfigurationError(GatewayError):
    pass


# ============================================================================
# ENUMS / CONTRACTS
# ============================================================================

class RoutingMode(str, Enum):
    BALANCED = "balanced"
    LOW_LATENCY = "low_latency"
    LOW_COST = "low_cost"
    HIGH_RELIABILITY = "high_reliability"
    HIGH_CAPACITY = "high_capacity"


@dataclass(slots=True)
class GatewayRequest:
    prompt: str
    system_prompt: Optional[str] = None
    model: Optional[str] = None
    temperature: float = 0.7
    max_tokens: Optional[int] = None
    service: str = "default"
    required_capabilities: frozenset[str] = frozenset()
    metadata: Dict[str, Any] = field(default_factory=dict)
    timeout_seconds: Optional[float] = None
    max_failovers: int = DEFAULT_MAX_FAILOVERS
    routing_mode: RoutingMode = RoutingMode.BALANCED
    preferred_provider: Optional[str] = None
    excluded_providers: frozenset[str] = frozenset()
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass(slots=True)
class GatewayResponse:
    success: bool
    output: str
    provider: str
    model: Optional[str]
    request_id: str
    latency_ms: float
    attempts: int
    status_code: Optional[int] = None
    raw_response: Any = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ProviderRuntime:
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    in_flight: int = 0
    last_latency_ms: float = 0.0
    ema_latency_ms: float = 0.0
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    last_success_at: float = 0.0
    last_failure_at: float = 0.0
    last_error: Optional[str] = None
    cooldown_until: float = 0.0
    rate_remaining: Optional[int] = None
    rate_reset_at: float = 0.0
    bytes_sent: int = 0
    bytes_received: int = 0

    estimated_tokens_in: int = 0
    estimated_tokens_out: int = 0
    estimated_cost: float = 0.0

    last_selection_score: float = 0.0
    last_selected_at: float = 0.0

    semaphore: Optional[asyncio.Semaphore] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


# ============================================================================
# GENERIC HELPERS
# ============================================================================

def _safe_float(value: Any, default: float) -> float:
    try:
        value = float(value)
        return value if math.isfinite(value) else default
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _fingerprint(value: Any) -> str:
    if value is None:
        return ""
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]


def _public_endpoint(url: str) -> str:
    if not url:
        return ""
    return str(url).split("?", 1)[0].split("#", 1)[0]


def _path_parts(path: Sequence[str] | str) -> List[str]:
    if isinstance(path, str):
        return [part for part in path.strip(".").split(".") if part]
    return [str(part) for part in path]


def _extract_path(
    payload: Any,
    path: Sequence[str] | str,
    default: Any = None,
) -> Any:
    current = payload
    for part in _path_parts(path):
        if isinstance(current, Mapping):
            if part not in current:
                return default
            current = current[part]
        elif isinstance(current, list):
            index = _safe_int(part)
            if index is None or index < 0 or index >= len(current):
                return default
            current = current[index]
        else:
            return default
    return current


def _assign_path(
    target: Dict[str, Any],
    path: str,
    value: Any,
) -> None:
    parts = _path_parts(path)
    if not parts:
        return
    current = target
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    current[parts[-1]] = value


def _normalize_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()

    if isinstance(value, list):
        parts: List[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, Mapping):
                for key in ("text", "content", "value"):
                    if key in item:
                        text = _normalize_text(item[key])
                        if text:
                            parts.append(text)
                        break
        return "".join(parts).strip()

    if isinstance(value, Mapping):
        for key in ("text", "content", "output", "response", "value"):
            if key in value:
                text = _normalize_text(value[key])
                if text:
                    return text

    return str(value).strip()


def _estimate_tokens(text: Any) -> int:
    """Conservative local token estimate used only for telemetry/cost hints."""
    if text is None:
        return 0
    raw = str(text)
    if not raw:
        return 0
    return max(1, math.ceil(len(raw) / 4))


def _resolve_template(value: Any, context: Mapping[str, Any]) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _resolve_template(item, context)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_resolve_template(item, context) for item in value]
    if not isinstance(value, str):
        return value

    if not value.startswith("${") or not value.endswith("}"):
        return value

    expression = value[2:-1].strip()
    result = _extract_path(context, expression)
    return result if result is not None else value


def _enum_value(value: Any, enum_type: type[Enum], default: Any) -> Any:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(str(value).lower())
    except (TypeError, ValueError):
        return default


# ============================================================================
# CONFIGURATION -> SDK CONTRACT
# ============================================================================

def _auth_config(
    value: Any,
) -> AuthenticationConfig:
    if isinstance(value, AuthenticationConfig):
        return value

    data = dict(value or {})
    auth_type = _enum_value(
        data.get("type", AuthenticationType.NONE.value),
        AuthenticationType,
        AuthenticationType.NONE,
    )

    return AuthenticationConfig(
        type=auth_type,
        token=data.get("token"),
        header_name=str(data.get("header_name", "Authorization")),
        query_name=str(data.get("query_name", "key")),
        prefix=str(data.get("prefix", "Bearer")),
        custom_headers={
            str(k): str(v)
            for k, v in dict(data.get("custom_headers", {})).items()
        },
    )


def _payload_config(value: Any) -> PayloadMapping:
    if isinstance(value, PayloadMapping):
        return value

    data = dict(value or {})
    return PayloadMapping(
        prompt_field=str(data.get("prompt_field", "prompt")),
        system_field=(
            str(data["system_field"])
            if data.get("system_field") is not None
            else None
        ),
        temperature_field=(
            str(data["temperature_field"])
            if data.get("temperature_field") is not None
            else None
        ),
        max_tokens_field=(
            str(data["max_tokens_field"])
            if data.get("max_tokens_field") is not None
            else None
        ),
        metadata_field=(
            str(data["metadata_field"])
            if data.get("metadata_field") is not None
            else None
        ),
        fixed_fields=dict(data.get("fixed_fields", {})),
    )


def _response_config(value: Any) -> ResponseMapping:
    if isinstance(value, ResponseMapping):
        return value

    data = dict(value or {})
    path = data.get("output_path", ("output",))
    if isinstance(path, str):
        path = tuple(_path_parts(path))
    else:
        path = tuple(str(item) for item in path)

    return ResponseMapping(output_path=path)


def _protocol(value: Any) -> ProviderProtocol:
    return _enum_value(
        value,
        ProviderProtocol,
        ProviderProtocol.REST,
    )


def _configuration_from_mapping(
    value: Mapping[str, Any],
) -> ProviderConfiguration:
    name = str(value.get("name", "")).strip()
    endpoint = str(value.get("endpoint", "")).strip()

    if not name:
        raise DynamicConfigurationError(
            "Provider configuration requires a non-empty name."
        )
    if not endpoint:
        raise DynamicConfigurationError(
            "Provider configuration requires a non-empty endpoint.",
            provider=name,
        )

    timeout = max(
        1,
        _safe_int(
            value.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS),
            DEFAULT_TIMEOUT_SECONDS,
        )
        or DEFAULT_TIMEOUT_SECONDS,
    )

    metadata = dict(value.get("metadata", {}))

    # Keep operational metadata in one canonical object. This avoids creating
    # a second provider schema inside the gateway.
    for field_name in (
        "services",
        "capabilities",
        "models",
        "default_model",
        "priority",
        "weight",
        "max_concurrency",
        "rate_limit_per_minute",
        "cost_per_1k_tokens",
        "model_discovery",
        "response_fallback_paths",
        "response_request_id_path",
        "response_usage_path",
        "rate_limit_headers",
        "http_method",
        "payload_template",
        "idempotency_header",
    ):
        if field_name in value and field_name not in metadata:
            metadata[field_name] = value[field_name]

    return ProviderConfiguration(
        name=name,
        endpoint=endpoint,
        protocol=_protocol(value.get("protocol")),
        enabled=bool(value.get("enabled", True)),
        timeout_seconds=timeout,
        authentication=_auth_config(
            value.get("authentication", {})
        ),
        payload=_payload_config(
            value.get("payload", {})
        ),
        response=_response_config(
            value.get("response", {})
        ),
        default_headers={
            str(k): str(v)
            for k, v in dict(
                value.get("default_headers", value.get("headers", {}))
            ).items()
        },
        metadata=metadata,
    )


# ============================================================================
# UNIVERSAL CONFIGURATION-DRIVEN ADAPTER
# ============================================================================

class DynamicProviderAdapter(ProviderAdapter):
    """
    Generic HTTP adapter used when no specialized adapter is registered.

    Provider-specific behavior comes entirely from ProviderConfiguration.
    """

    def build_headers(self) -> Dict[str, str]:
        configuration = self.configuration
        headers: Dict[str, str] = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        headers.update(configuration.default_headers)

        auth = configuration.authentication

        if auth.type == AuthenticationType.BEARER and auth.token:
            headers[auth.header_name] = (
                f"{auth.prefix} {auth.token}"
            )

        elif (
            auth.type == AuthenticationType.API_KEY_HEADER
            and auth.token
        ):
            headers[auth.header_name] = auth.token

        if auth.type == AuthenticationType.CUSTOM:
            headers.update(auth.custom_headers)

        metadata = configuration.metadata or {}
        idempotency_header = metadata.get("idempotency_header")
        if idempotency_header:
            headers[str(idempotency_header)] = str(
                self._current_request_id or ""
            )

        return headers

    @property
    def _current_request_id(self) -> Optional[str]:
        # Set transiently by invoke(). This keeps the SDK contract unchanged.
        return getattr(self, "__request_id", None)

    @_current_request_id.setter
    def _current_request_id(self, value: str) -> None:
        setattr(self, "__request_id", value)

    def build_query(self) -> Dict[str, str]:
        auth = self.configuration.authentication
        if (
            auth.type == AuthenticationType.API_KEY_QUERY
            and auth.token
        ):
            return {auth.query_name: auth.token}
        return {}

    def build_payload(
        self,
        request: PromptRequest,
    ) -> Dict[str, Any]:
        configuration = self.configuration
        metadata = configuration.metadata or {}

        context = {
            "prompt": request.prompt,
            "system_prompt": request.system_prompt,
            "model": request.metadata.get("model"),
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
            "metadata": request.metadata,
        }

        template = metadata.get("payload_template")
        if isinstance(template, Mapping):
            payload = _resolve_template(template, context)
            if isinstance(payload, dict):
                return payload

        mapping = configuration.payload
        payload: Dict[str, Any] = {}

        if mapping.fixed_fields:
            payload.update(
                _resolve_template(
                    mapping.fixed_fields,
                    context,
                )
            )

        _assign_path(
            payload,
            mapping.prompt_field,
            request.prompt,
        )

        if (
            mapping.system_field
            and request.system_prompt is not None
        ):
            _assign_path(
                payload,
                mapping.system_field,
                request.system_prompt,
            )

        if mapping.temperature_field:
            _assign_path(
                payload,
                mapping.temperature_field,
                request.temperature,
            )

        if (
            mapping.max_tokens_field
            and request.max_tokens is not None
        ):
            _assign_path(
                payload,
                mapping.max_tokens_field,
                request.max_tokens,
            )

        if mapping.metadata_field and request.metadata:
            _assign_path(
                payload,
                mapping.metadata_field,
                request.metadata,
            )

        model_field = metadata.get("model_field")
        if model_field and request.metadata.get("model") is not None:
            _assign_path(
                payload,
                str(model_field),
                request.metadata["model"],
            )

        return payload

    async def invoke(
        self,
        request: PromptRequest,
    ) -> ProviderResponse:
        configuration = self.configuration
        self._current_request_id = str(
            request.metadata.get("gateway_request_id", "")
        )

        method = str(
            (configuration.metadata or {}).get(
                "http_method",
                "POST",
            )
        ).upper()

        url = configuration.endpoint
        timeout_seconds = max(
            1,
            _safe_int(
                request.metadata.get(
                    "timeout_seconds",
                    configuration.timeout_seconds,
                ),
                configuration.timeout_seconds,
            )
            or configuration.timeout_seconds,
        )

        payload = self.build_payload(request)
        headers = self.build_headers()
        query = self.build_query()

        session = HTTP_CLIENT.session()
        started = time.perf_counter()

        kwargs: Dict[str, Any] = {
            "headers": headers,
            "params": query,
            "timeout": aiohttp.ClientTimeout(
                total=timeout_seconds
            ),
        }

        if method not in {"GET", "HEAD", "DELETE"}:
            kwargs["json"] = payload

        request_bytes = len(
            json.dumps(
                payload,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        )

        try:
            async with session.request(
                method,
                url,
                **kwargs,
            ) as response:

                body = await response.read()
                elapsed_ms = (
                    time.perf_counter() - started
                ) * 1000.0

                if len(body) > MAX_RESPONSE_BYTES:
                    raise ProviderExecutionError(
                        "Provider response exceeded gateway safety limit.",
                        provider=configuration.name,
                        status=response.status,
                        retryable=False,
                        category="response_limit",
                    )

                if response.status >= 400:
                    detail = body.decode(
                        "utf-8",
                        errors="replace",
                    )[:4000]

                    raise ProviderExecutionError(
                        (
                            f"Dynamic provider request failed: "
                            f"HTTP {response.status}: {detail}"
                        ),
                        provider=configuration.name,
                        status=response.status,
                        retryable=(
                            response.status
                            in RETRYABLE_HTTP_CODES
                        ),
                        category="http",
                    )

                content_type = response.headers.get(
                    "Content-Type",
                    "",
                ).lower()

                if "json" in content_type:
                    try:
                        raw: Any = json.loads(
                            body.decode(
                                "utf-8",
                                errors="replace",
                            )
                        )
                    except json.JSONDecodeError as exc:
                        raise ProviderExecutionError(
                            "Provider returned invalid JSON.",
                            provider=configuration.name,
                            status=response.status,
                            retryable=False,
                            category="invalid_json",
                        ) from exc
                else:
                    raw = body.decode(
                        "utf-8",
                        errors="replace",
                    )

                response_mapping = configuration.response
                output = _extract_path(
                    raw,
                    response_mapping.output_path,
                )

                metadata = configuration.metadata or {}

                if output is None:
                    for path in metadata.get(
                        "response_fallback_paths",
                        (),
                    ):
                        output = _extract_path(
                            raw,
                            path,
                        )
                        if output is not None:
                            break

                output_text = _normalize_text(output)

                if not output_text and isinstance(raw, str):
                    output_text = raw.strip()

                if not output_text:
                    raise ProviderExecutionError(
                        "Provider returned no extractable output.",
                        provider=configuration.name,
                        status=response.status,
                        retryable=False,
                        category="empty_output",
                    )

                request_id_path = metadata.get(
                    "response_request_id_path"
                )

                provider_request_id = (
                    _extract_path(
                        raw,
                        request_id_path,
                    )
                    if request_id_path
                    else None
                )

                request_id = (
                    response.headers.get("x-request-id")
                    or response.headers.get("request-id")
                    or (
                        str(provider_request_id)
                        if provider_request_id is not None
                        else None
                    )
                )

                usage_path = metadata.get(
                    "response_usage_path"
                )
                usage = (
                    _extract_path(raw, usage_path)
                    if usage_path
                    else None
                )

                rate_headers = metadata.get(
                    "rate_limit_headers",
                    {},
                )

                remaining_header = (
                    rate_headers.get("remaining")
                    if isinstance(rate_headers, Mapping)
                    else None
                )
                reset_header = (
                    rate_headers.get("reset")
                    if isinstance(rate_headers, Mapping)
                    else None
                )

                response_metadata: Dict[str, Any] = {
                    "status_code": response.status,
                    "request_id": request_id,
                    "latency_ms": elapsed_ms,
                    "request_bytes": request_bytes,
                    "response_bytes": len(body),
                    "estimated_input_tokens": _estimate_tokens(request.prompt),
                    "estimated_output_tokens": _estimate_tokens(output_text),
                }

                if usage is not None:
                    response_metadata["usage"] = usage

                if remaining_header:
                    response_metadata[
                        "rate_remaining"
                    ] = _safe_int(
                        response.headers.get(
                            str(remaining_header)
                        )
                    )

                if reset_header:
                    reset_value = response.headers.get(
                        str(reset_header)
                    )
                    response_metadata[
                        "rate_reset"
                    ] = _safe_float(
                        reset_value,
                        0.0,
                    )

                # Optional generic telemetry header mapping. Providers may
                # configure any header names; nothing is vendor-specific.
                telemetry_headers = metadata.get(
                    "telemetry_headers",
                    {},
                )
                if isinstance(telemetry_headers, Mapping):
                    for logical_name, header_name in telemetry_headers.items():
                        value = response.headers.get(str(header_name))
                        if value is not None:
                            response_metadata[str(logical_name)] = value

                cost_per_1k = _safe_float(
                    metadata.get("cost_per_1k_tokens"),
                    0.0,
                )
                if cost_per_1k > 0:
                    response_metadata["cost_per_1k_tokens"] = cost_per_1k

                return ProviderResponse(
                    success=True,
                    provider=configuration.name,
                    output=output_text,
                    raw_response=raw,
                    metadata=response_metadata,
                )

        except aiohttp.ClientResponseError as exc:
            raise ProviderExecutionError(
                str(exc),
                provider=configuration.name,
                status=getattr(exc, "status", None),
                retryable=(
                    getattr(exc, "status", None)
                    in RETRYABLE_HTTP_CODES
                ),
                category="transport",
            ) from exc

        except asyncio.TimeoutError as exc:
            raise ProviderExecutionError(
                "Provider request timed out.",
                provider=configuration.name,
                retryable=True,
                category="timeout",
            ) from exc

        except aiohttp.ClientError as exc:
            raise ProviderExecutionError(
                f"HTTP transport failure: {exc}",
                provider=configuration.name,
                retryable=True,
                category="transport",
            ) from exc

        finally:
            self._current_request_id = None


# ============================================================================
# ROUTER
# ============================================================================

class GatewayRouter:
    """
    Canonical dynamic gateway.

    Provider configuration is owned by PROVIDER_REGISTRY.
    Gateway owns only runtime state and routing policy.
    """

    _legacy_vault: Dict[str, List[Mapping[str, Any]]] = {}

    def __init__(
        self,
        *,
        max_global_concurrency: int = DEFAULT_GLOBAL_CONCURRENCY,
    ) -> None:
        self.router = APIRouter()
        self.started_at = time.time()

        self.max_global_concurrency = max(
            1,
            int(max_global_concurrency),
        )

        self._global_semaphore = asyncio.Semaphore(
            self.max_global_concurrency
        )

        self._runtime: Dict[str, ProviderRuntime] = {}
        self._registry_lock = threading.RLock()

        self._total_requests = 0
        self._successful_requests = 0
        self._failed_requests = 0

        self._register_routes()
        self.refresh_registry()

        logger.info(
            "Dynamic provider-agnostic gateway initialized."
        )

    # ------------------------------------------------------------------
    # ROUTES
    # ------------------------------------------------------------------

    def _register_routes(self) -> None:

        @self.router.get(
            "/health",
            tags=["Gateway"],
        )
        async def health() -> Dict[str, Any]:
            snapshot = self.health_snapshot()
            eligible = sum(
                1
                for item in snapshot.values()
                if item["eligible"]
            )

            return {
                "status": (
                    "ONLINE"
                    if eligible > 0
                    else (
                        "DEGRADED"
                        if snapshot
                        else "NO_PROVIDERS"
                    )
                ),
                "uptime_seconds": round(
                    time.time() - self.started_at,
                    2,
                ),
                "providers_total": len(snapshot),
                "providers_eligible": eligible,
                "active_requests": sum(
                    item["in_flight"]
                    for item in snapshot.values()
                ),
                "total_requests": self._total_requests,
                "successful_requests": (
                    self._successful_requests
                ),
                "failed_requests": self._failed_requests,
            }

        @self.router.get(
            "/providers",
            tags=["Gateway"],
        )
        async def providers() -> Dict[str, Any]:
            self.refresh_registry()
            return {
                "providers": self.health_snapshot()
            }

        @self.router.get(
            "/routes",
            tags=["Gateway"],
        )
        async def routes() -> Dict[str, Any]:
            self.refresh_registry()
            return {
                "routes": self.route_snapshot()
            }

    def get_router(self) -> APIRouter:
        return self.router

    # ------------------------------------------------------------------
    # REGISTRY SYNCHRONIZATION
    # ------------------------------------------------------------------

    def refresh_registry(self) -> None:
        configurations = PROVIDER_REGISTRY.providers()

        with self._registry_lock:
            known = set(self._runtime)
            current = set(configurations)

            for removed in known - current:
                self._runtime.pop(removed, None)

            for name, configuration in configurations.items():
                runtime = self._runtime.get(name)

                if runtime is None:
                    runtime = ProviderRuntime()
                    self._runtime[name] = runtime

                if runtime.semaphore is None:
                    runtime.semaphore = asyncio.Semaphore(
                        self._provider_limit(configuration)
                    )

    def register_provider(
        self,
        configuration: ProviderConfiguration | Mapping[str, Any],
        *,
        adapter: type[ProviderAdapter] = DynamicProviderAdapter,
    ) -> None:
        if isinstance(configuration, Mapping):
            configuration = _configuration_from_mapping(
                configuration
            )

        if not isinstance(
            configuration,
            ProviderConfiguration,
        ):
            raise TypeError(
                "configuration must be ProviderConfiguration or mapping."
            )

        if not configuration.name.strip():
            raise DynamicConfigurationError(
                "Provider name cannot be empty."
            )

        if not configuration.endpoint.strip():
            raise DynamicConfigurationError(
                "Provider endpoint cannot be empty.",
                provider=configuration.name,
            )

        if not isinstance(adapter, type) or not issubclass(
            adapter,
            ProviderAdapter,
        ):
            raise DynamicConfigurationError(
                "adapter must be a ProviderAdapter subclass.",
                provider=configuration.name,
            )

        PROVIDER_REGISTRY.register(
            configuration,
            adapter,
        )

        CIRCUIT_REGISTRY.register(
            configuration.name
        )

        self.refresh_registry()

    def unregister_provider(
        self,
        provider: str,
    ) -> bool:
        existed = PROVIDER_REGISTRY.exists(provider)
        PROVIDER_REGISTRY.unregister(provider)
        self._runtime.pop(provider, None)
        return existed

    def get_gateway(
        self,
        service_type: str = "default",
    ) -> "GatewayHandle":
        return GatewayHandle(
            self,
            service_type or "default",
        )

    # ------------------------------------------------------------------
    # LEGACY CONFIG LOADING
    # ------------------------------------------------------------------

    @classmethod
    def load_vault(
        cls,
        configuration: Mapping[str, Any],
    ) -> None:
        """
        Backward-compatible configuration entry point.

        Plain API-key strings are deliberately rejected.
        Provider identity must never be inferred from key prefixes.
        """
        if not isinstance(configuration, Mapping):
            raise DynamicConfigurationError(
                "Gateway configuration must be a mapping."
            )

        normalized: Dict[str, List[Mapping[str, Any]]] = {}

        for service, entries in configuration.items():
            service_name = str(service).strip()
            if not service_name:
                continue

            if isinstance(entries, Mapping):
                entries = [entries]

            if isinstance(entries, str):
                raise DynamicConfigurationError(
                    (
                        "Plain API-key entries are unsupported. "
                        "Use explicit provider configuration."
                    )
                )

            if not isinstance(entries, Sequence):
                continue

            bucket: List[Mapping[str, Any]] = []

            for entry in entries:
                if not isinstance(entry, Mapping):
                    continue

                data = dict(entry)
                services = set(
                    str(item)
                    for item in data.get("services", ())
                )
                services.add(service_name)
                data["services"] = sorted(services)
                bucket.append(data)

            if bucket:
                normalized[service_name] = bucket

        cls._legacy_vault = normalized

    # ------------------------------------------------------------------
    # REQUEST NORMALIZATION
    # ------------------------------------------------------------------

    @staticmethod
    def _build_request(
        prompt: str,
        *,
        system_prompt: Optional[str],
        model: Optional[str],
        temperature: float,
        max_tokens: Optional[int],
        service: str,
        required_capabilities: Optional[Iterable[str]],
        metadata: Optional[Mapping[str, Any]],
        timeout_seconds: Optional[float],
        max_failovers: int,
        routing_mode: str | RoutingMode,
        preferred_provider: Optional[str],
        excluded_providers: Optional[Iterable[str]],
    ) -> GatewayRequest:

        mode = _enum_value(
            routing_mode,
            RoutingMode,
            RoutingMode.BALANCED,
        )

        request = GatewayRequest(
            prompt=prompt,
            system_prompt=system_prompt,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            service=service or "default",
            required_capabilities=frozenset(
                str(value)
                for value in (required_capabilities or ())
            ),
            metadata=dict(metadata or {}),
            timeout_seconds=timeout_seconds,
            max_failovers=max(
                1,
                int(max_failovers),
            ),
            routing_mode=mode,
            preferred_provider=preferred_provider,
            excluded_providers=frozenset(
                str(value)
                for value in (excluded_providers or ())
            ),
        )

        GatewayRouter._validate_request(request)
        return request

    @staticmethod
    def _validate_request(
        request: GatewayRequest,
    ) -> None:
        if not isinstance(
            request.prompt,
            str,
        ):
            raise GatewayValidationError(
                "Prompt must be a string."
            )

        if not request.prompt.strip():
            raise GatewayValidationError(
                "Prompt cannot be empty."
            )

        if len(request.prompt) > MAX_PROMPT_LENGTH:
            raise GatewayValidationError(
                "Prompt exceeds gateway input limit."
            )

        if not (
            0.0
            <= request.temperature
            <= 2.0
        ):
            raise GatewayValidationError(
                "Temperature must be between 0 and 2."
            )

        if (
            request.max_tokens is not None
            and request.max_tokens <= 0
        ):
            raise GatewayValidationError(
                "max_tokens must be positive."
            )

        if (
            request.timeout_seconds is not None
            and request.timeout_seconds <= 0
        ):
            raise GatewayValidationError(
                "timeout_seconds must be positive."
            )

    # ------------------------------------------------------------------
    # METADATA
    # ------------------------------------------------------------------

    @staticmethod
    def _metadata(
        configuration: ProviderConfiguration,
    ) -> Mapping[str, Any]:
        return configuration.metadata or {}

    @classmethod
    def _services(
        cls,
        configuration: ProviderConfiguration,
    ) -> Set[str]:
        raw = cls._metadata(configuration).get(
            "services",
            (),
        )

        if isinstance(raw, str):
            return {raw}

        if isinstance(raw, Iterable):
            return {
                str(item)
                for item in raw
            }

        return set()

    @classmethod
    def _capabilities(
        cls,
        configuration: ProviderConfiguration,
    ) -> Set[str]:
        raw = cls._metadata(configuration).get(
            "capabilities",
            (),
        )

        if isinstance(raw, str):
            return {raw}

        if isinstance(raw, Iterable):
            return {
                str(item)
                for item in raw
            }

        return set()

    @classmethod
    def _models(
        cls,
        configuration: ProviderConfiguration,
    ) -> Set[str]:
        raw = cls._metadata(configuration).get(
            "models",
            (),
        )

        models: Set[str] = set()

        if isinstance(raw, str):
            models.add(raw)
        elif isinstance(raw, Iterable):
            models.update(
                str(item)
                for item in raw
            )

        default_model = cls._metadata(
            configuration
        ).get("default_model")

        if default_model:
            models.add(
                str(default_model)
            )

        return {
            model.strip()
            for model in models
            if model.strip()
        }

    @classmethod
    def _provider_limit(
        cls,
        configuration: ProviderConfiguration,
    ) -> int:
        metadata = cls._metadata(configuration)

        return max(
            1,
            _safe_int(
                metadata.get(
                    "max_concurrency"
                ),
                DEFAULT_PROVIDER_CONCURRENCY,
            )
            or DEFAULT_PROVIDER_CONCURRENCY,
        )

    # ------------------------------------------------------------------
    # CANDIDATES
    # ------------------------------------------------------------------

    def _candidates(
        self,
        request: GatewayRequest,
    ) -> List[
        Tuple[
            ProviderConfiguration,
            ProviderRuntime,
        ]
    ]:
        self.refresh_registry()

        result = []

        with self._registry_lock:
            names = sorted(self._runtime)

            for name in names:
                runtime = self._runtime[name]

                if not PROVIDER_REGISTRY.exists(name):
                    continue

                configuration = (
                    PROVIDER_REGISTRY.configuration(name)
                )

                if not configuration.enabled:
                    continue

                if name in request.excluded_providers:
                    continue

                if runtime.cooldown_until > time.monotonic():
                    continue

                services = self._services(
                    configuration
                )

                if (
                    services
                    and request.service not in services
                ):
                    continue

                capabilities = self._capabilities(
                    configuration
                )

                if (
                    request.required_capabilities
                    and not request.required_capabilities.issubset(
                        capabilities
                    )
                ):
                    continue

                models = self._models(
                    configuration
                )

                if (
                    request.model
                    and models
                    and request.model not in models
                ):
                    continue

                if CIRCUIT_REGISTRY is not None:
                    health = (
                        CIRCUIT_REGISTRY
                        .get(name)
                        .snapshot()
                    )
                    state = str(
                        health.state
                    ).lower()

                    if (
                        state.endswith("open")
                        and health.opened_until > time.time()
                    ):
                        continue

                result.append(
                    (
                        configuration,
                        runtime,
                    )
                )

        return result

    # ------------------------------------------------------------------
    # SCORING
    # ------------------------------------------------------------------

    def _score(
        self,
        configuration: ProviderConfiguration,
        runtime: ProviderRuntime,
        mode: RoutingMode,
    ) -> float:
        metadata = self._metadata(
            configuration
        )

        # Circuit / health.
        health_factor = 1.0
        if CIRCUIT_REGISTRY is not None:
            health = (
                CIRCUIT_REGISTRY
                .get(configuration.name)
                .snapshot()
            )
            health_factor = max(
                0.01,
                min(
                    1.0,
                    _safe_float(
                        health.score,
                        100.0,
                    ) / 100.0,
                ),
            )

            state = str(
                health.state
            ).lower()

            if state.endswith("half_open"):
                health_factor *= 0.25

        # Latency.
        latency = (
            runtime.ema_latency_ms
            or runtime.last_latency_ms
            or 250.0
        )

        latency_factor = 1.0 / (
            1.0 + latency / 1000.0
        )

        # Reliability.
        if runtime.total_requests > 0:
            failure_rate = (
                runtime.failed_requests
                / runtime.total_requests
            )
            reliability_factor = max(
                0.02,
                1.0 - failure_rate,
            )
        else:
            # Small exploration bonus keeps a never-used healthy provider
            # from being permanently starved.
            reliability_factor = 0.90

        # Capacity.
        limit = self._provider_limit(
            configuration
        )

        utilization = (
            runtime.in_flight
            / max(1, limit)
        )

        capacity_factor = max(
            0.02,
            1.0 - min(
                1.0,
                utilization,
            ),
        )

        # Rate-limit headroom.
        rate_factor = 1.0
        configured_rate = _safe_int(
            metadata.get(
                "rate_limit_per_minute"
            )
        )

        if (
            runtime.rate_remaining is not None
            and configured_rate
        ):
            rate_factor = max(
                0.02,
                min(
                    1.0,
                    runtime.rate_remaining
                    / max(
                        1,
                        configured_rate,
                    ),
                ),
            )

        # Cost.
        cost = _safe_float(
            metadata.get(
                "cost_per_1k_tokens"
            ),
            0.0,
        )

        cost_factor = 1.0 / (
            1.0 + max(
                0.0,
                cost,
            )
        )

        weight = max(
            0.01,
            _safe_float(
                metadata.get(
                    "weight"
                ),
                1.0,
            ),
        )

        priority = max(
            0.0,
            _safe_float(
                metadata.get(
                    "priority"
                ),
                100.0,
            ),
        )

        priority_factor = 1.0 / (
            1.0 + priority / 100.0
        )

        if mode == RoutingMode.LOW_LATENCY:
            score = (
                latency_factor * 0.45
                + health_factor * 0.25
                + reliability_factor * 0.15
                + capacity_factor * 0.15
            )

        elif mode == RoutingMode.LOW_COST:
            score = (
                cost_factor * 0.40
                + health_factor * 0.25
                + reliability_factor * 0.20
                + latency_factor * 0.15
            )

        elif mode == RoutingMode.HIGH_RELIABILITY:
            score = (
                health_factor * 0.40
                + reliability_factor * 0.35
                + capacity_factor * 0.15
                + latency_factor * 0.10
            )

        elif mode == RoutingMode.HIGH_CAPACITY:
            score = (
                capacity_factor * 0.45
                + rate_factor * 0.25
                + health_factor * 0.20
                + latency_factor * 0.10
            )

        else:
            score = (
                health_factor * 0.25
                + reliability_factor * 0.20
                + latency_factor * 0.15
                + capacity_factor * 0.15
                + rate_factor * 0.10
                + cost_factor * 0.10
                + priority_factor * 0.05
            )

        return max(
            0.0001,
            score * weight,
        )

    def _select(
        self,
        candidates: Sequence[
            Tuple[
                ProviderConfiguration,
                ProviderRuntime,
            ]
        ],
        request: GatewayRequest,
    ) -> Tuple[
        ProviderConfiguration,
        ProviderRuntime,
    ]:
        if not candidates:
            raise ProviderUnavailableError(
                "No eligible dynamic provider exists.",
                category="routing",
            )

        preferred = [
            item
            for item in candidates
            if (
                request.preferred_provider
                and item[0].name
                == request.preferred_provider
            )
        ]

        if preferred:
            return max(
                preferred,
                key=lambda item: self._score(
                    item[0],
                    item[1],
                    request.routing_mode,
                ),
            )

        weighted = [
            (
                item,
                self._score(
                    item[0],
                    item[1],
                    request.routing_mode,
                ),
            )
            for item in candidates
        ]

        total = sum(
            score
            for _, score in weighted
        )

        if not math.isfinite(total) or total <= 0:
            return min(
                candidates,
                key=lambda item: (
                    item[1].in_flight,
                    item[0].name,
                ),
            )

        target = random.random() * total
        cursor = 0.0

        for item, score in weighted:
            cursor += score
            if target <= cursor:
                return item

        return weighted[-1][0]

    # ------------------------------------------------------------------
    # EXECUTION
    # ------------------------------------------------------------------

    async def generate(
        self,
        prompt: str,
        *,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        service: str = "default",
        required_capabilities: Optional[
            Iterable[str]
        ] = None,
        metadata: Optional[
            Mapping[str, Any]
        ] = None,
        timeout_seconds: Optional[float] = None,
        max_failovers: int = DEFAULT_MAX_FAILOVERS,
        routing_mode: str | RoutingMode = RoutingMode.BALANCED,
        preferred_provider: Optional[str] = None,
        excluded_providers: Optional[
            Iterable[str]
        ] = None,
    ) -> GatewayResponse:

        request = self._build_request(
            prompt,
            system_prompt=system_prompt,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            service=service,
            required_capabilities=required_capabilities,
            metadata=metadata,
            timeout_seconds=timeout_seconds,
            max_failovers=max_failovers,
            routing_mode=routing_mode,
            preferred_provider=preferred_provider,
            excluded_providers=excluded_providers,
        )

        # One logical gateway request; provider failovers are attempts of the
        # same request and are tracked separately below.
        self._total_requests += 1

        if not HTTP_CLIENT.initialized():
            await HTTP_CLIENT.startup()

        candidates = self._candidates(
            request
        )

        if not candidates:
            raise ProviderUnavailableError(
                (
                    "No registered provider matches "
                    "service/capability/model constraints."
                ),
                category="routing",
            )

        # max_failovers means maximum provider execution slots tried.
        max_attempts = min(
            len(candidates),
            request.max_failovers,
        )

        attempted: Set[str] = set()
        last_error: Optional[GatewayError] = None

        for attempt in range(
            1,
            max_attempts + 1,
        ):
            available = [
                item
                for item in self._candidates(request)
                if (
                    item[0].name not in attempted
                )
            ]

            if not available:
                break

            configuration, runtime = self._select(
                available,
                request,
            )

            runtime.last_selection_score = self._score(
                configuration,
                runtime,
                request.routing_mode,
            )
            runtime.last_selected_at = time.time()

            provider = configuration.name
            attempted.add(provider)

            started = time.perf_counter()

            try:
                provider_response = await self._execute_provider(
                    configuration,
                    runtime,
                    request,
                )

                elapsed_ms = (
                    time.perf_counter()
                    - started
                ) * 1000.0

                await self._record_success(
                    runtime,
                    provider_response,
                    elapsed_ms,
                )

                self._successful_requests += 1

                metadata_out = dict(
                    provider_response.metadata or {}
                )
                metadata_out.update(
                    {
                        "gateway_attempt": attempt,
                        "service": request.service,
                        "routing_mode": (
                            request.routing_mode.value
                        ),
                        "request_id": request.request_id,
                        "provider_protocol": (
                            configuration.protocol.value
                        ),
                    }
                )

                return GatewayResponse(
                    success=True,
                    output=provider_response.output,
                    provider=provider,
                    model=(
                        request.model
                        or self._metadata(
                            configuration
                        ).get("default_model")
                    ),
                    request_id=(
                        provider_response.metadata.get(
                            "request_id"
                        )
                        or request.request_id
                    ),
                    latency_ms=round(
                        elapsed_ms,
                        2,
                    ),
                    attempts=attempt,
                    status_code=_safe_int(
                        provider_response.metadata.get(
                            "status_code"
                        )
                    ),
                    raw_response=(
                        provider_response.raw_response
                    ),
                    metadata=metadata_out,
                )

            except GatewayError as exc:
                last_error = exc
                elapsed_ms = (
                    time.perf_counter()
                    - started
                ) * 1000.0

                await self._record_failure(
                    runtime,
                    exc,
                    elapsed_ms,
                )
                self._failed_requests += 1

                # ProviderExecutor already performed its configured retry
                # policy. Gateway now chooses a different provider.
                continue

            except Exception as exc:
                wrapped = ProviderExecutionError(
                    str(exc),
                    provider=provider,
                    retryable=True,
                    category="unexpected",
                )

                last_error = wrapped

                elapsed_ms = (
                    time.perf_counter()
                    - started
                ) * 1000.0

                await self._record_failure(
                    runtime,
                    wrapped,
                    elapsed_ms,
                )
                self._failed_requests += 1
                continue

        if last_error is not None:
            raise last_error

        raise ProviderUnavailableError(
            "All eligible providers failed.",
            category="exhausted",
        )

    async def _execute_provider(
        self,
        configuration: ProviderConfiguration,
        runtime: ProviderRuntime,
        request: GatewayRequest,
    ) -> ProviderResponse:

        if runtime.semaphore is None:
            runtime.semaphore = asyncio.Semaphore(
                self._provider_limit(configuration)
            )

        async with self._global_semaphore:
            async with runtime.semaphore:
                runtime.in_flight += 1
                try:
                    return await self._execute_with_circuit(
                        configuration,
                        request,
                    )
                finally:
                    runtime.in_flight = max(
                        0,
                        runtime.in_flight - 1,
                    )

    async def _execute_with_circuit(
        self,
        configuration: ProviderConfiguration,
        request: GatewayRequest,
    ) -> ProviderResponse:

        sdk_request = PromptRequest(
            prompt=request.prompt,
            system_prompt=request.system_prompt,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            metadata={
                **request.metadata,
                "model": request.model,
                "gateway_request_id": request.request_id,
                "timeout_seconds": (
                    request.timeout_seconds
                    or configuration.timeout_seconds
                ),
            },
        )

        async def operation() -> ProviderResponse:
            adapter = PROVIDER_REGISTRY.create(
                configuration.name
            )

            # Custom adapters registered in ProviderRegistry are authoritative.
            return await adapter.invoke(
                sdk_request
            )

        try:
            return await asyncio.wait_for(
                PROVIDER_EXECUTOR.execute(
                    configuration.name,
                    operation,
                ),
                timeout=(
                    request.timeout_seconds
                    or configuration.timeout_seconds
                    or DEFAULT_TIMEOUT_SECONDS
                ) + 5,
            )

        except asyncio.TimeoutError as exc:
            raise ProviderExecutionError(
                "Provider execution timed out.",
                provider=configuration.name,
                retryable=True,
                category="timeout",
            ) from exc

        except RuntimeError as exc:
            # ProviderExecutor uses RuntimeError for an open circuit.
            raise ProviderUnavailableError(
                str(exc),
                provider=configuration.name,
                retryable=True,
                category="circuit",
            ) from exc

    # ------------------------------------------------------------------
    # RUNTIME METRICS
    # ------------------------------------------------------------------

    async def _record_success(
        self,
        runtime: ProviderRuntime,
        response: ProviderResponse,
        latency_ms: float,
    ) -> None:
        async with runtime.lock:
            runtime.total_requests += 1
            runtime.successful_requests += 1
            runtime.last_latency_ms = latency_ms

            if runtime.ema_latency_ms <= 0:
                runtime.ema_latency_ms = latency_ms
            else:
                runtime.ema_latency_ms = (
                    runtime.ema_latency_ms * 0.8
                    + latency_ms * 0.2
                )

            runtime.consecutive_successes += 1
            runtime.consecutive_failures = 0
            runtime.last_success_at = time.time()
            runtime.cooldown_until = 0.0
            runtime.last_error = None

            metadata = response.metadata or {}

            remaining = _safe_int(
                metadata.get(
                    "rate_remaining"
                )
            )

            if remaining is not None:
                runtime.rate_remaining = remaining

            reset = _safe_float(
                metadata.get(
                    "rate_reset"
                ),
                0.0,
            )

            if reset > 0:
                runtime.rate_reset_at = reset

            runtime.bytes_sent += (
                _safe_int(
                    metadata.get(
                        "request_bytes"
                    ),
                    0,
                )
                or 0
            )

            runtime.bytes_received += (
                _safe_int(
                    metadata.get(
                        "response_bytes"
                    ),
                    0,
                )
                or 0
            )

            runtime.estimated_tokens_in += (
                _safe_int(
                    metadata.get(
                        "estimated_input_tokens"
                    ),
                    0,
                )
                or 0
            )

            runtime.estimated_tokens_out += (
                _safe_int(
                    metadata.get(
                        "estimated_output_tokens"
                    ),
                    0,
                )
                or 0
            )

            cost_per_1k = _safe_float(
                metadata.get("cost_per_1k_tokens"),
                0.0,
            )

            if cost_per_1k > 0:
                total_tokens = (
                    runtime.estimated_tokens_in
                    + runtime.estimated_tokens_out
                )
                runtime.estimated_cost += (
                    total_tokens / 1000.0
                ) * cost_per_1k

    async def _record_failure(
        self,
        runtime: ProviderRuntime,
        error: GatewayError,
        latency_ms: float,
    ) -> None:
        async with runtime.lock:
            runtime.total_requests += 1
            runtime.failed_requests += 1
            runtime.last_latency_ms = latency_ms

            if runtime.ema_latency_ms <= 0:
                runtime.ema_latency_ms = latency_ms
            else:
                runtime.ema_latency_ms = (
                    runtime.ema_latency_ms * 0.8
                    + latency_ms * 0.2
                )

            runtime.consecutive_failures += 1
            runtime.consecutive_successes = 0
            runtime.last_failure_at = time.time()
            runtime.last_error = str(error)[:1000]

            # Local admission cooldown complements the circuit breaker.
            # It does not replace it or retry the request.
            failure_level = min(
                6,
                runtime.consecutive_failures,
            )

            runtime.cooldown_until = (
                time.monotonic()
                + min(
                    30.0,
                    0.5 * (
                        2 ** (
                            failure_level - 1
                        )
                    ),
                )
            )

    # ------------------------------------------------------------------
    # MODEL DISCOVERY
    # ------------------------------------------------------------------

    async def discover_models(
        self,
        provider: str,
        *,
        force_refresh: bool = False,
    ) -> List[str]:

        self.refresh_registry()

        if not PROVIDER_REGISTRY.exists(provider):
            raise ProviderUnavailableError(
                f"Provider '{provider}' is not registered.",
                provider=provider,
            )

        if (
            MODEL_CACHE is not None
            and not force_refresh
        ):
            cached = await MODEL_CACHE.get_models(
                provider
            )
            if cached is not None:
                return list(cached)

        configuration = (
            PROVIDER_REGISTRY.configuration(
                provider
            )
        )

        discovery = self._metadata(
            configuration
        ).get("model_discovery")

        models: List[str] = []

        if isinstance(
            discovery,
            Mapping,
        ):
            endpoint = discovery.get(
                "endpoint"
            )

            if endpoint:
                method = str(
                    discovery.get(
                        "method",
                        "GET",
                    )
                ).upper()

                response_path = discovery.get(
                    "response_path",
                    "models",
                )

                model_path = discovery.get(
                    "model_path"
                )

                adapter = DynamicProviderAdapter(
                    configuration
                )

                async with self._global_semaphore:
                    async with self._runtime_for(
                        configuration
                    ).semaphore_context():
                        session = HTTP_CLIENT.session()

                        async with session.request(
                            method,
                            str(endpoint),
                            headers=adapter.build_headers(),
                            params=adapter.build_query(),
                            timeout=aiohttp.ClientTimeout(
                                total=configuration.timeout_seconds
                            ),
                        ) as response:

                            body = await response.read()

                            if response.status >= 400:
                                raise ProviderExecutionError(
                                    (
                                        "Dynamic model discovery failed: "
                                        f"HTTP {response.status}"
                                    ),
                                    provider=provider,
                                    status=response.status,
                                    retryable=(
                                        response.status
                                        in RETRYABLE_HTTP_CODES
                                    ),
                                    category="model_discovery",
                                )

                            if len(body) > MAX_RESPONSE_BYTES:
                                raise ProviderExecutionError(
                                    "Model discovery response is too large.",
                                    provider=provider,
                                    category="model_discovery",
                                )

                            content_type = response.headers.get(
                                "Content-Type",
                                "",
                            ).lower()

                            if "json" in content_type:
                                payload = json.loads(
                                    body.decode(
                                        "utf-8",
                                        errors="replace",
                                    )
                                )
                            else:
                                payload = body.decode(
                                    "utf-8",
                                    errors="replace",
                                )

                            values = _extract_path(
                                payload,
                                response_path,
                                [],
                            )

                            if isinstance(values, list):
                                for item in values:
                                    if model_path:
                                        item = _extract_path(
                                            item,
                                            model_path,
                                        )
                                    if item is not None:
                                        models.append(
                                            str(item)
                                        )

        if not models:
            models = sorted(
                self._models(configuration)
            )

        models = list(
            dict.fromkeys(
                value.strip()
                for value in models
                if value
                and value.strip()
            )
        )

        if MODEL_CACHE is not None:
            await MODEL_CACHE.set_models(
                provider,
                models,
            )

        return models

    # ------------------------------------------------------------------
    # METRICS / SNAPSHOTS
    # ------------------------------------------------------------------

    def health_snapshot(
        self,
    ) -> Dict[str, Any]:
        self.refresh_registry()

        snapshot: Dict[str, Any] = {}
        now = time.time()

        with self._registry_lock:
            for name, runtime in self._runtime.items():
                if not PROVIDER_REGISTRY.exists(name):
                    continue

                configuration = (
                    PROVIDER_REGISTRY.configuration(name)
                )

                state = "unknown"
                score = 100.0
                opened_until = 0.0

                with contextlib.suppress(Exception):
                    health = (
                        CIRCUIT_REGISTRY
                        .get(name)
                        .snapshot()
                    )
                    state = str(
                        health.state
                    ).split(".")[-1].lower()
                    score = _safe_float(
                        health.score,
                        100.0,
                    )
                    opened_until = _safe_float(
                        health.opened_until,
                        0.0,
                    )

                snapshot[name] = {
                    "enabled": configuration.enabled,
                    "eligible": (
                        configuration.enabled
                        and runtime.cooldown_until
                        <= time.monotonic()
                        and not (
                            state.endswith("open")
                            and opened_until > now
                        )
                    ),
                    "protocol": (
                        configuration.protocol.value
                    ),
                    "endpoint": _public_endpoint(
                        configuration.endpoint
                    ),
                    "models": sorted(
                        self._models(
                            configuration
                        )
                    ),
                    "services": sorted(
                        self._services(
                            configuration
                        )
                    ),
                    "capabilities": sorted(
                        self._capabilities(
                            configuration
                        )
                    ),
                    "priority": _safe_float(
                        self._metadata(
                            configuration
                        ).get(
                            "priority"
                        ),
                        100.0,
                    ),
                    "weight": _safe_float(
                        self._metadata(
                            configuration
                        ).get(
                            "weight"
                        ),
                        1.0,
                    ),
                    "health_score": score,
                    "circuit_state": state,
                    "circuit_open_until": opened_until,
                    "in_flight": runtime.in_flight,
                    "avg_latency_ms": round(
                        runtime.ema_latency_ms,
                        2,
                    ),
                    "total_requests": (
                        runtime.total_requests
                    ),
                    "successful_requests": (
                        runtime.successful_requests
                    ),
                    "failed_requests": (
                        runtime.failed_requests
                    ),
                    "consecutive_failures": (
                        runtime.consecutive_failures
                    ),
                    "rate_remaining": (
                        runtime.rate_remaining
                    ),
                    "estimated_tokens_in": (
                        runtime.estimated_tokens_in
                    ),
                    "estimated_tokens_out": (
                        runtime.estimated_tokens_out
                    ),
                    "estimated_cost": round(
                        runtime.estimated_cost,
                        8,
                    ),
                    "last_selection_score": round(
                        runtime.last_selection_score,
                        6,
                    ),
                    "last_selected_at": runtime.last_selected_at,
                    "last_error": runtime.last_error,
                    "credential_fingerprint": (
                        _fingerprint(
                            configuration
                            .authentication
                            .token
                        )
                    ),
                }

        return snapshot

    def route_snapshot(
        self,
    ) -> Dict[str, Any]:
        self.refresh_registry()

        routes: Dict[str, List[str]] = {}

        for name in sorted(self._runtime):
            if not PROVIDER_REGISTRY.exists(name):
                continue

            configuration = (
                PROVIDER_REGISTRY.configuration(name)
            )

            services = self._services(
                configuration
            )
            if not services:
                services = {"default"}

            for service in services:
                routes.setdefault(
                    service,
                    [],
                ).append(name)

        return {
            service: sorted(
                providers
            )
            for service, providers
            in routes.items()
        }

    def _runtime_for(
        self,
        configuration: ProviderConfiguration,
    ) -> "RuntimeHandle":
        runtime = self._runtime.setdefault(
            configuration.name,
            ProviderRuntime(),
        )

        if runtime.semaphore is None:
            runtime.semaphore = asyncio.Semaphore(
                self._provider_limit(
                    configuration
                )
            )

        return RuntimeHandle(runtime)


# ============================================================================
# RUNTIME SEMAPHORE HANDLE
# ============================================================================

class RuntimeHandle:
    def __init__(
        self,
        runtime: ProviderRuntime,
    ) -> None:
        self.runtime = runtime

    class _SemaphoreContext:
        def __init__(
            self,
            semaphore: Optional[asyncio.Semaphore],
        ) -> None:
            self.semaphore = semaphore

        async def __aenter__(self):
            if self.semaphore is not None:
                await self.semaphore.acquire()

        async def __aexit__(
            self,
            exc_type,
            exc,
            tb,
        ):
            if self.semaphore is not None:
                self.semaphore.release()

    def semaphore_context(self):
        return self._SemaphoreContext(
            self.runtime.semaphore
        )


# ============================================================================
# BASE COMPATIBILITY CONTRACT
# ============================================================================

class BaseGateway:
    """Minimal synchronous/asynchronous gateway contract."""

    async def agenerate(self, prompt: str, **kwargs: Any) -> GatewayResponse:
        raise NotImplementedError

    def generate(self, prompt: str, **kwargs: Any) -> str:
        raise NotImplementedError


# ============================================================================
# COMPATIBILITY HANDLE
# ============================================================================

class GatewayHandle(BaseGateway):
    """
    Compatibility facade for existing agents.

    It never pins the request to one provider. Routing stays dynamic.
    """

    def __init__(
        self,
        gateway: GatewayRouter,
        service: str,
    ) -> None:
        self.gateway = gateway
        self.service = service

    async def agenerate(
        self,
        prompt: str,
        **kwargs: Any,
    ) -> GatewayResponse:
        kwargs.setdefault(
            "service",
            self.service,
        )
        return await self.gateway.generate(
            prompt,
            **kwargs,
        )

    def generate(
        self,
        prompt: str,
        **kwargs: Any,
    ) -> str:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self.agenerate(
                    prompt,
                    **kwargs,
                )
            ).output

        raise RuntimeError(
            (
                "Use 'await gateway.agenerate(...)' "
                "inside an active event loop."
            )
        )


# ============================================================================
# OPTIONAL ENVIRONMENT LOADING
# ============================================================================

def _load_environment_configuration(
    gateway: GatewayRouter,
) -> None:
    raw = os.getenv(
        "RIOT_PROVIDERS_JSON"
    )

    if not raw:
        return

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error(
            "RIOT_PROVIDERS_JSON is invalid JSON: %s",
            exc,
        )
        return

    if not isinstance(parsed, list):
        logger.error(
            "RIOT_PROVIDERS_JSON must be a list."
        )
        return

    for item in parsed:
        if not isinstance(item, Mapping):
            continue

        try:
            gateway.register_provider(
                _configuration_from_mapping(item)
            )
        except Exception as exc:
            logger.error(
                "Dynamic provider registration failed: %s",
                exc,
            )


# ============================================================================
# GLOBAL INSTANCE
# ============================================================================

gateway_router = GatewayRouter()
_load_environment_configuration(
    gateway_router
)


# ============================================================================
# PUBLIC EXPORTS
# ============================================================================

__all__ = [
    "BaseGateway",
    "DynamicConfigurationError",
    "DynamicProviderAdapter",
    "GatewayError",
    "GatewayHandle",
    "GatewayRequest",
    "GatewayResponse",
    "GatewayRouter",
    "GatewayValidationError",
    "ProviderExecutionError",
    "ProviderUnavailableError",
    "RoutingMode",
    "gateway_router",
]
