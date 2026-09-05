from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import logging
import math
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from pydantic import BaseModel, Field

from god_brain.agents.base_agent import GodBaseAgent


# ============================================================================
# LOGGING
# ============================================================================

logger = logging.getLogger("GodNode.QATester")

if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s - [QA TESTER] - %(levelname)s - %(message)s"
        )
    )
    logger.addHandler(handler)

logger.setLevel(logging.INFO)


# ============================================================================
# LIMITS
# ============================================================================

MAX_FILES = 256
MAX_FILE_SIZE = 2_000_000
MAX_TOTAL_SOURCE_SIZE = 25_000_000
MAX_ISSUES = 256
MAX_WARNINGS = 256
MAX_TESTED_FILES = 256
MAX_STRING = 4096


# ============================================================================
# QA DATA CONTRACTS
# ============================================================================

class QAIssue(BaseModel):
    severity: str = Field(
        pattern="^(CRITICAL|HIGH|MEDIUM|LOW|INFO)$"
    )
    category: str
    message: str
    file: Optional[str] = None
    line: Optional[int] = None
    evidence: Optional[str] = None
    remediation: Optional[str] = None


class QAReport(BaseModel):
    """
    Evidence-based QA report.

    SUCCESS means all performed checks passed and no blocking defect was
    detected. It does NOT mean the game was physically rendered or executed
    unless execution evidence is explicitly supplied.
    """

    status: str = Field(
        pattern="^(SUCCESS|FAILED)$"
    )

    evidence_level: str = Field(
        default="STATIC_ANALYSIS",
        pattern="^(STATIC_ANALYSIS|STATIC_PLUS_RUNTIME|FULL_RUNTIME_VERIFIED)$",
    )

    critical_errors: List[str] = Field(
        default_factory=list
    )

    visual_glitches: List[str] = Field(
        default_factory=list
    )

    warnings: List[str] = Field(
        default_factory=list
    )

    issues: List[QAIssue] = Field(
        default_factory=list
    )

    correction_prompt: Optional[str] = None

    verified_code: Optional[str] = None

    tested_files: List[str] = Field(
        default_factory=list
    )

    checks: Dict[str, Any] = Field(
        default_factory=dict
    )

    metadata: Dict[str, Any] = Field(
        default_factory=dict
    )


# ============================================================================
# INTERNAL REPRESENTATIONS
# ============================================================================

@dataclass(slots=True)
class SourceUnit:
    path: str
    content: str
    language: str


# ============================================================================
# SAFE HELPERS
# ============================================================================

def _clean(value: Any, default: str = "") -> str:
    text = str(value or "").strip()
    return (text or default)[:MAX_STRING]


def _safe_json(value: Any) -> Any:
    if value is None or isinstance(
        value,
        (str, int, float, bool),
    ):
        return value

    if isinstance(value, Mapping):
        return {
            str(key): _safe_json(item)
            for key, item in value.items()
        }

    if isinstance(value, (list, tuple, set, frozenset)):
        return [
            _safe_json(item)
            for item in value
        ]

    model_dump = getattr(
        value,
        "model_dump",
        None,
    )

    if callable(model_dump):
        try:
            return _safe_json(
                model_dump(
                    mode="json"
                )
            )
        except Exception:
            pass

    to_dict = getattr(
        value,
        "to_dict",
        None,
    )

    if callable(to_dict):
        try:
            return _safe_json(
                to_dict()
            )
        except Exception:
            pass

    return str(value)


def _sha256(value: str) -> str:
    return hashlib.sha256(
        value.encode("utf-8")
    ).hexdigest()


def _language_for_path(path: str) -> str:
    suffix = PurePosixPath(
        path
    ).suffix.lower()

    mapping = {
        ".py": "python",
        ".js": "javascript",
        ".mjs": "javascript",
        ".cjs": "javascript",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".jsx": "javascript",
        ".html": "html",
        ".htm": "html",
        ".css": "css",
        ".json": "json",
        ".glsl": "shader",
        ".vert": "shader",
        ".frag": "shader",
        ".wgsl": "shader",
    }

    return mapping.get(
        suffix,
        "text",
    )


