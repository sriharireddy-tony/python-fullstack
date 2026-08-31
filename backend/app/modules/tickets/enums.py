"""Ticket domain enums and the priority derivation rule.

Priority is **never entered directly**. CS answers three factual questions --
how broken, who is affected, is there a workaround -- and the system derives the
priority from them.

    When the person raising a ticket also sets its priority, everything becomes
    P1 within a few months. Not through bad faith: every agent genuinely
    believes their customer's problem is urgent, and nothing costs them for
    saying so. Deriving priority from observable facts removes the lever.

See docs/01-product-requirements.md.
"""

from __future__ import annotations

from enum import StrEnum


class Severity(StrEnum):
    S1_CRITICAL = "s1_critical"  # unusable / data loss / payroll blocked
    S2_MAJOR = "s2_major"  # key function broken
    S3_MINOR = "s3_minor"  # degraded
    S4_COSMETIC = "s4_cosmetic"  # no functional impact


class Impact(StrEnum):
    WHOLE_ORG = "whole_org"
    DEPARTMENT = "department"
    FEW_USERS = "few_users"
    SINGLE_USER = "single_user"


class Workaround(StrEnum):
    NONE = "none"
    PAINFUL = "painful"
    EASY = "easy"


class Priority(StrEnum):
    P1 = "p1"
    P2 = "p2"
    P3 = "p3"
    P4 = "p4"


class TicketStatus(StrEnum):
    OPEN = "open"  # unassigned, in the team inbox
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"
    ON_HOLD = "on_hold"
    RESOLVED = "resolved"  # engineering done, awaiting CS
    REJECTED = "rejected"  # no change needed, awaiting CS
    CLOSED = "closed"  # terminal; only CS gets here


#: ``resolved`` and ``rejected`` are the same *kind* of state -- engineering is
#: finished and CS must act. Both appear in the CS "Awaiting Closure" queue.
AWAITING_CS: frozenset[TicketStatus] = frozenset({TicketStatus.RESOLVED, TicketStatus.REJECTED})

#: Statuses where work is still live.
OPEN_STATUSES: frozenset[TicketStatus] = frozenset(
    {
        TicketStatus.OPEN,
        TicketStatus.ASSIGNED,
        TicketStatus.IN_PROGRESS,
        TicketStatus.ON_HOLD,
    }
)


class Environment(StrEnum):
    PRODUCTION = "production"
    STAGING = "staging"
    UAT = "uat"


class RejectionReason(StrEnum):
    NOT_A_BUG = "not_a_bug"
    DUPLICATE = "duplicate"
    WONT_FIX = "wont_fix"
    CANNOT_REPRODUCE = "cannot_reproduce"


class WaitingOn(StrEnum):
    CS = "cs"
    CLIENT = "client"
    OTHER_TEAM = "other_team"


# --------------------------------------------------------- priority matrix

#: Severity x Impact, from docs/01-product-requirements.md.
_PRIORITY_MATRIX: dict[tuple[Severity, Impact], Priority] = {
    (Severity.S1_CRITICAL, Impact.WHOLE_ORG): Priority.P1,
    (Severity.S1_CRITICAL, Impact.DEPARTMENT): Priority.P1,
    (Severity.S1_CRITICAL, Impact.FEW_USERS): Priority.P2,
    (Severity.S1_CRITICAL, Impact.SINGLE_USER): Priority.P2,
    (Severity.S2_MAJOR, Impact.WHOLE_ORG): Priority.P1,
    (Severity.S2_MAJOR, Impact.DEPARTMENT): Priority.P2,
    (Severity.S2_MAJOR, Impact.FEW_USERS): Priority.P2,
    (Severity.S2_MAJOR, Impact.SINGLE_USER): Priority.P3,
    (Severity.S3_MINOR, Impact.WHOLE_ORG): Priority.P2,
    (Severity.S3_MINOR, Impact.DEPARTMENT): Priority.P3,
    (Severity.S3_MINOR, Impact.FEW_USERS): Priority.P3,
    (Severity.S3_MINOR, Impact.SINGLE_USER): Priority.P4,
    (Severity.S4_COSMETIC, Impact.WHOLE_ORG): Priority.P3,
    (Severity.S4_COSMETIC, Impact.DEPARTMENT): Priority.P4,
    (Severity.S4_COSMETIC, Impact.FEW_USERS): Priority.P4,
    (Severity.S4_COSMETIC, Impact.SINGLE_USER): Priority.P4,
}

_ORDER: list[Priority] = [Priority.P1, Priority.P2, Priority.P3, Priority.P4]


def derive_priority(severity: Severity, impact: Impact, workaround: Workaround) -> Priority:
    """Compute a ticket's priority.

    A pure function: no I/O, no database, fully deterministic. That makes it
    trivially testable and safe to call from a form preview as the user types.

    The workaround modifier shifts one step and is clamped at both ends -- no
    workaround raises urgency, an easy one lowers it.
    """
    base = _PRIORITY_MATRIX[(severity, impact)]
    index = _ORDER.index(base)

    if workaround is Workaround.NONE:
        index = max(0, index - 1)
    elif workaround is Workaround.EASY:
        index = min(len(_ORDER) - 1, index + 1)

    return _ORDER[index]


# Every severity/impact combination must be covered; a missing pair would raise
# KeyError at ticket creation, which is the worst possible time to find out.
assert len(_PRIORITY_MATRIX) == len(Severity) * len(Impact)
