from __future__ import annotations

from pathlib import Path


FILE = Path("god_brain/orchestrator.py")


IMPORT_ANCHOR = """from core.game_project import (
    ArchitecturePlan,
    AssetBlueprint as CanonicalAssetBlueprint,
    AssetRequest as CanonicalAssetRequest,
    AssetType as CanonicalAssetType,
    AssetGenerationStatus as CanonicalAssetGenerationStatus,
    GameplayModule,
    GameplayModuleType,
    GameProject,
    PhysicsConfig,
    ProjectStatus,
    QAReport,
    QAStatus,
    RuntimeType,
    WorldManifest,
    ChunkManifest,
)
"""


IMPORT_BLOCK = IMPORT_ANCHOR + """
from core.runtime_compiler import (
    RuntimeCompilationResult,
    compile_runtime,
)
"""


PIPELINE_ANCHOR = """                project.validate_for_build(require_qa=self.config.require_qa)
                _project_transition(project, ProjectStatus.QA_TESTING)
                qa_stage = project.pipeline_stages.get(ProjectStatus.QA_TESTING.value)
                if qa_stage is not None and qa_stage.status.value == "RUNNING":
                    project.complete_stage(ProjectStatus.QA_TESTING, success=True)
                _project_transition(project, ProjectStatus.READY_FOR_BUILD)

                state.transition(PipelineStage.ASSEMBLY)
                builder_config = project.to_builder_config()
                state.transition(PipelineStage.COMPLETE)
"""


PIPELINE_REPLACEMENT = """                project.validate_for_build(require_qa=self.config.require_qa)
                _project_transition(project, ProjectStatus.QA_TESTING)
                qa_stage = project.pipeline_stages.get(ProjectStatus.QA_TESTING.value)
                if qa_stage is not None and qa_stage.status.value == "RUNNING":
                    project.complete_stage(ProjectStatus.QA_TESTING, success=True)
                _project_transition(project, ProjectStatus.READY_FOR_BUILD)

                # ---------------------------------------------------------
                # 7. CANONICAL RUNTIME COMPILATION
                # ---------------------------------------------------------
                # Additive integration only:
                # - Existing QA flow remains unchanged.
                # - Existing GameProject remains the source of truth.
                # - Existing builder configuration remains unchanged.
                # - Runtime compilation is declarative and does not execute
                #   providers, agents, builders or schedulers.
                runtime_compilation: RuntimeCompilationResult = compile_runtime(
                    project
                )

                if not runtime_compilation.diagnostics.valid:
                    raise RuntimeError(
                        "Runtime compilation failed: "
                        + "; ".join(
                            runtime_compilation.diagnostics.errors
                        )
                    )

                runtime_pipeline_request = (
                    runtime_compilation.request.model_dump(mode="json")
                    if hasattr(
                        runtime_compilation.request,
                        "model_dump",
                    )
                    else _json_safe(
                        runtime_compilation.request
                    )
                )

                runtime_execution_request = (
                    runtime_compilation.runtime_request.model_dump(mode="json")
                    if hasattr(
                        runtime_compilation.runtime_request,
                        "model_dump",
                    )
                    else _json_safe(
                        runtime_compilation.runtime_request
                    )
                )

                runtime_compilation_metadata = {
                    "compiler_version": (
                        runtime_compilation.diagnostics.compiler_version
                    ),
                    "valid": (
                        runtime_compilation.diagnostics.valid
                    ),
                    "warnings": list(
                        runtime_compilation.diagnostics.warnings
                    ),
                    "stage_count": (
                        runtime_compilation.diagnostics.stage_count
                    ),
                    "dependency_count": (
                        runtime_compilation.diagnostics.dependency_count
                    ),
                    "topological_order": list(
                        runtime_compilation.diagnostics.topological_order
                    ),
                    "parallel_levels": [
                        list(level)
                        for level
                        in runtime_compilation.diagnostics.parallel_levels
                    ],
                }

                # Persist runtime compilation alongside the canonical project
                # without changing any existing project/build contract.
                project.metadata["runtime_compilation"] = _json_safe(
                    runtime_compilation_metadata
                )
                project.metadata["runtime_pipeline_request"] = _json_safe(
                    runtime_pipeline_request
                )
                project.metadata["runtime_execution_request"] = _json_safe(
                    runtime_execution_request
                )

                state.context_data["runtime_compilation"] = _json_safe(
                    runtime_compilation_metadata
                )

                # ---------------------------------------------------------
                # Existing builder connection — DO NOT CHANGE
                # ---------------------------------------------------------
                state.transition(PipelineStage.ASSEMBLY)
                builder_config = project.to_builder_config()
                state.transition(PipelineStage.COMPLETE)
"""


