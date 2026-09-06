r"""RAGAS evaluation of the chat assistant's generated answers.

    .venv\Scripts\python -m scripts.eval_ragas
    .venv\Scripts\python -m scripts.eval_ragas --limit 3

## What this measures that nothing else does

`eval_retrieval.py` scores *retrieval*: was the right ticket in the top five.
It is free, deterministic, and it is the gate on every change. What it cannot
score is whether the sentence the assistant produced was any good — recall@20
has nothing to say about an answer that cites a ticket that does not exist.

That gap is what RAGAS fills here. Three metrics, all LLM-judged:

* **Faithfulness** — is every claim in the answer supported by the context the
  assistant actually retrieved? This is the hallucination detector, and it is
  the reason this script exists: the local model has been observed inventing
  ticket references, and faithfulness is the number that catches it without a
  human reading every answer.
* **Response relevancy** — does the answer address the question that was asked,
  rather than a nearby one?
* **Context precision** — of the tool results the assistant pulled in, how many
  were actually useful? A low score means the agent is over-fetching.

## Why this runs in two phases

RAGAS calls ``nest_asyncio.apply()`` when it is imported, which patches the
running event loop. LangGraph's anyio cancel scopes do not survive that: a
graph invoked in a loop ragas has touched fails with ``RuntimeError: Timeout
should be used inside a task``, from deep inside pregel where the cause is
invisible.

So the phases are separated and ragas is imported **after** the last graph call
has finished:

1. ``--generate`` runs the probes through the real chat graph and writes
   samples to JSON. ragas is not imported.
2. ``--score`` loads that JSON and evaluates it. No graph runs.

Running with neither flag does both, in that order, in one process. The split
earns its keep beyond the bug: answers can be re-scored without re-running the
assistant, and samples captured from production traffic can be dropped into the
same file and scored identically.

## Why it drives the graph directly instead of the HTTP API

The API deliberately does not return the assistant's observations — a user has
no need for them, and exposing internals to satisfy a test is how internals
stop being internal. RAGAS needs them as `retrieved_contexts`, so this invokes
the chat graph the same way `eval_retrieval.py` invokes the similarity graph.

## Choosing the judge

``--judge local`` (the default) runs the judge on ``qwen3:8b`` through
``langchain-ollama``. Nothing leaves the machine and nothing is billed.
``--judge hosted`` uses Gemini instead.

The local default is a deliberate choice with a real cost, and the cost should
be understood rather than discovered:

**The judge and the answerer become the same model.** With
``AI_PREFER_LOCAL=true``, ``qwen3:8b`` both writes the answers and grades them.
That measures *self-consistency*, not quality: a model is unlikely to notice
the kinds of mistake it is itself prone to. The scores are still useful for
detecting a regression -- if faithfulness drops after a change, something
changed -- but they are weak evidence that the system is *good*.

**Both judges are noisy, and the local one is not obviously worse.** Scoring one
identical sample three times at ``temperature=0``:

    gemini-3.5-flash-lite   0.25, 0.25, 1.00
    qwen3:8b                0.75, 0.33, 0.33

So the hosted judge is not a reliable oracle either. Read RAGAS as a trend
across runs, never as a per-row verdict, whichever judge produced it.

``reasoning=False`` is passed to ``ChatOllama`` for the same reason our own
adapter sets ``"think": false``: qwen3 emits chain-of-thought by default, and
the output here is schema-constrained, so that reasoning is generated and then
discarded.

## Where the numbers go

RAGAS has no UI -- it returns a dict and a dataframe. Two things are wired here
so the scores do not live only in a terminal that gets closed:

* **MLflow** receives every score as a run, so metrics can be compared across
  runs and plotted over time. It is a local SQLite file under ``var/mlflow.db``
  and needs no server to write; ``mlflow ui --backend-store-uri
  sqlite:///var/mlflow.db`` serves the dashboard when you want to look.
* **LangSmith** receives the judge's own LLM calls, because tracing is enabled
  before the metrics run. Useful for a different question: not "what did the
  system score" but "what exactly did the judge see when it scored it", which
  is how you audit a metric you do not trust.

Neither is required. Both are skipped silently when unconfigured.

## Cost

Every metric is one or more LLM calls **per sample**. Ten questions across three
metrics is roughly 40-60 hosted calls. That is why this is a deliberate,
occasional run and `eval_retrieval.py` is the thing wired into every change.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
import types
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

# --------------------------------------------------------------------- shim
# ragas 0.4.3 imports ChatVertexAI from a `langchain_community` path that was
# removed when that package was sunset. It is used for an isinstance check
# against a provider we do not use, so a stub satisfies the import. Installed
# before ragas is imported, and nowhere else in the codebase.
if "langchain_community.chat_models.vertexai" not in sys.modules:
    _stub = types.ModuleType("langchain_community.chat_models.vertexai")
    _stub.ChatVertexAI = type("ChatVertexAI", (), {})  # type: ignore[attr-defined]
    sys.modules["langchain_community.chat_models.vertexai"] = _stub

from app.core.config import settings
from app.core.database import session_scope
from app.modules.intelligence.deps import get_ai_deps
from app.modules.intelligence.llm.registry import ModelRegistry
from app.modules.intelligence.observability.tracing import configure_tracing
from app.modules.intelligence.pipelines.chat import (
    ChatDeps,
    ChatState,
    build_chat_graph,
)
from app.modules.intelligence.tools.registry import build_tools
from app.modules.intelligence.tools.tickets import ToolContext
from langchain_core.embeddings import Embeddings
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_ollama import ChatOllama

# ragas is imported inside `score()`, never at module scope -- see the
# two-phase note above. Importing it here would patch the event loop before
# the chat graph ever runs.

SAMPLE_FILE = pathlib.Path("var/ragas_samples.json")
#: MLflow 3.x refuses the plain filesystem store ("in maintenance mode") and
#: wants a database. SQLite is a file too, so this stays a zero-service
#: local setup -- it just gets the schema MLflow now expects.
MLFLOW_DB = pathlib.Path("var/mlflow.db")

TENANT = uuid.UUID("01a05489-439d-716d-bc78-ab0867838221")
USER = uuid.UUID(int=0)


@dataclass(frozen=True, slots=True)
class Probe:
    """One question, and what a correct answer would have to contain.

    ``must_mention`` is a cheap deterministic check run alongside the LLM
    metrics -- it costs nothing and it catches the failure mode RAGAS is
    weakest at, which is an answer that is fluent, faithful to its context, and
    about the wrong thing entirely.
    """

    question: str
    must_mention: tuple[str, ...] = ()


#: Questions chosen to exercise different tools, not to be easy.
#:
#: Two of them are deliberately unanswerable from the corpus. An assistant that
#: answers those confidently is worse than one that says it cannot -- and
#: faithfulness is the metric that notices.
PROBES: list[Probe] = [
    Probe("How many tickets does the Payroll team have?"),
    Probe("What is ticket OS-4 about?", ("SCIM",)),
    Probe("Which tickets are still open?"),
    Probe("Are there any duplicate reports of the SCIM provisioning problem?", ("OS-",)),
    Probe("What was the resolution for OS-1?"),
    Probe("Show me the highest priority tickets."),
    Probe("Who is assigned to the Workforce team tickets?"),
    Probe("What is the average salary of employees at Keka?"),  # unanswerable
    Probe("How many tickets were raised last Tuesday?"),  # unanswerable-ish
    Probe("Which team has the most open bugs?"),
]


def _run_sync(coro: Any) -> Any:
    """Await a coroutine from synchronous code, safely, whatever loop exists.

    ragas drives its metrics through nest_asyncio, so by the time an embedding
    is requested there is already a running loop on this thread and
    ``run_until_complete`` raises ``RuntimeError: This event loop is already
    running`` -- leaving the coroutine unawaited and the metric NaN.

    Running it on a private thread with a private loop sidesteps the question
    entirely: nothing is nested, because nothing is shared.
    """
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


class OllamaLangchainEmbeddings(Embeddings):
    """Adapt our embedding port to LangChain's interface, for RAGAS only.

    RAGAS needs embeddings for ResponseRelevancy, which works by generating
    questions from the answer and comparing them to the original. It wants a
    LangChain `Embeddings`; our provider is a plain port. A small adapter is
    cheaper than either taking a LangChain dependency into the embedding path
    or letting RAGAS reach for OpenAI by default.
    """

    def __init__(self, provider: Any) -> None:
        self._provider = provider

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        vectors = _run_sync(self._provider.embed_documents(texts))
        return [[float(v) for v in vector] for vector in vectors]

    def embed_query(self, text: str) -> list[float]:
        vector = _run_sync(self._provider.embed_query(text))
        return [float(v) for v in vector]

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        vectors = await self._provider.embed_documents(texts)
        return [[float(v) for v in vector] for vector in vectors]

    async def aembed_query(self, text: str) -> list[float]:
        vector = await self._provider.embed_query(text)
        return [float(v) for v in vector]


def _contexts_from(observations: list[dict[str, Any]]) -> list[str]:
    """Turn the assistant's tool results into RAGAS `retrieved_contexts`.

    One string per tool call, carrying the tool name so a judge can tell
    "I looked this up" from "I inferred it". Serialised rather than summarised:
    a judge scoring faithfulness needs what the assistant actually saw, not a
    tidied version of it.
    """
    contexts: list[str] = []
    for observation in observations:
        tool = observation.get("tool", "?")
        result = observation.get("result", {})
        contexts.append(f"[{tool}] {json.dumps(result, default=str)[:4000]}")
    return contexts


async def _answer(probe: Probe) -> tuple[str, list[str], str, list[str]]:
    """Run one question through the real chat graph.

    Returns the answer, the retrieved contexts, the model that served it, and
    the node trace. A fresh thread id per probe, so no question can be answered
    from a previous one's context -- which would make the contexts wrong and
    the faithfulness score meaningless.
    """
    from langgraph.checkpoint.memory import InMemorySaver

    ai = await get_ai_deps()
    async with session_scope(tenant_id=TENANT) as db:
        graph = build_chat_graph(InMemorySaver())
        deps = ChatDeps(
            registry=ModelRegistry(db, TENANT),
            tools={
                tool.name: tool
                for tool in build_tools(db, ToolContext(tenant_id=TENANT, user_id=USER), ai)
            },
            known_people=(),
        )
        state: ChatState = {
            "tenant_id": TENANT,
            "user_id": USER,
            "conversation_id": uuid.uuid4(),
            "messages": [{"role": "user", "content": probe.question}],
            "observations": [],
            "call_signatures": [],
            "tool_calls": 0,
        }
        final = await graph.ainvoke(
            state,
            context=deps,
            config={"configurable": {"thread_id": str(uuid.uuid4())}},
        )

    return (
        str(final.get("answer", "")).strip(),
        _contexts_from(final.get("observations", [])),
        str(final.get("model", "?")),
        list(final.get("trace", [])),
    )


def _log_to_mlflow(result: Any, records: list[dict[str, Any]], judge_name: str) -> str | None:
    """Record one evaluation as an MLflow run. Returns the run id, or None.

    Failure here must never lose the scores -- they have already been computed
    and printed by the time this is called, and an unreachable tracking store
    is not a reason to discard them.
    """
    try:
        import mlflow
    except ImportError:
        return None

    try:
        MLFLOW_DB.parent.mkdir(parents=True, exist_ok=True)
        mlflow.set_tracking_uri(f"sqlite:///{MLFLOW_DB.resolve().as_posix()}")
        mlflow.set_experiment("ragas-chat")

        with mlflow.start_run() as run:
            # What produced the answers, so two runs are comparable only when
            # they should be. A score improving because the model changed is a
            # different fact from one improving because the code did.
            answering = {r.get("model", "?") for r in records}
            mlflow.log_params(
                {
                    "answering_model": ",".join(sorted(answering)),
                    "judge_model": judge_name,
                    # Whether the judge and the answerer were the same model.
                    # When they are, the score measures self-consistency and
                    # must not be compared against a run where they differed.
                    "self_judged": judge_name in {r.get("model") for r in records},
                    "prefer_local": settings.AI_PREFER_LOCAL,
                    "embedding_model": settings.OLLAMA_EMBED_MODEL,
                    "samples": len(records),
                    "rerank_enabled": settings.AI_RERANK_ENABLED,
                }
            )

            scores = dict(getattr(result, "_repr_dict", {}) or {})
            for name, value in scores.items():
                try:
                    mlflow.log_metric(name, float(value))
                except (TypeError, ValueError):
                    continue

            # The samples themselves, so a score can be traced back to the
            # answer that earned it. A metric without its evidence is a number
            # nobody can act on.
            mlflow.log_dict({"samples": records}, "samples.json")
            return str(run.info.run_id)
    except Exception as exc:
        print(f"  (mlflow logging skipped: {type(exc).__name__}: {exc})")
        return None


def _judge(kind: str) -> tuple[Any, str]:
    """Build the evaluating model. Returns the wrapper and its name.

    The name is returned so it can be logged as an MLflow param -- a
    faithfulness score is not comparable across judges, and a run that does not
    record which one produced it cannot be compared to anything later.
    """
    from ragas.llms import LangchainLLMWrapper

    if kind == "local":
        wrapped: Any = LangchainLLMWrapper(
            ChatOllama(
                model=settings.OLLAMA_CHAT_MODEL,
                base_url=settings.OLLAMA_BASE_URL,
                temperature=0.0,
                # As in `llm/ollama.py`: qwen3 reasons before answering by
                # default, and the output here is schema-constrained, so that
                # reasoning is paid for and thrown away.
                reasoning=False,
            )
        )
        return wrapped, settings.OLLAMA_CHAT_MODEL

    if not settings.GOOGLE_API_KEY:
        print(
            "No GOOGLE_API_KEY configured, so --judge hosted cannot run.\n"
            "Use --judge local, which needs nothing but Ollama.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    wrapped = LangchainLLMWrapper(
        ChatGoogleGenerativeAI(
            model=settings.GEMINI_MODEL_STRONG,
            google_api_key=settings.GOOGLE_API_KEY,
            temperature=0.0,
        )
    )
    return wrapped, settings.GEMINI_MODEL_STRONG


async def generate(limit: int) -> list[dict[str, Any]]:
    """Phase one: answer every probe through the real chat graph.

    ragas must not be imported while this runs.
    """
    probes = PROBES[:limit]
    records: list[dict[str, Any]] = []

    print("ANSWERING")
    for index, probe in enumerate(probes, start=1):
        answer, contexts, model, trace = await _answer(probe)
        missing = [m for m in probe.must_mention if m.lower() not in answer.lower()]
        flag = "" if not missing else f"   MISSING {missing}"
        print(f"  {index:>2}. [{model}] {len(contexts)} context(s){flag}")
        print(f"      Q: {probe.question}")
        print(f"      A: {answer[:110]}")
        records.append(
            {
                "question": probe.question,
                "answer": answer,
                "contexts": contexts,
                "model": model,
                "trace": trace,
                "missing_terms": missing,
            }
        )

    SAMPLE_FILE.parent.mkdir(parents=True, exist_ok=True)
    SAMPLE_FILE.write_text(json.dumps(records, indent=2, default=str), encoding="utf-8")
    print(f"\n  wrote {len(records)} sample(s) to {SAMPLE_FILE}")
    return records


def score(records: list[dict[str, Any]], judge_kind: str) -> None:
    """Phase two: judge the collected answers. No graph runs here."""
    from ragas import EvaluationDataset, SingleTurnSample, evaluate
    from ragas.embeddings import LangchainEmbeddingsWrapper
    from ragas.metrics import (
        Faithfulness,
        LLMContextPrecisionWithoutReference,
        ResponseRelevancy,
    )

    scorable = [r for r in records if r["contexts"]]
    if not scorable:
        print("\nNo sample had any retrieved context. Nothing for RAGAS to score.")
        _report_unscored(records, scorable)
        return

    judge, judge_name = _judge(judge_kind)

    # A fresh provider, built here rather than passed in: the one used during
    # generation belongs to an event loop that has already closed.
    from app.modules.intelligence.embeddings.embedder import OllamaEmbeddingProvider
    from app.modules.intelligence.llm.registry import _build  # noqa: F401

    embeddings = LangchainEmbeddingsWrapper(OllamaLangchainEmbeddings(OllamaEmbeddingProvider()))

    samples: list[Any] = [
        SingleTurnSample(
            user_input=r["question"],
            retrieved_contexts=r["contexts"],
            response=r["answer"],
        )
        for r in scorable
    ]

    print(f"\nSCORING  ({len(samples)} of {len(records)} had retrieved context)")
    print("  each metric is at least one LLM call per sample; this takes a minute\n")

    # `evaluate` is typed as returning a union with Executor; in this call
    # shape it is always an EvaluationResult.
    result: Any = evaluate(
        dataset=EvaluationDataset(samples=samples),
        metrics=[
            Faithfulness(llm=judge),
            # strictness=1, not the default 3. ResponseRelevancy works by
            # generating questions from the answer and comparing them to the
            # real one; by default it asks the judge for three candidates in
            # one call, and the flash-lite models reject that outright:
            #
            #     400 INVALID_ARGUMENT: Multiple candidates is not enabled
            #
            # The metric then returns NaN for that sample, which reads as a
            # scoring failure rather than a provider limitation. One
            # generation is noisier per sample and actually produces a number.
            ResponseRelevancy(llm=judge, embeddings=embeddings, strictness=1),
            LLMContextPrecisionWithoutReference(llm=judge),
        ],
    )

    print()
    print("=" * 78)
    print("SCORES")
    print("=" * 78)
    print(result)

    try:
        frame = result.to_pandas()
        print("\nPER QUESTION")
        for _, row in frame.iterrows():
            question = str(row.get("user_input", ""))[:56]
            faith = row.get("faithfulness")
            rel = row.get("answer_relevancy")
            prec = row.get("llm_context_precision_without_reference")

            def _fmt(value: Any) -> str:
                try:
                    return f"{float(value):.2f}"
                except (TypeError, ValueError):
                    return "  - "

            print(f"  faith {_fmt(faith)}  rel {_fmt(rel)}  prec {_fmt(prec)}   {question}")
    except Exception:
        # to_pandas is convenience, not the result. A failure here must not
        # discard scores that were already computed and printed above.
        pass

    run_id = _log_to_mlflow(result, records, judge_name)
    if run_id:
        print(f"\n  logged to MLflow as run {run_id[:8]}")
        print("  view it:  mlflow ui --backend-store-uri sqlite:///var/mlflow.db")

    print()
    print("  faithfulness       claims in the answer supported by retrieved context")
    print("  answer_relevancy   does the answer address the question asked")
    print("  llm_context_precision_without_reference")
    print("                     how much of what was retrieved was actually useful")
    print()
    print("  Low faithfulness with high relevancy is the dangerous combination:")
    print("  a fluent, on-topic answer that the evidence does not support.")

    _report_unscored(records, scorable)


def _report_unscored(records: list[dict[str, Any]], scorable: list[dict[str, Any]]) -> None:
    unscored = [r for r in records if r not in scorable]
    if not unscored:
        return
    print(f"\n  {len(unscored)} question(s) answered with NO tool call, so not scored:")
    for record in unscored:
        print(f"    - {record['question']}")
    print("  Read these by hand. An assistant answering from nothing is the failure")
    print("  faithfulness cannot see, because there is no context to be unfaithful to.")


def main() -> None:
    parser = argparse.ArgumentParser(description="RAGAS evaluation of the chat assistant.")
    parser.add_argument("--limit", type=int, default=len(PROBES), help="how many probes to run")
    parser.add_argument("--generate", action="store_true", help="answer only, do not score")
    parser.add_argument("--score", action="store_true", help="score the saved samples only")
    parser.add_argument(
        "--judge",
        choices=("local", "hosted"),
        default="local",
        help="which model grades the answers (default: local qwen3:8b)",
    )
    args = parser.parse_args()

    print("=" * 78)
    print("RAGAS EVALUATION")
    print("=" * 78)
    judge_label = (
        settings.OLLAMA_CHAT_MODEL if args.judge == "local" else settings.GEMINI_MODEL_STRONG
    )
    print(f"  judging model : {judge_label} ({args.judge}, no fallback)")
    if args.judge == "local" and settings.AI_PREFER_LOCAL:
        print("  NOTE          : judge and answerer are the same model, so these")
        print("                  scores measure self-consistency, not quality.")
    print()

    # Trace the judge's own calls. Enabled here rather than in `score()` so it
    # is on before any LangChain object exists, and only for the scoring phase
    # -- the answering phase runs LangGraph, which ragas' event-loop patching
    # would otherwise collide with.
    if configure_tracing():
        print(f"  tracing       : LangSmith project {settings.LANGSMITH_PROJECT}")

    if args.score:
        if not SAMPLE_FILE.exists():
            raise SystemExit(f"{SAMPLE_FILE} not found. Run with --generate first.")
        records = json.loads(SAMPLE_FILE.read_text(encoding="utf-8"))
    else:
        # asyncio.run must fully return before ragas is imported.
        records = asyncio.run(generate(args.limit))

    if args.generate:
        print("\n  --generate only; run with --score to evaluate.")
        return

    score(records, args.judge)


if __name__ == "__main__":
    main()