def _safe_path(path: str) -> str:
    raw = str(path or "").replace(
        "\\",
        "/",
    ).strip("/")

    parsed = PurePosixPath(raw)

    if (
        not raw
        or parsed.is_absolute()
        or ".." in parsed.parts
    ):
        raise ValueError(
            f"unsafe source path: {path!r}"
        )

    return parsed.as_posix()


# ============================================================================
# SOURCE NORMALIZATION
# ============================================================================

def _extract_fenced_code(value: str) -> str:
    match = re.search(
        r"```(?:[a-zA-Z0-9_+-]+)?\s*\n(.*?)```",
        value,
        re.DOTALL,
    )

    if match:
        return match.group(1).strip()

    return value.strip()


def _normalize_source(
    generated_code: Any,
) -> List[SourceUnit]:

    if generated_code is None:
        return []

    # Mapping:
    # {"files": {"index.html": "..."}}
    # {"source_files": [{"path": "...", "content": "..."}]}
    # {"index.html": "..."}
    if isinstance(
        generated_code,
        Mapping,
    ):

        source = generated_code

        if isinstance(
            source.get("source_bundle"),
            Mapping,
        ):
            source = source["source_bundle"]

        if isinstance(
            source.get("files"),
            Mapping,
        ):
            file_mapping = source["files"]

        elif isinstance(
            source.get("source_files"),
            (list, tuple),
        ):
            file_mapping = source[
                "source_files"
            ]

        else:
            direct = {
                key: value
                for key, value in source.items()
                if isinstance(
                    key,
                    str,
                )
                and isinstance(
                    value,
                    (str, bytes),
                )
            }

            if direct:
                file_mapping = direct
            else:
                return []

        units: List[SourceUnit] = []

        if isinstance(
            file_mapping,
            Mapping,
        ):
            for raw_path, raw_content in list(
                file_mapping.items()
            )[:MAX_FILES]:

                try:
                    path = _safe_path(
                        str(raw_path)
                    )
                except ValueError:
                    continue

                if isinstance(
                    raw_content,
                    bytes,
                ):
                    content = raw_content.decode(
                        "utf-8",
                        errors="replace",
                    )
                else:
                    content = str(
                        raw_content
                    )

                content = _extract_fenced_code(
                    content
                )

                if len(content) > MAX_FILE_SIZE:
                    content = content[
                        :MAX_FILE_SIZE
                    ]

                units.append(
                    SourceUnit(
                        path=path,
                        content=content,
                        language=_language_for_path(
                            path
                        ),
                    )
                )

            return units

        if isinstance(
            file_mapping,
            (list, tuple),
        ):
            for item in file_mapping[
                :MAX_FILES
            ]:

                if not isinstance(
                    item,
                    Mapping,
                ):
                    continue

                raw_path = (
                    item.get("path")
                    or item.get("file")
                    or item.get("name")
                )

                if not raw_path:
                    continue

                raw_content = (
                    item.get("content")
                    or ""
                )

                try:
                    path = _safe_path(
                        str(raw_path)
                    )
                except ValueError:
                    continue

                content = str(
                    raw_content
                )

                if len(content) > MAX_FILE_SIZE:
                    content = content[
                        :MAX_FILE_SIZE
                    ]

                units.append(
                    SourceUnit(
                        path=path,
                        content=_extract_fenced_code(
                            content
                        ),
                        language=_language_for_path(
                            path
                        ),
                    )
                )

            return units

        return []

    # Direct string
    if isinstance(
        generated_code,
        str,
    ):
        return [
            SourceUnit(
                path="generated_source",
                content=_extract_fenced_code(
                    generated_code
                )[
                    :MAX_FILE_SIZE
                ],
                language="text",
            )
        ]

    return [
        SourceUnit(
            path="generated_source",
            content=str(
                generated_code
            )[
                :MAX_FILE_SIZE
            ],
            language="text",
        )
    ]


