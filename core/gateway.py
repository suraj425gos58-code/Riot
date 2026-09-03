"""
Riot Dynamic API Gateway
========================

Design goals
------------

This gateway is intentionally provider-agnostic.

The gateway MUST NOT know:
- vendor names
- API-key prefixes
- concrete model names
- vendor-specific endpoints
- vendor-specific authentication rules
- vendor-specific response shapes

Everything above comes from runtime configuration.

Architecture
------------

Request
  |
  v
Request validation
  |
  v
Capability / service filtering
  |
  v
Healthy provider candidates
  |
  +--> Circuit state
  +--> Health score
  +--> Latency
  +--> Failure rate
  +--> Weight
  +--> Priority
  +--> Cost signal
  +--> Rate-limit signal
  +--> Capability match
  +--> Model match
  |
  v
Dynamic weighted selection
  |
  v
Concurrency admission
  |
  v
Circuit breaker / retry executor
  |
  v
Dynamic protocol adapter
  |
  +--> REST
  +--> OpenAI-compatible
  +--> CUSTOM (registered adapter)
  |
  v
HTTP execution through shared connection pool
  |
  v
Dynamic response-path extraction
  |
  v
Normalized GatewayResponse

Important:
This file is the routing/execution layer.
It does NOT own business logic, agent logic, project generation,
world generation, or game orchestration.
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
import threading
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Type,
    Union,
)

import aiohttp
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse


# ============================================================================
# INTERNAL PACKAGE IMPORTS
# ============================================================================
#
# provider_sdk.py currently imports its sibling modules using top-level
# imports. We repair that import boundary locally without modifying any
# other repository file.
#
# The aliases point to the SAME module objects already used by the package,
# avoiding duplicate singleton connection pools / circuit registries.
# ============================================================================

try:
    import sys

    from god_brain import connection_pool as _connection_pool_module
    from god_brain import circuit_breaker as _circuit_breaker_module

    sys.modules.setdefault(
        "connection_pool",
        _connection_pool_module,
    )

    sys.modules.setdefault(
        "circuit_breaker",
        _circuit_breaker_module,
    )

    from god_brain.connection_pool import (
        HTTP_CLIENT,
        MODEL_CACHE,
    )

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

    PROVIDER_SDK_AVAILABLE = True

except Exception as exc:
    PROVIDER_SDK_AVAILABLE = False

    HTTP_CLIENT = None
    MODEL_CACHE = None
    CIRCUIT_REGISTRY = None
    PROVIDER_EXECUTOR = None

    AuthenticationConfig = None
    AuthenticationType = None
    PayloadMapping = None
    ProviderAdapter = object
    ProviderConfiguration = None
    ProviderProtocol = None
    ProviderResponse = None
    PROVIDER_REGISTRY = None
    PromptRequest = None
    ResponseMapping = None

    _PROVIDER_SDK_IMPORT_ERROR = exc


# ============================================================================
# LOGGING
# ============================================================================

logger = logging.getLogger("Riot.DynamicGateway")

if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(
        logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        )
    )
    logger.addHandler(_handler)

logger.setLevel(
    os.getenv(
        "RIOT_GATEWAY_LOG_LEVEL",
        "INFO",
    ).upper()
)


# ============================================================================
# CONSTANTS
# ============================================================================

DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_MAX_CONCURRENCY = 8

MAX_PROMPT_LENGTH = 2_000_000
MAX_RESPONSE_BYTES = 32 * 1024 * 1024

RETRYABLE_HTTP_STATUS = frozenset(
    {
        408,
        425,
        429,
        500,
        502,
        503,
        504,
    }
)

NON_RETRYABLE_AUTH_STATUS = frozenset(
    {
        401,
        403,
    }
)


# ============================================================================
# ENUMS
# ============================================================================

class GatewayProtocol(str, Enum):
    REST = "rest"
    OPENAI_COMPATIBLE = "openai_compatible"
    CUSTOM = "custom"


class RoutingMode(str, Enum):
    BALANCED = "balanced"
    LOW_LATENCY = "low_latency"
    LOW_COST = "low_cost"
    HIGH_RELIABILITY = "high_reliability"
    PRIORITY = "priority"


# ============================================================================
# EXCEPTIONS
# ============================================================================

class GatewayError(RuntimeError):
    """Base gateway error."""

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
# REQUEST / RESPONSE CONTRACTS
# ============================================================================

@dataclass(slots=True)
class GatewayRequest:
    prompt: str

    system_prompt: Optional[str] = None
    model: Optional[str] = None

    temperature: float = 0.7
    max_tokens: Optional[int] = None

    service: str = "default"

    required_capabilities: frozenset[str] = frozenset()

    metadata: Dict[str, Any] = field(
        default_factory=dict
    )

    timeout_seconds: Optional[float] = None

    max_attempts: int = DEFAULT_MAX_ATTEMPTS

    routing_mode: RoutingMode = RoutingMode.BALANCED

    excluded_providers: frozenset[str] = frozenset()

    request_id: str = field(
        default_factory=lambda: uuid.uuid4().hex
    )


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

    raw_response: Optional[Any] = None

    metadata: Dict[str, Any] = field(
        default_factory=dict
    )


# ============================================================================
# DYNAMIC PROVIDER DEFINITION
# ============================================================================

@dataclass(slots=True)
class DynamicProviderDefinition:
    """
    Runtime provider definition.

    This class is intentionally generic.

    The configuration can describe:
    - endpoint
    - protocol
    - authentication
    - payload mapping
    - response mapping
    - capabilities
    - services
    - models
    - model discovery
    - cost signals
    - priority
    - traffic weight
    - concurrency limits

    None of these values are hardcoded by the gateway.
    """

    name: str

    endpoint: str

    protocol: str = GatewayProtocol.REST.value

    enabled: bool = True

    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    max_concurrency: int = DEFAULT_MAX_CONCURRENCY

    priority: int = 100

    weight: float = 1.0

    model: Optional[str] = None

    models: Tuple[str, ...] = ()

    capabilities: frozenset[str] = frozenset()

    services: frozenset[str] = frozenset()

    cost_per_1k_tokens: Optional[float] = None

    rate_limit_per_minute: Optional[int] = None

    authentication: Dict[str, Any] = field(
        default_factory=dict
    )

    payload: Dict[str, Any] = field(
        default_factory=dict
    )

    response: Dict[str, Any] = field(
        default_factory=dict
    )

    headers: Dict[str, str] = field(
        default_factory=dict
    )

    metadata: Dict[str, Any] = field(
        default_factory=dict
    )

    adapter_class: Optional[
        Type[ProviderAdapter]
    ] = None


# ============================================================================
# RUNTIME METRICS
# ============================================================================

@dataclass(slots=True)
class ProviderRuntime:
    """
    Mutable runtime statistics.

    Kept separate from configuration so configuration can be replaced
    dynamically without destroying operational history.
    """

    total_requests: int = 0

    successful_requests: int = 0

    failed_requests: int = 0

    in_flight: int = 0

    last_latency_ms: float = 0.0

    ema_latency_ms: float = 0.0

    last_success_at: float = 0.0

    last_failure_at: float = 0.0

    consecutive_failures: int = 0

    consecutive_successes: int = 0

    last_error: Optional[str] = None

    cooldown_until: float = 0.0

    estimated_rate_remaining: Optional[int] = None

    rate_window_reset_at: float = 0.0

    bytes_sent: int = 0

    bytes_received: int = 0

    lock: asyncio.Lock = field(
        default_factory=asyncio.Lock
    )

    semaphore: Optional[asyncio.Semaphore] = None


@dataclass(slots=True)
class ProviderSlot:
    definition: DynamicProviderDefinition

    runtime: ProviderRuntime

    generation: int = 0


# ============================================================================
# CUSTOM ADAPTER CONTRACT
# ============================================================================

class GatewayCustomAdapter(ABC):
    """
    Custom protocol extension contract.

    A new transport can be added without changing routing logic.
    """

    @abstractmethod
    async def invoke(
        self,
        configuration: DynamicProviderDefinition,
        request: GatewayRequest,
    ) -> GatewayResponse:
        raise NotImplementedError


# ============================================================================
# GENERIC HELPERS
# ============================================================================

def _fingerprint(value: Optional[str]) -> str:
    if not value:
        return ""

    return hashlib.sha256(
        value.encode("utf-8")
    ).hexdigest()[:16]


def _redact(value: Optional[str]) -> str:
    if not value:
        return ""

    if len(value) <= 8:
        return "***"

    return (
        value[:4]
        + "..."
        + value[-4:]
    )


def _normalize_path(path: str) -> List[str]:
    return [
        part
        for part in str(path)
        .strip(".")
        .split(".")
        if part
    ]


def _extract_path(
    payload: Any,
    path: Sequence[str] | str,
    default: Any = None,
) -> Any:

    parts = (
        _normalize_path(path)
        if isinstance(path, str)
        else list(path)
    )

    current = payload

    for part in parts:

        if isinstance(current, Mapping):

            if part not in current:
                return default

            current = current[part]

            continue

        if isinstance(current, list):

            try:
                index = int(part)
            except (TypeError, ValueError):
                return default

            if index < 0 or index >= len(current):
                return default

            current = current[index]

            continue

        return default

    return current


def _assign_path(
    target: Dict[str, Any],
    path: str,
    value: Any,
) -> None:

    parts = _normalize_path(path)

    if not parts:
        return

    current: Dict[str, Any] = target

    for part in parts[:-1]:

        existing = current.get(part)

        if not isinstance(existing, dict):
            existing = {}
            current[part] = existing

        current = existing

    current[parts[-1]] = value


def _safe_float(
    value: Any,
    default: float,
) -> float:
    try:
        return float(value)
    except (
        TypeError,
        ValueError,
    ):
        return default


def _safe_int(
    value: Any,
    default: Optional[int] = None,
) -> Optional[int]:

    try:
        return int(value)
    except (
        TypeError,
        ValueError,
    ):
        return default


def _is_retryable_status(
    status: Optional[int],
) -> bool:
    return status in RETRYABLE_HTTP_STATUS


def _normalize_output(
    value: Any,
) -> str:

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
                text = (
                    item.get("text")
                    or item.get("content")
                    or item.get("value")
                )

                if text is not None:
                    parts.append(
                        str(text)
                    )

        return "".join(parts).strip()

    if isinstance(value, Mapping):

        for key in (
            "text",
            "content",
            "output",
            "response",
            "value",
        ):

            if key in value:
                nested = _normalize_output(
                    value[key]
                )

                if nested:
                    return nested

    return str(value).strip()


# ============================================================================
# GENERIC DYNAMIC HTTP ADAPTER
# ============================================================================

class DynamicHTTPAdapter:
    """
    Configuration-driven HTTP adapter.

    No provider/vendor logic lives here.

    The following are all runtime values:
    - URL
    - method
    - auth
    - payload fields
    - nested paths
    - headers
    - response extraction
    """

    def __init__(
        self,
        definition: DynamicProviderDefinition,
    ) -> None:
        self.definition = definition

    # ------------------------------------------------------------------
    # AUTH
    # ------------------------------------------------------------------

    def build_headers(
        self,
        request: GatewayRequest,
    ) -> Dict[str, str]:

        headers: Dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        headers.update(
            self.definition.headers
        )

        auth = self.definition.authentication

        auth_type = str(
            auth.get(
                "type",
                "none",
            )
        ).lower()

        token = auth.get("token")

        if auth_type == "bearer":

            header_name = auth.get(
                "header_name",
                "Authorization",
            )

            prefix = auth.get(
                "prefix",
                "Bearer",
            )

            if token:
                headers[header_name] = (
                    f"{prefix} {token}"
                )

        elif auth_type == "api_key_header":

            header_name = auth.get(
                "header_name",
                "X-API-Key",
            )

            if token:
                headers[header_name] = (
                    str(token)
                )

        elif auth_type == "custom":

            custom_headers = auth.get(
                "custom_headers",
                {},
            )

            if isinstance(
                custom_headers,
                Mapping,
            ):
                headers.update(
                    {
                        str(k): str(v)
                        for k, v
                        in custom_headers.items()
                    }
                )

        idempotency_header = self.definition.metadata.get(
            "idempotency_header"
        )

        if idempotency_header:
            headers[str(idempotency_header)] = (
                request.request_id
            )

        return headers

    # ------------------------------------------------------------------
    # QUERY AUTH
    # ------------------------------------------------------------------

    def build_query(
        self,
    ) -> Dict[str, str]:

        auth = self.definition.authentication

        auth_type = str(
            auth.get(
                "type",
                "none",
            )
        ).lower()

        if auth_type != "api_key_query":
            return {}

        token = auth.get("token")

        if not token:
            return {}

        query_name = auth.get(
            "query_name",
            "key",
        )

        return {
            str(query_name): str(token)
        }

    # ------------------------------------------------------------------
    # PAYLOAD
    # ------------------------------------------------------------------

    def build_payload(
        self,
        request: GatewayRequest,
    ) -> Dict[str, Any]:

        config = self.definition.payload

        fixed_fields = config.get(
            "fixed_fields",
            {},
        )

        payload: Dict[str, Any] = {}

        if isinstance(
            fixed_fields,
            Mapping,
        ):
            payload.update(
                fixed_fields
            )

        # Fully custom payload object.
        template = config.get(
            "template"
        )

        if isinstance(
            template,
            Mapping,
        ):
            payload = json.loads(
                json.dumps(
                    template
                )
            )

        # Simple field mapping.
        prompt_field = config.get(
            "prompt_field",
            "prompt",
        )

        system_field = config.get(
            "system_field"
        )

        temperature_field = config.get(
            "temperature_field",
            "temperature",
        )

        max_tokens_field = config.get(
            "max_tokens_field",
            "max_tokens",
        )

        model_field = config.get(
            "model_field",
        )

        metadata_field = config.get(
            "metadata_field"
        )

        if prompt_field:
            _assign_path(
                payload,
                str(prompt_field),
                request.prompt,
            )

        if (
            system_field
            and request.system_prompt
        ):
            _assign_path(
                payload,
                str(system_field),
                request.system_prompt,
            )

        if temperature_field is not None:
            _assign_path(
                payload,
                str(temperature_field),
                request.temperature,
            )

        if (
            max_tokens_field
            and request.max_tokens is not None
        ):
            _assign_path(
                payload,
                str(max_tokens_field),
                request.max_tokens,
            )

        if (
            model_field
            and request.model
        ):
            _assign_path(
                payload,
                str(model_field),
                request.model,
            )

        if (
            metadata_field
            and request.metadata
        ):
            _assign_path(
                payload,
                str(metadata_field),
                request.metadata,
            )

        # Fully custom runtime mapping.
        #
        # Example configuration:
        #
        # "logical_fields": {
        #   "prompt": "input.messages.0.content",
        #   "model": "options.model"
        # }
        #
        logical_fields = config.get(
            "logical_fields",
            {},
        )

        if isinstance(
            logical_fields,
            Mapping,
        ):

            if logical_fields.get(
                "prompt"
            ):
                _assign_path(
                    payload,
                    logical_fields["prompt"],
                    request.prompt,
                )

            if (
                logical_fields.get("system")
                and request.system_prompt
            ):
                _assign_path(
                    payload,
                    logical_fields["system"],
                    request.system_prompt,
                )

            if (
                logical_fields.get("model")
                and request.model
            ):
                _assign_path(
                    payload,
                    logical_fields["model"],
                    request.model,
                )

            if logical_fields.get(
                "temperature"
            ):
                _assign_path(
                    payload,
                    logical_fields["temperature"],
                    request.temperature,
                )

            if (
                logical_fields.get(
                    "max_tokens"
                )
                and request.max_tokens is not None
            ):
                _assign_path(
                    payload,
                    logical_fields["max_tokens"],
                    request.max_tokens,
                )

            if logical_fields.get(
                "metadata"
            ):
                _assign_path(
                    payload,
                    logical_fields["metadata"],
                    request.metadata,
                )

        return payload

    # ------------------------------------------------------------------
    # RESPONSE
    # ------------------------------------------------------------------

    def extract_output(
        self,
        data: Any,
    ) -> str:

        response_config = (
            self.definition.response
        )

        output_path = response_config.get(
            "output_path",
            "output",
        )

        value = _extract_path(
            data,
            output_path,
        )

        if value is None:

            fallback_paths = (
                response_config.get(
                    "fallback_paths",
                    (),
                )
            )

            for fallback in fallback_paths:

                value = _extract_path(
                    data,
                    fallback,
                )

                if value is not None:
                    break

        return _normalize_output(
            value
        )

    # ------------------------------------------------------------------
    # MODEL DISCOVERY
    # ------------------------------------------------------------------

    async def discover_models(
        self,
        timeout_seconds: Optional[float] = None,
    ) -> List[str]:

        discovery = self.definition.metadata.get(
            "model_discovery"
        )

        if not isinstance(
            discovery,
            Mapping,
        ):
            return list(
                self.definition.models
            )

        discovery_url = discovery.get(
            "endpoint"
        )

        if not discovery_url:
            return list(
                self.definition.models
            )

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

        session = HTTP_CLIENT.session()

        headers = self.build_headers(
            GatewayRequest(
                prompt=""
            )
        )

        params = self.build_query()

        timeout = aiohttp.ClientTimeout(
            total=(
                timeout_seconds
                or self.definition.timeout_seconds
            )
        )

        async with session.request(
            method,
            discovery_url,
            headers=headers,
            params=params,
            timeout=timeout,
        ) as response:

            body = await response.read()

            if response.status >= 400:
                raise ProviderExecutionError(
                    "Model discovery request failed.",
                    provider=self.definition.name,
                    status=response.status,
                    retryable=_is_retryable_status(
                        response.status
                    ),
                    category="model_discovery",
                )

            if len(body) > MAX_RESPONSE_BYTES:
                raise ProviderExecutionError(
                    "Model discovery response is too large.",
                    provider=self.definition.name,
                    category="model_discovery",
                )

            data = json.loads(
                body.decode(
                    "utf-8",
                    errors="replace",
                )
            )

            values = _extract_path(
                data,
                response_path,
                [],
            )

            models: List[str] = []

            if isinstance(
                values,
                list,
            ):

                item_path = discovery.get(
                    "model_path"
                )

                for item in values:

                    if item_path:
                        item = _extract_path(
                            item,
                            item_path,
                        )

                    if item is not None:
                        models.append(
                            str(item)
                        )

            elif isinstance(
                values,
                Mapping,
            ):
                models.extend(
                    str(k)
                    for k in values.keys()
                )

            else:
                normalized = _normalize_output(
                    values
                )

                if normalized:
                    models.append(
                        normalized
                    )

            unique_models = tuple(
                dict.fromkeys(
                    m.strip()
                    for m in models
                    if m
                    and m.strip()
                )
            )

            return list(
                unique_models
            )

    # ------------------------------------------------------------------
    # EXECUTE
    # ------------------------------------------------------------------

    async def invoke(
        self,
        request: GatewayRequest,
    ) -> Tuple[
        str,
        int,
        Optional[str],
        Any,
        int,
        int,
    ]:

        method = str(
            self.definition.metadata.get(
                "http_method",
                "POST",
            )
        ).upper()

        headers = self.build_headers(
            request
        )

        params = self.build_query()

        payload = self.build_payload(
            request
        )

        url = self.definition.endpoint

        timeout = aiohttp.ClientTimeout(
            total=(
                request.timeout_seconds
                or self.definition.timeout_seconds
            )
        )

        session = HTTP_CLIENT.session()

        started = time.perf_counter()

        body_bytes = len(
            json.dumps(
                payload,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        )

        request_kwargs: Dict[str, Any] = {
            "headers": headers,
            "timeout": timeout,
            "params": params,
        }

        if method not in {
            "GET",
            "HEAD",
            "DELETE",
        }:
            request_kwargs["json"] = payload

        async with session.request(
            method,
            url,
            **request_kwargs,
        ) as response:

            status_code = response.status

            raw = await response.read()

            latency_ms = int(
                (
                    time.perf_counter()
                    - started
                ) * 1000
            )

            if len(raw) > MAX_RESPONSE_BYTES:

                raise ProviderExecutionError(
                    "Provider response exceeds gateway limit.",
                    provider=self.definition.name,
                    status=status_code,
                    retryable=False,
                )

            if status_code >= 400:

                text = raw.decode(
                    "utf-8",
                    errors="replace",
                )[:4000]

                raise ProviderExecutionError(
                    (
                        "Dynamic provider request failed: "
                        f"HTTP {status_code}: {text}"
                    ),
                    provider=self.definition.name,
                    status=status_code,
                    retryable=(
                        status_code
                        in RETRYABLE_HTTP_STATUS
                    ),
                    category="provider_http",
                )

            content_type = (
                response.headers.get(
                    "Content-Type",
                    ""
                ).lower()
            )

            if (
                "json"
                in content_type
            ):

                try:
                    data = json.loads(
                        raw.decode(
                            "utf-8",
                            errors="replace",
                        )
                    )

                except json.JSONDecodeError as exc:

                    raise ProviderExecutionError(
                        "Provider returned invalid JSON.",
                        provider=self.definition.name,
                        status=status_code,
                        retryable=False,
                        category="invalid_json",
                    ) from exc

            else:
                data = raw.decode(
                    "utf-8",
                    errors="replace",
                )

            output = self.extract_output(
                data
            )

            if not output:

                raise ProviderExecutionError(
                    (
                        "Provider returned no "
                        "extractable output."
                    ),
                    provider=self.definition.name,
                    status=status_code,
                    retryable=False,
                    category="empty_output",
                )

            request_id = (
                response.headers.get(
                    "x-request-id"
                )
                or response.headers.get(
                    "request-id"
                )
            )

            return (
                output,
                status_code,
                request_id,
                data,
                latency_ms,
                body_bytes,
            )


# ============================================================================
# GATEWAY HANDLE
# ============================================================================

class GatewayHandle:
    """
    Compatibility object returned by get_gateway().

    It intentionally does not lock the caller to a concrete provider.
    """

    def __init__(
        self,
        router: "GatewayRouter",
        service: str,
    ) -> None:
        self._router = router
        self.service = service

    def generate(
        self,
        prompt: str,
        **kwargs: Any,
    ) -> str:

        try:
            asyncio.get_running_loop()

        except RuntimeError:

            result = asyncio.run(
                self.agenerate(
                    prompt,
                    **kwargs,
                )
            )

            return result.output

        raise RuntimeError(
            "Use 'await gateway.agenerate(...)' "
            "inside an active event loop."
        )

    async def agenerate(
        self,
        prompt: str,
        **kwargs: Any,
    ) -> GatewayResponse:

        kwargs.setdefault(
            "service",
            self.service,
        )

        return await self._router.generate(
            prompt,
            **kwargs,
        )


# ============================================================================
# MAIN ROUTER
# ============================================================================

class GatewayRouter:
    """
    Dynamic AI/API Gateway.

    Public responsibilities:
    - runtime provider registration
    - runtime provider replacement/removal
    - dynamic route selection
    - health-aware load balancing
    - capability matching
    - model matching
    - service matching
    - rate-limit awareness
    - cost-aware routing
    - circuit-breaker execution
    - failover
    - shared HTTP connection pool
    - model discovery cache
    - safe diagnostics
    - compatibility API for legacy callers
    """

    _legacy_vault: Dict[
        str,
        List[Mapping[str, Any]],
    ] = {}

    def __init__(
        self,
        *,
        max_global_concurrency: int = 64,
    ) -> None:

        self.router = APIRouter()

        self.system_start_time = time.time()

        self.active_connections = 0

        self.total_requests = 0

        self.successful_requests = 0

        self.failed_requests = 0

        self._providers: Dict[
            str,
            ProviderSlot,
        ] = {}

        self._services: Dict[
            str,
            set[str],
        ] = {}

        self._custom_adapters: Dict[
            str,
            Type[GatewayCustomAdapter],
        ] = {}

        self._registry_lock = threading.RLock()

        self._global_semaphore = asyncio.Semaphore(
            max(
                1,
                max_global_concurrency,
            )
        )

        self._register_routes()

        self._load_environment_config()

        logger.info(
            "Dynamic API Gateway initialized."
        )

    # ======================================================================
    # ROUTES
    # ======================================================================

    def _register_routes(self) -> None:

        @self.router.get(
            "/health",
            tags=["System Core"],
        )
        async def health() -> Dict[str, Any]:

            snapshot = (
                self._health_snapshot()
            )

            eligible = sum(
                1
                for value
                in snapshot.values()
                if value["eligible"]
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
                    time.time()
                    - self.system_start_time,
                    2,
                ),
                "providers_total": len(
                    snapshot
                ),
                "providers_eligible": eligible,
                "active_connections": (
                    self.active_connections
                ),
                "total_requests": (
                    self.total_requests
                ),
                "successful_requests": (
                    self.successful_requests
                ),
                "failed_requests": (
                    self.failed_requests
                ),
                "providers": snapshot,
            }

        @self.router.get(
            "/providers",
            tags=["System Core"],
        )
        async def providers() -> Dict[str, Any]:

            return {
                "providers": (
                    self._health_snapshot()
                )
            }

        @self.router.get(
            "/routes",
            tags=["System Core"],
        )
        async def routes() -> Dict[str, Any]:

            with self._registry_lock:
                return {
                    service: sorted(
                        providers
                    )
                    for service, providers
                    in self._services.items()
                }

        @self.router.post(
            "/discover/{provider_name}",
            tags=["System Core"],
        )
        async def discover(
            provider_name: str,
        ) -> JSONResponse:

            result = await self.discover_models(
                provider_name
            )

            return JSONResponse(
                content={
                    "provider": provider_name,
                    "models": result,
                }
            )

    def get_router(self) -> APIRouter:
        return self.router

    # ======================================================================
    # ENVIRONMENT CONFIG
    # ======================================================================

    def _load_environment_config(
        self,
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

        if not isinstance(
            parsed,
            list,
        ):
            logger.error(
                "RIOT_PROVIDERS_JSON must be a JSON list."
            )
            return

        for item in parsed:

            if not isinstance(
                item,
                Mapping,
            ):
                continue

            try:
                self.register_provider(
                    item
                )

            except Exception as exc:

                logger.error(
                    "Dynamic provider registration failed: %s",
                    exc,
                )

    # ======================================================================
    # CUSTOM ADAPTERS
    # ======================================================================

    def register_custom_adapter(
        self,
        protocol: str,
        adapter: Type[GatewayCustomAdapter],
    ) -> None:

        normalized = str(
            protocol
        ).strip().lower()

        if not normalized:
            raise ValueError(
                "Protocol cannot be empty."
            )

        self._custom_adapters[
            normalized
        ] = adapter

    # ======================================================================
    # PROVIDER REGISTRATION
    # ======================================================================

    def register_provider(
        self,
        definition: Union[
            DynamicProviderDefinition,
            Mapping[str, Any],
        ],
    ) -> None:

        if isinstance(
            definition,
            DynamicProviderDefinition,
        ):
            item = definition

        else:
            item = self._definition_from_mapping(
                definition
            )

        if not item.name.strip():
            raise DynamicConfigurationError(
                "Provider name is required."
            )

        if not item.endpoint.strip():
            raise DynamicConfigurationError(
                "Provider endpoint is required.",
                provider=item.name,
            )

        if item.max_concurrency < 1:
            raise DynamicConfigurationError(
                "max_concurrency must be >= 1",
                provider=item.name,
            )

        if item.timeout_seconds <= 0:
            raise DynamicConfigurationError(
                "timeout_seconds must be > 0",
                provider=item.name,
            )

        item.protocol = str(
            item.protocol
        ).strip().lower()

        if item.weight <= 0:
            item.weight = 0.01

        runtime = ProviderRuntime()

        runtime.semaphore = asyncio.Semaphore(
            item.max_concurrency
        )

        slot = ProviderSlot(
            definition=item,
            runtime=runtime,
            generation=int(
                time.time_ns()
            ),
        )

        with self._registry_lock:

            self._remove_provider_unlocked(
                item.name
            )

            self._providers[
                item.name
            ] = slot

            self._bind_services_unlocked(
                item
            )

        # Register circuit state.
        if CIRCUIT_REGISTRY is not None:
            CIRCUIT_REGISTRY.register(
                item.name
            )

        # Register a generic Provider SDK adapter too.
        self._sync_with_provider_sdk(
            item
        )

        logger.info(
            "Dynamic provider registered: %s",
            item.name,
        )

    def register_providers(
        self,
        definitions: Iterable[
            Union[
                DynamicProviderDefinition,
                Mapping[str, Any],
            ]
        ],
    ) -> None:

        for definition in definitions:
            self.register_provider(
                definition
            )

    def unregister_provider(
        self,
        provider: str,
    ) -> bool:

        with self._registry_lock:

            existed = (
                provider
                in self._providers
            )

            self._remove_provider_unlocked(
                provider
            )

        if (
            existed
            and PROVIDER_REGISTRY is not None
        ):
            with contextlib.suppress(
                Exception
            ):
                PROVIDER_REGISTRY.unregister(
                    provider
                )

        return existed

    def _remove_provider_unlocked(
        self,
        provider: str,
    ) -> None:

        self._providers.pop(
            provider,
            None,
        )

        for service in list(
            self._services
        ):

            self._services[
                service
            ].discard(provider)

            if not self._services[
                service
            ]:
                self._services.pop(
                    service,
                    None,
                )

    def _bind_services_unlocked(
        self,
        definition: DynamicProviderDefinition,
    ) -> None:

        for service in (
            definition.services
        ):

            self._services.setdefault(
                service,
                set(),
            ).add(
                definition.name
            )

    # ======================================================================
    # CONFIG NORMALIZATION
    # ======================================================================

    def _definition_from_mapping(
        self,
        value: Mapping[str, Any],
    ) -> DynamicProviderDefinition:

        name = str(
            value.get(
                "name",
                "",
            )
        ).strip()

        endpoint = str(
            value.get(
                "endpoint",
                "",
            )
        ).strip()

        capabilities = value.get(
            "capabilities",
            (),
        )

        services = value.get(
            "services",
            (),
        )

        models = value.get(
            "models",
            (),
        )

        return DynamicProviderDefinition(
            name=name,
            endpoint=endpoint,
            protocol=str(
                value.get(
                    "protocol",
                    GatewayProtocol.REST.value,
                )
            ).lower(),
            enabled=bool(
                value.get(
                    "enabled",
                    True,
                )
            ),
            timeout_seconds=_safe_float(
                value.get(
                    "timeout_seconds",
                    DEFAULT_TIMEOUT_SECONDS,
                ),
                DEFAULT_TIMEOUT_SECONDS,
            ),
            max_concurrency=int(
                value.get(
                    "max_concurrency",
                    DEFAULT_MAX_CONCURRENCY,
                )
            ),
            priority=int(
                value.get(
                    "priority",
                    100,
                )
            ),
            weight=max(
                0.01,
                _safe_float(
                    value.get(
                        "weight",
                        1.0,
                    ),
                    1.0,
                ),
            ),
            model=(
                str(
                    value["model"]
                )
                if value.get("model")
                is not None
                else None
            ),
            models=tuple(
                str(item)
                for item in (
                    models
                    if isinstance(
                        models,
                        Iterable,
                    )
                    and not isinstance(
                        models,
                        (str, bytes),
                    )
                    else ()
                )
            ),
            capabilities=frozenset(
                str(item)
                for item in (
                    capabilities
                    if isinstance(
                        capabilities,
                        Iterable,
                    )
                    and not isinstance(
                        capabilities,
                        (str, bytes),
                    )
                    else ()
                )
            ),
            services=frozenset(
                str(item)
                for item in (
                    services
                    if isinstance(
                        services,
                        Iterable,
                    )
                    and not isinstance(
                        services,
                        (str, bytes),
                    )
                    else ()
                )
            ),
            cost_per_1k_tokens=(
                _safe_float(
                    value.get(
                        "cost_per_1k_tokens"
                    ),
                    0.0,
                )
                if value.get(
                    "cost_per_1k_tokens"
                )
                is not None
                else None
            ),
            rate_limit_per_minute=(
                _safe_int(
                    value.get(
                        "rate_limit_per_minute"
                    )
                )
            ),
            authentication=dict(
                value.get(
                    "authentication",
                    {},
                )
            ),
            payload=dict(
                value.get(
                    "payload",
                    {},
                )
            ),
            response=dict(
                value.get(
                    "response",
                    {},
                )
            ),
            headers={
                str(k): str(v)
                for k, v
                in dict(
                    value.get(
                        "headers",
                        {},
                    )
                ).items()
            },
            metadata=dict(
                value.get(
                    "metadata",
                    {},
                )
            ),
        )

    # ======================================================================
    # PROVIDER SDK SYNCHRONIZATION
    # ======================================================================

    def _sync_with_provider_sdk(
        self,
        definition: DynamicProviderDefinition,
    ) -> None:

        if (
            not PROVIDER_SDK_AVAILABLE
            or PROVIDER_REGISTRY is None
            or ProviderConfiguration is None
        ):
            return

        try:

            sdk_protocol = self._sdk_protocol(
                definition.protocol
            )

            auth = self._build_sdk_auth(
                definition
            )

            payload = self._build_sdk_payload(
                definition
            )

            response = self._build_sdk_response(
                definition
            )

            configuration = ProviderConfiguration(
                name=definition.name,
                endpoint=definition.endpoint,
                protocol=sdk_protocol,
                enabled=definition.enabled,
                timeout_seconds=int(
                    definition.timeout_seconds
                ),
                authentication=auth,
                payload=payload,
                response=response,
                default_headers=dict(
                    definition.headers
                ),
                metadata={
                    **definition.metadata,
                    "dynamic_gateway": True,
                    "model": definition.model,
                    "models": list(
                        definition.models
                    ),
                    "capabilities": list(
                        definition.capabilities
                    ),
                    "services": list(
                        definition.services
                    ),
                    "priority": definition.priority,
                    "weight": definition.weight,
                },
            )

            adapter_cls = (
                definition.adapter_class
            )

            if adapter_cls is None:

                adapter_cls = (
                    DynamicSDKAdapter
                )

            PROVIDER_REGISTRY.register(
                configuration,
                adapter_cls,
            )

        except Exception as exc:

            logger.warning(
                "Provider SDK sync skipped for '%s': %s",
                definition.name,
                exc,
            )

    @staticmethod
    def _sdk_protocol(
        protocol: str,
    ) -> Any:

        value = str(
            protocol
        ).lower()

        if value == GatewayProtocol.OPENAI_COMPATIBLE.value:

            return ProviderProtocol.OPENAI_COMPATIBLE

        if value == GatewayProtocol.CUSTOM.value:

            return ProviderProtocol.CUSTOM

        return ProviderProtocol.REST

    @staticmethod
    def _build_sdk_auth(
        definition: DynamicProviderDefinition,
    ) -> Any:

        auth = definition.authentication

        auth_type = str(
            auth.get(
                "type",
                "none",
            )
        ).lower()

        mapping = {
            "none": AuthenticationType.NONE,
            "bearer": AuthenticationType.BEARER,
            "api_key_header": (
                AuthenticationType.API_KEY_HEADER
            ),
            "api_key_query": (
                AuthenticationType.API_KEY_QUERY
            ),
            "custom": AuthenticationType.CUSTOM,
        }

        return AuthenticationConfig(
            type=mapping.get(
                auth_type,
                AuthenticationType.NONE,
            ),
            token=(
                str(
                    auth["token"]
                )
                if auth.get(
                    "token"
                ) is not None
                else None
            ),
            header_name=str(
                auth.get(
                    "header_name",
                    "Authorization",
                )
            ),
            query_name=str(
                auth.get(
                    "query_name",
                    "key",
                )
            ),
            prefix=str(
                auth.get(
                    "prefix",
                    "Bearer",
                )
            ),
            custom_headers={
                str(k): str(v)
                for k, v
                in dict(
                    auth.get(
                        "custom_headers",
                        {},
                    )
                ).items()
            },
        )

    @staticmethod
    def _build_sdk_payload(
        definition: DynamicProviderDefinition,
    ) -> Any:

        p = definition.payload

        return PayloadMapping(
            prompt_field=str(
                p.get(
                    "prompt_field",
                    "prompt",
                )
            ),
            system_field=(
                str(
                    p["system_field"]
                )
                if p.get(
                    "system_field"
                ) is not None
                else None
            ),
            temperature_field=(
                str(
                    p["temperature_field"]
                )
                if p.get(
                    "temperature_field"
                ) is not None
                else None
            ),
            max_tokens_field=(
                str(
                    p["max_tokens_field"]
                )
                if p.get(
                    "max_tokens_field"
                ) is not None
                else None
            ),
            metadata_field=(
                str(
                    p["metadata_field"]
                )
                if p.get(
                    "metadata_field"
                ) is not None
                else None
            ),
            fixed_fields=dict(
                p.get(
                    "fixed_fields",
                    {},
                )
            ),
        )

    @staticmethod
    def _build_sdk_response(
        definition: DynamicProviderDefinition,
    ) -> Any:

        path = definition.response.get(
            "output_path",
            ("output",),
        )

        if isinstance(
            path,
            str,
        ):
            path = tuple(
                _normalize_path(path)
            )

        return ResponseMapping(
            output_path=tuple(
                path
            )
        )


# ============================================================================
# SDK ADAPTER
# ============================================================================

class DynamicSDKAdapter(
    ProviderAdapter
):
    """
    Adapter registered into provider_sdk.

    It still executes through configuration.
    No vendor branches are present.
    """

    async def invoke(
        self,
        request: PromptRequest,
    ) -> ProviderResponse:

        configuration = (
            self.configuration
        )

        definition = (
            DynamicProviderDefinition(
                name=configuration.name,
                endpoint=configuration.endpoint,
                protocol=str(
                    configuration.protocol.value
                    if hasattr(
                        configuration.protocol,
                        "value",
                    )
                    else configuration.protocol
                ),
                enabled=configuration.enabled,
                timeout_seconds=(
                    configuration.timeout_seconds
                ),
                authentication={
                    "type": (
                        configuration.authentication.type.value
                        if hasattr(
                            configuration.authentication.type,
                            "value",
                        )
                        else configuration.authentication.type
                    ),
                    "token": (
                        configuration.authentication.token
                    ),
                    "header_name": (
                        configuration.authentication.header_name
                    ),
                    "query_name": (
                        configuration.authentication.query_name
                    ),
                    "prefix": (
                        configuration.authentication.prefix
                    ),
                    "custom_headers": dict(
                        configuration.authentication.custom_headers
                    ),
                },
                payload={
                    "prompt_field": (
                        configuration.payload.prompt_field
                    ),
                    "system_field": (
                        configuration.payload.system_field
                    ),
                    "temperature_field": (
                        configuration.payload.temperature_field
                    ),
                    "max_tokens_field": (
                        configuration.payload.max_tokens_field
                    ),
                    "metadata_field": (
                        configuration.payload.metadata_field
                    ),
                    "fixed_fields": dict(
                        configuration.payload.fixed_fields
                    ),
                },
                response={
                    "output_path": tuple(
                        configuration.response.output_path
                    )
                },
                headers=dict(
                    configuration.default_headers
                ),
                metadata=dict(
                    configuration.metadata
                ),
            )
        )

        adapter = DynamicHTTPAdapter(
            definition
        )

        gateway_request = GatewayRequest(
            prompt=request.prompt,
            system_prompt=request.system_prompt,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            metadata=dict(
                request.metadata
            ),
        )

        (
            output,
            status_code,
            request_id,
            raw_response,
            latency_ms,
            _,
        ) = await adapter.invoke(
            gateway_request
        )

        return ProviderResponse(
            success=True,
            provider=configuration.name,
            output=output,
            raw_response=raw_response,
            metadata={
                "status_code": status_code,
                "request_id": request_id,
                "latency_ms": latency_ms,
            },
        )

    def build_headers(
        self,
    ) -> Dict[str, str]:

        configuration = self.configuration

        definition = DynamicProviderDefinition(
            name=configuration.name,
            endpoint=configuration.endpoint,
            authentication={
                "type": (
                    configuration.authentication.type.value
                    if hasattr(
                        configuration.authentication.type,
                        "value",
                    )
                    else configuration.authentication.type
                ),
                "token": (
                    configuration.authentication.token
                ),
                "header_name": (
                    configuration.authentication.header_name
                ),
                "prefix": (
                    configuration.authentication.prefix
                ),
                "custom_headers": dict(
                    configuration.authentication.custom_headers
                ),
            },
            headers=dict(
                configuration.default_headers
            ),
        )

        return DynamicHTTPAdapter(
            definition
        ).build_headers(
            GatewayRequest(
                prompt=""
            )
        )

    def build_payload(
        self,
        request: PromptRequest,
    ) -> Dict[str, Any]:

        configuration = self.configuration

        definition = DynamicProviderDefinition(
            name=configuration.name,
            endpoint=configuration.endpoint,
            payload={
                "prompt_field": (
                    configuration.payload.prompt_field
                ),
                "system_field": (
                    configuration.payload.system_field
                ),
                "temperature_field": (
                    configuration.payload.temperature_field
                ),
                "max_tokens_field": (
                    configuration.payload.max_tokens_field
                ),
                "metadata_field": (
                    configuration.payload.metadata_field
                ),
                "fixed_fields": dict(
                    configuration.payload.fixed_fields
                ),
            },
        )

        return DynamicHTTPAdapter(
            definition
        ).build_payload(
            GatewayRequest(
                prompt=request.prompt,
                system_prompt=request.system_prompt,
                temperature=request.temperature,
                max_tokens=request.max_tokens,
                metadata=dict(
                    request.metadata
                ),
            )
        )


# ============================================================================
# GATEWAY ROUTER METHODS
# ============================================================================

async def _gateway_generate(
    self: GatewayRouter,
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
    timeout_seconds: Optional[
        float
    ] = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    routing_mode: Union[
        RoutingMode,
        str
    ] = RoutingMode.BALANCED,
    excluded_providers: Optional[
        Iterable[str]
    ] = None,
) -> GatewayResponse:

    request = GatewayRequest(
        prompt=prompt,
        system_prompt=system_prompt,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        service=service,
        required_capabilities=frozenset(
            str(x)
            for x in (
                required_capabilities
                or ()
            )
        ),
        metadata=dict(
            metadata or {}
        ),
        timeout_seconds=timeout_seconds,
        max_attempts=max_attempts,
        routing_mode=(
            routing_mode
            if isinstance(
                routing_mode,
                RoutingMode,
            )
            else RoutingMode(
                str(
                    routing_mode
                ).lower()
            )
        ),
        excluded_providers=frozenset(
            str(x)
            for x in (
                excluded_providers
                or ()
            )
        ),
    )

    self._validate_request(
        request
    )

    if HTTP_CLIENT is None:
        raise GatewayError(
            "Shared HTTP client is unavailable.",
            category="infrastructure",
        )

    if not HTTP_CLIENT.initialized():
        await HTTP_CLIENT.startup()

    candidates = self._candidate_slots(
        request
    )

    if not candidates:
        raise ProviderUnavailableError(
            (
                "No eligible provider matches "
                "the requested routing constraints."
            ),
            category="routing",
        )

    last_error: Optional[
        Exception
    ] = None

    attempted: set[str] = set()

    for attempt in range(
        1,
        request.max_attempts + 1,
    ):

        candidates = [
            candidate
            for candidate
            in self._candidate_slots(
                request
            )
            if (
                candidate.definition.name
                not in attempted
                or len(candidates) == 1
            )
        ]

        if not candidates:
            candidates = self._candidate_slots(
                request
            )

        if not candidates:
            break

        slot = self._select_slot(
            candidates,
            request.routing_mode,
        )

        attempted.add(
            slot.definition.name
        )

        started = time.perf_counter()

        self.total_requests += 1

        try:

            result = await self._execute_slot(
                slot,
                request,
            )

            elapsed_ms = (
                time.perf_counter()
                - started
            ) * 1000.0

            await self._record_success(
                slot,
                elapsed_ms,
            )

            self.successful_requests += 1

            return GatewayResponse(
                success=True,
                output=result[0],
                provider=slot.definition.name,
                model=(
                    request.model
                    or slot.definition.model
                ),
                request_id=(
                    result[2]
                    or request.request_id
                ),
                latency_ms=round(
                    elapsed_ms,
                    2,
                ),
                attempts=attempt,
                status_code=result[1],
                raw_response=result[3],
                metadata={
                    "service": service,
                    "protocol": (
                        slot.definition.protocol
                    ),
                    "routing_mode": (
                        request.routing_mode.value
                    ),
                    "attempt": attempt,
                    "key_fingerprint": (
                        _fingerprint(
                            str(
                                slot.definition.authentication.get(
                                    "token",
                                    ""
                                )
                            )
                        )
                    ),
                    **request.metadata,
                },
            )

        except GatewayError as exc:

            last_error = exc

            elapsed_ms = (
                time.perf_counter()
                - started
            ) * 1000.0

            await self._record_failure(
                slot,
                exc,
                elapsed_ms,
            )

            self.failed_requests += 1

            if (
                exc.status
                in NON_RETRYABLE_AUTH_STATUS
            ):
                continue

            if exc.retryable:
                if (
                    attempt
                    < request.max_attempts
                ):

                    await self._backoff(
                        attempt
                    )

                    continue

            if attempt < request.max_attempts:

                await self._backoff(
                    attempt
                )

                continue

            raise

        except Exception as exc:

            last_error = exc

            elapsed_ms = (
                time.perf_counter()
                - started
            ) * 1000.0

            wrapped = ProviderExecutionError(
                str(exc),
                provider=slot.definition.name,
                retryable=True,
                category="unexpected",
            )

            await self._record_failure(
                slot,
                wrapped,
                elapsed_ms,
            )

            self.failed_requests += 1

            if (
                attempt
                < request.max_attempts
            ):

                await self._backoff(
                    attempt
                )

                continue

    if isinstance(
        last_error,
        GatewayError,
    ):
        raise last_error

    raise ProviderUnavailableError(
        "All dynamic gateway attempts failed.",
        category="exhausted",
    )


# Attach method to keep the source organized.
GatewayRouter.generate = _gateway_generate  # type: ignore[attr-defined]


# ============================================================================
# REQUEST VALIDATION
# ============================================================================

def _validate_request(
    self: GatewayRouter,
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

    if len(
        request.prompt
    ) > MAX_PROMPT_LENGTH:

        raise GatewayValidationError(
            "Prompt exceeds gateway input limit."
        )

    if not 0.0 <= request.temperature <= 2.0:

        raise GatewayValidationError(
            "temperature must be between 0 and 2."
        )

    if (
        request.max_tokens is not None
        and request.max_tokens <= 0
    ):

        raise GatewayValidationError(
            "max_tokens must be positive."
        )

    if request.max_attempts < 1:

        raise GatewayValidationError(
            "max_attempts must be >= 1."
        )


GatewayRouter._validate_request = (
    _validate_request
)


# ============================================================================
# CANDIDATE FILTER
# ============================================================================

def _candidate_slots(
    self: GatewayRouter,
    request: GatewayRequest,
) -> List[ProviderSlot]:

    now = time.time()

    result: List[ProviderSlot] = []

    with self._registry_lock:

        for name, slot in self._providers.items():

            definition = slot.definition
            runtime = slot.runtime

            if not definition.enabled:
                continue

            if name in request.excluded_providers:
                continue

            if runtime.cooldown_until > now:
                continue

            # --------------------------------------------------------------
            # Service filter
            # --------------------------------------------------------------

            if (
                definition.services
                and request.service
                and request.service
                not in definition.services
            ):
                continue

            # --------------------------------------------------------------
            # Capability filter
            # --------------------------------------------------------------

            if (
                request.required_capabilities
                and not request.required_capabilities.issubset(
                    definition.capabilities
                )
            ):
                continue

            # --------------------------------------------------------------
            # Model filter
            # --------------------------------------------------------------

            if request.model:

                available_models = set(
                    definition.models
                )

                if definition.model:
                    available_models.add(
                        definition.model
                    )

                # Empty model inventory means that this provider has not
                # declared model restrictions.
                if (
                    available_models
                    and request.model
                    not in available_models
                ):
                    continue

            # --------------------------------------------------------------
            # Circuit filter
            # --------------------------------------------------------------

            if CIRCUIT_REGISTRY is not None:

                breaker = (
                    CIRCUIT_REGISTRY.get(
                        name
                    )
                )

                health = breaker.snapshot()

                if (
                    getattr(
                        health,
                        "opened_until",
                        0.0,
                    )
                    > now
                    and str(
                        getattr(
                            health,
                            "state",
                            ""
                        )
                    ).lower().endswith(
                        "open"
                    )
                ):
                    continue

            result.append(
                slot
            )

    return result


GatewayRouter._candidate_slots = (
    _candidate_slots
)


# ============================================================================
# ROUTING SCORE
# ============================================================================

def _routing_score(
    self: GatewayRouter,
    slot: ProviderSlot,
    mode: RoutingMode,
) -> float:

    definition = slot.definition
    runtime = slot.runtime

    health_score = 1.0

    latency_factor = 1.0

    reliability_factor = 1.0

    utilization_factor = 1.0

    cost_factor = 1.0

    rate_limit_factor = 1.0

    circuit_factor = 1.0

    # ------------------------------------------------------------------
    # Circuit / health
    # ------------------------------------------------------------------

    if CIRCUIT_REGISTRY is not None:

        breaker = (
            CIRCUIT_REGISTRY.get(
                definition.name
            )
        )

        health = breaker.snapshot()

        health_score = max(
            0.01,
            min(
                1.0,
                _safe_float(
                    getattr(
                        health,
                        "score",
                        100.0,
                    ),
                    100.0,
                ) / 100.0,
            ),
        )

        state = str(
            getattr(
                health,
                "state",
                ""
            )
        ).lower()

        if state.endswith(
            "half_open"
        ):
            circuit_factor = 0.35

    # ------------------------------------------------------------------
    # Latency
    # ------------------------------------------------------------------

    latency = (
        runtime.ema_latency_ms
        or runtime.last_latency_ms
        or 250.0
    )

    latency_factor = 1.0 / (
        1.0 + (
            latency / 1000.0
        )
    )

    # ------------------------------------------------------------------
    # Reliability
    # ------------------------------------------------------------------

    total = (
        runtime.total_requests
    )

    if total > 0:

        failure_rate = (
            runtime.failed_requests
            / max(
                1,
                total,
            )
        )

        reliability_factor = max(
            0.05,
            1.0 - failure_rate,
        )

    # ------------------------------------------------------------------
    # Concurrency utilization
    # ------------------------------------------------------------------

    concurrency_limit = max(
        1,
        definition.max_concurrency,
    )

    utilization = (
        runtime.in_flight
        / concurrency_limit
    )

    utilization_factor = max(
        0.05,
        1.0 - min(
            1.0,
            utilization,
        ),
    )

    # ------------------------------------------------------------------
    # Rate-limit signal
    # ------------------------------------------------------------------

    if (
        runtime.estimated_rate_remaining
        is not None
        and definition.rate_limit_per_minute
    ):

        ratio = (
            runtime.estimated_rate_remaining
            / max(
                1,
                definition.rate_limit_per_minute,
            )
        )

        rate_limit_factor = max(
            0.05,
            min(
                1.0,
                ratio,
            ),
        )

    # ------------------------------------------------------------------
    # Cost signal
    # ------------------------------------------------------------------

    if (
        definition.cost_per_1k_tokens
        is not None
    ):

        cost = max(
            0.000001,
            definition.cost_per_1k_tokens,
        )

        cost_factor = 1.0 / (
            1.0 + cost
        )

    # ------------------------------------------------------------------
    # Routing mode
    # ------------------------------------------------------------------

    if mode == RoutingMode.LOW_LATENCY:

        base = (
            latency_factor * 0.50
            + health_score * 0.25
            + reliability_factor * 0.15
            + utilization_factor * 0.10
        )

    elif mode == RoutingMode.LOW_COST:

        base = (
            cost_factor * 0.45
            + health_score * 0.25
            + reliability_factor * 0.15
            + latency_factor * 0.15
        )

    elif mode == RoutingMode.HIGH_RELIABILITY:

        base = (
            health_score * 0.40
            + reliability_factor * 0.35
            + circuit_factor * 0.15
            + latency_factor * 0.10
        )

    elif mode == RoutingMode.PRIORITY:

        priority_factor = 1.0 / (
            1.0
            + max(
                0,
                definition.priority,
            )
            / 100.0
        )

        base = (
            priority_factor * 0.35
            + health_score * 0.25
            + reliability_factor * 0.20
            + latency_factor * 0.20
        )

    else:

        base = (
            health_score * 0.25
            + latency_factor * 0.20
            + reliability_factor * 0.20
            + utilization_factor * 0.15
            + rate_limit_factor * 0.10
            + cost_factor * 0.10
        )

    return max(
        0.0001,
        base
        * max(
            0.01,
            definition.weight,
        )
        * (
            1.0
            + min(
                5.0,
                100.0
                / max(
                    1,
                    definition.priority,
                ),
            )
            * 0.02
        ),
    )


GatewayRouter._routing_score = (
    _routing_score
)


# ============================================================================
# DYNAMIC SELECTION
# ============================================================================

def _select_slot(
    self: GatewayRouter,
    candidates: Sequence[ProviderSlot],
    mode: RoutingMode,
) -> ProviderSlot:

    if not candidates:
        raise ProviderUnavailableError(
            "No candidate providers available.",
            category="routing",
        )

    weighted: List[
        Tuple[
            ProviderSlot,
            float,
        ]
    ] = [
        (
            slot,
            self._routing_score(
                slot,
                mode,
            ),
        )
        for slot in candidates
    ]

    total_weight = sum(
        score
        for _, score in weighted
    )

    if (
        not math.isfinite(
            total_weight
        )
        or total_weight <= 0
    ):
        return min(
            candidates,
            key=lambda item: (
                item.runtime.in_flight,
                item.definition.priority,
            ),
        )

    target = (
        random.random()
        * total_weight
    )

    cursor = 0.0

    for slot, score in weighted:

        cursor += score

        if target <= cursor:
            return slot

    return weighted[-1][0]


GatewayRouter._select_slot = (
    _select_slot
)


# ============================================================================
# EXECUTION
# ============================================================================

async def _execute_slot(
    self: GatewayRouter,
    slot: ProviderSlot,
    request: GatewayRequest,
) -> Tuple[
    str,
    int,
    Optional[str],
    Any,
    int,
    int,
]:

    runtime = slot.runtime

    if runtime.semaphore is None:

        runtime.semaphore = asyncio.Semaphore(
            max(
                1,
                slot.definition.max_concurrency,
            )
        )

    async with self._global_semaphore:

        async with runtime.semaphore:

            runtime.in_flight += 1
            self.active_connections += 1

            try:

                return await self._execute_with_circuit(
                    slot,
                    request,
                )

            finally:

                runtime.in_flight = max(
                    0,
                    runtime.in_flight - 1,
                )

                self.active_connections = max(
                    0,
                    self.active_connections - 1,
                )


GatewayRouter._execute_slot = (
    _execute_slot
)


async def _execute_with_circuit(
    self: GatewayRouter,
    slot: ProviderSlot,
    request: GatewayRequest,
) -> Tuple[
    str,
    int,
    Optional[str],
    Any,
    int,
    int,
]:

    definition = slot.definition

    # ---------------------------------------------------------------
    # Custom protocol
    # ---------------------------------------------------------------

    custom_adapter_cls = (
        self._custom_adapters.get(
            definition.protocol
        )
    )

    if (
        definition.protocol
        == GatewayProtocol.CUSTOM.value
        and custom_adapter_cls is not None
    ):

        adapter = (
            custom_adapter_cls()
        )

        async def operation():
            response = await adapter.invoke(
                definition,
                request,
            )

            return (
                response.output,
                200,
                response.metadata.get(
                    "request_id"
                ),
                response.raw_response,
                _safe_float(
                    response.metadata.get(
                        "latency_ms"
                    ),
                    0.0,
                ),
                0,
            )

        if PROVIDER_EXECUTOR is None:
            return await operation()

        return await PROVIDER_EXECUTOR.execute(
            definition.name,
            operation,
        )

    # ---------------------------------------------------------------
    # Generic dynamic protocol
    # ---------------------------------------------------------------

    adapter = DynamicHTTPAdapter(
        definition
    )

    async def operation():

        return await adapter.invoke(
            request
        )

    if PROVIDER_EXECUTOR is None:

        return await operation()

    try:

        return await asyncio.wait_for(
            PROVIDER_EXECUTOR.execute(
                definition.name,
                operation,
            ),
            timeout=(
                request.timeout_seconds
                or definition.timeout_seconds
            ) + 3.0,
        )

    except asyncio.TimeoutError as exc:

        raise ProviderExecutionError(
            "Provider execution timeout.",
            provider=definition.name,
            retryable=True,
            category="timeout",
        ) from exc


GatewayRouter._execute_with_circuit = (
    _execute_with_circuit
)


# ============================================================================
# SUCCESS / FAILURE RECORDING
# ============================================================================

async def _record_success(
    self: GatewayRouter,
    slot: ProviderSlot,
    latency_ms: float,
) -> None:

    runtime = slot.runtime

    async with runtime.lock:

        runtime.total_requests += 1

        runtime.successful_requests += 1

        runtime.last_latency_ms = (
            latency_ms
        )

        if runtime.ema_latency_ms <= 0:

            runtime.ema_latency_ms = (
                latency_ms
            )

        else:

            runtime.ema_latency_ms = (
                runtime.ema_latency_ms * 0.8
                + latency_ms * 0.2
            )

        runtime.last_success_at = (
            time.time()
        )

        runtime.consecutive_successes += 1

        runtime.consecutive_failures = 0

        runtime.cooldown_until = 0.0

        runtime.last_error = None


GatewayRouter._record_success = (
    _record_success
)


async def _record_failure(
    self: GatewayRouter,
    slot: ProviderSlot,
    error: GatewayError,
    latency_ms: float,
) -> None:

    runtime = slot.runtime

    async with runtime.lock:

        runtime.total_requests += 1

        runtime.failed_requests += 1

        runtime.last_latency_ms = (
            latency_ms
        )

        if runtime.ema_latency_ms <= 0:

            runtime.ema_latency_ms = (
                latency_ms
            )

        else:

            runtime.ema_latency_ms = (
                runtime.ema_latency_ms * 0.8
                + latency_ms * 0.2
            )

        runtime.last_failure_at = (
            time.time()
        )

        runtime.consecutive_failures += 1

        runtime.consecutive_successes = 0

        runtime.last_error = str(
            error
        )[:1000]

        failure_count = min(
            7,
            runtime.consecutive_failures,
        )

        runtime.cooldown_until = (
            time.time()
            + min(
                30.0,
                0.5
                * (
                    2 ** (
                        failure_count - 1
                    )
                ),
            )
        )


GatewayRouter._record_failure = (
    _record_failure
)


# ============================================================================
# BACKOFF
# ============================================================================

async def _backoff(
    self: GatewayRouter,
    attempt: int,
) -> None:

    base = min(
        8.0,
        0.5
        * (
            2 ** (
                max(
                    0,
                    attempt - 1,
                )
            )
        ),
    )

    jitter = random.uniform(
        0.0,
        0.35,
    )

    await asyncio.sleep(
        base + jitter
    )


GatewayRouter._backoff = (
    _backoff
)


# ============================================================================
# MODEL DISCOVERY
# ============================================================================

async def _discover_models(
    self: GatewayRouter,
    provider: str,
    *,
    force_refresh: bool = False,
) -> List[str]:

    slot = self._providers.get(
        provider
    )

    if slot is None:
        raise ProviderUnavailableError(
            f"Provider '{provider}' is not registered.",
            provider=provider,
        )

    if MODEL_CACHE is not None:

        cached = await MODEL_CACHE.get_models(
            provider
        )

        if cached and not force_refresh:
            return list(
                cached
            )

    adapter = DynamicHTTPAdapter(
        slot.definition
    )

    models = await adapter.discover_models()

    if not models:

        models = list(
            slot.definition.models
        )

        if (
            slot.definition.model
            and slot.definition.model
            not in models
        ):
            models.append(
                slot.definition.model
            )

    if MODEL_CACHE is not None:

        await MODEL_CACHE.set_models(
            provider,
            list(models),
        )

    return list(
        models
    )


GatewayRouter.discover_models = (
    _discover_models
)


# ============================================================================
# HEALTH SNAPSHOT
# ============================================================================

def _health_snapshot(
    self: GatewayRouter,
) -> Dict[str, Any]:

    snapshot: Dict[str, Any] = {}

    now = time.time()

    with self._registry_lock:

        for name, slot in self._providers.items():

            runtime = slot.runtime

            state = "unknown"

            score = 0.0

            circuit_open_until = 0.0

            if CIRCUIT_REGISTRY is not None:

                health = (
                    CIRCUIT_REGISTRY
                    .get(name)
                    .snapshot()
                )

                state = str(
                    getattr(
                        health,
                        "state",
                        "unknown",
                    )
                ).split(".")[-1].lower()

                score = _safe_float(
                    getattr(
                        health,
                        "score",
                        0.0,
                    ),
                    0.0,
                )

                circuit_open_until = (
                    _safe_float(
                        getattr(
                            health,
                            "opened_until",
                            0.0,
                        ),
                        0.0,
                    )
                )

            eligible = (
                slot.definition.enabled
                and runtime.cooldown_until <= now
                and not (
                    state.endswith(
                        "open"
                    )
                    and circuit_open_until > now
                )
            )

            snapshot[name] = {
                "enabled": (
                    slot.definition.enabled
                ),
                "eligible": eligible,
                "protocol": (
                    slot.definition.protocol
                ),
                "endpoint": (
                    slot.definition.endpoint
                ),
                "model": (
                    slot.definition.model
                ),
                "models": list(
                    slot.definition.models
                ),
                "capabilities": sorted(
                    slot.definition.capabilities
                ),
                "services": sorted(
                    slot.definition.services
                ),
                "priority": (
                    slot.definition.priority
                ),
                "weight": (
                    slot.definition.weight
                ),
                "max_concurrency": (
                    slot.definition.max_concurrency
                ),
                "in_flight": (
                    runtime.in_flight
                ),
                "health_score": score,
                "circuit_state": state,
                "circuit_open_until": (
                    circuit_open_until
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
                "avg_latency_ms": round(
                    runtime.ema_latency_ms,
                    2,
                ),
                "rate_remaining": (
                    runtime.estimated_rate_remaining
                ),
                "last_error": (
                    runtime.last_error
                ),
                "credential_fingerprint": (
                    _fingerprint(
                        str(
                            slot.definition.authentication.get(
                                "token",
                                ""
                            )
                        )
                    )
                ),
            }

    return snapshot


GatewayRouter._health_snapshot = (
    _health_snapshot
)


# ============================================================================
# LEGACY VAULT COMPATIBILITY
# ============================================================================

@classmethod
def _load_vault(
    cls,
    multi_api_vault: Mapping[
        str,
        Any,
    ],
) -> None:
    """
    Compatibility entry point.

    IMPORTANT:
    Plain API-key strings are NOT provider-discovered from key prefixes.

    The old gateway used key-prefix guessing.
    That behavior has intentionally been removed.

    Runtime configuration must describe the endpoint/protocol/auth.
    """

    if not isinstance(
        multi_api_vault,
        Mapping,
    ):
        raise DynamicConfigurationError(
            "Vault configuration must be a mapping."
        )

    normalized: Dict[
        str,
        List[Mapping[str, Any]],
    ] = {}

    for service, entries in (
        multi_api_vault.items()
    ):

        service_name = str(
            service
        ).strip()

        if not service_name:
            continue

        if isinstance(
            entries,
            Mapping,
        ):
            entries = [entries]

        if isinstance(
            entries,
            str,
        ):
            raise DynamicConfigurationError(
                (
                    "Plain API-key vault entries are no longer accepted "
                    "because provider discovery from key signatures is "
                    "intentionally disabled."
                )
            )

        if not isinstance(
            entries,
            Sequence,
        ):
            continue

        bucket: List[
            Mapping[str, Any]
        ] = []

        for item in entries:

            if not isinstance(
                item,
                Mapping,
            ):
                continue

            data = dict(
                item
            )

            services = set(
                str(x)
                for x in data.get(
                    "services",
                    (),
                )
            )

            services.add(
                service_name
            )

            data["services"] = list(
                services
            )

            bucket.append(
                data
            )

        if bucket:
            normalized[
                service_name
            ] = bucket

    cls._legacy_vault = normalized


GatewayRouter.load_vault = (
    _load_vault
)


# ============================================================================
# LEGACY GATEWAY FACTORY
# ============================================================================

def _get_gateway(
    self: GatewayRouter,
    service_type: str = "default",
) -> GatewayHandle:

    service = str(
        service_type
    ).strip()

    if not service:
        service = "default"

    # If legacy vault was loaded before this router instance was created,
    # dynamically register those definitions now.
    entries = (
        self._legacy_vault.get(
            service
        )
    )

    if entries:

        for item in entries:

            try:
                self.register_provider(
                    item
                )

            except Exception as exc:

                logger.error(
                    (
                        "Failed to register "
                        "legacy dynamic provider: %s"
                    ),
                    exc,
                )

    if not self._services.get(
        service
    ):

        # A provider without explicit services may still act as a default
        # catch-all provider.
        with self._registry_lock:

            for name, slot in (
                self._providers.items()
            ):

                if (
                    not slot.definition.services
                    and slot.definition.enabled
                ):
                    self._services.setdefault(
                        service,
                        set(),
                    ).add(
                        name
                    )

    if not self._services.get(
        service
    ):
        raise ProviderUnavailableError(
            (
                "No providers are registered "
                f"for service '{service}'."
            ),
            category="routing",
        )

    return GatewayHandle(
        self,
        service,
    )


GatewayRouter.get_gateway = (
    _get_gateway
)


# ============================================================================
# GLOBAL INSTANCE
# ============================================================================

gateway_router = GatewayRouter()


# ============================================================================
# EXPORTS
# ============================================================================

__all__ = [
    "BaseGateway",
    "DynamicProviderDefinition",
    "GatewayCustomAdapter",
    "GatewayError",
    "GatewayHandle",
    "GatewayProtocol",
    "GatewayRequest",
    "GatewayResponse",
    "GatewayRouter",
    "GatewayValidationError",
    "ProviderExecutionError",
    "ProviderUnavailableError",
    "RoutingMode",
    "gateway_router",
]
