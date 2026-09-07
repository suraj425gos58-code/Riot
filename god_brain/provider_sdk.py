"""
provider_sdk.py

Provider-neutral configuration and adapter contracts.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Mapping, Optional, Sequence

from .circuit_breaker import PROVIDER_EXECUTOR
from .connection_pool import HTTP_CLIENT


# ============================================================
# ENUMS
# ============================================================

class AuthenticationType(str, Enum):
    NONE = "none"
    BEARER = "bearer"
    API_KEY_HEADER = "api_key_header"
    API_KEY_QUERY = "api_key_query"
    CUSTOM = "custom"


class ProviderProtocol(str, Enum):
    REST = "rest"
    OPENAI_COMPATIBLE = "openai_compatible"
    CUSTOM = "custom"


# ============================================================
# REQUEST / RESPONSE
# ============================================================

@dataclass(slots=True)
class PromptRequest:
    prompt: str
    system_prompt: Optional[str] = None
    temperature: float = 0.7
    max_tokens: Optional[int] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if not isinstance(self.prompt, str):
            raise TypeError("prompt must be a string")

        if not self.prompt.strip():
            raise ValueError("prompt must not be empty")

        if not 0.0 <= self.temperature <= 2.0:
            raise ValueError(
                "temperature must be between 0.0 and 2.0"
            )

        if (
            self.max_tokens is not None
            and self.max_tokens <= 0
        ):
            raise ValueError(
                "max_tokens must be > 0"
            )


@dataclass(slots=True)
class ProviderResponse:
    success: bool
    provider: str
    output: str
    raw_response: Optional[Any] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


# ============================================================
# AUTH
# ============================================================

@dataclass(slots=True)
class AuthenticationConfig:
    type: AuthenticationType = AuthenticationType.BEARER

    token: Optional[str] = None

    header_name: str = "Authorization"

    query_name: str = "key"

    prefix: str = "Bearer"

    custom_headers: Dict[str, str] = field(
        default_factory=dict
    )

    def validate(self) -> None:
        if not self.header_name.strip():
            raise ValueError(
                "header_name must not be empty"
            )

        if (
            self.type
            in {
                AuthenticationType.BEARER,
                AuthenticationType.API_KEY_HEADER,
                AuthenticationType.API_KEY_QUERY,
            }
            and not self.token
        ):
            raise ValueError(
                f"token is required for auth type "
                f"{self.type.value}"
            )

        if (
            self.type == AuthenticationType.API_KEY_QUERY
            and not self.query_name.strip()
        ):
            raise ValueError(
                "query_name is required for API_KEY_QUERY"
            )


# ============================================================
# PAYLOAD
# ============================================================

@dataclass(slots=True)
class PayloadMapping:
    prompt_field: str = "prompt"

    system_field: Optional[str] = None

    temperature_field: Optional[str] = "temperature"

    max_tokens_field: Optional[str] = "max_tokens"

    metadata_field: Optional[str] = None

    fixed_fields: Dict[str, Any] = field(
        default_factory=dict
    )

    def validate(self) -> None:
        if not self.prompt_field.strip():
            raise ValueError(
                "prompt_field must not be empty"
            )


@dataclass(slots=True)
class ResponseMapping:
    output_path: Sequence[str] = field(
        default_factory=lambda: ("output",)
    )

    def validate(self) -> None:
        if not self.output_path:
            raise ValueError(
                "output_path must not be empty"
            )

        if any(
            not isinstance(part, str) or not part
            for part in self.output_path
        ):
            raise ValueError(
                "output_path must contain valid string keys"
            )


# ============================================================
# PROVIDER CONFIGURATION
# ============================================================

@dataclass(slots=True)
class ProviderConfiguration:
    name: str
    endpoint: str

    protocol: ProviderProtocol = (
        ProviderProtocol.REST
    )

    enabled: bool = True

    timeout_seconds: int = 60

    authentication: AuthenticationConfig = field(
        default_factory=AuthenticationConfig
    )

    payload: PayloadMapping = field(
        default_factory=PayloadMapping
    )

    response: ResponseMapping = field(
        default_factory=ResponseMapping
    )

    default_headers: Dict[str, str] = field(
        default_factory=dict
    )

    metadata: Dict[str, Any] = field(
        default_factory=dict
    )

    def validate(self) -> None:
        if not self.name.strip():
            raise ValueError(
                "provider name must not be empty"
            )

        if not self.endpoint.strip():
            raise ValueError(
                "provider endpoint must not be empty"
            )

        if not (
            self.endpoint.startswith("http://")
            or self.endpoint.startswith("https://")
        ):
            raise ValueError(
                "provider endpoint must be HTTP(S)"
            )

        if self.timeout_seconds <= 0:
            raise ValueError(
                "timeout_seconds must be > 0"
            )

        self.authentication.validate()
        self.payload.validate()
        self.response.validate()


# ============================================================
# ADAPTER
# ============================================================

class ProviderAdapter(ABC):
    def __init__(
        self,
        configuration: ProviderConfiguration,
    ) -> None:
        configuration.validate()
        self.configuration = configuration

    @property
    def session(self):
        return HTTP_CLIENT.session()

    @property
    def executor(self):
        return PROVIDER_EXECUTOR

    @property
    def provider_name(self) -> str:
        return self.configuration.name

    @abstractmethod
    def build_headers(self) -> Dict[str, str]:
        ...

    @abstractmethod
    def build_payload(
        self,
        request: PromptRequest,
    ) -> Dict[str, Any]:
        ...

    @abstractmethod
    async def invoke(
        self,
        request: PromptRequest,
    ) -> ProviderResponse:
        ...


# ============================================================
# BASE REST ADAPTER
# ============================================================

class BaseRESTAdapter(ProviderAdapter):

    def build_headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        headers.update(
            self.configuration.default_headers
        )

        auth = self.configuration.authentication

        if auth.type == AuthenticationType.BEARER:
            if auth.token:
                headers[auth.header_name] = (
                    f"{auth.prefix} {auth.token}"
                )

        elif (
            auth.type
            == AuthenticationType.API_KEY_HEADER
        ):
            if auth.token:
                headers[auth.header_name] = auth.token

        headers.update(auth.custom_headers)

        return headers

    def build_payload(
        self,
        request: PromptRequest,
    ) -> Dict[str, Any]:
        request.validate()

        mapping = self.configuration.payload

        payload: Dict[str, Any] = {}

        payload.update(mapping.fixed_fields)

        payload[mapping.prompt_field] = request.prompt

        if (
            mapping.system_field
            and request.system_prompt
        ):
            payload[mapping.system_field] = (
                request.system_prompt
            )

        if mapping.temperature_field:
            payload[
                mapping.temperature_field
            ] = request.temperature

        if (
            mapping.max_tokens_field
            and request.max_tokens is not None
        ):
            payload[
                mapping.max_tokens_field
            ] = request.max_tokens

        if (
            mapping.metadata_field
            and request.metadata
        ):
            payload[
                mapping.metadata_field
            ] = request.metadata

        return payload

    def build_query_params(self) -> Dict[str, str]:
        auth = self.configuration.authentication

        if (
            auth.type
            == AuthenticationType.API_KEY_QUERY
        ):
            if not auth.token:
                raise ValueError(
                    "API query authentication requires token"
                )

            return {
                auth.query_name: auth.token
            }

        return {}

    async def invoke(
        self,
        request: PromptRequest,
    ) -> ProviderResponse:
        raise NotImplementedError(
            "Concrete adapters must implement invoke()."
        )


# ============================================================
# REGISTRY
# ============================================================

class ProviderRegistry:
    def __init__(self) -> None:
        self._configs: Dict[
            str,
            ProviderConfiguration,
        ] = {}

        self._adapters: Dict[
            str,
            type[ProviderAdapter],
        ] = {}

    def register(
        self,
        configuration: ProviderConfiguration,
        adapter: type[ProviderAdapter],
    ) -> None:
        configuration.validate()

        if not issubclass(
            adapter,
            ProviderAdapter,
        ):
            raise TypeError(
                "adapter must subclass ProviderAdapter"
            )

        self._configs[configuration.name] = (
            configuration
        )

        self._adapters[configuration.name] = adapter

    def unregister(self, provider: str) -> None:
        self._configs.pop(provider, None)
        self._adapters.pop(provider, None)

    def exists(self, provider: str) -> bool:
        return provider in self._configs

    def configuration(
        self,
        provider: str,
    ) -> ProviderConfiguration:
        try:
            return self._configs[provider]
        except KeyError as exc:
            raise KeyError(
                f"Unknown provider: {provider}"
            ) from exc

    def create(
        self,
        provider: str,
    ) -> ProviderAdapter:
        configuration = self.configuration(
            provider
        )

        try:
            adapter_cls = self._adapters[provider]
        except KeyError as exc:
            raise KeyError(
                f"No adapter registered for provider: {provider}"
            ) from exc

        return adapter_cls(configuration)

    def providers(
        self,
    ) -> Mapping[
        str,
        ProviderConfiguration,
    ]:
        return dict(self._configs)


PROVIDER_REGISTRY = ProviderRegistry()
