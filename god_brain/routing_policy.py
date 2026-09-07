"""
routing_policy.py

Pure provider eligibility and health-based routing policy.

No HTTP.
No provider execution.
No API calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Iterable, List, Mapping, Optional, Protocol

from .circuit_breaker import CIRCUIT_REGISTRY
from .provider_sdk import PromptRequest


# ============================================================
# CAPABILITIES
# ============================================================

class ProviderCapability(str, Enum):
    GENERAL = "general"
    CHAT = "chat"
    CODE = "code"
    REASONING = "reasoning"
    CREATIVE = "creative"
    TOOLS = "tools"
    EMBEDDING = "embedding"
    AUDIO = "audio"
    VISION = "vision"


# ============================================================
# REQUEST
# ============================================================

@dataclass(slots=True)
class RoutingRequest:
    prompt: PromptRequest

    required_capabilities: set[
        ProviderCapability
    ] = field(default_factory=set)

    preferred_provider: Optional[str] = None

    excluded_providers: set[str] = field(
        default_factory=set
    )

    metadata: Dict[str, Any] = field(
        default_factory=dict
    )


# ============================================================
# PROVIDER
# ============================================================

@dataclass(slots=True)
class ProviderDescriptor:
    name: str

    enabled: bool = True

    priority: int = 100

    capabilities: set[
        ProviderCapability
    ] = field(default_factory=set)

    metadata: Dict[str, Any] = field(
        default_factory=dict
    )


# ============================================================
# NORMALIZATION
# ============================================================

@dataclass(slots=True)
class NormalizedPayload:
    body: Dict[str, Any]

    headers: Dict[str, str] = field(
        default_factory=dict
    )

    metadata: Dict[str, Any] = field(
        default_factory=dict
    )


class PayloadBuilder(Protocol):
    def build(
        self,
        request: RoutingRequest,
    ) -> NormalizedPayload:
        ...


class RequestNormalizer:
    def normalize(
        self,
        request: RoutingRequest,
    ) -> NormalizedPayload:
        raise NotImplementedError


class ProviderAdapterSkeleton(
    RequestNormalizer
):
    def __init__(
        self,
        descriptor: ProviderDescriptor,
    ) -> None:
        self.descriptor = descriptor

    def normalize(
        self,
        request: RoutingRequest,
    ) -> NormalizedPayload:
        return self.build_payload(request)

    def build_payload(
        self,
        request: RoutingRequest,
    ) -> NormalizedPayload:
        raise NotImplementedError


class GenericJSONAdapter(
    ProviderAdapterSkeleton
):
    def build_payload(
        self,
        request: RoutingRequest,
    ) -> NormalizedPayload:
        request.prompt.validate()

        return NormalizedPayload(
            body={
                "prompt": request.prompt.prompt,
                "system_prompt": (
                    request.prompt.system_prompt
                ),
                "temperature": (
                    request.prompt.temperature
                ),
                "max_tokens": (
                    request.prompt.max_tokens
                ),
                "metadata": dict(
                    request.prompt.metadata
                ),
            }
        )


# ============================================================
# ROUTING POLICY
# ============================================================

class HealthRoutingPolicy:
    """
    Pure deterministic provider selection.

    Eligibility is checked BEFORE scoring:
    1. enabled
    2. not excluded
    3. capabilities
    4. circuit availability

    Preferred provider is only a preference; it cannot bypass
    safety/eligibility rules.
    """

    def _eligible(
        self,
        provider: ProviderDescriptor,
        request: RoutingRequest,
    ) -> bool:

        if not provider.enabled:
            return False

        if provider.name in request.excluded_providers:
            return False

        required = request.required_capabilities

        if (
            required
            and not required.issubset(
                provider.capabilities
            )
        ):
            return False

        health = CIRCUIT_REGISTRY.health().get(
            provider.name
        )

        if (
            health is not None
            and health.state.value == "open"
        ):
            return False

        return True

    def _score(
        self,
        provider: ProviderDescriptor,
    ) -> tuple[float, int, str]:

        health = CIRCUIT_REGISTRY.health().get(
            provider.name
        )

        health_score = (
            100.0
            if health is None
            else health.score
        )

        return (
            health_score,
            -provider.priority,
            provider.name,
        )

    def select_provider(
        self,
        providers: Iterable[ProviderDescriptor],
        request: RoutingRequest,
    ) -> Optional[str]:

        provider_list = list(providers)

        # Preferred provider is valid only if it satisfies
        # EVERY eligibility rule.
        if request.preferred_provider:
            for provider in provider_list:
                if (
                    provider.name
                    == request.preferred_provider
                    and self._eligible(
                        provider,
                        request,
                    )
                ):
                    return provider.name

        candidates = [
            provider
            for provider in provider_list
            if self._eligible(
                provider,
                request,
            )
        ]

        if not candidates:
            return None

        candidates.sort(
            key=self._score,
            reverse=True,
        )

        return candidates[0].name


# ============================================================
# PROVIDER CATALOG
# ============================================================

class ProviderCatalog:
    def __init__(self) -> None:
        self._providers: Dict[
            str,
            ProviderDescriptor,
        ] = {}

    def register(
        self,
        provider: ProviderDescriptor,
    ) -> None:
        if not provider.name.strip():
            raise ValueError(
                "provider name must not be empty"
            )

        self._providers[
            provider.name
        ] = provider

    def unregister(
        self,
        provider_name: str,
    ) -> None:
        self._providers.pop(
            provider_name,
            None,
        )

    def get(
        self,
        provider_name: str,
    ) -> Optional[ProviderDescriptor]:
        return self._providers.get(
            provider_name
        )

    def all(self) -> List[
        ProviderDescriptor
    ]:
        return list(
            self._providers.values()
        )

    def enabled(self) -> List[
        ProviderDescriptor
    ]:
        return [
            provider
            for provider in self._providers.values()
            if provider.enabled
        ]

    def as_mapping(
        self,
    ) -> Mapping[
        str,
        ProviderDescriptor,
    ]:
        return dict(self._providers)


# ============================================================
# FACADE
# ============================================================

class RoutingPolicy:
    def __init__(self) -> None:
        self.catalog = ProviderCatalog()
        self.policy = HealthRoutingPolicy()

    def register(
        self,
        provider: ProviderDescriptor,
    ) -> None:
        self.catalog.register(provider)

    def unregister(
        self,
        provider_name: str,
    ) -> None:
        self.catalog.unregister(
            provider_name
        )

    def choose(
        self,
        request: RoutingRequest,
    ) -> Optional[str]:
        return self.policy.select_provider(
            self.catalog.enabled(),
            request,
        )


DEFAULT_ROUTING_POLICY = RoutingPolicy()
