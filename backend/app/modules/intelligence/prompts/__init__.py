"""Versioned prompt templates.

## Why prompts live in their own package

They were inline string constants in the graph and node files that used them,
which works and costs nothing — until you want to answer a question the
evaluation harness keeps raising: *which prompt produced this number?*

Three things become possible once a prompt is a named, versioned artefact:

* **Diffing.** A prompt change shows up as a change to a prompt file rather
  than buried in a diff that also moves control flow around. Reviewing "we
  reworded the rerank instruction" separately from "we changed the rerank
  routing" is the whole point.
* **Attribution.** Every eval result can record the prompt version it ran
  against. Without that, a metric that moves between two runs has two candidate
  explanations — the retrieval change and the prompt change — and no way to
  tell them apart.
* **Reuse.** The injection-resistance paragraph is the same argument in all
  three prompts. It is built by one function in `shared.py`, so hardening it
  hardens every prompt rather than two out of three. Unifying it already did:
  the chat prompt carried the weakest of the three wordings.

## What does *not* live here

No f-strings over user data, and no prompt assembly. These are the static
system instructions only; the per-request parts — the ticket text, the
candidate list, the transcript — are built by the node or graph that owns the
request, because that is where the guardrails run. A prompt module that
formatted user input would be a second place for injection defence to be
forgotten.
"""

from app.modules.intelligence.prompts.analysis import ANALYSIS_SYSTEM
from app.modules.intelligence.prompts.chat import CHAT_SYSTEM
from app.modules.intelligence.prompts.rerank import RERANK_SYSTEM
from app.modules.intelligence.prompts.versions import PROMPT_VERSIONS, prompt_version

__all__ = [
    "ANALYSIS_SYSTEM",
    "CHAT_SYSTEM",
    "PROMPT_VERSIONS",
    "RERANK_SYSTEM",
    "prompt_version",
]
