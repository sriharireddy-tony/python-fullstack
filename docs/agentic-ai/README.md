# Agentic AI Layer

**Status: design. None of this is built yet.**

The AI layer for OS Tracker. Three capabilities on one shared retrieval
foundation:

| Capability | Trigger | Shape |
|---|---|---|
| **Similar Issues** | Automatic, when a ticket is opened | Deterministic RAG pipeline |
| **Bug Analysis Agent** | On demand, a button | Multi-turn agent loop |
| **Chatbot** | Conversational | Stateful graph with memory |

---

## Reading order

Each document follows the same template: **what it is → why we chose it → how it
works technically → a worked scenario → interview questions → gotchas.**

| # | Document | Covers |
|---|---|---|
| 01 | [Overview](01-overview.md) | The problem, the three capabilities, the full stack, and the glossary |
| 02 | [Embeddings](02-embeddings.md) | Ollama + Qwen3-Embedding, dimensions, the instruction asymmetry |
| 03 | [Vector Store](03-vector-store.md) | Chroma, collection-per-tenant, the sync problem, reconciliation |
| 04 | [RAG & Retrieval](04-rag-retrieval.md) | Hybrid search, exact-token probing, why RRF was measured and dropped, corrective RAG, reranking |
| 05 | [Models & Rate Limits](05-models-and-limits.md) | Gemini, the model registry, quota strategy, local fallback |
| 06 | [LangGraph](06-langgraph.md) | State, nodes, edges, cycles, checkpointing, the three graphs |
| 07 | [The Agent](07-agent.md) | Bug Analysis Agent: tools, ReAct loop, control limits |
| 08 | [The Chatbot](08-chatbot.md) | Stateful chat, intent routing, structured query, analytics |
| 09 | [Guardrails](09-guardrails.md) | Every rail, prompt injection, tool-result injection |
| 10 | [Evaluation](10-evaluation.md) | Three tiers, RAGAS, retrieval metrics, the golden set |
| 11 | [Code Structure](11-code-structure.md) | Folders, ports and adapters, data model, testing |
| 12 | [Roadmap](12-roadmap.md) | Phases, dependencies, definition of done — **A and B complete, with outcomes** |
| 13 | [Interview Prep](13-interview-prep.md) | Consolidated scenario questions across every topic |

---

## The stack, and why each piece

| Layer | Choice | One-line reason |
|---|---|---|
| Embeddings | **Ollama + `qwen3-embedding`** | Local, unlimited, and no ticket text leaves the network |
| Vector store | **Chroma**, one collection per tenant | Isolation is structural — the wrong collection returns nothing |
| LLM | **Gemini Flash Lite / 2.5 Flash** | Free tier for development |
| LLM fallback | **Ollama `qwen3:8b`** | Development is never blocked by quota |
| Orchestration | **LangGraph** | Cycles, conditional edges, and durable state |
| Framework | **LangChain** | Uniform provider interfaces |
| State | **PostgresSaver** | Checkpoints live in the database we already run |
| Tracing | **LangSmith** | Traces graph nodes natively, zero setup |
| Evaluation | **RAGAS** + retrieval metrics | Faithfulness and relevancy, plus free offline metrics |

---

## The rules that shape everything

1. **The model is never an authorization boundary.** It produces suggestions.
   Applying one is a separate authorized action through the same `policies.py` a
   human click goes through.

2. **The agent has no write tools.** Ticket text is authored by people outside
   the organisation, and injection can arrive mid-loop through a tool result.
   Having no lever is the mitigation that actually holds.

3. **Severity and impact stay human-entered.** [D-04](../11-decisions-and-risks.md)
   derives priority from facts so it cannot be inflated. A model that infers
   severity from the reporter's prose can be talked into P1 by the reporter.

4. **AI is never on the critical path.** Ticket creation, workflow, and search
   all work unchanged if Ollama, Chroma, or Gemini are down.

5. **Retrieval works with zero LLM calls.** The rerank is an enhancement, not a
   dependency. This is both correct degradation and how development stays free.

---

## Scope

- Similar issues appear **only on the ticket detail page**. The create form is
  untouched.
- Analysis is **on demand**, never automatic.
- Development uses **synthetic data** and the free API tier; production will use
  paid APIs.
