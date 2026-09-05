# OS Tracker — Architecture Documentation

**OS Tracker** (Operational Support Tracker) is an internal, multi-tenant web application
that connects a customer-support team to engineering teams. Support agents raise issues
reported by customers; engineering teams pick them up, work them, and resolve them; support
verifies and closes.

This folder is the agreed architecture. It is written to be read before any code exists, and
to stay useful as a reference while the application is built.

---

## Document index

| # | Document | Covers |
|---|---|---|
| 01 | [Product Requirements](01-product-requirements.md) | Problem, scope, functional and non-functional requirements, what is deliberately excluded |
| 02 | [Roles & Permissions](02-roles-and-permissions.md) | Roles, permission matrix, the three authorization layers, navigation visibility |
| 03 | [Architecture Overview](03-architecture-overview.md) | Modular monolith, module map, technology stack with rationale, repository layout |
| 04 | [Database](04-database.md) | Full schema, enums, indexes, row-level security, ticket numbering, migrations |
| 05 | [Backend](05-backend.md) | Module structure, layer responsibilities, request lifecycle, configuration, patterns |
| 06 | [API](06-api.md) | REST conventions, error contract, pagination, endpoint catalogue, state machine |
| 07 | [Frontend](07-frontend.md) | Layout, navigation, routing, state strategy, screens, theming |
| 08 | [Security](08-security.md) | Authentication, tokens, CSRF, tenant isolation, data protection, uploads |
| 09 | [Logging & Caching](09-logging-and-caching.md) | Structured logging, redaction, Redis usage and boundaries |
| 10 | [Roadmap](10-roadmap.md) | Delivery phases, scope per phase, definition of done |
| 11 | [Decisions & Risks](11-decisions-and-risks.md) | Decision log with alternatives, accepted risks, parked work |
| — | [**Agentic AI**](agentic-ai/README.md) | The AI layer: RAG, LangGraph, agent, chatbot, guardrails, evaluation |

---

## Status legend

Used throughout these documents.

| Marker | Meaning |
|---|---|
| **Decided** | Agreed. Build to this. Changing it needs a new decision recorded in doc 11 |
| **Deferred** | Consciously excluded from v1. The design leaves room for it |
| **Parked** | Not yet discussed or decided. Needs a decision before the relevant phase |

---

## The shape of the system in one paragraph

A single FastAPI application, internally split into modules that talk to each other through
service interfaces, backed by one PostgreSQL database with row-level security enforcing
tenant isolation, and Redis for rate limiting, idempotency, and reference-data caching. A
React + TypeScript single-page application consumes the API, with its TypeScript types
generated directly from the backend's OpenAPI schema. Authentication is JWT delivered in
httpOnly cookies. Authorization is permission-based, checked in three independent layers.

---

## Currently parked

Deployment, CI/CD, hosting, monitoring, and automated testing are **not yet decided**.
The application is built to be portable — configuration through environment variables,
logs to stdout, no host-specific assumptions — so these decisions can be made later without
rework. See [doc 11](11-decisions-and-risks.md) for the full parked list and the risks
that come with it.
