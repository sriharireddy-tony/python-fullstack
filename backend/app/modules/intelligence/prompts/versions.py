"""Prompt versions, derived from content rather than declared.

## Why the version is a hash and not a number

A hand-maintained `VERSION = 3` is wrong the moment somebody edits a prompt and
forgets to bump it — and that is exactly when the version matters, because an
eval result has just been attributed to a prompt that is no longer the one that
produced it. A version that can silently disagree with the thing it versions is
worse than no version, since it invites trust it has not earned.

Hashing the prompt text makes the two impossible to separate. Edit a word and
the version changes; change nothing and it does not. There is no bump to
forget.

The trade is that the version is not human-orderable — `a3f19c` does not
announce itself as newer than `7b204e`. That is acceptable here because the
question being asked is never "which is newer", it is "was this the same
prompt". Chronology already lives in git.

## What this is for

Recording *which prompt produced a result*. An eval number that moved between
two runs has as many explanations as things that changed; stamping the prompt
version onto a run removes one of them from the list.
"""

from __future__ import annotations

import hashlib

from app.modules.intelligence.prompts.analysis import ANALYSIS_SYSTEM
from app.modules.intelligence.prompts.chat import CHAT_SYSTEM
from app.modules.intelligence.prompts.rerank import RERANK_SYSTEM

#: How many hex characters of the digest to keep.
#:
#: Six gives ~16 million values, which is far beyond the number of prompt
#: revisions this project will ever have, and stays short enough to sit in a
#: log line or a table cell without wrapping.
_LENGTH = 6


def prompt_version(text: str) -> str:
    """A stable short identity for one prompt's exact text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:_LENGTH]


#: Role -> version of the prompt currently in use.
#:
#: Keyed by role rather than by module path so a caller asks the question it
#: actually has ("which rerank prompt is live?") without knowing where the text
#: is defined.
PROMPT_VERSIONS: dict[str, str] = {
    "analysis": prompt_version(ANALYSIS_SYSTEM),
    "chat": prompt_version(CHAT_SYSTEM),
    "rerank": prompt_version(RERANK_SYSTEM),
}
