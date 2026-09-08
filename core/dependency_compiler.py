"""
RIOT / GOD NODE — Dependency Compiler
=====================================

Phase 7A: Deterministic dependency graph compiler.

Responsibilities
----------------
* Build a dependency DAG from runtime stages.
* Validate dependency references.
* Detect duplicate nodes.
* Detect self-dependencies.
* Detect dependency cycles.
* Produce deterministic topological execution order.
* Preserve parallelism information through graph levels.
* Never execute anything.
* Never fabricate successful outputs.

This module intentionally depends only on runtime contracts and standard
library functionality.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from core.runtime_contracts import (
    RuntimeStageContract,
)


# ============================================================================
# CONSTANTS
# ============================================================================

MAX_NODES = 4096
MAX_EDGES = 16384


# ============================================================================
# ERRORS
# ============================================================================

class DependencyCompilerError(ValueError):
    """Base dependency compilation error."""


class DuplicateNodeError(DependencyCompilerError):
    """Two nodes use the same identifier."""


class MissingDependencyError(DependencyCompilerError):
    """A node references a dependency that does not exist."""


class SelfDependencyError(DependencyCompilerError):
    """A node depends on itself."""


class DependencyCycleError(DependencyCompilerError):
    """The dependency graph contains a cycle."""


class DependencyCapacityError(DependencyCompilerError):
    """The dependency graph exceeds configured bounds."""


# ============================================================================
# NODE
# ============================================================================

@dataclass(slots=True, frozen=True)
class DependencyNode:
    """
    Immutable graph-node representation.

    The original RuntimeStageContract remains the source of truth.
    """

    node_id: str
    dependencies: Tuple[str, ...] = ()
    metadata: Mapping[str, object] = field(
        default_factory=dict
    )


# ============================================================================
# COMPILED GRAPH
# ============================================================================

@dataclass(slots=True, frozen=True)
class DependencyGraph:
    nodes: Mapping[str, DependencyNode]
    dependents: Mapping[str, Tuple[str, ...]]
    levels: Tuple[Tuple[str, ...], ...]
    topological_order: Tuple[str, ...]

    @property
    def node_count(self) -> int:
        return len(self.nodes)

    @property
    def edge_count(self) -> int:
        return sum(
            len(node.dependencies)
            for node in self.nodes.values()
        )

    @property
    def max_parallelism(self) -> int:
        return max(
            (len(level) for level in self.levels),
            default=0,
        )

    def dependencies_for(
        self,
        node_id: str,
    ) -> Tuple[str, ...]:
        node = self.nodes.get(node_id)

        if node is None:
            raise KeyError(
                f"Unknown dependency node: {node_id}"
            )

        return node.dependencies

    def dependents_for(
        self,
        node_id: str,
    ) -> Tuple[str, ...]:
        if node_id not in self.nodes:
            raise KeyError(
                f"Unknown dependency node: {node_id}"
            )

        return self.dependents.get(
            node_id,
            (),
        )


# ============================================================================
# COMPILER
# ============================================================================

class DependencyCompiler:
    """
    Deterministic DAG compiler.

    Input:
        RuntimeStageContract sequence

    Output:
        DependencyGraph

    No execution occurs in this class.
    """

    def __init__(
        self,
        *,
        max_nodes: int = MAX_NODES,
        max_edges: int = MAX_EDGES,
    ) -> None:
        self.max_nodes = max(
            1,
            int(max_nodes),
        )
        self.max_edges = max(
            0,
            int(max_edges),
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def compile(
        self,
        stages: Sequence[
            RuntimeStageContract
        ],
    ) -> DependencyGraph:
        if len(stages) > self.max_nodes:
            raise DependencyCapacityError(
                f"dependency graph has {len(stages)} nodes; "
                f"maximum is {self.max_nodes}"
            )

        nodes: Dict[
            str,
            DependencyNode,
        ] = {}

        total_edges = 0

        for stage in stages:
            node_id = self._normalize_node_id(
                stage.stage_id
            )

            if node_id in nodes:
                raise DuplicateNodeError(
                    f"duplicate dependency node: {node_id}"
                )

            dependencies = self._normalize_dependencies(
                node_id,
                stage.depends_on,
            )

            total_edges += len(
                dependencies
            )

            if total_edges > self.max_edges:
                raise DependencyCapacityError(
                    f"dependency graph has more than "
                    f"{self.max_edges} edges"
                )

            nodes[node_id] = DependencyNode(
                node_id=node_id,
                dependencies=tuple(
                    dependencies
                ),
                metadata={
                    "name": stage.name,
                    "owner": stage.owner,
                    "project_id": stage.project_id,
                },
            )

        self._validate_references(
            nodes
        )

        dependents = self._build_dependents(
            nodes
        )

        topological_order = (
            self._topological_sort(
                nodes,
                dependents,
            )
        )

        levels = self._build_levels(
            nodes,
            topological_order,
        )

        return DependencyGraph(
            nodes=nodes,
            dependents={
                key: tuple(value)
                for key, value in dependents.items()
            },
            levels=levels,
            topological_order=(
                topological_order
            ),
        )

    def compile_mapping(
        self,
        stages: Iterable[
            RuntimeStageContract
        ],
    ) -> DependencyGraph:
        """
        Convenience API accepting any iterable.
        """
        return self.compile(
            list(stages)
        )

    def validate(
        self,
        stages: Sequence[
            RuntimeStageContract
        ],
    ) -> None:
        """
        Validate graph structure without returning a graph.
        """
        self.compile(stages)

    # ------------------------------------------------------------------
    # Normalization
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_node_id(
        value: str,
    ) -> str:
        node_id = str(
            value or ""
        ).strip()

        if not node_id:
            raise DependencyCompilerError(
                "dependency node id cannot be empty"
            )

        return node_id

    @staticmethod
    def _normalize_dependencies(
        node_id: str,
        dependencies: Iterable[str],
    ) -> List[str]:
        result: List[str] = []
        seen: Set[str] = set()

        for raw in dependencies:
            dependency = str(
                raw or ""
            ).strip()

            if not dependency:
                continue

            if dependency == node_id:
                raise SelfDependencyError(
                    f"node '{node_id}' cannot depend on itself"
                )

            if dependency in seen:
                continue

            seen.add(dependency)
            result.append(dependency)

        result.sort()

        return result

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_references(
        nodes: Mapping[
            str,
            DependencyNode,
        ],
    ) -> None:
        known = set(
            nodes.keys()
        )

        for node in nodes.values():
            missing = [
                dependency
                for dependency
                in node.dependencies
                if dependency not in known
            ]

            if missing:
                raise MissingDependencyError(
                    f"node '{node.node_id}' references "
                    f"missing dependencies: {missing}"
                )

    # ------------------------------------------------------------------
    # Reverse edges
    # ------------------------------------------------------------------

    @staticmethod
    def _build_dependents(
        nodes: Mapping[
            str,
            DependencyNode,
        ],
    ) -> Dict[
        str,
        List[str],
    ]:
        dependents: Dict[
            str,
            List[str],
        ] = defaultdict(list)

        for node_id in nodes:
            dependents[node_id] = []

        for node in nodes.values():
            for dependency in node.dependencies:
                dependents[
                    dependency
                ].append(
                    node.node_id
                )

        for node_id in dependents:
            dependents[node_id].sort()

        return dependents

    # ------------------------------------------------------------------
    # Topological sort
    # ------------------------------------------------------------------

    @staticmethod
    def _topological_sort(
        nodes: Mapping[
            str,
            DependencyNode,
        ],
        dependents: Mapping[
            str,
            Sequence[str],
        ],
    ) -> Tuple[str, ...]:
        indegree = {
            node_id: len(
                node.dependencies
            )
            for node_id, node
            in nodes.items()
        }

        ready = [
            node_id
            for node_id, degree
            in indegree.items()
            if degree == 0
        ]

        ready.sort()

        queue = deque(
            ready
        )

        order: List[str] = []

        while queue:
            node_id = queue.popleft()
            order.append(
                node_id
            )

            children = sorted(
                dependents.get(
                    node_id,
                    (),
                )
            )

            for child in children:
                indegree[child] -= 1

                if indegree[child] == 0:
                    queue.append(
                        child
                    )

            if len(queue) > 1:
                ordered = sorted(
                    queue
                )
                queue.clear()
                queue.extend(
                    ordered
                )

        if len(order) != len(nodes):
            remaining = sorted(
                node_id
                for node_id, degree
                in indegree.items()
                if degree > 0
            )

            raise DependencyCycleError(
                "dependency graph contains a cycle; "
                f"unresolved nodes: {remaining}"
            )

        return tuple(order)

    # ------------------------------------------------------------------
    # Parallel levels
    # ------------------------------------------------------------------

    @staticmethod
    def _build_levels(
        nodes: Mapping[
            str,
            DependencyNode,
        ],
        topological_order: Sequence[str],
    ) -> Tuple[
        Tuple[str, ...],
        ...,
    ]:
        level_by_node: Dict[
            str,
            int,
        ] = {}

        for node_id in topological_order:
            dependencies = nodes[
                node_id
            ].dependencies

            if not dependencies:
                level_by_node[
                    node_id
                ] = 0
                continue

            level_by_node[
                node_id
            ] = 1 + max(
                level_by_node[
                    dependency
                ]
                for dependency
                in dependencies
            )

        max_level = max(
            level_by_node.values(),
            default=-1,
        )

        if max_level < 0:
            return ()

        levels: List[
            List[str]
        ] = [
            []
            for _ in range(
                max_level + 1
            )
        ]

        for node_id in topological_order:
            levels[
                level_by_node[node_id]
            ].append(
                node_id
            )

        return tuple(
            tuple(
                sorted(level)
            )
            for level in levels
        )


# ============================================================================
# CONVENIENCE FUNCTIONS
# ============================================================================

def compile_dependencies(
    stages: Sequence[
        RuntimeStageContract
    ],
) -> DependencyGraph:
    """
    Compile a deterministic dependency graph using default limits.
    """
    return DependencyCompiler().compile(
        stages
    )


def topological_order(
    stages: Sequence[
        RuntimeStageContract
    ],
) -> Tuple[str, ...]:
    """
    Return only deterministic execution order.
    """
    return compile_dependencies(
        stages
    ).topological_order


def parallel_levels(
    stages: Sequence[
        RuntimeStageContract
    ],
) -> Tuple[
    Tuple[str, ...],
    ...,
]:
    """
    Return stage groups that can execute concurrently.
    """
    return compile_dependencies(
        stages
    ).levels


__all__ = [
    "DependencyCapacityError",
    "DependencyCompiler",
    "DependencyCompilerError",
    "DependencyCycleError",
    "DependencyGraph",
    "DependencyNode",
    "DuplicateNodeError",
    "MissingDependencyError",
    "SelfDependencyError",
    "compile_dependencies",
    "parallel_levels",
    "topological_order",
]
