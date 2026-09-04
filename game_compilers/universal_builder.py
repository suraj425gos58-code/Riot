"""
Universal Build Orchestrator
============================

Production-oriented build boundary for Riot / God Node.

Design goals
------------
* Never manufacture an APK/EXE and call it a successful build.
* Treat generated project source as the source of truth.
* Detect the capabilities actually available on the host.
* Execute external build tools asynchronously with hard timeouts.
* Stage builds in isolated temporary workspaces.
* Prevent path traversal and accidental writes outside the workspace.
* Emit deterministic, hash-verified artifact metadata.
* Keep platform backends extensible without hard-coding vendor APIs.
* Preserve the public ``game_builder.build_game(config)`` contract used by main.py.

Supported targets
-----------------
* web     -> packaged static web artifact (ZIP)
* mobile  -> Android build when a real Android-capable project/toolchain exists
* pc      -> native/desktop build when a real desktop-capable project/toolchain exists

A target that cannot be built on the current host returns ``UNAVAILABLE`` or
``FAILED``.  It is never reported as ``SUCCESS`` with dummy bytes.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import uuid
import zipfile
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)


# ============================================================================
# CONSTANTS
# ============================================================================

DEFAULT_TIMEOUT_SECONDS = 15 * 60
DEFAULT_MAX_LOG_BYTES = 1_000_000
DEFAULT_MAX_SOURCE_FILE_BYTES = 25 * 1024 * 1024
DEFAULT_MAX_SOURCE_FILES = 20_000
DEFAULT_OUTPUT_ROOT = os.getenv("RIOT_BUILD_ROOT", "build_output")
DEFAULT_WORK_ROOT = os.getenv("RIOT_BUILD_WORK_ROOT", ".riot_build_work")
DEFAULT_MAX_TOTAL_SOURCE_BYTES = int(os.getenv("RIOT_BUILD_MAX_TOTAL_SOURCE_BYTES", str(250 * 1024 * 1024)))
DEFAULT_MAX_CONCURRENT_BUILDS = max(1, int(os.getenv("RIOT_BUILD_MAX_CONCURRENT_BUILDS", "2")))

_RESERVED_MOCK_MARKERS = (
    "Compiled by God Node",
    "Game Initialized",
    "DUMMY_EXE_BINARY_DATA",
    "DUMMY_APK_BINARY_DATA",
    "mock_config",
)

_SECRET_ENV_RE = re.compile(r"(?i)(token|secret|password|api[_-]?key|authorization|private[_-]?key)")


# ============================================================================
# ERRORS
# ============================================================================

class BuildError(RuntimeError):
    """Base class for builder errors."""


class BuildValidationError(BuildError):
    """Input/project validation failed."""


class BuildUnavailableError(BuildError):
    """Requested target cannot be built with the current capabilities."""


class BuildExecutionError(BuildError):
    """A real external build command failed."""


class BuildTimeoutError(BuildExecutionError):
    """External build exceeded its hard timeout."""


# ============================================================================
# CONTRACTS
# ============================================================================

class BuildStatus(str, Enum):
    QUEUED = "QUEUED"
    STAGING = "STAGING"
    BUILDING = "BUILDING"
    VALIDATING = "VALIDATING"
    SUCCESS = "SUCCESS"
    UNAVAILABLE = "UNAVAILABLE"
    FAILED = "FAILED"


class BuildTarget(str, Enum):
    WEB = "web"
    MOBILE = "mobile"
    PC = "pc"


@dataclass(slots=True, frozen=True)
class BuildLimits:
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    max_log_bytes: int = DEFAULT_MAX_LOG_BYTES
    max_source_file_bytes: int = DEFAULT_MAX_SOURCE_FILE_BYTES
    max_source_files: int = DEFAULT_MAX_SOURCE_FILES
    max_total_source_bytes: int = DEFAULT_MAX_TOTAL_SOURCE_BYTES


@dataclass(slots=True)
class BuildCommandResult:
    command: list[str]
    return_code: int
    duration_ms: float
    stdout: str
    stderr: str
    timed_out: bool = False


@dataclass(slots=True)
class ToolchainInfo:
    executable: str
    available: bool
    version: Optional[str] = None
    path: Optional[str] = None
    detail: Optional[str] = None


@dataclass(slots=True)
class BuildArtifact:
    path: str
    target: str
    size_bytes: int
    sha256: str
    media_type: str
    verified: bool


@dataclass(slots=True)
class BuildReport:
    build_id: str
    game_id: str
    target_platform: str
    status: BuildStatus
    started_at: float
    finished_at: Optional[float] = None
    duration_ms: float = 0.0
    workspace: Optional[str] = None
    artifact: Optional[BuildArtifact] = None
    commands: list[BuildCommandResult] = field(default_factory=list)
    toolchains: list[ToolchainInfo] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def success(self) -> bool:
        return self.status is BuildStatus.SUCCESS

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["status"] = self.status.value
        return result


# ============================================================================
# LOW-LEVEL HELPERS
# ============================================================================

def _safe_game_id(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise BuildValidationError("game_id is required")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", raw):
        raise BuildValidationError("game_id contains unsupported characters")
    return raw


def _normalize_target(value: Any) -> BuildTarget:
    raw = str(value or "").strip().lower()
    aliases = {
        "web_html5": BuildTarget.WEB,
        "html5": BuildTarget.WEB,
        "mobile_apk": BuildTarget.MOBILE,
        "android": BuildTarget.MOBILE,
        "apk": BuildTarget.MOBILE,
        "pc_exe": BuildTarget.PC,
        "windows": BuildTarget.PC,
        "desktop": BuildTarget.PC,
        "exe": BuildTarget.PC,
        "cloud_stream": BuildTarget.WEB,
    }
    try:
        return BuildTarget(raw)
    except ValueError:
        if raw in aliases:
            return aliases[raw]
        raise BuildValidationError(f"unsupported target_platform: {value!r}")


def _sanitize_relative_path(value: str) -> str:
    raw = str(value).replace("\\", "/").strip("/")
    if not raw:
        raise BuildValidationError("empty source path")
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts:
        raise BuildValidationError(f"unsafe source path: {value!r}")
    if any(part in {"", "."} for part in path.parts):
        raise BuildValidationError(f"invalid source path: {value!r}")
    return path.as_posix()


def _within(root: Path, candidate: Path) -> bool:
    root_resolved = root.resolve()
    candidate_resolved = candidate.resolve()
    try:
        candidate_resolved.relative_to(root_resolved)
        return True
    except ValueError:
        return False


def _sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _media_type_for(path: Path) -> str:
    suffix = path.suffix.lower()
    return {
        ".zip": "application/zip",
        ".apk": "application/vnd.android.package-archive",
        ".aab": "application/octet-stream",
        ".exe": "application/vnd.microsoft.portable-executable",
        ".msi": "application/x-msi",
    }.get(suffix, "application/octet-stream")


def _trim_log(value: str, max_bytes: int) -> str:
    encoded = (value or "").encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return value or ""
    return encoded[-max_bytes:].decode("utf-8", errors="replace")


def _display_env(env: Mapping[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in env.items():
        result[key] = "***REDACTED***" if _SECRET_ENV_RE.search(key) else value
    return result


def _detect_tool(executable: str, version_args: Sequence[str] = ("--version",)) -> ToolchainInfo:
    path = shutil.which(executable)
    if not path:
        return ToolchainInfo(executable=executable, available=False)
    version = None
    detail = None
    try:
        completed = subprocess.run(
            [path, *version_args],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        combined = (completed.stdout or completed.stderr or "").strip()
        version = combined.splitlines()[0][:300] if combined else None
        if completed.returncode != 0:
            detail = f"version command returned {completed.returncode}"
    except Exception as exc:  # pragma: no cover - defensive
        detail = str(exc)
    return ToolchainInfo(executable=executable, available=True, version=version, path=path, detail=detail)


# ============================================================================
# PROJECT EXTRACTION
# ============================================================================

class ProjectSourceLoader:
    """Normalizes several canonical/legacy project representations."""

    def load(self, config: Mapping[str, Any]) -> dict[str, bytes]:
        raw_files = self._find_files(config)
        if not raw_files:
            raise BuildValidationError(
                "No real project source was supplied. Expected source_bundle.files, files, "
                "or a project_directory."
            )

        files: dict[str, bytes] = {}
        for raw_path, raw_value in raw_files.items():
            safe_path = _sanitize_relative_path(str(raw_path))
            payload = self._coerce_bytes(raw_value)
            if not payload:
                raise BuildValidationError(f"empty source file is not allowed: {safe_path}")
            self._reject_mock_source(safe_path, payload)
            files[safe_path] = payload
        return files

    @staticmethod
    def _find_files(config: Mapping[str, Any]) -> Mapping[str, Any]:
        source_bundle = config.get("source_bundle")
        if isinstance(source_bundle, Mapping):
            nested = source_bundle.get("files")
            if isinstance(nested, Mapping):
                return nested

        direct = config.get("files")
        if isinstance(direct, Mapping):
            return direct

        project_directory = config.get("project_directory")
        if project_directory:
            root = Path(str(project_directory)).expanduser().resolve()
            if not root.is_dir():
                raise BuildValidationError(f"project_directory does not exist: {root}")
            result: dict[str, bytes] = {}
            ignored_dirs = {".git", ".hg", ".svn", "node_modules", ".gradle", "__pycache__", ".riot_build_work", "build_output"}
            for path in root.rglob("*"):
                relative_path = path.relative_to(root)
                if any(part in ignored_dirs for part in relative_path.parts):
                    continue
                if path.is_symlink():
                    raise BuildValidationError(f"symlinked project entry is not allowed: {relative_path.as_posix()}")
                if path.is_file():
                    mode = path.stat().st_mode
                    if not stat.S_ISREG(mode):
                        raise BuildValidationError(f"non-regular project file is not allowed: {relative_path.as_posix()}")
                    relative = relative_path.as_posix()
                    result[relative] = path.read_bytes()
            return result

        # Compatibility with the current caller, but only when actual content is supplied.
        legacy: dict[str, Any] = {}
        if isinstance(config.get("html_content"), str):
            legacy["index.html"] = config["html_content"]
        if isinstance(config.get("js_content"), str):
            legacy["game.js"] = config["js_content"]
        return legacy

    @staticmethod
    def _coerce_bytes(value: Any) -> bytes:
        if isinstance(value, bytes):
            return value
        if isinstance(value, bytearray):
            return bytes(value)
        if isinstance(value, str):
            return value.encode("utf-8")
        if isinstance(value, Mapping) and "content" in value:
            return ProjectSourceLoader._coerce_bytes(value["content"])
        raise BuildValidationError(f"unsupported source content type: {type(value).__name__}")

    @staticmethod
    def _reject_mock_source(path: str, payload: bytes) -> None:
        if not path.lower().endswith((".html", ".htm", ".js", ".ts", ".json", ".txt")):
            return
        text = payload[:512 * 1024].decode("utf-8", errors="ignore")
        for marker in _RESERVED_MOCK_MARKERS:
            if marker in text:
                raise BuildValidationError(
                    f"mock/placeholder source detected in {path!r}; refusing fake build success"
                )


# ============================================================================
# STAGING
# ============================================================================

class BuildWorkspace:
    """Ephemeral, path-safe staging area for one build transaction."""

    def __init__(self, work_root: str | Path, build_id: str):
        self.work_root = Path(work_root).expanduser().resolve()
        self.build_id = build_id
        self.root: Optional[Path] = None

    def create(self) -> Path:
        self.work_root.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix=f"{self.build_id}-", dir=self.work_root))
        return self.root

    def materialize(self, files: Mapping[str, bytes], limits: BuildLimits) -> Path:
        if self.root is None:
            raise BuildError("workspace has not been created")
        if len(files) > limits.max_source_files:
            raise BuildValidationError("project contains too many source files")

        total_bytes = 0
        for relative, payload in files.items():
            if len(payload) > limits.max_source_file_bytes:
                raise BuildValidationError(f"source file too large: {relative}")
            total_bytes += len(payload)
            if total_bytes > limits.max_total_source_bytes:
                raise BuildValidationError(f"project source exceeds {limits.max_total_source_bytes} bytes")
            destination = self.root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not _within(self.root, destination):
                raise BuildValidationError(f"path escaped workspace: {relative}")
            destination.write_bytes(payload)

        (self.root / ".riot-build-manifest.json").write_text(
            json.dumps(
                {
                    "build_id": self.build_id,
                    "files": sorted(files.keys()),
                    "total_source_bytes": total_bytes,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return self.root

    def cleanup(self) -> None:
        if self.root and self.root.exists():
            shutil.rmtree(self.root, ignore_errors=True)
        self.root = None


# ============================================================================
# COMMAND EXECUTION
# ============================================================================

class AsyncCommandRunner:
    """Runs real external processes without blocking the asyncio event loop."""

    def __init__(self, limits: BuildLimits):
        self.limits = limits

    async def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        env: Optional[Mapping[str, str]] = None,
        timeout_seconds: Optional[float] = None,
    ) -> BuildCommandResult:
        started = time.perf_counter()
        timeout = timeout_seconds or self.limits.timeout_seconds
        safe_command = [str(part) for part in command]
        logger.info("Build command: %s", " ".join(safe_command))

        process_env = self._build_process_env(env)
        process = None

        try:
            process = await asyncio.create_subprocess_exec(
                *safe_command,
                cwd=str(cwd),
                env=process_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=(os.name != "nt"),
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError as exc:
            await self._terminate_process_tree(process)
            raise BuildTimeoutError(
                f"build command timed out after {timeout:.0f}s: {safe_command[0]}"
            ) from exc
        except FileNotFoundError as exc:
            raise BuildUnavailableError(f"required executable unavailable: {safe_command[0]}") from exc
        except OSError as exc:
            raise BuildExecutionError(f"failed to start build command: {exc}") from exc

        duration_ms = (time.perf_counter() - started) * 1000.0
        result = BuildCommandResult(
            command=safe_command,
            return_code=int(process.returncode or 0),
            duration_ms=duration_ms,
            stdout=_trim_log(stdout_bytes.decode("utf-8", errors="replace"), self.limits.max_log_bytes),
            stderr=_trim_log(stderr_bytes.decode("utf-8", errors="replace"), self.limits.max_log_bytes),
        )
        if result.return_code != 0:
            raise BuildExecutionError(
                f"build command failed with exit code {result.return_code}: {result.stderr[-2000:] or result.stdout[-2000:]}"
            )
        return result


    @staticmethod
    def _build_process_env(extra: Optional[Mapping[str, str]]) -> dict[str, str]:
        """Pass only build-essential environment variables by default.

        Provider/API credentials are intentionally not inherited by arbitrary build scripts.
        Projects that genuinely need custom values must supply them through build_env.
        """
        allowed = {
            "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TMP", "TEMP",
            "HOME", "USERPROFILE", "JAVA_HOME", "ANDROID_HOME", "ANDROID_SDK_ROOT",
            "ANDROID_NDK_HOME", "GRADLE_USER_HOME", "NODE_PATH", "CI",
        }
        result = {k: v for k, v in os.environ.items() if k in allowed}
        if "PATH" not in result:
            result["PATH"] = os.defpath
        if extra:
            for key, value in extra.items():
                result[str(key)] = str(value)
        return result

    @staticmethod
    async def _terminate_process_tree(process: Any) -> None:
        if process is None:
            return
        try:
            if process.returncode is not None:
                return
            if os.name != "nt":
                import signal
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            else:
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except asyncio.TimeoutError:
                if os.name != "nt":
                    import signal
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                else:
                    process.kill()
                await process.wait()
        except ProcessLookupError:
            return
        except Exception:
            try:
                process.kill()
                await process.wait()
            except Exception:
                pass


# ============================================================================
# BACKENDS
# ============================================================================

class BuildBackend:
    """Abstract platform backend."""

    target: BuildTarget

    def detect(self) -> list[ToolchainInfo]:
        raise NotImplementedError

    async def build(
        self,
        workspace: Path,
        output_dir: Path,
        config: Mapping[str, Any],
        runner: AsyncCommandRunner,
        report: BuildReport,
    ) -> Path:
        raise NotImplementedError


class WebBackend(BuildBackend):
    target = BuildTarget.WEB

    def detect(self) -> list[ToolchainInfo]:
        return [_detect_tool("zip")]

    async def build(self, workspace, output_dir, config, runner, report) -> Path:
        index = workspace / "index.html"
        if not index.exists():
            raise BuildValidationError("web build requires index.html")

        output_dir.mkdir(parents=True, exist_ok=True)
        destination = output_dir / f"{report.game_id}-{report.build_id}.zip"

        manifest = {
            "schema": "riot.artifact.v1",
            "build_id": report.build_id,
            "game_id": report.game_id,
            "target": self.target.value,
            "generated_at": time.time(),
        }
        (workspace / "artifact-manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )

        def _zip() -> None:
            with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
                for path in sorted(workspace.rglob("*")):
                    if not path.is_file():
                        continue
                    if path == destination:
                        continue
                    archive.write(path, path.relative_to(workspace).as_posix())

        await asyncio.to_thread(_zip)
        return destination


class AndroidBackend(BuildBackend):
    target = BuildTarget.MOBILE

    def detect(self) -> list[ToolchainInfo]:
        tools = [
            _detect_tool("java"),
            _detect_tool("gradle"),
        ]
        if os.getenv("ANDROID_HOME"):
            tools.append(ToolchainInfo("ANDROID_HOME", True, path=os.getenv("ANDROID_HOME")))
        elif os.getenv("ANDROID_SDK_ROOT"):
            tools.append(ToolchainInfo("ANDROID_SDK_ROOT", True, path=os.getenv("ANDROID_SDK_ROOT")))
        else:
            tools.append(ToolchainInfo("ANDROID_SDK", False, detail="ANDROID_HOME/ANDROID_SDK_ROOT not set"))
        return tools

    async def build(self, workspace, output_dir, config, runner, report) -> Path:
        gradlew = workspace / ("gradlew.bat" if os.name == "nt" else "gradlew")
        gradle = shutil.which("gradle")
        android_project = (workspace / "settings.gradle").exists() or (workspace / "settings.gradle.kts").exists()
        if not android_project:
            raise BuildUnavailableError(
                "Android target requires a real Gradle Android project in the generated source bundle"
            )
        if gradlew.exists():
            executable = str(gradlew)
            if os.name != "nt":
                gradlew.chmod(gradlew.stat().st_mode | 0o111)
        elif gradle:
            executable = gradle
        else:
            raise BuildUnavailableError("Gradle/gradlew is not available")

        output_dir.mkdir(parents=True, exist_ok=True)
        command = [executable, "assembleRelease", "--no-daemon", "--stacktrace"]
        result = await runner.run(
            command,
            cwd=workspace,
            env=config.get("build_env") if isinstance(config.get("build_env"), Mapping) else None,
        )
        report.commands.append(result)

        candidates = list(workspace.rglob("*.apk")) + list(workspace.rglob("*.aab"))
        candidates = [p for p in candidates if p.is_file() and "build" in p.parts]
        if not candidates:
            raise BuildExecutionError("Gradle completed but produced no APK/AAB artifact")
        artifact = max(candidates, key=lambda p: p.stat().st_mtime)
        destination = output_dir / artifact.name
        shutil.copy2(artifact, destination)
        return destination


class PcBackend(BuildBackend):
    target = BuildTarget.PC

    def detect(self) -> list[ToolchainInfo]:
        return [
            _detect_tool("node"),
            _detect_tool("npm"),
            _detect_tool("pnpm"),
            _detect_tool("yarn"),
            _detect_tool("electron-builder"),
        ]

    async def build(self, workspace, output_dir, config, runner, report) -> Path:
        package_json = workspace / "package.json"
        if not package_json.exists():
            raise BuildUnavailableError(
                "PC target requires a real desktop/web project with package.json"
            )

        try:
            package = json.loads(package_json.read_text(encoding="utf-8"))
        except Exception as exc:
            raise BuildValidationError(f"invalid package.json: {exc}") from exc

        scripts = package.get("scripts") if isinstance(package, Mapping) else None
        if not isinstance(scripts, Mapping) or not scripts.get("build"):
            raise BuildUnavailableError(
                "PC target requires package.json scripts.build plus a real desktop builder configuration"
            )

        npm = shutil.which("npm")
        if not npm:
            raise BuildUnavailableError("npm is not available")

        install_needed = not (workspace / "node_modules").exists()
        if install_needed:
            install = await runner.run(
                [npm, "ci", "--no-audit", "--no-fund"],
                cwd=workspace,
                env=config.get("build_env") if isinstance(config.get("build_env"), Mapping) else None,
            )
            report.commands.append(install)

        build = await runner.run(
            [npm, "run", "build"],
            cwd=workspace,
            env=config.get("build_env") if isinstance(config.get("build_env"), Mapping) else None,
        )
        report.commands.append(build)

        configured_candidates = config.get("pc_artifact_patterns")
        candidates: list[Path] = []
        if isinstance(configured_candidates, Sequence) and not isinstance(configured_candidates, (str, bytes)):
            for pattern in configured_candidates:
                candidates.extend(workspace.glob(str(pattern)))
        if not candidates:
            for pattern in ("dist/**/*.exe", "release/**/*.exe", "build/**/*.exe", "dist/**/*.msi", "release/**/*.msi"):
                candidates.extend(workspace.glob(pattern))

        candidates = [p for p in candidates if p.is_file()]
        if not candidates:
            raise BuildExecutionError(
                "desktop build command completed but no .exe/.msi artifact was found"
            )
        artifact = max(candidates, key=lambda p: p.stat().st_mtime)
        output_dir.mkdir(parents=True, exist_ok=True)
        destination = output_dir / artifact.name
        shutil.copy2(artifact, destination)
        return destination


# ============================================================================
# BUILDER CORE
# ============================================================================

class UniversalBuilder:
    """Coordinates source validation, platform selection, real build, and artifact verification."""

    def __init__(
        self,
        *,
        output_root: str | Path = DEFAULT_OUTPUT_ROOT,
        work_root: str | Path = DEFAULT_WORK_ROOT,
        limits: Optional[BuildLimits] = None,
    ) -> None:
        self.output_root = Path(output_root).expanduser().resolve()
        self.work_root = Path(work_root).expanduser().resolve()
        self.limits = limits or BuildLimits(
            timeout_seconds=int(os.getenv("RIOT_BUILD_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)),
            max_log_bytes=int(os.getenv("RIOT_BUILD_MAX_LOG_BYTES", DEFAULT_MAX_LOG_BYTES)),
            max_source_file_bytes=int(
                os.getenv("RIOT_BUILD_MAX_SOURCE_FILE_BYTES", DEFAULT_MAX_SOURCE_FILE_BYTES)
            ),
            max_source_files=int(os.getenv("RIOT_BUILD_MAX_SOURCE_FILES", DEFAULT_MAX_SOURCE_FILES)),
            max_total_source_bytes=int(os.getenv("RIOT_BUILD_MAX_TOTAL_SOURCE_BYTES", DEFAULT_MAX_TOTAL_SOURCE_BYTES)),
        )
        self.loader = ProjectSourceLoader()
        self.runner = AsyncCommandRunner(self.limits)
        self.backends: dict[BuildTarget, BuildBackend] = {
            BuildTarget.WEB: WebBackend(),
            BuildTarget.MOBILE: AndroidBackend(),
            BuildTarget.PC: PcBackend(),
        }
        self._build_semaphore = asyncio.Semaphore(DEFAULT_MAX_CONCURRENT_BUILDS)

    async def build_game(self, config: Mapping[str, Any]) -> dict[str, Any]:
        """Build a game project and return a JSON-serializable report."""
        if hasattr(config, "to_builder_config") and callable(getattr(config, "to_builder_config")):
            config = config.to_builder_config()
        if not isinstance(config, Mapping):
            raise BuildValidationError("build_game expects a mapping or canonical GameProject")

        incoming_build_id = str(config.get("build_id") or "").strip()
        build_id = incoming_build_id or f"BUILD_{uuid.uuid4().hex}"
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", build_id):
            raise BuildValidationError("build_id contains unsupported characters")
        game_id = _safe_game_id(config.get("game_id"))
        target = _normalize_target(config.get("target_platform"))
        started = time.time()
        report = BuildReport(
            build_id=build_id,
            game_id=game_id,
            target_platform=target.value,
            status=BuildStatus.QUEUED,
            started_at=started,
        )
        workspace = BuildWorkspace(self.work_root, build_id)

        async with self._build_semaphore:
            try:
                report.status = BuildStatus.STAGING
                files = self.loader.load(config)
                root = workspace.create()
                report.workspace = str(root)
                workspace.materialize(files, self.limits)

                backend = self.backends[target]
                report.toolchains = backend.detect()

                # Web only needs a packager; mobile/PC require actual native build infrastructure.
                if target is not BuildTarget.WEB:
                    required = self._backend_has_minimum_capability(target, report.toolchains)
                    if not required:
                        report.status = BuildStatus.UNAVAILABLE
                        report.errors.append(
                            f"No usable toolchain/project detected for target {target.value}"
                        )
                        return self._finalize(report)

                report.status = BuildStatus.BUILDING
                output_dir = self.output_root / target.value
                artifact_path = await backend.build(root, output_dir, config, self.runner, report)

                report.status = BuildStatus.VALIDATING
                artifact = self._verify_artifact(artifact_path, target, report)
                report.artifact = artifact
                report.status = BuildStatus.SUCCESS
                return self._finalize(report)

            except BuildUnavailableError as exc:
                report.status = BuildStatus.UNAVAILABLE
                report.errors.append(str(exc))
                return self._finalize(report)
            except BuildError as exc:
                report.status = BuildStatus.FAILED
                report.errors.append(str(exc))
                logger.exception("Build failed: %s", exc)
                return self._finalize(report)
            except Exception as exc:  # fail closed
                report.status = BuildStatus.FAILED
                report.errors.append(f"unexpected builder failure: {exc}")
                logger.exception("Unexpected build failure")
                return self._finalize(report)
            finally:
                workspace.cleanup()
    @staticmethod
    def _backend_has_minimum_capability(target: BuildTarget, tools: Sequence[ToolchainInfo]) -> bool:
        if target is BuildTarget.MOBILE:
            has_gradle = any(t.executable == "gradle" and t.available for t in tools)
            has_java = any(t.executable == "java" and t.available for t in tools)
            has_sdk = any(t.executable in {"ANDROID_HOME", "ANDROID_SDK_ROOT"} and t.available for t in tools)
            # A generated project may carry its own Gradle wrapper, so host Gradle
            # is optional; Java + Android SDK are the host-level essentials.
            has_wrapper = True  # actual wrapper availability is checked during build()
            return has_java and has_sdk and (has_gradle or has_wrapper)
        if target is BuildTarget.PC:
            return any(t.executable == "npm" and t.available for t in tools) and any(
                t.executable == "node" and t.available for t in tools
            )
        return True

    def _verify_output_path(self, artifact_path: Path) -> None:
        output_root = self.output_root.resolve()
        resolved = artifact_path.resolve()
        try:
            resolved.relative_to(output_root)
        except ValueError as exc:
            raise BuildExecutionError("artifact path escaped configured output root") from exc

    @staticmethod
    def _atomic_copy(source: Path, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + f".tmp-{uuid.uuid4().hex}")
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
        return destination

    def _verify_artifact(self, artifact_path: Path, target: BuildTarget, report: BuildReport) -> BuildArtifact:
        self._verify_output_path(artifact_path)
        if not artifact_path.exists() or not artifact_path.is_file():
            raise BuildExecutionError("builder reported an artifact that does not exist")
        size = artifact_path.stat().st_size
        if size <= 0:
            raise BuildExecutionError("builder produced an empty artifact")

        digest = _sha256_file(artifact_path)
        # Minimum plausibility checks stop common fake-success regressions.
        if target is BuildTarget.WEB:
            if artifact_path.suffix.lower() != ".zip" or size < 100:
                raise BuildExecutionError("invalid web ZIP artifact")
            try:
                with zipfile.ZipFile(artifact_path, "r") as archive:
                    names = set(archive.namelist())
                    if "index.html" not in names:
                        raise BuildExecutionError("web artifact is missing index.html")
                    if archive.testzip() is not None:
                        raise BuildExecutionError("web ZIP integrity check failed")
            except zipfile.BadZipFile as exc:
                raise BuildExecutionError("web artifact is not a valid ZIP") from exc
        elif target is BuildTarget.MOBILE:
            if artifact_path.suffix.lower() not in {".apk", ".aab"}:
                raise BuildExecutionError("Android backend produced an unexpected artifact type")
            self._verify_binary_signature(artifact_path, b"PK")
        elif target is BuildTarget.PC:
            if artifact_path.suffix.lower() not in {".exe", ".msi"}:
                raise BuildExecutionError("PC backend produced an unexpected artifact type")
            if artifact_path.suffix.lower() == ".exe":
                self._verify_binary_signature(artifact_path, b"MZ")
            else:
                self._verify_binary_signature(artifact_path, b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")

        return BuildArtifact(
            path=str(artifact_path.resolve()),
            target=target.value,
            size_bytes=size,
            sha256=digest,
            media_type=_media_type_for(artifact_path),
            verified=True,
        )

    @staticmethod
    def _verify_binary_signature(path: Path, signature: bytes) -> None:
        with path.open("rb") as handle:
            prefix = handle.read(len(signature))
        if prefix != signature:
            raise BuildExecutionError(
                f"artifact signature mismatch for {path.name}: expected {signature!r}, got {prefix!r}"
            )

    @staticmethod
    def _finalize(report: BuildReport) -> dict[str, Any]:
        report.finished_at = time.time()
        report.duration_ms = max(0.0, (report.finished_at - report.started_at) * 1000.0)
        result = report.to_dict()
        result["contract_version"] = "riot.builder.v3"
        # Keep API compatibility with older main.py callers.
        result["message"] = {
            BuildStatus.SUCCESS: "Build completed and artifact verified.",
            BuildStatus.UNAVAILABLE: "Build target is unavailable on the current environment.",
            BuildStatus.FAILED: "Build failed; no fake artifact was generated.",
        }.get(report.status, f"Build status: {report.status.value}")
        return result

    def capabilities(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for target, backend in self.backends.items():
            result[target.value] = [asdict(item) for item in backend.detect()]
        return result


# ============================================================================
# COMPATIBILITY SINGLETON
# ============================================================================


game_builder = UniversalBuilder()


async def build_game(config: Mapping[str, Any]) -> dict[str, Any]:
    """Module-level compatibility wrapper."""
    return await game_builder.build_game(config)


__all__ = [
    "BuildArtifact",
    "BuildBackend",
    "BuildError",
    "BuildLimits",
    "BuildReport",
    "BuildStatus",
    "BuildTarget",
    "BuildUnavailableError",
    "BuildValidationError",
    "UniversalBuilder",
    "build_game",
    "game_builder",
]
