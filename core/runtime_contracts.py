"""
RIOT / GOD NODE — Runtime Compiler
==================================

Phase 7: Compile canonical GameProject state into deterministic runtime
execution contracts.

Pipeline
--------
GameProject
    ->
RuntimeTargetContract
    ->
RuntimeStageContract[]
    ->
DependencyCompiler
    ->
RuntimePipelineRequest
    ->
RuntimeExecutionRequest

Responsibilities
----------------
* Convert canonical project state into runtime contracts.
* Preserve project-defined information.
* Derive only deterministic runtime metadata.
* Build an explicit dependency graph.
* Detect invalid project/runtime combinations early.
* Keep compilation separate from execution.
* Never execute providers, agents, schedulers or build tools.
* Never fabricate successful runtime/QA/build evidence.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from core.dependency_compiler import (
    DependencyCompiler,
    DependencyGraph,
)
from core.game_project import (
    CapabilityStatus,
    GameProject,
    ProjectStatus,
    RuntimeType,
    TargetPlatform,
)
from core.runtime_contracts import (
    RuntimeArtifactReference,
    RuntimeExecutionMode,
    RuntimeExecutionRequest,
    RuntimePipelineRequest,
    RuntimeStageContract,
    RuntimeTargetContract,
)


# ============================================================================
# CONSTANTS
# ============================================================================

COMPILER_VERSION = "riot.runtime-compiler.v1"

MAX_STAGES = 4096
MAX_CAPABILITIES = 512
MAX_DEPENDENCIES = 256


# ============================================================================
# ERRORS
# ============================================================================

class RuntimeCompilerError(ValueError):
    """Base runtime compiler error."""


class RuntimeCompilerValidationError(
    RuntimeCompilerError
):
    """Project state is insufficient for compilation."""


class UnsupportedRuntimeTargetError(
    RuntimeCompilerError
):
    """Requested runtime/target combination is unsupported."""


# ============================================================================
# RESULT CONTRACT
# ============================================================================

@dataclass(slots=True)
class RuntimeCompilationDiagnostics:
    compiler_version: str = COMPILER_VERSION

    warnings: List[str] = field(
        default_factory=list
    )

    errors: List[str] = field(
        default_factory=list
    )

    stage_count: int = 0
    dependency_count: int = 0

    topological_order: Tuple[
        str,
        ...
    ] = ()

    parallel_levels: Tuple[
        Tuple[str, ...],
        ...
    ] = ()

    metadata: Dict[
        str,
        Any,
    ] = field(
        default_factory=dict
    )

    @property
    def valid(self) -> bool:
        return not self.errors

    def add_warning(
        self,
        message: str,
    ) -> None:
        if message:
            self.warnings.append(
                str(message)
            )

    def add_error(
        self,
        message: str,
    ) -> None:
        if message:
            self.errors.append(
                str(message)
            )


@dataclass(slots=True)
class RuntimeCompilationResult:
    request: RuntimePipelineRequest
    diagnostics: RuntimeCompilationDiagnostics
    dependency_graph: DependencyGraph
    runtime_request: RuntimeExecutionRequest


# ============================================================================
# DETERMINISTIC HELPERS
# ============================================================================

def _stable_id(
    prefix: str,
    *parts: Any,
) -> str:
    material = "\x1f".join(
        str(part or "").strip()
        for part in parts
    )

    digest = hashlib.sha256(
        material.encode("utf-8")
    ).hexdigest()[:16]

    return f"{prefix}_{digest}"


def _target_runtime_type(
    target: TargetPlatform,
) -> RuntimeType:
    mapping = {
        TargetPlatform.WEB_HTML5: RuntimeType.WEB,
        TargetPlatform.MOBILE_APK: RuntimeType.NATIVE_MOBILE,
        TargetPlatform.PC_EXE: RuntimeType.DESKTOP,
        TargetPlatform.CLOUD_STREAM: RuntimeType.CLOUD_STREAM,
    }

    try:
        return mapping[target]
    except KeyError as exc:
        raise UnsupportedRuntimeTargetError(
            f"Unsupported target platform: {target!r}"
        ) from exc


def _target_capabilities(
    target: TargetPlatform,
) -> Set[str]:
    common = {
        "runtime_execution",
        "frame_processing",
        "state_management",
    }

    platform_capabilities = {
        TargetPlatform.WEB_HTML5: {
            "web_runtime",
            "html5",
            "javascript",
        },
        TargetPlatform.MOBILE_APK: {
            "mobile_runtime",
            "android",
        },
        TargetPlatform.PC_EXE: {
            "desktop_runtime",
        },
        TargetPlatform.CLOUD_STREAM: {
            "cloud_stream_runtime",
        },
    }

    return common | platform_capabilities[target]


def _source_reference(
    path: str,
    *,
    checksum: Optional[str],
    size_bytes: int,
    kind: str,
    media_type: Optional[str] = None,
) -> RuntimeArtifactReference:
    return RuntimeArtifactReference(
        kind=kind,
        path=path,
        checksum=checksum,
        size_bytes=size_bytes,
        verified=True,
        media_type=media_type,
    )


# ============================================================================
# RUNTIME COMPILER
# ============================================================================

class RuntimeCompiler:
    """
    Canonical GameProject -> RuntimePipeline compiler.
    """

    def __init__(
        self,
        *,
        dependency_compiler: Optional[
            DependencyCompiler
        ] = None,
        max_stages: int = MAX_STAGES,
    ) -> None:
        self.dependency_compiler = (
            dependency_compiler
            or DependencyCompiler()
        )

        self.max_stages = max(
            1,
            int(max_stages),
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def compile(
        self,
        project: GameProject,
        *,
        execution_mode: RuntimeExecutionMode = (
            RuntimeExecutionMode.REALTIME
        ),
    ) -> RuntimeCompilationResult:
        if not isinstance(
            project,
            GameProject,
        ):
            raise TypeError(
                "RuntimeCompiler.compile() requires GameProject."
            )

        diagnostics = (
            RuntimeCompilationDiagnostics()
        )

        self._validate_project(
            project,
            diagnostics,
        )

        target = self._compile_target(
            project
        )

        stages = self._compile_stages(
            project,
            target,
            diagnostics,
        )

        if len(stages) > self.max_stages:
            raise RuntimeCompilerValidationError(
                "runtime pipeline exceeds configured maximum "
                f"stage count of {self.max_stages}"
            )

        graph = self.dependency_compiler.compile(
            stages
        )

        diagnostics.stage_count = (
            graph.node_count
        )

        diagnostics.dependency_count = (
            graph.edge_count
        )

        diagnostics.topological_order = (
            graph.topological_order
        )

        diagnostics.parallel_levels = (
            graph.levels
        )

        runtime_request = (
            self._compile_execution_request(
                project,
                target,
                stages,
                graph,
                execution_mode,
            )
        )

        pipeline_request = (
            RuntimePipelineRequest(
                project_id=project.project_id,
                target=target,
                stages=stages,
                runtime_request=runtime_request,
                metadata={
                    "compiler": COMPILER_VERSION,
                    "compiler_version": COMPILER_VERSION,
                    "project_status": project.status.value,
                    "build_id": project.build_id,
                    "target_platform": (
                        project.target_platform.value
                    ),
                    "runtime_type": (
                        target.runtime_type.value
                    ),
                    "dependency_order": list(
                        graph.topological_order
                    ),
                    "parallel_levels": [
                        list(level)
                        for level
                        in graph.levels
                    ],
                },
            )
        )

        diagnostics.metadata.update(
            {
                "project_id": project.project_id,
                "build_id": project.build_id,
                "compiler": COMPILER_VERSION,
                "stage_count": graph.node_count,
                "dependency_count": graph.edge_count,
                "max_parallelism": graph.max_parallelism,
            }
        )

        return RuntimeCompilationResult(
            request=pipeline_request,
            diagnostics=diagnostics,
            dependency_graph=graph,
            runtime_request=runtime_request,
        )

    def compile_pipeline(
        self,
        project: GameProject,
    ) -> RuntimePipelineRequest:
        return self.compile(
            project
        ).request

    def compile_execution_request(
        self,
        project: GameProject,
        *,
        execution_mode: RuntimeExecutionMode = (
            RuntimeExecutionMode.REALTIME
        ),
    ) -> RuntimeExecutionRequest:
        return self.compile(
            project,
            execution_mode=execution_mode,
        ).runtime_request

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_project(
        project: GameProject,
        diagnostics: RuntimeCompilationDiagnostics,
    ) -> None:
        if not project.project_id.strip():
            raise RuntimeCompilerValidationError(
                "project_id cannot be empty"
            )

        if project.status is ProjectStatus.FAILED:
            raise RuntimeCompilerValidationError(
                "cannot compile a FAILED GameProject"
            )

        if not project.name.strip():
            diagnostics.add_warning(
                "project name is empty"
            )

        if project.world_manifest is None:
            diagnostics.add_warning(
                "world_manifest is absent"
            )

        if project.scene_graph is None:
            diagnostics.add_warning(
                "scene_graph is absent"
            )

        if project.physics_config is None:
            diagnostics.add_warning(
                "physics_config is absent"
            )

        if not project.gameplay_modules:
            diagnostics.add_warning(
                "no gameplay modules are registered"
            )

        if project.source_bundle.is_empty:
            diagnostics.add_warning(
                "source_bundle is empty; runtime compilation "
                "will produce a declarative runtime plan only"
            )

    # ------------------------------------------------------------------
    # Target compilation
    # ------------------------------------------------------------------

    @staticmethod
    def _compile_target(
        project: GameProject,
    ) -> RuntimeTargetContract:
        runtime_type = (
            project.runtime_type
            or _target_runtime_type(
                project.target_platform
            )
        )

        capabilities = _target_capabilities(
            project.target_platform
        )

        if project.runtime_target is not None:
            runtime_name = (
                project.runtime_target.name
            )

            runtime_version = (
                project.runtime_target.version
            )

            capabilities.update(
                project.runtime_target.capabilities
            )

            capability_status = (
                project.runtime_target.capability_status
            )

        else:
            runtime_name = (
                f"riot-{project.target_platform.value}"
            )

            runtime_version = None

            capability_status = (
                CapabilityStatus.NOT_CONFIGURED
            )

        return RuntimeTargetContract(
            runtime_type=runtime_type,
            target_platform=project.target_platform,
            name=runtime_name,
            version=runtime_version,
            capabilities=set(
                sorted(capabilities)
            ),
            configuration={
                "project_id": project.project_id,
                "build_id": project.build_id,
                "seed": project.seed,
            },
            capability_status=capability_status,
        )

    # ------------------------------------------------------------------
    # Stage compilation
    # ------------------------------------------------------------------

    def _compile_stages(
        self,
        project: GameProject,
        target: RuntimeTargetContract,
        diagnostics: RuntimeCompilationDiagnostics,
    ) -> List[RuntimeStageContract]:
        planning_id = self._stage_id(
            project,
            "planning",
        )

        assets_id = self._stage_id(
            project,
            "assets",
        )

        world_id = self._stage_id(
            project,
            "world",
        )

        scene_id = self._stage_id(
            project,
            "scene",
        )

        physics_id = self._stage_id(
            project,
            "physics",
        )

        gameplay_id = self._stage_id(
            project,
            "gameplay",
        )

        assembly_id = self._stage_id(
            project,
            "assembly",
        )

        qa_id = self._stage_id(
            project,
            "qa",
        )

        stages = [
            RuntimeStageContract(
                stage_id=planning_id,
                name="planning",
                project_id=project.project_id,
                project_status=ProjectStatus.PLANNING,
                owner="director",
                depends_on=[],
                required_capabilities={
                    "runtime_execution",
                },
                expected_outputs=[
                    "architecture_plan",
                ],
                metadata={
                    "role": "project_planning",
                    "target_platform": (
                        target.target_platform.value
                    ),
                },
            ),

            RuntimeStageContract(
                stage_id=assets_id,
                name="assets",
                project_id=project.project_id,
                project_status=ProjectStatus.ASSET_GENERATION,
                owner="asset_generator",
                depends_on=[
                    planning_id,
                ],
                required_capabilities={
                    "runtime_execution",
                },
                expected_outputs=[
                    "asset_manifest",
                ],
                metadata={
                    "asset_requests": len(
                        project.asset_requests
                    ),
                    "asset_blueprints": len(
                        project.asset_blueprints
                    ),
                    "generated_assets": (
                        project.asset_manifest.generated_count
                    ),
                },
            ),

            RuntimeStageContract(
                stage_id=world_id,
                name="world",
                project_id=project.project_id,
                project_status=ProjectStatus.WORLD_GENERATION,
                owner="map_builder",
                depends_on=[
                    assets_id,
                ],
                required_capabilities={
                    "runtime_execution",
                },
                expected_outputs=[
                    "world_manifest",
                ],
                metadata={
                    "world_present": (
                        project.world_manifest
                        is not None
                    ),
                    "chunk_count": (
                        len(
                            project.world_manifest.chunks
                        )
                        if project.world_manifest
                        else 0
                    ),
                },
            ),

            RuntimeStageContract(
                stage_id=scene_id,
                name="scene",
                project_id=project.project_id,
                project_status=ProjectStatus.SCENE_GENERATION,
                owner="scene_compiler",
                depends_on=[
                    world_id,
                    assets_id,
                ],
                required_capabilities={
                    "runtime_execution",
                },
                expected_outputs=[
                    "scene_graph",
                ],
                metadata={
                    "scene_present": (
                        project.scene_graph
                        is not None
                    ),
                    "scene_object_count": (
                        len(
                            project.scene_graph.objects
                        )
                        if project.scene_graph
                        else 0
                    ),
                },
            ),

            RuntimeStageContract(
                stage_id=physics_id,
                name="physics",
                project_id=project.project_id,
                project_status=ProjectStatus.PHYSICS_CONFIG,
                owner="physics_agent",
                depends_on=[
                    scene_id,
                ],
                required_capabilities={
                    "runtime_execution",
                },
                expected_outputs=[
                    "physics_config",
                ],
                metadata={
                    "physics_configured": (
                        project.physics_config
                        is not None
                    ),
                    "physics_engine": (
                        project.physics_config.physics_engine
                        if project.physics_config
                        else None
                    ),
                },
            ),

            RuntimeStageContract(
                stage_id=gameplay_id,
                name="gameplay",
                project_id=project.project_id,
                project_status=ProjectStatus.GAMEPLAY_GENERATION,
                owner="gameplay_agent",
                depends_on=[
                    physics_id,
                    world_id,
                ],
                required_capabilities=(
                    self._gameplay_capabilities(
                        project
                    )
                ),
                expected_outputs=[
                    "gameplay_modules",
                    "runtime_state_schema",
                ],
                metadata={
                    "module_count": len(
                        project.gameplay_modules
                    ),
                    "module_types": sorted(
                        {
                            module.module_type.value
                            for module
                            in project.gameplay_modules.values()
                        }
                    ),
                },
            ),

            RuntimeStageContract(
                stage_id=assembly_id,
                name="assembly",
                project_id=project.project_id,
                project_status=ProjectStatus.SOURCE_GENERATION,
                owner="runtime_compiler",
                depends_on=[
                    assets_id,
                    world_id,
                    scene_id,
                    physics_id,
                    gameplay_id,
                ],
                required_capabilities={
                    "runtime_execution",
                },
                inputs=(
                    self._source_artifact_inputs(
                        project
                    )
                ),
                expected_outputs=[
                    "runtime_execution_request",
                    "runtime_pipeline",
                ],
                metadata={
                    "source_file_count": (
                        project.source_bundle.text_file_count
                    ),
                    "binary_file_count": (
                        project.source_bundle.binary_file_count
                    ),
                    "entry_point": (
                        project.source_bundle.entry_point
                    ),
                },
            ),

            RuntimeStageContract(
                stage_id=qa_id,
                name="qa",
                project_id=project.project_id,
                project_status=ProjectStatus.QA_TESTING,
                owner="qa_tester",
                depends_on=[
                    assembly_id,
                ],
                required_capabilities={
                    "runtime_execution",
                },
                expected_outputs=[
                    "qa_report",
                ],
                metadata={
                    "qa_present": (
                        project.qa_report
                        is not None
                    ),
                    "qa_status": (
                        project.qa_report.status.value
                        if project.qa_report
                        else "NOT_TESTED"
                    ),
                },
            ),
        ]

        return stages

    # ------------------------------------------------------------------
    # Stage helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _stage_id(
        project: GameProject,
        stage_name: str,
    ) -> str:
        return _stable_id(
            "stage",
            project.project_id,
            stage_name,
        )

    @staticmethod
    def _gameplay_capabilities(
        project: GameProject,
    ) -> Set[str]:
        capabilities = {
            "runtime_execution",
        }

        for module in (
            project.gameplay_modules.values()
        ):
            capabilities.update(
                module.runtime_capabilities
            )

        if len(capabilities) > MAX_CAPABILITIES:
            raise RuntimeCompilerValidationError(
                "gameplay capability count exceeds compiler limit"
            )

        return set(
            sorted(capabilities)
        )

    @staticmethod
    def _source_artifact_inputs(
        project: GameProject,
    ) -> List[
        RuntimeArtifactReference
    ]:
        result: List[
            RuntimeArtifactReference
        ] = []

        for path in sorted(
            project.source_bundle.files
        ):
            source_file = (
                project.source_bundle.files[path]
            )

            result.append(
                _source_reference(
                    path,
                    checksum=source_file.checksum,
                    size_bytes=len(
                        source_file.content.encode(
                            "utf-8",
                            errors="replace",
                        )
                    ),
                    kind="source",
                )
            )

        for path in sorted(
            project.source_bundle.binary_files
        ):
            binary_file = (
                project.source_bundle.binary_files[path]
            )

            result.append(
                _source_reference(
                    path,
                    checksum=binary_file.checksum,
                    size_bytes=len(
                        binary_file.content
                    ),
                    kind="binary_source",
                    media_type=binary_file.media_type,
                )
            )

        return result

    # ------------------------------------------------------------------
    # Execution request
    # ------------------------------------------------------------------

    def _compile_execution_request(
        self,
        project: GameProject,
        target: RuntimeTargetContract,
        stages: Sequence[
            RuntimeStageContract
        ],
        graph: DependencyGraph,
        execution_mode: RuntimeExecutionMode,
    ) -> RuntimeExecutionRequest:
        runtime_id = (
            f"runtime_{project.project_id}"
        )

        dependencies = list(
            graph.topological_order
        )

        if len(dependencies) > MAX_DEPENDENCIES:
            raise RuntimeCompilerValidationError(
                "runtime dependency count exceeds configured limit"
            )

        return RuntimeExecutionRequest(
            project_id=project.project_id,
            runtime_id=runtime_id,
            target=target,
            mode=execution_mode,
            commands=[],
            initial_state=None,
            requested_frame_count=None,
            requested_delta_time=(
                project.physics_config.fixed_timestep
                if project.physics_config
                else None
            ),
            required_capabilities=set(
                target.capabilities
            ),
            dependencies=dependencies,
            input_artifacts=(
                self._source_artifact_inputs(
                    project
                )
            ),
            metadata={
                "compiler": COMPILER_VERSION,
                "stage_ids": [
                    stage.stage_id
                    for stage in stages
                ],
                "dependency_levels": [
                    list(level)
                    for level
                    in graph.levels
                ],
                "project_seed": project.seed,
                "entry_point": (
                    project.source_bundle.entry_point
                ),
                "build_id": project.build_id,
                "target_platform": (
                    project.target_platform.value
                ),
            },
        )


# ============================================================================
# DEFAULT COMPILER
# ============================================================================

runtime_compiler = RuntimeCompiler()


# ============================================================================
# CONVENIENCE FUNCTIONS
# ============================================================================

def compile_runtime(
    project: GameProject,
    *,
    execution_mode: RuntimeExecutionMode = (
        RuntimeExecutionMode.REALTIME
    ),
) -> RuntimeCompilationResult:
    return runtime_compiler.compile(
        project,
        execution_mode=execution_mode,
    )


def compile_runtime_pipeline(
    project: GameProject,
) -> RuntimePipelineRequest:
    return runtime_compiler.compile_pipeline(
        project
    )


def compile_runtime_request(
    project: GameProject,
    *,
    execution_mode: RuntimeExecutionMode = (
        RuntimeExecutionMode.REALTIME
    ),
) -> RuntimeExecutionRequest:
    return runtime_compiler.compile_execution_request(
        project,
        execution_mode=execution_mode,
    )


__all__ = [
    "COMPILER_VERSION",
    "RuntimeCompilationDiagnostics",
    "RuntimeCompilationResult",
    "RuntimeCompiler",
    "RuntimeCompilerError",
    "RuntimeCompilerValidationError",
    "UnsupportedRuntimeTargetError",
    "compile_runtime",
    "compile_runtime_pipeline",
    "compile_runtime_request",
    "runtime_compiler",
]
