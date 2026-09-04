"""
Riot / God Node — Professional Agent Runtime Foundation
=========================================================

Canonical execution substrate for specialist AI agents.

Compatibility
-------------
Existing specialist agents may continue to call:
    super().__init__(role_name="...", service_type="brain")
    await/self.think_and_execute(...)

The base class now:
* uses an injected/process-local GatewayRouter instance correctly;
* never calls provider retry logic itself;
* bounds prompt/context/output sizes;
* supports async and sync gateway implementations;
* validates JSON without fabricating missing data;
* provides structured result envelopes and bounded telemetry;
* supports response-schema validation through Pydantic-like contracts;
* propagates cancellation and hard timeouts;
* supports bounded parallel execution;
* avoids blocking the asyncio event loop on synchronous providers.

It intentionally does not perform hidden second requests when model output is
malformed. Provider retry/failover remains the gateway/provider responsibility.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import re
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Deque, Iterable, Mapping, Optional, Sequence, Type, TypeVar

from core.gateway import GatewayRouter


logger = logging.getLogger("Riot.AgentRuntime")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s - [AGENT-RUNTIME] - %(levelname)s - %(message)s"
        )
    )
    logger.addHandler(handler)
logger.setLevel(os.getenv("RIOT_AGENT_LOG_LEVEL", "INFO").upper())


T = TypeVar("T")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return min(maximum, max(minimum, value))


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return min(maximum, max(minimum, value))


DEFAULT_MAX_PROMPT_CHARS = _env_int(
    "RIOT_AGENT_MAX_PROMPT_CHARS", 180_000, 4_096, 2_000_000
)
DEFAULT_MAX_CONTEXT_CHARS = _env_int(
    "RIOT_AGENT_MAX_CONTEXT_CHARS", 220_000, 4_096, 2_000_000
)
DEFAULT_MAX_OUTPUT_CHARS = _env_int(
    "RIOT_AGENT_MAX_OUTPUT_CHARS", 500_000, 4_096, 4_000_000
)
DEFAULT_TIMEOUT_SECONDS = _env_float(
    "RIOT_AGENT_TIMEOUT_SECONDS", 300.0, 5.0, 1_800.0
)
DEFAULT_HISTORY_SIZE = _env_int("RIOT_AGENT_HISTORY_SIZE", 64, 8, 512)
DEFAULT_MAX_CONCURRENCY = _env_int(
    "RIOT_MAX_AGENT_CONCURRENCY_PER_INSTANCE", 8, 1, 128
)
_DEFAULT_MAX_PARALLEL_BATCH = _env_int(
    "RIOT_AGENT_MAX_PARALLEL_BATCH", 64, 1, 512
)


@dataclass(slots=True, frozen=True)
class AgentRuntimeConfig:
    max_prompt_chars: int = DEFAULT_MAX_PROMPT_CHARS
    max_context_chars: int = DEFAULT_MAX_CONTEXT_CHARS
    max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    history_size: int = DEFAULT_HISTORY_SIZE
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY
    max_parallel_batch: int = _DEFAULT_MAX_PARALLEL_BATCH
    temperature: float = _env_float("RIOT_AGENT_TEMPERATURE", 0.2, 0.0, 2.0)
    max_tokens: Optional[int] = (
        _env_int("RIOT_AGENT_MAX_TOKENS", 12_000, 256, 64_000)
        if os.getenv("RIOT_AGENT_MAX_TOKENS") is not None
        else None
    )


# ---------------------------------------------------------------------------
# Typed-ish execution contracts
# ---------------------------------------------------------------------------

class AgentExecutionStatus(str, Enum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMEOUT = "TIMEOUT"
    INVALID_OUTPUT = "INVALID_OUTPUT"
    GATEWAY_UNAVAILABLE = "GATEWAY_UNAVAILABLE"


@dataclass(slots=True, frozen=True)
class AgentRequest:
    request_id: str
    role_name: str
    service_type: str
    directive: str
    context: Mapping[str, Any]
    system_prompt: str
    temperature: float
    max_tokens: Optional[int]
    timeout_seconds: float
    required_capabilities: frozenset[str]
    metadata: Mapping[str, Any]


@dataclass(slots=True)
class AgentExecutionRecord:
    request_id: str
    role_name: str
    status: AgentExecutionStatus
    started_at: float
    finished_at: float
    duration_ms: float
    context_sha256: str
    output_sha256: Optional[str] = None
    provider: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value
        return payload


@dataclass(slots=True)
class AgentResult:
    status: AgentExecutionStatus
    role: str
    request_id: str
    data: Any = None
    error: Optional[str] = None
    provider: Optional[str] = None
    duration_ms: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status is AgentExecutionStatus.SUCCESS

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "role": self.role,
            "request_id": self.request_id,
            "data": _json_safe(self.data),
            "error": self.error,
            "provider": self.provider,
            "duration_ms": round(self.duration_ms, 3),
            "metadata": _json_safe(self.metadata),
        }


# ---------------------------------------------------------------------------
# Serialization / bounds
# ---------------------------------------------------------------------------

def _json_safe(value: Any, *, max_depth: int = 12, _depth: int = 0) -> Any:
    if _depth > max_depth:
        return "<max-depth>"

    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, Enum):
        return value.value

    if isinstance(value, Mapping):
        return {
            str(key): _json_safe(item, max_depth=max_depth, _depth=_depth + 1)
            for key, item in value.items()
        }

    if isinstance(value, (list, tuple, set, frozenset)):
        return [
            _json_safe(item, max_depth=max_depth, _depth=_depth + 1)
            for item in value
        ]

    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return _json_safe(
                model_dump(mode="json"),
                max_depth=max_depth,
                _depth=_depth + 1,
            )
        except Exception:
            pass

    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return _json_safe(
                to_dict(),
                max_depth=max_depth,
                _depth=_depth + 1,
            )
        except Exception:
            pass

    try:
        return str(value)
    except Exception:
        return repr(value)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _json_safe(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _bounded_text(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else _canonical_json(value)
    if len(text) <= limit:
        return text

    head = max(1, int(limit * 0.72))
    tail = max(1, limit - head - 80)
    return (
        text[:head]
        + "\n...[TRUNCATED BY AGENT INPUT BUDGET]...\n"
        + text[-tail:]
    )


def _normalize_context(context: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    if not context:
        return {}

    safe = _json_safe(context)
    if isinstance(safe, Mapping):
        return {str(key): value for key, value in safe.items()}
    return {"value": safe}


def _extract_response_text(response: Any) -> tuple[str, Optional[str], dict[str, Any]]:
    provider: Optional[str] = None
    metadata: dict[str, Any] = {}

    if response is None:
        return "", provider, metadata

    if isinstance(response, str):
        return response, provider, metadata

    if isinstance(response, Mapping):
        raw_provider = response.get("provider")
        if raw_provider is not None:
            provider = str(raw_provider)

        raw_metadata = response.get("metadata")
        if isinstance(raw_metadata, Mapping):
            metadata = _json_safe(raw_metadata)

        for key in ("output", "text", "content", "result", "data"):
            if key not in response:
                continue
            candidate = response[key]
            if isinstance(candidate, str):
                return candidate, provider, metadata
            if isinstance(candidate, (Mapping, list, tuple)):
                return _canonical_json(candidate), provider, metadata

        return _canonical_json(response), provider, metadata

    for attr in ("output", "text", "content", "result", "data"):
        candidate = getattr(response, attr, None)
        if candidate is not None:
            if isinstance(candidate, str):
                return candidate, provider, metadata
            return _canonical_json(candidate), provider, metadata

    return str(response), provider, metadata


# ---------------------------------------------------------------------------
# Shared gateway wiring
# ---------------------------------------------------------------------------

class _GatewayHolder:
    """
    Process-local gateway holder.

    main.py can inject its singleton gateway to ensure the agent swarm uses the
    same provider registry and circuit/health state as the rest of Riot.
    """

    _gateway: Any = None
    _lock = threading.RLock()

    @classmethod
    def set_gateway(cls, gateway: Any) -> None:
        if gateway is None:
            raise ValueError("gateway cannot be None")
        with cls._lock:
            cls._gateway = gateway

    @classmethod
    def get_gateway(cls) -> Any:
        with cls._lock:
            if cls._gateway is None:
                cls._gateway = GatewayRouter()
            return cls._gateway


class AgentCapabilityProfile:
    """Role metadata shared by all specialist agents."""

    role_name: str = "Generic Agent"
    service_type: str = "brain"
    required_capabilities: frozenset[str] = frozenset({"text_generation"})
    preferred_temperature: Optional[float] = None
    default_task_timeout: Optional[float] = None

    def profile_dict(self) -> dict[str, Any]:
        return {
            "role_name": self.role_name,
            "service_type": self.service_type,
            "required_capabilities": sorted(self.required_capabilities),
            "preferred_temperature": self.preferred_temperature,
            "default_task_timeout": self.default_task_timeout,
        }


# ---------------------------------------------------------------------------
# Base agent
# ---------------------------------------------------------------------------

class GodBaseAgent(AgentCapabilityProfile):
    """
    Production base class for Riot specialist agents.

    Existing subclasses remain source-compatible while gaining:
    * proper gateway instance wiring;
    * bounded per-agent concurrency;
    * structured telemetry;
    * hard execution timeout;
    * output schema validation;
    * cancellation propagation;
    * bounded parallel execution.
    """

    role_name: str = "Generic Agent"
    service_type: str = "brain"
    required_capabilities: frozenset[str] = frozenset({"text_generation"})

    def __init__(
        self,
        role_name: str,
        service_type: str = "brain",
        *,
        gateway: Any = None,
        config: Optional[AgentRuntimeConfig] = None,
        required_capabilities: Optional[Iterable[str]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.role_name = str(role_name).strip() or "Unnamed Agent"
        self.service_type = str(service_type).strip() or "brain"
        self.config = config or AgentRuntimeConfig()

        caps = (
            {
                str(item).strip()
                for item in required_capabilities
                if str(item).strip()
            }
            if required_capabilities is not None
            else set(self.required_capabilities)
        )
        self.required_capabilities = frozenset(caps or {"text_generation"})

        self.default_temperature = (
            self.config.temperature
            if temperature is None
            else max(0.0, min(2.0, float(temperature)))
        )
        self.default_max_tokens = (
            self.config.max_tokens
            if max_tokens is None
            else max(1, int(max_tokens))
        )
        self.agent_metadata = dict(metadata or {})

        self._gateway = gateway
        if gateway is not None:
            _GatewayHolder.set_gateway(gateway)

        self._concurrency = asyncio.Semaphore(self.config.max_concurrency)
        self._history: Deque[AgentExecutionRecord] = deque(
            maxlen=self.config.history_size
        )
        self._history_lock = threading.RLock()

        self._invocations = 0
        self._successes = 0
        self._failures = 0
        self._timeouts = 0
        self._invalid_outputs = 0

        logger.info(
            "[AGENT INIT] role=%s service=%s capabilities=%s",
            self.role_name,
            self.service_type,
            sorted(self.required_capabilities),
        )

    @property
    def gateway(self) -> Any:
        if self._gateway is None:
            self._gateway = _GatewayHolder.get_gateway()
        return self._gateway

    def set_gateway(self, gateway: Any) -> None:
        _GatewayHolder.set_gateway(gateway)
        self._gateway = gateway

    def get_gateway(self) -> Any:
        """
        Return a real GatewayHandle/service handle.

        The previous implementation called GatewayRouter.get_gateway as if it
        were a class method. This version first resolves the actual router
        instance and then requests its service handle.
        """
        router = self.gateway
        get_gateway = getattr(router, "get_gateway", None)
        if get_gateway is None:
            if hasattr(router, "generate"):
                return router
            raise RuntimeError(
                "Configured gateway exposes neither get_gateway() nor generate()"
            )

        try:
            return get_gateway(service_type=self.service_type)
        except TypeError:
            return get_gateway(self.service_type)

    # ------------------------------------------------------------------
    # Prompt construction
    # ------------------------------------------------------------------

    def build_system_prompt(
        self,
        task_directive: str,
        *,
        context: Optional[Mapping[str, Any]] = None,
    ) -> str:
        directive = _bounded_text(
            str(task_directive or "").strip(),
            self.config.max_prompt_chars,
        )
        normalized_context = _normalize_context(context)
        context_text = _bounded_text(
            normalized_context,
            self.config.max_context_chars,
        )

        parts = [
            f"You are the {self.role_name} inside the Riot / God Node game-generation engine.",
            "You are a specialist production agent. Your response is consumed by downstream software.",
            "",
            "ROLE PROFILE:",
            _canonical_json(self.profile_dict()),
            "",
            "NON-NEGOTIABLE ENGINE RULES:",
            "1. Preserve upstream information and dependencies.",
            "2. Never invent assets, files, coordinates, providers, tests, builds, or artifacts.",
            "3. Separate plan/recommendation from verified/generated evidence.",
            "4. Do not claim execution, compilation, rendering, QA, or artifact creation without evidence.",
            "5. Optimize outputs for deterministic downstream processing and bounded resource use.",
            "6. Return one machine-readable JSON object only; no Markdown and no conversational prose.",
            "",
            "DIRECTIVE:",
            directive,
        ]

        if context_text != "{}":
            parts.extend(["", "UPSTREAM CONTEXT:", context_text])

        parts.extend(
            [
                "",
                "OUTPUT CONTRACT:",
                '{"status":"SUCCESS|FAILED","data":{...},"warnings":[],"errors":[]}',
            ]
        )
        return "\n".join(parts)

    def create_request(
        self,
        task_directive: str,
        *,
        context: Optional[Mapping[str, Any]] = None,
        timeout_seconds: Optional[float] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        required_capabilities: Optional[Iterable[str]] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> AgentRequest:
        directive = str(task_directive or "").strip()
        if not directive:
            raise ValueError("task_directive cannot be empty")

        normalized_context = _normalize_context(context)
        system_prompt = self.build_system_prompt(
            directive,
            context=normalized_context,
        )

        material = (
            f"{self.role_name}\x1f{self.service_type}\x1f"
            f"{time.time_ns()}\x1f{directive}"
        )
        request_id = (
            f"agent_{time.time_ns()}_"
            f"{hashlib.sha1(material.encode('utf-8')).hexdigest()[:12]}"
        )

        caps = (
            frozenset(
                str(item).strip()
                for item in required_capabilities
                if str(item).strip()
            )
            if required_capabilities is not None
            else self.required_capabilities
        )

        request_metadata = dict(self.agent_metadata)
        if metadata:
            request_metadata.update(_json_safe(metadata))
        request_metadata.update(
            {
                "agent_role": self.role_name,
                "service_type": self.service_type,
                "contract": "riot.agent.v2",
            }
        )

        timeout = (
            self.config.timeout_seconds
            if timeout_seconds is None
            else max(5.0, min(1_800.0, float(timeout_seconds)))
        )

        return AgentRequest(
            request_id=request_id,
            role_name=self.role_name,
            service_type=self.service_type,
            directive=directive,
            context=normalized_context,
            system_prompt=system_prompt,
            temperature=(
                self.default_temperature
                if temperature is None
                else max(0.0, min(2.0, float(temperature)))
            ),
            max_tokens=(
                self.default_max_tokens
                if max_tokens is None
                else max(1, int(max_tokens))
            ),
            timeout_seconds=timeout,
            required_capabilities=caps,
            metadata=request_metadata,
        )

    # ------------------------------------------------------------------
    # Output sanitation / parsing
    # ------------------------------------------------------------------

    def _sanitize_json(self, raw_text: str) -> str:
        """
        Remove only common transport wrappers.

        The method deliberately avoids speculative JSON repair so corrupted
        model output becomes an explicit INVALID_OUTPUT result.
        """
        clean = str(raw_text or "").strip().lstrip("\ufeff")

        fenced = re.fullmatch(
            r"```(?:json|JSON)?\s*(.*?)\s*```",
            clean,
            flags=re.DOTALL,
        )
        if fenced:
            clean = fenced.group(1).strip()

        if clean.startswith("{") or clean.startswith("["):
            return clean

        candidates: list[tuple[int, str]] = []

        first_object = clean.find("{")
        last_object = clean.rfind("}")
        if first_object >= 0 and last_object > first_object:
            candidates.append((first_object, clean[first_object : last_object + 1]))

        first_array = clean.find("[")
        last_array = clean.rfind("]")
        if first_array >= 0 and last_array > first_array:
            candidates.append((first_array, clean[first_array : last_array + 1]))

        if candidates:
            candidates.sort(key=lambda item: item[0])
            return candidates[0][1]

        return clean

    def parse_model_output(
        self,
        raw_text: str,
        *,
        response_schema: Optional[Type[T]] = None,
    ) -> tuple[Any, Optional[str]]:
        bounded = _bounded_text(raw_text, self.config.max_output_chars)
        cleaned = self._sanitize_json(bounded)

        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
            ) from exc

        if response_schema is not None:
            model_validate = getattr(response_schema, "model_validate", None)
            if callable(model_validate):
                parsed = model_validate(parsed)
            else:
                parse_obj = getattr(response_schema, "parse_obj", None)
                if callable(parse_obj):
                    parsed = parse_obj(parsed)
                elif isinstance(response_schema, type) and not isinstance(
                    parsed, response_schema
                ):
                    parsed = response_schema(parsed)

        status = None
        if isinstance(parsed, Mapping) and parsed.get("status") is not None:
            status = str(parsed["status"]).upper()

        return parsed, status

    # ------------------------------------------------------------------
    # Gateway invocation
    # ------------------------------------------------------------------

    async def _invoke_gateway(
        self,
        request: AgentRequest,
    ) -> tuple[str, Optional[str], dict[str, Any]]:
        gateway = self.get_gateway()
        generate = getattr(gateway, "generate", None)
        if generate is None:
            raise RuntimeError(
                f"Gateway handle for {self.role_name} has no generate() method"
            )

        kwargs = {
            "system_prompt": request.system_prompt,
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
            "service": request.service_type,
            "required_capabilities": request.required_capabilities,
            "metadata": dict(request.metadata),
            "timeout_seconds": request.timeout_seconds,
        }

        async def _async_call() -> Any:
            try:
                return await generate(request.directive, **kwargs)
            except TypeError as exc:
                # Signature compatibility only; this is not a provider retry.
                logger.debug(
                    "Gateway optional-argument compatibility fallback for %s: %s",
                    self.role_name,
                    exc,
                )
                compact_kwargs = {
                    "system_prompt": request.system_prompt,
                    "temperature": request.temperature,
                    "max_tokens": request.max_tokens,
                }
                return await generate(request.directive, **compact_kwargs)

        if inspect.iscoroutinefunction(generate):
            response = await asyncio.wait_for(
                _async_call(),
                timeout=request.timeout_seconds,
            )
        else:
            def _sync_call() -> Any:
                try:
                    return generate(request.directive, **kwargs)
                except TypeError as exc:
                    logger.debug(
                        "Gateway optional-argument compatibility fallback for %s: %s",
                        self.role_name,
                        exc,
                    )
                    compact_kwargs = {
                        "system_prompt": request.system_prompt,
                        "temperature": request.temperature,
                        "max_tokens": request.max_tokens,
                    }
                    return generate(request.directive, **compact_kwargs)

            response = await asyncio.wait_for(
                asyncio.to_thread(_sync_call),
                timeout=request.timeout_seconds,
            )

        if inspect.isawaitable(response):
            response = await asyncio.wait_for(
                response,
                timeout=request.timeout_seconds,
            )

        text, provider, response_metadata = _extract_response_text(response)
        if not text.strip():
            raise ValueError("gateway returned an empty model response")

        return text, provider, response_metadata

    # ------------------------------------------------------------------
    # Main result API
    # ------------------------------------------------------------------

    async def think_and_execute_result(
        self,
        task_directive: str,
        context: Optional[Mapping[str, Any]] = None,
        *,
        timeout_seconds: Optional[float] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        required_capabilities: Optional[Iterable[str]] = None,
        metadata: Optional[Mapping[str, Any]] = None,
        response_schema: Optional[Type[T]] = None,
    ) -> AgentResult:
        request = self.create_request(
            task_directive,
            context=context,
            timeout_seconds=timeout_seconds,
            temperature=temperature,
            max_tokens=max_tokens,
            required_capabilities=required_capabilities,
            metadata=metadata,
        )

        started = time.perf_counter()
        context_hash = _sha256_text(_canonical_json(request.context))
        self._invocations += 1

        async with self._concurrency:
            try:
                raw_response, provider, response_metadata = await self._invoke_gateway(
                    request
                )
                parsed, status = self.parse_model_output(
                    raw_response,
                    response_schema=response_schema,
                )

                if status == "FAILED":
                    execution_status = AgentExecutionStatus.FAILED
                    self._failures += 1
                    error = (
                        str(parsed.get("error"))
                        if isinstance(parsed, Mapping) and parsed.get("error")
                        else "Specialist agent returned FAILED"
                    )
                else:
                    execution_status = AgentExecutionStatus.SUCCESS
                    self._successes += 1
                    error = None

                data = _json_safe(parsed)
                output_hash = _sha256_text(_canonical_json(data))
                duration_ms = (time.perf_counter() - started) * 1000.0

                record = AgentExecutionRecord(
                    request_id=request.request_id,
                    role_name=self.role_name,
                    status=execution_status,
                    started_at=started,
                    finished_at=time.perf_counter(),
                    duration_ms=duration_ms,
                    context_sha256=context_hash,
                    output_sha256=output_hash,
                    provider=provider,
                    error=error,
                )
                self._record_history(record)

                return AgentResult(
                    status=execution_status,
                    role=self.role_name,
                    request_id=request.request_id,
                    data=data,
                    error=error,
                    provider=provider,
                    duration_ms=duration_ms,
                    metadata={
                        **response_metadata,
                        "context_sha256": context_hash,
                        "output_sha256": output_hash,
                        "agent_contract": "riot.agent.v2",
                    },
                )

            except asyncio.CancelledError:
                duration_ms = (time.perf_counter() - started) * 1000.0
                self._failures += 1
                self._record_history(
                    AgentExecutionRecord(
                        request_id=request.request_id,
                        role_name=self.role_name,
                        status=AgentExecutionStatus.CANCELLED,
                        started_at=started,
                        finished_at=time.perf_counter(),
                        duration_ms=duration_ms,
                        context_sha256=context_hash,
                        error="cancelled",
                    )
                )
                raise

            except asyncio.TimeoutError:
                duration_ms = (time.perf_counter() - started) * 1000.0
                self._timeouts += 1
                self._failures += 1
                self._record_history(
                    AgentExecutionRecord(
                        request_id=request.request_id,
                        role_name=self.role_name,
                        status=AgentExecutionStatus.TIMEOUT,
                        started_at=started,
                        finished_at=time.perf_counter(),
                        duration_ms=duration_ms,
                        context_sha256=context_hash,
                        error="agent execution timed out",
                    )
                )
                return AgentResult(
                    status=AgentExecutionStatus.TIMEOUT,
                    role=self.role_name,
                    request_id=request.request_id,
                    error="agent execution timed out",
                    duration_ms=duration_ms,
                )

            except ValueError as exc:
                duration_ms = (time.perf_counter() - started) * 1000.0
                self._invalid_outputs += 1
                self._failures += 1
                self._record_history(
                    AgentExecutionRecord(
                        request_id=request.request_id,
                        role_name=self.role_name,
                        status=AgentExecutionStatus.INVALID_OUTPUT,
                        started_at=started,
                        finished_at=time.perf_counter(),
                        duration_ms=duration_ms,
                        context_sha256=context_hash,
                        error=str(exc),
                    )
                )
                return AgentResult(
                    status=AgentExecutionStatus.INVALID_OUTPUT,
                    role=self.role_name,
                    request_id=request.request_id,
                    error=str(exc),
                    duration_ms=duration_ms,
                )

            except Exception as exc:
                duration_ms = (time.perf_counter() - started) * 1000.0
                self._failures += 1
                logger.exception(
                    "[AGENT ERROR] role=%s request=%s",
                    self.role_name,
                    request.request_id,
                )
                self._record_history(
                    AgentExecutionRecord(
                        request_id=request.request_id,
                        role_name=self.role_name,
                        status=AgentExecutionStatus.FAILED,
                        started_at=started,
                        finished_at=time.perf_counter(),
                        duration_ms=duration_ms,
                        context_sha256=context_hash,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
                return AgentResult(
                    status=AgentExecutionStatus.FAILED,
                    role=self.role_name,
                    request_id=request.request_id,
                    error=f"{type(exc).__name__}: {exc}",
                    duration_ms=duration_ms,
                )

    async def think_and_execute(
        self,
        task_directive: str,
        context: Optional[Mapping[str, Any]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Backward-compatible dictionary API."""
        result = await self.think_and_execute_result(
            task_directive,
            context,
            **kwargs,
        )

        if isinstance(result.data, Mapping):
            payload = dict(result.data)
        else:
            payload = {"data": result.data}

        payload.setdefault("status", "SUCCESS" if result.ok else "FAILED")
        payload.setdefault(
            "_agent_runtime",
            {
                "request_id": result.request_id,
                "role": result.role,
                "provider": result.provider,
                "status": result.status.value,
                "duration_ms": round(result.duration_ms, 3),
                **result.metadata,
            },
        )
        if result.error:
            payload.setdefault("error", result.error)
        return payload

    # ------------------------------------------------------------------
    # Bounded parallel execution
    # ------------------------------------------------------------------

    async def execute_many(
        self,
        directives: Sequence[str],
        *,
        contexts: Optional[Sequence[Optional[Mapping[str, Any]]]] = None,
        fail_fast: bool = False,
        **kwargs: Any,
    ) -> list[AgentResult]:
        if len(directives) > self.config.max_parallel_batch:
            raise ValueError(
                f"parallel batch exceeds configured limit "
                f"{self.config.max_parallel_batch}"
            )

        if contexts is not None and len(contexts) != len(directives):
            raise ValueError("contexts length must equal directives length")

        tasks = [
            asyncio.create_task(
                self.think_and_execute_result(
                    directive,
                    contexts[index] if contexts is not None else None,
                    **kwargs,
                ),
                name=f"riot-agent-{self.role_name}-{index}",
            )
            for index, directive in enumerate(directives)
        ]

        if not tasks:
            return []

        if fail_fast:
            try:
                return list(await asyncio.gather(*tasks))
            except BaseException:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise

        completed = await asyncio.gather(*tasks, return_exceptions=True)
        results: list[AgentResult] = []
        for index, item in enumerate(completed):
            if isinstance(item, AgentResult):
                results.append(item)
            elif isinstance(item, asyncio.CancelledError):
                results.append(
                    AgentResult(
                        status=AgentExecutionStatus.CANCELLED,
                        role=self.role_name,
                        request_id=f"batch_{index}",
                        error="cancelled",
                    )
                )
            else:
                results.append(
                    AgentResult(
                        status=AgentExecutionStatus.FAILED,
                        role=self.role_name,
                        request_id=f"batch_{index}",
                        error=f"{type(item).__name__}: {item}",
                    )
                )
        return results

    # ------------------------------------------------------------------
    # Telemetry
    # ------------------------------------------------------------------

    def _record_history(self, record: AgentExecutionRecord) -> None:
        with self._history_lock:
            self._history.append(record)

    def history(self) -> list[dict[str, Any]]:
        with self._history_lock:
            return [record.to_dict() for record in self._history]

    def stats(self) -> dict[str, Any]:
        with self._history_lock:
            history_entries = len(self._history)

        return {
            "role": self.role_name,
            "service_type": self.service_type,
            "required_capabilities": sorted(self.required_capabilities),
            "invocations": self._invocations,
            "successes": self._successes,
            "failures": self._failures,
            "timeouts": self._timeouts,
            "invalid_outputs": self._invalid_outputs,
            "success_rate": round(
                self._successes / self._invocations, 4
            ) if self._invocations else 0.0,
            "history_entries": history_entries,
            "concurrency_limit": self.config.max_concurrency,
            "parallel_batch_limit": self.config.max_parallel_batch,
        }

    async def perform_role(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        """
        Explicit failure for the abstract base compatibility surface.

        Concrete specialists must override this method.
        """
        return {
            "status": "FAILED",
            "error": (
                f"{self.role_name} has no specialist perform_role() implementation."
            ),
        }


__all__ = [
    "AgentCapabilityProfile",
    "AgentExecutionRecord",
    "AgentExecutionStatus",
    "AgentRequest",
    "AgentResult",
    "AgentRuntimeConfig",
    "GodBaseAgent",
]
