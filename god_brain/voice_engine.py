"""
Riot Voice & Audio Engine
=========================
Provider-agnostic, production-oriented voice/audio orchestration boundary.

Design goals
------------
* Dynamic provider routing through the existing GatewayRouter.
* No hard-coded vendor, model, endpoint, credential, or voice identity.
* No fake/silent audio success: every successful synthesis must yield verifiable
  audio bytes or a provider-owned asset URI explicitly marked as external.
* Deterministic request fingerprints for de-duplication and cache reuse.
* Bounded concurrency, request/asset size limits, timeouts and cancellation.
* Voice profiles support language, style, emotion, speaking rate, pitch and
  engine-specific options without coupling the core to a provider.
* Supports TTS/voice lines, narration, dialogue batches, SFX/music asset
  descriptors, and provider-returned audio payloads.
* Optional ffmpeg post-processing without making ffmpeg a mandatory dependency.
* WAV/MP3/OGG/Opus/FLAC/MP4/M4A container sanity checks, SHA-256 integrity,
  atomic writes and bounded in-memory cache.
* Safe for mobile/server constrained deployments; no unbounded queues.

The engine intentionally does not invent audio. If the configured voice provider
cannot return usable audio data or an explicit external asset reference, the
request fails closed.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import math
import os
import re
import shutil
import tempfile
import time
import uuid
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

logger = logging.getLogger("Riot.VoiceEngine")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s - [VOICE] - %(levelname)s - %(message)s")
    )
    logger.addHandler(handler)
logger.setLevel(os.getenv("RIOT_VOICE_LOG_LEVEL", "INFO").upper())


# ---------------------------------------------------------------------------
# Limits / defaults
# ---------------------------------------------------------------------------
DEFAULT_CONCURRENCY = max(1, int(os.getenv("RIOT_VOICE_CONCURRENCY", "4")))
DEFAULT_TIMEOUT = max(5.0, float(os.getenv("RIOT_VOICE_TIMEOUT_SECONDS", "180")))
DEFAULT_MAX_TEXT = max(256, int(os.getenv("RIOT_VOICE_MAX_TEXT", "100000")))
DEFAULT_MAX_AUDIO_BYTES = max(
    64 * 1024, int(os.getenv("RIOT_VOICE_MAX_AUDIO_BYTES", str(32 * 1024 * 1024)))
)
DEFAULT_CACHE_ENTRIES = max(0, int(os.getenv("RIOT_VOICE_CACHE_ENTRIES", "128")))
DEFAULT_CACHE_TTL = max(0.0, float(os.getenv("RIOT_VOICE_CACHE_TTL_SECONDS", "1800")))
DEFAULT_OUTPUT_ROOT = os.getenv("RIOT_VOICE_OUTPUT_ROOT", "audio_output")
DEFAULT_FFMPEG_TIMEOUT = max(5.0, float(os.getenv("RIOT_VOICE_FFMPEG_TIMEOUT_SECONDS", "120")))

_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_SAFE_PATH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./-]{0,255}$")


class VoiceError(RuntimeError):
    """Base voice subsystem error."""


class VoiceValidationError(VoiceError):
    """Input validation failed."""


class VoiceUnavailableError(VoiceError):
    """No usable voice provider/asset backend is available."""


class VoiceSynthesisError(VoiceError):
    """Provider synthesis failed or returned unusable data."""


class VoiceAssetError(VoiceError):
    """Audio asset verification or persistence failed."""


class VoiceFormat(str, Enum):
    WAV = "wav"
    MP3 = "mp3"
    OGG = "ogg"
    OPUS = "opus"
    FLAC = "flac"
    M4A = "m4a"
    AAC = "aac"


class VoiceTaskType(str, Enum):
    TTS = "tts"
    DIALOGUE = "dialogue"
    NARRATION = "narration"
    SFX = "sfx"
    MUSIC = "music"


@dataclass(slots=True, frozen=True)
class VoiceProfile:
    """Provider-neutral voice rendering parameters."""

    voice_id: Optional[str] = None
    language: Optional[str] = None
    locale: Optional[str] = None
    gender: Optional[str] = None
    style: Optional[str] = None
    emotion: Optional[str] = None
    speaking_rate: float = 1.0
    pitch: float = 0.0
    volume: float = 1.0
    stability: Optional[float] = None
    similarity: Optional[float] = None
    expressiveness: Optional[float] = None
    provider_options: Mapping[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if self.voice_id is not None and not _SAFE_ID.fullmatch(self.voice_id):
            raise VoiceValidationError("invalid voice_id")
        if not (0.25 <= self.speaking_rate <= 4.0):
            raise VoiceValidationError("speaking_rate must be between 0.25 and 4.0")
        if not (-24.0 <= self.pitch <= 24.0):
            raise VoiceValidationError("pitch must be between -24 and 24 semitones")
        if not (0.0 <= self.volume <= 2.0):
            raise VoiceValidationError("volume must be between 0 and 2")
        for name, value in (
            ("stability", self.stability),
            ("similarity", self.similarity),
            ("expressiveness", self.expressiveness),
        ):
            if value is not None and not (0.0 <= value <= 1.0):
                raise VoiceValidationError(f"{name} must be between 0 and 1")
        if len(self.provider_options) > 128:
            raise VoiceValidationError("provider_options contains too many fields")


@dataclass(slots=True, frozen=True)
class VoiceSynthesisRequest:
    text: str
    game_id: str
    task_type: VoiceTaskType = VoiceTaskType.TTS
    profile: VoiceProfile = field(default_factory=VoiceProfile)
    output_format: VoiceFormat = VoiceFormat.MP3
    sample_rate: Optional[int] = None
    channels: int = 1
    speed: Optional[float] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    preferred_provider: Optional[str] = None
    excluded_providers: frozenset[str] = frozenset()
    required_capabilities: frozenset[str] = frozenset({"voice"})
    timeout_seconds: Optional[float] = None
    allow_external_asset_uri: bool = True

    def validate(self, max_text: int, max_audio_bytes: int) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise VoiceValidationError("text must be non-empty")
        if len(self.text) > max_text:
            raise VoiceValidationError(f"text exceeds {max_text} characters")
        if not _SAFE_ID.fullmatch(self.game_id):
            raise VoiceValidationError("invalid game_id")
        self.profile.validate()
        if self.channels not in {1, 2}:
            raise VoiceValidationError("channels must be 1 or 2")
        if self.sample_rate is not None and not (8000 <= self.sample_rate <= 192000):
            raise VoiceValidationError("unsupported sample_rate")
        if self.speed is not None and not (0.25 <= self.speed <= 4.0):
            raise VoiceValidationError("speed must be between 0.25 and 4.0")
        if self.timeout_seconds is not None and not (1.0 <= self.timeout_seconds <= 900.0):
            raise VoiceValidationError("timeout_seconds out of range")
        if self.preferred_provider and not _SAFE_ID.fullmatch(self.preferred_provider):
            raise VoiceValidationError("invalid preferred_provider")
        if len(self.metadata) > 128:
            raise VoiceValidationError("metadata contains too many fields")
        # Referenced here so validation stays aligned with the asset budget.
        if max_audio_bytes <= 0:
            raise VoiceValidationError("audio budget must be positive")


@dataclass(slots=True)
class AudioPayload:
    data: Optional[bytes] = None
    content_type: Optional[str] = None
    extension: Optional[str] = None
    external_uri: Optional[str] = None
    provider: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def has_bytes(self) -> bool:
        return bool(self.data)

    @property
    def is_external(self) -> bool:
        return bool(self.external_uri)


@dataclass(slots=True)
class VoiceArtifact:
    asset_id: str
    game_id: str
    task_type: str
    path: Optional[str]
    external_uri: Optional[str]
    format: str
    content_type: Optional[str]
    size_bytes: int
    sha256: Optional[str]
    verified: bool
    provider: Optional[str]
    request_id: str
    duration_ms: float
    cached: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class VoiceStats:
    requests: int = 0
    cache_hits: int = 0
    successes: int = 0
    failures: int = 0
    external_assets: int = 0
    bytes_written: int = 0
    active: int = 0
    peak_active: int = 0
    validation_failures: int = 0


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
class _AudioCache:
    """Small LRU/TTL cache; only verified bytes are stored."""

    def __init__(self, max_entries: int, ttl_seconds: float):
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self._data: OrderedDict[str, tuple[float, AudioPayload]] = OrderedDict()
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Optional[AudioPayload]:
        if self.max_entries <= 0:
            return None
        async with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            expires, payload = item
            if self.ttl_seconds and time.time() >= expires:
                self._data.pop(key, None)
                return None
            self._data.move_to_end(key)
            return AudioPayload(
                data=bytes(payload.data) if payload.data is not None else None,
                content_type=payload.content_type,
                extension=payload.extension,
                external_uri=payload.external_uri,
                provider=payload.provider,
                metadata=dict(payload.metadata),
            )

    async def put(self, key: str, payload: AudioPayload) -> None:
        if self.max_entries <= 0 or self.ttl_seconds <= 0:
            return
        async with self._lock:
            self._data[key] = (
                time.time() + self.ttl_seconds,
                AudioPayload(
                    data=bytes(payload.data) if payload.data is not None else None,
                    content_type=payload.content_type,
                    extension=payload.extension,
                    external_uri=payload.external_uri,
                    provider=payload.provider,
                    metadata=dict(payload.metadata),
                ),
            )
            self._data.move_to_end(key)
            while len(self._data) > self.max_entries:
                self._data.popitem(last=False)

    async def clear(self) -> None:
        async with self._lock:
            self._data.clear()


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
class VoiceEngine:
    """
    Orchestrates all generated voice/audio assets through the dynamic gateway.

    The gateway is duck-typed intentionally. It must expose an async
    ``generate(...)`` method compatible with core.gateway.GatewayRouter.
    """

    def __init__(
        self,
        gateway: Any = None,
        *,
        output_root: str | Path = DEFAULT_OUTPUT_ROOT,
        concurrency: int = DEFAULT_CONCURRENCY,
        timeout_seconds: float = DEFAULT_TIMEOUT,
        max_text: int = DEFAULT_MAX_TEXT,
        max_audio_bytes: int = DEFAULT_MAX_AUDIO_BYTES,
        cache_entries: int = DEFAULT_CACHE_ENTRIES,
        cache_ttl_seconds: float = DEFAULT_CACHE_TTL,
    ) -> None:
        self.gateway = gateway
        self.output_root = Path(output_root).expanduser().resolve()
        self.concurrency = max(1, int(concurrency))
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.max_text = max(256, int(max_text))
        self.max_audio_bytes = max(1024, int(max_audio_bytes))
        self._semaphore = asyncio.Semaphore(self.concurrency)
        self._cache = _AudioCache(cache_entries, cache_ttl_seconds)
        self._stats = VoiceStats()
        self._stats_lock = asyncio.Lock()
        self._closed = False
        self._inflight: dict[str, asyncio.Task[Any]] = {}
        self._inflight_lock = asyncio.Lock()

    async def startup(self) -> None:
        self.output_root.mkdir(parents=True, exist_ok=True)
        if self.gateway is None:
            try:
                from core.gateway import GatewayRouter

                self.gateway = GatewayRouter()
            except Exception as exc:
                raise VoiceUnavailableError(
                    "VoiceEngine requires a configured GatewayRouter"
                ) from exc
        startup = getattr(self.gateway, "startup", None)
        if startup is not None:
            result = startup()
            if asyncio.iscoroutine(result):
                await result
        self._closed = False

    async def shutdown(self) -> None:
        self._closed = True
        async with self._inflight_lock:
            tasks = list(self._inflight.values())
            self._inflight.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._cache.clear()

    @staticmethod
    def _fingerprint(request: VoiceSynthesisRequest) -> str:
        profile = asdict(request.profile)
        payload = {
            "text": request.text,
            "game_id": request.game_id,
            "task_type": request.task_type.value,
            "profile": profile,
            "format": request.output_format.value,
            "sample_rate": request.sample_rate,
            "channels": request.channels,
            "speed": request.speed,
            "metadata": dict(request.metadata),
            "preferred_provider": request.preferred_provider,
            "excluded_providers": sorted(request.excluded_providers),
            "required_capabilities": sorted(request.required_capabilities),
        }
        material = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    @staticmethod
    def _safe_extension(value: Optional[str], fallback: VoiceFormat) -> str:
        raw = (value or fallback.value).strip().lower().lstrip(".")
        return raw if re.fullmatch(r"[a-z0-9]{1,8}", raw) else fallback.value

    def _artifact_path(self, request: VoiceSynthesisRequest, asset_id: str, extension: str) -> Path:
        game_dir = self.output_root / request.game_id
        path = game_dir / f"{asset_id}.{extension}"
        try:
            path.resolve().relative_to(self.output_root)
        except ValueError as exc:
            raise VoiceAssetError("voice artifact path escaped output root") from exc
        return path

    async def synthesize(self, request: VoiceSynthesisRequest) -> VoiceArtifact:
        if self._closed:
            raise VoiceUnavailableError("VoiceEngine is shut down")
        request.validate(self.max_text, self.max_audio_bytes)
        await self._inc("requests")

        key = self._fingerprint(request)
        cached = await self._cache.get(key)
        if cached is not None:
            await self._inc("cache_hits")
            return await self._materialize_payload(request, cached, key=key, cached=True)

        request_id = uuid.uuid4().hex
        task = asyncio.current_task()
        if task is not None:
            async with self._inflight_lock:
                self._inflight[request_id] = task

        started = time.perf_counter()
        async with self._semaphore:
            await self._set_active(+1)
            try:
                payload = await asyncio.wait_for(
                    self._synthesize_via_gateway(request, request_id),
                    timeout=request.timeout_seconds or self.timeout_seconds,
                )
                if payload.data is not None:
                    if len(payload.data) > self.max_audio_bytes:
                        raise VoiceAssetError(
                            f"provider returned {len(payload.data)} bytes; limit is {self.max_audio_bytes}"
                        )
                    self._verify_audio(payload.data, request.output_format, payload.content_type)
                elif not payload.external_uri or not request.allow_external_asset_uri:
                    raise VoiceSynthesisError(
                        "voice provider returned neither verified audio bytes nor an allowed external URI"
                    )

                await self._cache.put(key, payload)
                artifact = await self._materialize_payload(request, payload, key=key, cached=False)
                await self._inc("successes")
                if artifact.external_uri:
                    await self._inc("external_assets")
                return artifact
            except asyncio.CancelledError:
                raise
            except (VoiceError, asyncio.TimeoutError):
                await self._inc("failures")
                raise
            except Exception as exc:
                await self._inc("failures")
                raise VoiceSynthesisError(f"voice synthesis failed: {exc}") from exc
            finally:
                await self._set_active(-1)
                async with self._inflight_lock:
                    self._inflight.pop(request_id, None)
                logger.debug(
                    "Voice request %s finished in %.2fms",
                    request_id,
                    (time.perf_counter() - started) * 1000.0,
                )

    async def synthesize_many(
        self,
        requests: Sequence[VoiceSynthesisRequest],
        *,
        fail_fast: bool = False,
    ) -> list[VoiceArtifact]:
        if len(requests) > self.concurrency * 32:
            raise VoiceValidationError("batch exceeds bounded voice queue capacity")
        tasks = [asyncio.create_task(self.synthesize(item)) for item in requests]
        if fail_fast:
            try:
                return list(await asyncio.gather(*tasks))
            except asyncio.CancelledError:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise
        results = await asyncio.gather(*tasks, return_exceptions=True)
        artifacts: list[VoiceArtifact] = []
        errors: list[Exception] = []
        for result in results:
            if isinstance(result, Exception):
                errors.append(result)
            else:
                artifacts.append(result)
        if errors:
            logger.warning("Voice batch completed with %d failures", len(errors))
        return artifacts

    async def _synthesize_via_gateway(
        self,
        request: VoiceSynthesisRequest,
        request_id: str,
    ) -> AudioPayload:
        if self.gateway is None:
            raise VoiceUnavailableError("Gateway is not initialized")
        generate = getattr(self.gateway, "generate", None)
        if not callable(generate):
            raise VoiceUnavailableError("Configured gateway does not expose generate()")

        system_prompt = (
            "You are a voice-generation control layer. Return ONLY structured metadata "
            "for the configured voice provider; do not fabricate audio bytes."
        )
        voice_payload = {
            "schema": "riot.voice.request.v2",
            "request_id": request_id,
            "task_type": request.task_type.value,
            "text": request.text,
            "voice": asdict(request.profile),
            "audio": {
                "format": request.output_format.value,
                "sample_rate": request.sample_rate,
                "channels": request.channels,
                "speed": request.speed,
            },
            "game_id": request.game_id,
            "metadata": dict(request.metadata),
        }
        response = await generate(
            json.dumps(voice_payload, ensure_ascii=False, separators=(",", ":")),
            system_prompt=system_prompt,
            service="voice",
            required_capabilities=request.required_capabilities,
            metadata={
                "riot_voice_request_id": request_id,
                "game_id": request.game_id,
                "task_type": request.task_type.value,
            },
            timeout_seconds=request.timeout_seconds or self.timeout_seconds,
            max_failovers=3,
            preferred_provider=request.preferred_provider,
            excluded_providers=request.excluded_providers,
        )
        if not getattr(response, "success", False):
            raise VoiceSynthesisError("voice gateway returned unsuccessful response")

        raw = getattr(response, "raw_response", None)
        metadata = dict(getattr(response, "metadata", {}) or {})
        provider = getattr(response, "provider", None)
        return self._extract_audio_payload(
            raw_response=raw,
            output_text=getattr(response, "output", ""),
            metadata=metadata,
            provider=provider,
        )

    def _extract_audio_payload(
        self,
        *,
        raw_response: Any,
        output_text: str,
        metadata: Mapping[str, Any],
        provider: Optional[str],
    ) -> AudioPayload:
        candidates: list[Any] = [raw_response, metadata]
        if output_text:
            try:
                candidates.append(json.loads(output_text))
            except (json.JSONDecodeError, TypeError):
                pass

        audio_keys = (
            "audio_base64",
            "audio_data",
            "base64_audio",
            "audio",
            "data",
            "content",
        )
        uri_keys = ("audio_url", "audio_uri", "url", "uri", "asset_url", "asset_uri")
        for candidate in candidates:
            payload = self._extract_from_object(candidate, audio_keys, uri_keys, provider)
            if payload is not None:
                return payload
        raise VoiceSynthesisError("voice provider response contained no usable audio payload")

    def _extract_from_object(
        self,
        candidate: Any,
        audio_keys: Sequence[str],
        uri_keys: Sequence[str],
        provider: Optional[str],
    ) -> Optional[AudioPayload]:
        if candidate is None:
            return None
        if isinstance(candidate, Mapping):
            # Common nested response envelopes are walked to a bounded depth.
            for key in audio_keys:
                if key not in candidate:
                    continue
                value = candidate[key]
                payload = self._decode_audio_value(value, candidate, provider)
                if payload:
                    return payload
            for key in uri_keys:
                value = candidate.get(key)
                if isinstance(value, str) and self._looks_like_external_uri(value):
                    return AudioPayload(
                        external_uri=value,
                        provider=provider,
                        metadata={"source": key},
                    )
            for key in ("result", "output", "response", "data", "payload"):
                nested = candidate.get(key)
                if isinstance(nested, Mapping):
                    payload = self._extract_from_object(nested, audio_keys, uri_keys, provider)
                    if payload:
                        return payload
        elif isinstance(candidate, (bytes, bytearray)):
            return AudioPayload(
                data=bytes(candidate),
                provider=provider,
                metadata={"source": "bytes"},
            )
        return None

    def _decode_audio_value(
        self,
        value: Any,
        container: Mapping[str, Any],
        provider: Optional[str],
    ) -> Optional[AudioPayload]:
        content_type = self._first_string(container, ("content_type", "mime_type", "mime"))
        extension = self._first_string(container, ("extension", "format", "audio_format"))
        if isinstance(value, (bytes, bytearray)):
            return AudioPayload(
                data=bytes(value),
                content_type=content_type,
                extension=extension,
                provider=provider,
                metadata={"source": "bytes"},
            )
        if not isinstance(value, str):
            return None
        raw = value.strip()
        if raw.startswith("data:audio/") and "," in raw:
            header, body = raw.split(",", 1)
            try:
                data = base64.b64decode(body, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise VoiceSynthesisError("invalid audio data URI") from exc
            media = header[5:].split(";", 1)[0]
            return AudioPayload(
                data=data,
                content_type=f"audio/{media}",
                extension=media.split("+")[-1],
                provider=provider,
                metadata={"source": "data_uri"},
            )
        # Base64-looking payload; do not decode arbitrary text unless it is
        # sufficiently large and alphabetically valid.
        compact = "".join(raw.split())
        if len(compact) >= 128 and len(compact) % 4 == 0 and re.fullmatch(r"[A-Za-z0-9+/=_-]+", compact):
            try:
                data = base64.b64decode(compact, validate=True)
            except binascii.Error:
                try:
                    data = base64.urlsafe_b64decode(compact)
                except (binascii.Error, ValueError):
                    data = None
            if data:
                return AudioPayload(
                    data=data,
                    content_type=content_type,
                    extension=extension,
                    provider=provider,
                    metadata={"source": "base64"},
                )
        return None

    @staticmethod
    def _first_string(mapping: Mapping[str, Any], keys: Sequence[str]) -> Optional[str]:
        for key in keys:
            value = mapping.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    @staticmethod
    def _looks_like_external_uri(value: str) -> bool:
        return bool(re.match(r"^https?://", value, re.IGNORECASE))

    def _verify_audio(
        self,
        data: bytes,
        requested_format: VoiceFormat,
        content_type: Optional[str],
    ) -> None:
        if not data:
            raise VoiceAssetError("empty audio payload")
        if len(data) > self.max_audio_bytes:
            raise VoiceAssetError("audio payload exceeds configured limit")

        # Magic-byte validation. Some providers use OGG/Opus for an 'opus' request.
        signatures = {
            VoiceFormat.WAV: (b"RIFF", 0),
            VoiceFormat.MP3: (b"ID3", 0),
            VoiceFormat.OGG: (b"OggS", 0),
            VoiceFormat.OPUS: (b"OggS", 0),
            VoiceFormat.FLAC: (b"fLaC", 0),
        }
        expected = signatures.get(requested_format)
        if expected and data[expected[1] : expected[1] + len(expected[0])] != expected[0]:
            # MP3 frames may omit ID3. Accept a valid frame sync as a secondary check.
            if requested_format is VoiceFormat.MP3 and len(data) >= 2:
                b0, b1 = data[0], data[1]
                if not (b0 == 0xFF and (b1 & 0xE0) == 0xE0):
                    raise VoiceAssetError("MP3 signature validation failed")
            else:
                raise VoiceAssetError(
                    f"audio container signature mismatch for requested {requested_format.value}"
                )

        if content_type:
            normalized = content_type.lower().split(";", 1)[0].strip()
            if not normalized.startswith("audio/"):
                raise VoiceAssetError("provider returned a non-audio content type")

    async def _materialize_payload(
        self,
        request: VoiceSynthesisRequest,
        payload: AudioPayload,
        *,
        key: str,
        cached: bool,
    ) -> VoiceArtifact:
        started = time.perf_counter()
        asset_id = f"voice_{key[:24]}"
        extension = self._safe_extension(payload.extension, request.output_format)
        if payload.external_uri and payload.data is None:
            return VoiceArtifact(
                asset_id=asset_id,
                game_id=request.game_id,
                task_type=request.task_type.value,
                path=None,
                external_uri=payload.external_uri,
                format=extension,
                content_type=payload.content_type,
                size_bytes=0,
                sha256=None,
                verified=True,
                provider=payload.provider,
                request_id=str(payload.metadata.get("request_id") or asset_id),
                duration_ms=(time.perf_counter() - started) * 1000.0,
                cached=cached,
                metadata={**payload.metadata, "cache_key": key},
            )

        if payload.data is None:
            raise VoiceAssetError("cannot materialize empty voice payload")
        digest = hashlib.sha256(payload.data).hexdigest()
        destination = self._artifact_path(request, asset_id, extension)
        destination.parent.mkdir(parents=True, exist_ok=True)
        await self._atomic_write(destination, payload.data)
        await self._inc("bytes_written", len(payload.data))
        return VoiceArtifact(
            asset_id=asset_id,
            game_id=request.game_id,
            task_type=request.task_type.value,
            path=str(destination),
            external_uri=None,
            format=extension,
            content_type=payload.content_type,
            size_bytes=len(payload.data),
            sha256=digest,
            verified=True,
            provider=payload.provider,
            request_id=str(payload.metadata.get("request_id") or asset_id),
            duration_ms=(time.perf_counter() - started) * 1000.0,
            cached=cached,
            metadata={**payload.metadata, "cache_key": key},
        )

    async def _atomic_write(self, destination: Path, data: bytes) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp_file: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{destination.name}.",
                dir=destination.parent,
                delete=False,
            ) as handle:
                temp_file = Path(handle.name)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            temp_file.replace(destination)
        except OSError as exc:
            raise VoiceAssetError(f"failed to atomically persist voice asset: {exc}") from exc
        finally:
            if temp_file and temp_file.exists():
                temp_file.unlink(missing_ok=True)

    async def normalize(
        self,
        artifact: VoiceArtifact,
        *,
        output_format: VoiceFormat,
        sample_rate: Optional[int] = None,
        channels: Optional[int] = None,
    ) -> VoiceArtifact:
        """Optionally normalize an on-disk artifact using ffmpeg, never silently fake success."""
        if not artifact.path:
            raise VoiceAssetError("external assets cannot be normalized locally")
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise VoiceUnavailableError("ffmpeg is required for local audio normalization")

        source = Path(artifact.path).resolve()
        if not source.is_file():
            raise VoiceAssetError("voice artifact file does not exist")
        output = source.with_name(f"{source.stem}.normalized.{output_format.value}")
        command = [ffmpeg, "-y", "-i", str(source)]
        if sample_rate:
            command += ["-ar", str(sample_rate)]
        if channels:
            command += ["-ac", str(channels)]
        command.append(str(output))

        async def run() -> tuple[int, bytes, bytes]:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=DEFAULT_FFMPEG_TIMEOUT
            )
            return int(proc.returncode or 0), stdout, stderr

        try:
            return_code, _stdout, stderr = await run()
        except asyncio.TimeoutError as exc:
            raise VoiceAssetError("ffmpeg normalization timed out") from exc
        if return_code != 0 or not output.exists() or output.stat().st_size == 0:
            raise VoiceAssetError(
                f"ffmpeg normalization failed: {stderr.decode('utf-8', 'replace')[-2000:]}"
            )
        data = output.read_bytes()
        self._verify_audio(data, output_format, None)
        digest = hashlib.sha256(data).hexdigest()
        return VoiceArtifact(
            asset_id=f"{artifact.asset_id}_normalized",
            game_id=artifact.game_id,
            task_type=artifact.task_type,
            path=str(output),
            external_uri=None,
            format=output_format.value,
            content_type=f"audio/{output_format.value}",
            size_bytes=len(data),
            sha256=digest,
            verified=True,
            provider=artifact.provider,
            request_id=artifact.request_id,
            duration_ms=artifact.duration_ms,
            cached=False,
            metadata={**artifact.metadata, "normalized": True},
        )

    def stats(self) -> dict[str, Any]:
        return asdict(self._stats)

    async def _inc(self, field_name: str, amount: int = 1) -> None:
        async with self._stats_lock:
            setattr(self._stats, field_name, getattr(self._stats, field_name) + amount)

    async def _set_active(self, delta: int) -> None:
        async with self._stats_lock:
            self._stats.active = max(0, self._stats.active + delta)
            self._stats.peak_active = max(self._stats.peak_active, self._stats.active)


# ---------------------------------------------------------------------------
# Compatibility singleton / helpers
# ---------------------------------------------------------------------------
voice_engine = VoiceEngine()


async def synthesize_voice(
    text: str,
    game_id: str,
    *,
    voice_id: Optional[str] = None,
    language: Optional[str] = None,
    output_format: str = "mp3",
    task_type: str = "tts",
    metadata: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Compatibility helper for existing/legacy callers."""
    request = VoiceSynthesisRequest(
        text=text,
        game_id=game_id,
        task_type=VoiceTaskType(task_type),
        profile=VoiceProfile(voice_id=voice_id, language=language),
        output_format=VoiceFormat(output_format),
        metadata=dict(metadata or {}),
    )
    await voice_engine.startup()
    result = await voice_engine.synthesize(request)
    return result.to_dict()


__all__ = [
    "AudioPayload",
    "VoiceArtifact",
    "VoiceEngine",
    "VoiceError",
    "VoiceFormat",
    "VoiceProfile",
    "VoiceSynthesisError",
    "VoiceSynthesisRequest",
    "VoiceTaskType",
    "VoiceUnavailableError",
    "VoiceValidationError",
    "synthesize_voice",
    "voice_engine",
]
