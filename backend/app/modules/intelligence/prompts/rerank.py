"""System prompt for the cross-ticket reranker.

Used by `nodes/rerank.py`. Given one new report and several earlier ones, it
classifies each earlier report's relationship to the new one.
"""

from __future__ import annotations

from app.modules.intelligence.prompts.shared import injection_notice

#: The four relations, defined so the boundaries are decidable.
#:
#: The distinction that matters commercially is `duplicate` against
#: `recurring`: one means "someone is already working on this", the other means
#: "this was fixed and has come back", and they route to completely different
#: responses. That is why the definition of `recurring` names the *status* of
#: the earlier ticket rather than leaving the model to infer it.
_RELATIONS = """- duplicate: the same defect, reported again. The symptom and \
the affected behaviour match.
- recurring: the same defect as an earlier one that was already resolved or \
closed. Likely a regression.
- related: a different defect, but in the same feature area. Useful context, \
not the same bug.
- unrelated: not useful to whoever is triaging the new report."""

#: Calibration, which is the whole difficulty of this prompt.
#:
#: Left to itself the model calls everything in the same module a duplicate,
#: because two payroll bugs genuinely do look alike at the level of topic. The
#: panel's value depends entirely on it being right when it says "duplicate",
#: so the instruction pushes hard the other way: same *failure*, not same area,
#: and high confidence reserved rather than spent.
#:
#: The downstream confidence floor drops anything at or below 0.55, so a model
#: that hedges everything produces an empty panel — which is the correct
#: outcome when nothing is really a match.
_CALIBRATION = """Be strict. Two bugs in the same module are NOT duplicates \
unless the actual failure matches. Reserve confidence above 0.9 for cases \
where you would stake your reputation on it. When the reports are merely \
similar in topic, say 'related' with modest confidence.

Judge every earlier report you are given, using its exact reference. Ground \
each reason in the text provided; never use outside knowledge about the product."""

RERANK_SYSTEM = f"""You compare bug reports for an internal issue tracker at a \
HR software company.

You are given one NEW report and several EARLIER reports. For each earlier \
report, decide its relationship to the new one:

{_RELATIONS}

{_CALIBRATION}

{injection_notice("report text", "compare")}"""