# ============================================================================
# QA ENGINE
# ============================================================================

class ProductionQAEngine:
    """
    Deterministic QA engine.

    It intentionally separates:
      - static evidence
      - heuristic runtime risk analysis
      - externally supplied runtime evidence

    This prevents a fake "simulated" check from being reported as a real
    browser/engine execution.
    """

    def __init__(self) -> None:
        self.issues: List[QAIssue] = []
        self.visual_glitches: List[str] = []
        self.warnings: List[str] = []
        self.checked_files: List[str] = []

    def add_issue(
        self,
        severity: str,
        category: str,
        message: str,
        *,
        file: Optional[str] = None,
        line: Optional[int] = None,
        evidence: Optional[str] = None,
        remediation: Optional[str] = None,
    ) -> None:

        if len(self.issues) >= MAX_ISSUES:
            return

        self.issues.append(
            QAIssue(
                severity=severity,
                category=category,
                message=_clean(message),
                file=file,
                line=line,
                evidence=_clean(
                    evidence
                )
                if evidence
                else None,
                remediation=_clean(
                    remediation
                )
                if remediation
                else None,
            )
        )

    def add_warning(
        self,
        message: str,
    ) -> None:

        if len(self.warnings) >= MAX_WARNINGS:
            return

        self.warnings.append(
            _clean(message)
        )

    # ------------------------------------------------------------------
    # Python AST
    # ------------------------------------------------------------------

    def inspect_python(
        self,
        unit: SourceUnit,
    ) -> Dict[str, Any]:

        result = {
            "syntax_valid": True,
            "functions": 0,
            "classes": 0,
            "imports": 0,
            "dangerous_constructs": 0,
        }

        try:
            tree = ast.parse(
                unit.content,
                filename=unit.path,
            )
        except SyntaxError as exc:
            result["syntax_valid"] = False

            self.add_issue(
                "CRITICAL",
                "SYNTAX",
                f"Python syntax error: {exc.msg}",
                file=unit.path,
                line=exc.lineno,
                remediation=(
                    "Fix the syntax error before runtime/build."
                ),
            )

            return result

        except Exception as exc:
            self.add_issue(
                "HIGH",
                "AST",
                f"Python AST parsing failed: {type(exc).__name__}",
                file=unit.path,
            )

            return result

        for node in ast.walk(tree):

            if isinstance(
                node,
                (
                    ast.FunctionDef,
                    ast.AsyncFunctionDef,
                ),
            ):
                result["functions"] += 1

            elif isinstance(
                node,
                ast.ClassDef,
            ):
                result["classes"] += 1

            elif isinstance(
                node,
                (
                    ast.Import,
                    ast.ImportFrom,
                ),
            ):
                result["imports"] += 1

            elif isinstance(
                node,
                ast.While,
            ):
                test = node.test

                if isinstance(
                    test,
                    ast.Constant,
                ) and test.value is True:

                    has_break = any(
                        isinstance(
                            child,
                            ast.Break,
                        )
                        for child in ast.walk(
                            node
                        )
                    )

                    if not has_break:
                        self.add_issue(
                            "HIGH",
                            "CONTROL_FLOW",
                            "Potential unbounded while-True loop without break.",
                            file=unit.path,
                            line=getattr(
                                node,
                                "lineno",
                                None,
                            ),
                            evidence="while True without break",
                            remediation=(
                                "Add a bounded exit condition, cancellation "
                                "path, or explicit lifecycle termination."
                            ),
                        )

                        result[
                            "dangerous_constructs"
                        ] += 1

            elif isinstance(
                node,
                ast.Call,
            ):
                func_name = self._call_name(
                    node.func
                )

                if func_name in {
                    "eval",
                    "exec",
                }:

                    self.add_issue(
                        "CRITICAL",
                        "SECURITY",
                        f"Dangerous dynamic execution call: {func_name}",
                        file=unit.path,
                        line=getattr(
                            node,
                            "lineno",
                            None,
                        ),
                        remediation=(
                            "Remove dynamic execution or place it behind "
                            "a restricted, sandboxed execution boundary."
                        ),
                    )

                    result[
                        "dangerous_constructs"
                    ] += 1

                elif func_name in {
                    "subprocess.call",
                    "subprocess.run",
                    "os.system",
                }:

                    self.add_warning(
                        f"{unit.path}: process execution API detected; "
                        "verify sandbox/allowlist policy."
                    )

                elif func_name in {
                    "open",
                }:

                    self.add_warning(
                        f"{unit.path}: direct file I/O detected; "
                        "verify resource lifecycle and path validation."
                    )

        return result

    @staticmethod
    def _call_name(
        node: ast.AST,
    ) -> str:

        if isinstance(
            node,
            ast.Name,
        ):
            return node.id

        if isinstance(
            node,
            ast.Attribute,
        ):
            parts: List[str] = []

            current: Any = node

            while isinstance(
                current,
                ast.Attribute,
            ):
                parts.append(
                    current.attr
                )
                current = current.value

            if isinstance(
                current,
                ast.Name,
            ):
                parts.append(
                    current.id
                )

            return ".".join(
                reversed(parts)
            )

        return ""

    # ------------------------------------------------------------------
    # JavaScript / TypeScript / HTML
    # ------------------------------------------------------------------

    def inspect_web_code(
        self,
        unit: SourceUnit,
    ) -> Dict[str, Any]:

        text = unit.content
        lower = text.lower()

        result = {
            "render_loop": True,
            "camera_configured": True,
            "webgl_present": False,
            "resource_cleanup": True,
            "pbr_material": True,
            "script_tags": 0,
        }

        if (
            "<canvas" in lower
            or "webgl" in lower
            or "three." in lower
        ):
            result[
                "webgl_present"
            ] = True

        # Render loop risk.
        if (
            "requestanimationframe"
            not in lower
            and (
                "three." in lower
                or "webgl" in lower
                or "<canvas" in lower
            )
        ):
            result[
                "render_loop"
            ] = False

            self.add_issue(
                "HIGH",
                "RUNTIME_LOOP",
                "3D/canvas code detected without requestAnimationFrame.",
                file=unit.path,
                remediation=(
                    "Provide a bounded and cancellable render/update loop."
                ),
            )

        # Camera risk.
        if (
            "perspectivecamera"
            in lower
            and "camera.position"
            not in lower
        ):
            result[
                "camera_configured"
            ] = False

            self.add_issue(
                "MEDIUM",
                "CAMERA",
                "PerspectiveCamera detected without explicit camera positioning.",
                file=unit.path,
                remediation=(
                    "Set camera position/orientation and verify "
                    "lookAt or equivalent camera transform."
                ),
            )

        # Mesh/resource lifecycle.
        if (
            "three.mesh("
            in lower
            or "new three.mesh("
            in lower
        ):
            if "dispose()" not in lower:
                result[
                    "resource_cleanup"
                ] = False

                self.add_issue(
                    "MEDIUM",
                    "MEMORY",
                    "Three.js mesh creation detected without visible dispose lifecycle.",
                    file=unit.path,
                    remediation=(
                        "Dispose geometry/material/texture resources when "
                        "objects leave the active scene."
                    ),
                )

        # PBR.
        if (
            "meshbasicmaterial"
            in lower
        ):
            result[
                "pbr_material"
            ] = False

            self.visual_glitches.append(
                f"{unit.path}: MeshBasicMaterial detected; "
                "verify whether PBR lighting is required."
            )

            self.add_issue(
                "MEDIUM",
                "VISUAL",
                "MeshBasicMaterial may bypass realistic lighting/PBR response.",
                file=unit.path,
                remediation=(
                    "Use an appropriate physically based material "
                    "when the visual target requires it."
                ),
            )

        if (
            "textureloader"
            in lower
            and "normalmap"
            not in lower
        ):
            self.add_warning(
                f"{unit.path}: texture loading detected without a visible normal map."
            )

        # JavaScript obvious infinite loop risk.
        if re.search(
            r"while\s*\(\s*true\s*\)",
            lower,
        ):
            if (
                "break"
                not in lower
                and "abortcontroller"
                not in lower
            ):
                self.add_issue(
                    "HIGH",
                    "CONTROL_FLOW",
                    "Potential JavaScript infinite loop detected.",
                    file=unit.path,
                    remediation=(
                        "Add an explicit termination/cancellation path."
                    ),
                )

        # Promise / async errors.
        if (
            "async function"
            in lower
            or "await "
            in lower
        ):
            if (
                ".catch("
                not in lower
                and "try {" not in lower
            ):
                self.add_warning(
                    f"{unit.path}: asynchronous code has no obvious error boundary."
                )

        result[
            "script_tags"
        ] = len(
            re.findall(
                r"<script\b",
                lower,
            )
        )

        return result

    # ------------------------------------------------------------------
    # Cross-file analysis
    # ------------------------------------------------------------------

    def inspect_cross_file_consistency(
        self,
        units: Sequence[SourceUnit],
    ) -> Dict[str, Any]:

        paths = {
            unit.path
            for unit in units
        }

        entry_points = [
            path
            for path in paths
            if path.endswith(
                (
                    "index.html",
                    "main.py",
                    "main.js",
                    "game.js",
                    "app.py",
                )
            )
        ]

        references: Dict[str, int] = {}

        for unit in units:

            for match in re.findall(
                r"""(?:src|href|import\s+(?:.*?\s+from\s+)?|from\s+)["']([^"']+)["']""",
                unit.content,
                re.IGNORECASE,
            ):

                reference = str(
                    match
                ).strip()

                if (
                    reference.startswith(
                        ("http://", "https://", "data:")
                    )
                ):
                    continue

                normalized = reference.split(
                    "?",
                    1,
                )[0]

                references[
                    normalized
                ] = (
                    references.get(
                        normalized,
                        0,
                    )
                    + 1
                )

                basename = PurePosixPath(
                    normalized
                ).name

                exists = (
                    normalized in paths
                    or basename in {
                        PurePosixPath(path).name
                        for path in paths
                    }
                )

                if (
                    not exists
                    and not normalized.startswith(
                        ("./", "../")
                    )
                ):
                    self.add_warning(
                        f"{unit.path}: external/unresolved reference '{normalized}'."
                    )

        # HTML entrypoint sanity.
        if any(
            unit.language == "html"
            for unit in units
        ):
            has_html = any(
                unit.language == "html"
                and "<html"
                in unit.content.lower()
                for unit in units
            )

            if not has_html:
                self.add_issue(
                    "MEDIUM",
                    "STRUCTURE",
                    "HTML source exists but no obvious <html> document was detected.",
                )

        return {
            "file_count": len(units),
            "entry_points": entry_points,
            "references": references,
        }

    # ------------------------------------------------------------------
    # Memory / game heuristics
    # ------------------------------------------------------------------

    def inspect_memory_patterns(
        self,
        units: Sequence[SourceUnit],
    ) -> int:

        issue_count = 0

        for unit in units:

            lower = unit.content.lower()

            dynamic_allocations = len(
                re.findall(
                    r"\bnew\s+",
                    unit.content,
                )
            )

            event_listeners = len(
                re.findall(
                    r"(?:addEventListener|on\w+\s*=)",
                    unit.content,
                    re.IGNORECASE,
                )
            )

            removers = len(
                re.findall(
                    r"(?:removeEventListener|off\w+)",
                    unit.content,
                    re.IGNORECASE,
                )
            )

            dispose_calls = len(
                re.findall(
                    r"\bdispose\s*\(",
                    lower,
                )
            )

            if (
                dynamic_allocations > 25
                and dispose_calls == 0
            ):
                self.add_issue(
                    "MEDIUM",
                    "MEMORY",
                    (
                        f"{unit.path} contains many dynamic allocations "
                        "without visible disposal semantics."
                    ),
                    file=unit.path,
                    remediation=(
                        "Audit resource ownership, especially meshes, "
                        "textures, buffers and event subscriptions."
                    ),
                )

                issue_count += 1

            if (
                event_listeners > 8
                and removers == 0
            ):
                self.add_issue(
                    "MEDIUM",
                    "MEMORY",
                    (
                        f"{unit.path} registers multiple event listeners "
                        "without visible removal."
                    ),
                    file=unit.path,
                    remediation=(
                        "Unregister listeners during scene/component teardown."
                    ),
                )

                issue_count += 1

        return issue_count

    # ------------------------------------------------------------------
    # Generated output safety
    # ------------------------------------------------------------------

    def inspect_output_integrity(
        self,
        units: Sequence[SourceUnit],
    ) -> Dict[str, Any]:

        total_bytes = sum(
            len(unit.content.encode(
                "utf-8",
                errors="replace",
            ))
            for unit in units
        )

        if total_bytes > MAX_TOTAL_SOURCE_SIZE:
            self.add_issue(
                "CRITICAL",
                "RESOURCE_LIMIT",
                (
                    "Generated source exceeds the QA input budget."
                ),
                remediation=(
                    "Split the build into bounded modules/assets or reduce "
                    "generated source size."
                ),
            )

        hashes = {
            unit.path: _sha256(
                unit.content
            )
            for unit in units
        }

        return {
            "total_source_bytes": total_bytes,
            "file_hashes": hashes,
        }

    # ------------------------------------------------------------------
    # External runtime evidence
    # ------------------------------------------------------------------

    def consume_runtime_evidence(
        self,
        evidence: Any,
    ) -> str:

        if not isinstance(
            evidence,
            Mapping,
        ):
            return "STATIC_ANALYSIS"

        executed = bool(
            evidence.get("executed")
            or evidence.get("runtime_executed")
        )

        browser = bool(
            evidence.get("browser_verified")
        )

        rendered = bool(
            evidence.get("rendered_frame_verified")
        )

        physics = bool(
            evidence.get("physics_runtime_verified")
        )

        if (
            executed
            and browser
            and rendered
            and physics
        ):
            return "FULL_RUNTIME_VERIFIED"

        if executed:
            return "STATIC_PLUS_RUNTIME"

        return "STATIC_ANALYSIS"