RETURN_ANCHOR = """                    "project": project.model_dump(mode="json"),
                    "build_config": builder_config,
                    "architecture": _json_safe(project.architecture_plan),
"""


RETURN_REPLACEMENT = """                    "project": project.model_dump(mode="json"),
                    "build_config": builder_config,
                    "runtime_pipeline": runtime_pipeline_request,
                    "runtime_execution": runtime_execution_request,
                    "runtime_compilation": runtime_compilation_metadata,
                    "architecture": _json_safe(project.architecture_plan),
"""


def main() -> None:
    if not FILE.exists():
        raise SystemExit(
            f"ERROR: required file not found: {FILE}"
        )

    original = FILE.read_text(
        encoding="utf-8"
    )

    updated = original

    # ---------------------------------------------------------
    # 1. Import integration
    # ---------------------------------------------------------
    if "from core.runtime_compiler import (" not in updated:
        if IMPORT_ANCHOR not in updated:
            raise SystemExit(
                "ERROR: import anchor not found. "
                "File was not modified."
            )

        updated = updated.replace(
            IMPORT_ANCHOR,
            IMPORT_BLOCK,
            1,
        )

    # ---------------------------------------------------------
    # 2. Runtime compiler insertion
    # ---------------------------------------------------------
    runtime_marker = (
        "# 7. CANONICAL RUNTIME COMPILATION"
    )

    if runtime_marker not in updated:
        if PIPELINE_ANCHOR not in updated:
            raise SystemExit(
                "ERROR: pipeline anchor not found. "
                "File was not modified."
            )

        updated = updated.replace(
            PIPELINE_ANCHOR,
            PIPELINE_REPLACEMENT,
            1,
        )

    # ---------------------------------------------------------
    # 3. Successful response integration
    # ---------------------------------------------------------
    runtime_return_marker = (
        '"runtime_pipeline": runtime_pipeline_request,'
    )

    if runtime_return_marker not in updated:
        if RETURN_ANCHOR not in updated:
            raise SystemExit(
                "ERROR: return anchor not found. "
                "File was not modified."
            )

        updated = updated.replace(
            RETURN_ANCHOR,
            RETURN_REPLACEMENT,
            1,
        )

    # ---------------------------------------------------------
    # Safety checks before writing
    # ---------------------------------------------------------
    required_strings = (
        "from core.runtime_compiler import (",
        "RuntimeCompilationResult",
        "compile_runtime(",
        "runtime_pipeline_request",
        "runtime_execution_request",
        "runtime_compilation_metadata",
        '"runtime_pipeline": runtime_pipeline_request',
        '"runtime_execution": runtime_execution_request',
        '"runtime_compilation": runtime_compilation_metadata',
        "builder_config = project.to_builder_config()",
    )

    missing = [
        item
        for item in required_strings
        if item not in updated
    ]

    if missing:
        raise SystemExit(
            "ERROR: safety verification failed. "
            "Missing expected integration blocks:\n"
            + "\n".join(
                f" - {item}"
                for item in missing
            )
        )

    # Ensure only one runtime integration block exists.
    if updated.count(
        "# 7. CANONICAL RUNTIME COMPILATION"
    ) > 1:
        raise SystemExit(
            "ERROR: duplicate runtime compilation block detected. "
            "File was not modified."
        )

    # Ensure builder call remains intact exactly.
    if updated.count(
        "builder_config = project.to_builder_config()"
    ) != original.count(
        "builder_config = project.to_builder_config()"
    ):
        raise SystemExit(
            "ERROR: builder connection count changed. "
            "File was not modified."
        )

    FILE.write_text(
        updated,
        encoding="utf-8",
        newline="\n",
    )

    print(
        "FIX 3 APPLIED SUCCESSFULLY"
    )
    print(
        "File:",
        FILE,
    )
    print(
        "Existing builder connection preserved."
    )
    print(
        "RuntimeCompiler integration added."
    )
    print(
        "Runtime pipeline/execution metadata exposed."
    )


if __name__ == "__main__":
    main()
