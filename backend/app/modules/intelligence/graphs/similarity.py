"""The similarity graph: corrective RAG, as a state machine.

```
              START
                |
              guard                 bound, redact, screen  (no LLM)
                |
         [blocked?] ---- yes ----> END
                |
                no
                |
            references              exact: OS-1042        (no LLM)
                |
            identifiers             exact: ERR-40001      (no LLM)
                |
              embed                 one local embedding call
                |
      +---------+---------+
      |                   |
 retrieve_semantic   retrieve_lexical      same superstep, concurrent
      |                   |
      +---------+---------+
                |
              fuse                  cascade               (no LLM)
                |
              grade                 counts, not a model   (no LLM)
                |
      +---------+---------+---------+
      |         |                   |
   rewrite    rerank             finish
      |         |                   |
      |     (hydrate first)         |
      +--> back to embed            |
                                   END
```

## What makes this *corrective* RAG

Plain RAG retrieves once and answers with whatever came back. Corrective RAG
grades its own retrieval and can go back for more. The cycle is the difference,
and it is why this is a graph rather than a sequence: `grade` routes to
`rewrite`, and `rewrite` routes back to `embed`.

Two things make the cycle safe:

* **`rewrites` is state, and the cap is checked in `grade`.** The loop's
  termination does not depend on the model deciding to stop, which is the usual
  way an agentic loop runs away.
* **Every path reaches `finish`.** There is no route that ends without the
  bookkeeping node running, so a rewrite that produced nothing still records
  what happened.

## Cost, stated plainly

* Blocked input: **0 LLM calls.**
* Good retrieval, rerank disabled: **0 LLM calls.**
* Good retrieval, rerank enabled: **1 call.**
* Weak retrieval: **1 rewrite call per cycle, then 1 rerank.** Capped at
  ``max_rewrites``, so the worst case is 3.

On a 500-request daily quota those numbers are the design, not a detail.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import lru_cache
from typing import Any, Protocol

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime

from app.core.logging import get_logger
from app.modules.intelligence.domain.deps import RetrievalDeps
from app.modules.intelligence.graphs.state import SimilarityState
from app.modules.intelligence.nodes.embed import embed_query
from app.modules.intelligence.nodes.exact import probe_identifiers, resolve_references
from app.modules.intelligence.nodes.fuse import fuse_candidates
from app.modules.intelligence.nodes.grade import grade_candidates, route_after_grading
from app.modules.intelligence.nodes.hydrate import guard_query, hydrate_candidates
from app.modules.intelligence.nodes.rerank import rerank_candidates
from app.modules.intelligence.nodes.retrieve import retrieve_lexical, retrieve_semantic
from app.modules.intelligence.nodes.rewrite import rewrite_query

logger = get_logger(__name__)

Node = Callable[[SimilarityState, RetrievalDeps], Awaitable[dict[str, object]]]


class _AdaptedNode(Protocol):
    """The shape LangGraph accepts: state positional, runtime keyword-only."""

    __name__: str

    def __call__(
        self, state: SimilarityState, *, runtime: Runtime[RetrievalDeps]
    ) -> Awaitable[dict[str, object]]: ...


def _adapt(node: Node) -> _AdaptedNode:
    """Adapt a ``(state, deps)`` node to LangGraph's ``(state, *, runtime)``.

    Keeps every node framework-agnostic and unit-testable with a plain dict.
    ``runtime`` is keyword-only because that is how LangGraph's node protocol
    declares it; a positional parameter of the same name does not match.
    """

    async def run(state: SimilarityState, *, runtime: Runtime[RetrievalDeps]) -> dict[str, object]:
        return await node(state, runtime.context)

    run.__name__ = node.__name__
    return run


async def finish(state: SimilarityState, deps: RetrievalDeps) -> dict[str, object]:
    """Terminal bookkeeping.

    Exists so every path — blocked, empty, reranked, or degraded — passes
    through one place that can log the outcome. Without it, "how often does the
    rewrite cycle actually fire" is a question answerable only by reading
    scattered log lines.
    """
    logger.info(
        "similarity run complete",
        extra={
            "grade": state.get("grade", "n/a"),
            "rewrites": state.get("rewrites", 0),
            "candidates": len(state.get("candidates", [])),
            "judgements": len(state.get("judgements", [])),
            "blocked": state.get("blocked", False),
            "rerank_model": state.get("rerank_model", "none"),
        },
    )
    return {"trace": ["finish"]}


def route_after_guard(state: SimilarityState) -> str:
    """Blocked input skips the entire pipeline.

    A conditional edge rather than an early return inside the guard node,
    because a node returning "stop" is not something LangGraph can express —
    control flow belongs in edges, and putting it there is what keeps the trace
    honest about which nodes ran.
    """
    return "finish" if state.get("blocked") else "references"


@lru_cache(maxsize=1)
def similarity_graph() -> CompiledStateGraph[SimilarityState, RetrievalDeps, Any, Any]:
    """Build and compile the graph once.

    Cached because compilation validates the topology and builds an executor,
    which is pure overhead to repeat. Safe to share across requests precisely
    because nothing request-specific is baked in — the session-bound
    dependencies arrive as runtime context.
    """
    graph: StateGraph[SimilarityState, RetrievalDeps, Any, Any] = StateGraph(
        SimilarityState, context_schema=RetrievalDeps
    )

    graph.add_node("guard", _adapt(guard_query))
    graph.add_node("references", _adapt(resolve_references))
    graph.add_node("identifiers", _adapt(probe_identifiers))
    graph.add_node("embed", _adapt(embed_query))
    graph.add_node("retrieve_semantic", _adapt(retrieve_semantic))
    graph.add_node("retrieve_lexical", _adapt(retrieve_lexical))
    graph.add_node("fuse", _adapt(fuse_candidates))
    graph.add_node("grade", _adapt(grade_candidates))
    graph.add_node("rewrite", _adapt(rewrite_query))
    graph.add_node("hydrate", _adapt(hydrate_candidates))
    graph.add_node("rerank", _adapt(rerank_candidates))
    graph.add_node("finish", _adapt(finish))

    graph.add_edge(START, "guard")
    graph.add_conditional_edges(
        "guard", route_after_guard, {"references": "references", "finish": "finish"}
    )

    # Exact lookups, sequential because both write `pinned` and the second must
    # see what the first added. Two indexed queries are not worth a reducer.
    graph.add_edge("references", "identifiers")
    graph.add_edge("identifiers", "embed")

    # Fan out: both retrievers land in the same superstep, so LangGraph runs
    # them concurrently and merges their `rankings` writes with the reducer.
    graph.add_edge("embed", "retrieve_semantic")
    graph.add_edge("embed", "retrieve_lexical")

    # Fan in: `fuse` has two incoming edges, so it waits for both branches --
    # no barrier code, and no chance of fusing a half-filled state.
    graph.add_edge("retrieve_semantic", "fuse")
    graph.add_edge("retrieve_lexical", "fuse")

    graph.add_edge("fuse", "grade")
    graph.add_conditional_edges(
        "grade",
        route_after_grading,
        {"rewrite": "rewrite", "rerank": "hydrate", "finish": "finish"},
    )

    # THE CYCLE. `rewrite` goes back to `embed`, not to `guard`: the text has
    # already been guarded, and re-guarding a model-generated rewrite would
    # redact labels the model was given, producing "[EMAIL]" turning into
    # "[[EMAIL]]" on the second pass.
    graph.add_edge("rewrite", "embed")

    graph.add_edge("hydrate", "rerank")
    graph.add_edge("rerank", "finish")
    graph.add_edge("finish", END)

    # No checkpointer: this graph is a pure function of its input and completes
    # in one call, so there is nothing to resume. The chatbot in Phase F is
    # where a checkpointer earns its cost.
    return graph.compile(name="similarity")


async def run_similarity(state: SimilarityState, deps: RetrievalDeps) -> SimilarityState:
    """Execute the graph.

    ``recursion_limit`` is LangGraph's own backstop, in supersteps rather than
    rewrites. Set above the worst legitimate path (guard, 2 exact, embed, 2
    retrievers, fuse, grade, three cycles, hydrate, rerank, finish) so a genuine
    run never trips it, while a topology bug that creates an unintended loop
    still fails loudly instead of running forever. Two independent limits
    guarding the same cycle is deliberate: ``max_rewrites`` is the business
    rule, this is the safety net for the rule being wrong.
    """
    result = await similarity_graph().ainvoke(state, context=deps, config={"recursion_limit": 40})
    return dict(result)  # type: ignore[return-value]