# ============================================================================
# QA AGENT
# ============================================================================

class QATesterAgent(GodBaseAgent):
    """
    Production QA / verification specialist.

    Important compatibility:
        await agent.perform_role(generated_code, error_logs=None)

    The first two positional arguments remain compatible with the existing
    orchestrator.

    Additional context may contain actual runtime evidence. Without such
    evidence, QA never claims a real browser/physics/render execution.
    """

    role_name = "QA Verification, Runtime Risk & Visual Quality Gatekeeper"
    service_type = "brain"

    def __init__(
        self,
        *,
        gateway: Any = None,
        config: Any = None,
    ) -> None:

        super().__init__(
            role_name=self.role_name,
            service_type=self.service_type,
            gateway=gateway,
            config=config,
            required_capabilities={
                "text_generation",
                "structured_output",
            },
            temperature=0.05,
            metadata={
                "contract": "riot.qa.report.v2",
                "stage": "qa_testing",
                "agent_version": "riot.qa.v2",
            },
        )

        self.engine = ProductionQAEngine()

    # ------------------------------------------------------------------
    # Machine-readable correction directive
    # ------------------------------------------------------------------

    def _build_correction_prompt(
        self,
        report: QAReport,
        source_preview: str,
    ) -> str:

        problems: List[str] = []

        for issue in report.issues[
            :32
        ]:

            location = (
                f"{issue.file}:{issue.line}"
                if issue.file
                and issue.line
                else issue.file
                or "global"
            )

            problems.append(
                f"[{issue.severity}] "
                f"{issue.category} "
                f"({location}): "
                f"{issue.message}"
            )

        if not problems:
            problems = list(
                report.warnings[
                    :32
                ]
            )

        details = "\n".join(
            problems
        )

        return (
            "RIOT QA REJECTION / REMEDIATION DIRECTIVE\n"
            "=========================================\n\n"
            "The generated project did not satisfy the current QA gate.\n\n"
            f"EVIDENCE LEVEL: {report.evidence_level}\n\n"
            "ISSUES:\n"
            f"{details}\n\n"
            "REMEDIATION RULES:\n"
            "1. Fix the underlying defect instead of hiding the warning.\n"
            "2. Preserve upstream contracts and identifiers.\n"
            "3. Do not fabricate runtime/build/visual evidence.\n"
            "4. Keep memory ownership and cleanup explicit.\n"
            "5. Preserve cross-file references.\n"
            "6. Re-submit the complete affected source set for QA.\n\n"
            "SOURCE PREVIEW:\n"
            f"{source_preview[:4000]}\n"
        )

    # ------------------------------------------------------------------
    # Public execution
    # ------------------------------------------------------------------

    async def perform_role(
        self,
        generated_code: Any,
        error_logs: Optional[str] = None,
        *,
        context: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:

        logger.info(
            "[%s] Starting evidence-based QA pipeline.",
            self.role_name,
        )

        # Fresh engine per request.
        self.engine = ProductionQAEngine()

        context = dict(
            context or {}
        )

        units = _normalize_source(
            generated_code
        )

        if not units:
            report = QAReport(
                status="FAILED",
                evidence_level="STATIC_ANALYSIS",
                critical_errors=[
                    "No generated source was supplied to QA."
                ],
                issues=[
                    QAIssue(
                        severity="CRITICAL",
                        category="INPUT",
                        message=(
                            "QA cannot validate an empty source set."
                        ),
                        remediation=(
                            "Provide the generated source bundle."
                        ),
                    )
                ],
                checks={
                    "source_present": False,
                },
                metadata={
                    "agent_contract": (
                        "riot.qa.report.v2"
                    ),
                    "agent_version": (
                        "riot.qa.v2"
                    ),
                },
            )

            return report.model_dump()

        # Bound total files.
        units = units[
            :MAX_FILES
        ]

        for unit in units:
            self.engine.checked_files.append(
                unit.path
            )

        # 1. Integrity / source bounds
        integrity = (
            self.engine.inspect_output_integrity(
                units
            )
        )

        # 2. Per-file analysis
        syntax_results: Dict[str, Any] = {}

        for unit in units:

            if not unit.content.strip():
                self.engine.add_issue(
                    "HIGH",
                    "INPUT",
                    "Generated source file is empty.",
                    file=unit.path,
                )

                continue

            if unit.language == "python":
                syntax_results[
                    unit.path
                ] = self.engine.inspect_python(
                    unit
                )

            elif unit.language in {
                "javascript",
                "typescript",
                "html",
            }:
                syntax_results[
                    unit.path
                ] = self.engine.inspect_web_code(
                    unit
                )

            elif unit.language == "json":
                try:
                    json.loads(
                        unit.content
                    )
                except json.JSONDecodeError as exc:
                    self.engine.add_issue(
                        "HIGH",
                        "SYNTAX",
                        f"Invalid JSON: {exc.msg}",
                        file=unit.path,
                        line=exc.lineno,
                    )

        # 3. Cross-file consistency
        cross_file = (
            self.engine.inspect_cross_file_consistency(
                units
            )
        )

        # 4. Memory/resource analysis
        memory_findings = (
            self.engine.inspect_memory_patterns(
                units
            )
        )

        # 5. Error-log correlation
        if error_logs:
            normalized_logs = str(
                error_logs
            )[
                :MAX_CONTEXT_CHARS
            ]

            log_lower = normalized_logs.lower()

            for keyword, category in (
                (
                    "traceback",
                    "RUNTIME",
                ),
                (
                    "exception",
                    "RUNTIME",
                ),
                (
                    "out of memory",
                    "MEMORY",
                ),
                (
                    "webgl",
                    "GRAPHICS",
                ),
                (
                    "shader",
                    "GRAPHICS",
                ),
                (
                    "collision",
                    "PHYSICS",
                ),
                (
                    "timeout",
                    "TIMEOUT",
                ),
            ):
                if keyword in log_lower:
                    self.engine.add_issue(
                        "HIGH",
                        category,
                        (
                            f"Reported runtime logs contain '{keyword}'."
                        ),
                        evidence=normalized_logs[:1000],
                        remediation=(
                            "Correlate the reported runtime failure "
                            "with the generated source before accepting the build."
                        ),
                    )

        # 6. Optional real runtime evidence.
        runtime_evidence = context.get(
            "runtime_evidence"
        )

        evidence_level = (
            self.engine.consume_runtime_evidence(
                runtime_evidence
            )
        )

        # We deliberately do not turn asyncio.sleep() into fake runtime evidence.
        if runtime_evidence is None:
            self.engine.add_warning(
                "No external runtime execution evidence was supplied; "
                "QA result is static/heuristic only."
            )

        # 7. Build report
        blocking = [
            issue
            for issue in self.engine.issues
            if issue.severity
            in {
                "CRITICAL",
                "HIGH",
            }
        ]

        critical_messages = [
            issue.message
            for issue in blocking
        ]

        visual = list(
            self.engine.visual_glitches
        )

        status = (
            "FAILED"
            if blocking
            else "SUCCESS"
        )

        report = QAReport(
            status=status,
            evidence_level=evidence_level,
            critical_errors=critical_messages[
                :MAX_ISSUES
            ],
            visual_glitches=visual[
                :MAX_WARNINGS
            ],
            warnings=self.engine.warnings[
                :MAX_WARNINGS
            ],
            issues=self.engine.issues[
                :MAX_ISSUES
            ],
            tested_files=self.engine.checked_files[
                :MAX_TESTED_FILES
            ],
            checks={
                "source_present": True,
                "source_file_count": len(
                    units
                ),
                "source_integrity": integrity,
                "syntax_results": syntax_results,
                "cross_file_consistency": cross_file,
                "memory_findings": memory_findings,
                "runtime_evidence_supplied": (
                    runtime_evidence is not None
                ),
                "blocking_issue_count": len(
                    blocking
                ),
                "total_issue_count": len(
                    self.engine.issues
                ),
            },
            metadata={
                "agent_contract": (
                    "riot.qa.report.v2"
                ),
                "agent_version": (
                    "riot.qa.v2"
                ),
                "execution_status": (
                    "VERIFIED"
                    if runtime_evidence is not None
                    else "STATIC_ANALYSIS_ONLY"
                ),
                "source_sha256": _sha256(
                    "\n".join(
                        unit.content
                        for unit in units
                    )
                ),
            },
        )

        # 8. Correction loop only on actual defects.
        if report.status == "FAILED":

            source_preview = "\n\n".join(
                (
                    f"=== {unit.path} ===\n"
                    f"{unit.content[:1000]}"
                )
                for unit in units[:8]
            )

            report.correction_prompt = (
                self._build_correction_prompt(
                    report,
                    source_preview,
                )
            )

            logger.warning(
                "[%s] QA FAILED: %d blocking issues, %d total issues.",
                self.role_name,
                len(blocking),
                len(self.engine.issues),
            )

            return report.model_dump()

        # 9. PASS — preserve source, but do NOT falsely claim build/run.
        combined_source = "\n\n".join(
            unit.content
            for unit in units
        )

        report.verified_code = combined_source

        logger.info(
            "[%s] QA PASSED at evidence level=%s.",
            self.role_name,
            evidence_level,
        )

        return report.model_dump()
