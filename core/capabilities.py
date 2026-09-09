"""
Riot / God Node — Dynamic Capability Registry

The registry detects optional runtime capabilities without hard-coding
feature availability into the application bootstrap.

Design goals:
- never crash because an optional feature is missing
- expose deterministic capability state
- avoid importing heavy SDKs until necessary
- provide diagnostics for startup / health endpoints
- support future providers without rewriting core boot logic
"""

from __future__ import annotations

import importlib.util
import logging
import shutil
from dataclasses import dataclass
from typing import Dict, Iterable, Mapping, Optional

logger = logging.getLogger("GodNode.Capabilities")


@dataclass(frozen=True, slots=True)
class CapabilitySpec:
    """Definition of one optional/runtime capability."""

    name: str
    packages: tuple[str, ...] = ()
    executables: tuple[str, ...] = ()
    description: str = ""
    optional: bool = True


@dataclass(frozen=True, slots=True)
class CapabilityState:
    """Detected state of a capability."""

    name: str
    available: bool
    missing_packages: tuple[str, ...] = ()
    missing_executables: tuple[str, ...] = ()
    description: str = ""

    @property
    def reason(self) -> Optional[str]:
        problems = []

        if self.missing_packages:
            problems.append(
                "missing packages: " + ", ".join(self.missing_packages)
            )

        if self.missing_executables:
            problems.append(
                "missing executables: " + ", ".join(self.missing_executables)
            )

        return "; ".join(problems) if problems else None


# ---------------------------------------------------------------------------
# Canonical capability catalog
# ---------------------------------------------------------------------------

CAPABILITY_SPECS: tuple[CapabilitySpec, ...] = (
    CapabilitySpec(
        name="core_api",
        packages=("fastapi", "pydantic", "aiohttp"),
        description="Core HTTP/API runtime",
        optional=False,
    ),
    CapabilitySpec(
        name="openai",
        packages=("openai",),
        description="OpenAI provider support",
    ),
    CapabilitySpec(
        name="gemini",
        packages=("google.generativeai",),
        description="Google Gemini provider support",
    ),
    CapabilitySpec(
        name="s3_storage",
        packages=("boto3",),
        description="S3-compatible cloud object storage",
    ),
    CapabilitySpec(
        name="realtime",
        packages=("websockets", "socketio"),
        description="Realtime WebSocket / Socket.IO support",
    ),
    CapabilitySpec(
        name="self_evolution",
        packages=("github",),
        description="GitHub-backed self-evolution tooling",
    ),
    CapabilitySpec(
        name="numerical",
        packages=("numpy",),
        description="Numerical / simulation acceleration",
    ),
    CapabilitySpec(
        name="black",
        packages=("black",),
        description="Python source formatting / AST workflow support",
    ),
    CapabilitySpec(
        name="ffmpeg",
        executables=("ffmpeg",),
        description="Audio/video processing support",
    ),
    CapabilitySpec(
        name="git",
        executables=("git",),
        description="Source-control operations",
    ),
)


def _package_available(module_name: str) -> bool:
    """Check whether a Python package/module can be resolved."""
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def _executable_available(executable: str) -> bool:
    """Check whether an executable is available on PATH."""
    try:
        return shutil.which(executable) is not None
    except Exception:
        return False


def detect_capability(spec: CapabilitySpec) -> CapabilityState:
    """Resolve one capability without importing the actual optional SDK."""

    missing_packages = tuple(
        package
        for package in spec.packages
        if not _package_available(package)
    )

    missing_executables = tuple(
        executable
        for executable in spec.executables
        if not _executable_available(executable)
    )

    available = not missing_packages and not missing_executables

    return CapabilityState(
        name=spec.name,
        available=available,
        missing_packages=missing_packages,
        missing_executables=missing_executables,
        description=spec.description,
    )


def detect_capabilities(
    specs: Iterable[CapabilitySpec] = CAPABILITY_SPECS,
) -> Dict[str, CapabilityState]:
    """Detect all known capabilities."""

    states: Dict[str, CapabilityState] = {}

    for spec in specs:
        state = detect_capability(spec)
        states[state.name] = state

        if state.available:
            logger.info("Capability ONLINE: %s", state.name)
        elif spec.optional:
            logger.warning(
                "Optional capability OFFLINE: %s (%s)",
                state.name,
                state.reason or "not available",
            )
        else:
            logger.error(
                "Required capability OFFLINE: %s (%s)",
                state.name,
                state.reason or "not available",
            )

    return states


def available_capabilities(
    states: Mapping[str, CapabilityState],
) -> tuple[str, ...]:
    """Return enabled capability names in deterministic order."""

    return tuple(
        name
        for name, state in sorted(states.items())
        if state.available
    )


def capability_available(
    states: Mapping[str, CapabilityState],
    name: str,
) -> bool:
    """Safe lookup for feature gates."""

    state = states.get(name)
    return bool(state and state.available)


def capabilities_report(
    states: Mapping[str, CapabilityState],
) -> dict[str, object]:
    """Serializable diagnostics payload for health/debug endpoints."""

    return {
        "available": list(available_capabilities(states)),
        "capabilities": {
            name: {
                "available": state.available,
                "description": state.description,
                "missing_packages": list(state.missing_packages),
                "missing_executables": list(state.missing_executables),
                "reason": state.reason,
            }
            for name, state in sorted(states.items())
        },
    }


# Detect once at import time for lightweight consumers.
#
# This does not import optional SDKs; it only checks whether they are
# discoverable in the current Python environment.
CAPABILITIES: Dict[str, CapabilityState] = detect_capabilities()


def is_available(name: str) -> bool:
    """Convenience API for runtime feature gates."""
    return capability_available(CAPABILITIES, name)


__all__ = [
    "CAPABILITY_SPECS",
    "CAPABILITIES",
    "CapabilitySpec",
    "CapabilityState",
    "available_capabilities",
    "capabilities_report",
    "capability_available",
    "detect_capabilities",
    "detect_capability",
    "is_available",
]
