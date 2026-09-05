"""Adapting plain node functions to LangGraph's calling convention.

The same twelve lines were written three times — once in each pipeline — with
nothing differing but the state and dependency types. That is what a generic
belongs to.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from langgraph.runtime import Runtime


class AdaptedNode[StateT](Protocol):
    """The shape LangGraph accepts: state positional, runtime keyword-only.

    The runtime is typed ``Runtime[Any]`` rather than parameterised by the
    dependency type. LangGraph bounds that parameter to dataclass / TypedDict /
    BaseModel, and an unbounded type variable cannot satisfy the bound — while
    restating the bound here would mean importing LangGraph's private union
    into a module whose whole purpose is to keep LangGraph out of the nodes.

    Nothing is lost that mattered: this value is handed straight back to a node
    whose own signature names its dependency type exactly, so the precision
    lives where it is checked.
    """

    __name__: str

    def __call__(self, state: StateT, *, runtime: Runtime[Any]) -> Awaitable[dict[str, object]]: ...


def adapt[StateT, DepsT](
    node: Callable[[StateT, DepsT], Awaitable[dict[str, object]]],
) -> AdaptedNode[StateT]:
    """Adapt a ``(state, deps)`` node to LangGraph's ``(state, *, runtime)``.

    This is what keeps every node framework-agnostic: a node is an ordinary
    async function of two arguments, testable by calling it with a plain dict
    and a fake, with no graph, no runtime, and no LangGraph import anywhere in
    the file that defines it.

    ``runtime`` is keyword-only because that is how LangGraph's node protocol
    declares it. A positional parameter of the same name does not match, and
    the resulting failure is a runtime signature error rather than anything the
    type checker points at — which is why it is stated here once instead of
    being rediscovered per pipeline.

    ``__name__`` is copied across because LangGraph uses it for the node's
    label in traces; without it every node in every graph reports as ``run``.
    """

    async def run(state: StateT, *, runtime: Runtime[Any]) -> dict[str, object]:
        # `context` is the dependency bundle the graph was invoked with. Typed
        # loosely here and narrowly at the node, which is where a mismatch
        # would actually be a bug.
        deps: DepsT = runtime.context
        return await node(state, deps)

    run.__name__ = getattr(node, "__name__", "node")
    return run


def node_names(graph: Any) -> list[str]:
    """The node labels of a compiled graph, for assertions and diagnostics.

    Small, but it exists so a test can check the topology without reaching into
    LangGraph internals in three different places.
    """
    return sorted(getattr(graph, "nodes", {}))
