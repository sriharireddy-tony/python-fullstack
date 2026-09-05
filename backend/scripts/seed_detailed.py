"""Ten richly-written Keka support tickets, for working on the index by hand.

## Why this file exists alongside `seed_scenarios.py`

The eighty scenarios in `seed_scenarios.py` are tuned for *measurement*: short
enough that a hundred of them seed quickly, uniform enough that retrieval
metrics compare like with like. They are a benchmark corpus.

These ten are tuned for *inspection*. Each carries the amount of text a real
support write-up has -- symptom, scope, what the customer already tried, what
the logs said -- because that is what makes an embedding interesting to look
at. At roughly 1,200 to 1,800 characters of body text apiece they exercise the
truncation caps in `build_embedding_text` properly, which the short scenarios
never do.

## No planted duplicates

All ten are genuinely different bugs across six modules. Nothing here is a
paraphrase of anything else, which is deliberate: this corpus is for watching
the *write* path -- select tickets, embed them, see vectors appear -- not for
scoring retrieval. Similarity search over ten unrelated tickets will return
weak matches, and that is the honest behaviour rather than a fault.

`scripts/seed_demo.py --profile full` restores the hundred-ticket corpus with
its known duplicate clusters when the evaluations need to mean something again.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.modules.tickets.enums import Impact, Severity, Workaround


@dataclass(frozen=True, slots=True)
class DetailedScenario:
    """One hand-written ticket, complete enough to read like a real one.

    Unlike `Scenario`, this carries its own steps and expected/actual results
    rather than taking generic boilerplate. A ticket whose reproduction steps
    are "perform the action described above" is fine for a benchmark row and
    useless when a human is reading the page.
    """

    team: str
    title: str
    description: str
    steps: str
    expected: str
    actual: str
    severity: Severity
    impact: Impact
    workaround: Workaround


S1, S2, S3, S4 = (
    Severity.S1_CRITICAL,
    Severity.S2_MAJOR,
    Severity.S3_MINOR,
    Severity.S4_COSMETIC,
)
ORG, DEPT, FEW, ONE = (
    Impact.WHOLE_ORG,
    Impact.DEPARTMENT,
    Impact.FEW_USERS,
    Impact.SINGLE_USER,
)
NONE, PAINFUL, EASY = Workaround.NONE, Workaround.PAINFUL, Workaround.EASY


DETAILED_SCENARIOS: list[DetailedScenario] = [
    DetailedScenario(
        team="Payroll",
        title="Full and final settlement omits the notice-period recovery for early exits",
        description=(
            "When an employee resigns and serves less than their contractual notice "
            "period, the full and final settlement is supposed to recover the shortfall "
            "at the rate defined in the notice-recovery policy. For the eleven exits "
            "processed in the March cycle the recovery line is missing entirely from "
            "the settlement statement, and the net payable is therefore higher than it "
            "should be. Finance caught it on the second one and has been holding the "
            "remaining nine manually.\n\n"
            "The policy screen shows the recovery rule as active with a per-day basis "
            "of basic plus dearness allowance. Employees who served their full notice "
            "settle correctly, so the calculation only diverges when there is a "
            "shortfall to recover. One case that does show a recovery line has it "
            "computed on basic alone, which suggests the component set being read is "
            "not the one configured.\n\n"
            "The finance controller has asked whether previously closed settlements "
            "are affected, because if the same rule has been misapplied since the "
            "policy was last edited in November, there are roughly forty settlements "
            "to revisit and some of those employees have already been paid."
        ),
        steps=(
            "1. Open an employee with a 60-day notice period and a resignation dated "
            "20 days before their last working day\n"
            "2. Initiate the full and final settlement from the exit workflow\n"
            "3. Review the settlement statement's deduction section\n"
            "4. Compare against the notice-recovery policy configured under Payroll "
            "Settings > Exit Policies"
        ),
        expected=(
            "The settlement includes a notice-period recovery line for the 40-day "
            "shortfall, computed on basic plus dearness allowance as configured."
        ),
        actual=(
            "No recovery line appears at all, and the net payable is overstated by the "
            "full shortfall amount."
        ),
        severity=S1,
        impact=DEPT,
        workaround=PAINFUL,
    ),
    DetailedScenario(
        team="Workforce",
        title="Shift roster publish silently drops employees added after the draft was created",
        description=(
            "A roster is built as a draft over several days, then published to the "
            "team. If an employee joins the department in between the draft being "
            "created and it being published, they are absent from the published roster "
            "even though they appear in the draft view when it is reopened. The "
            "publish confirmation reports the original headcount, so nothing signals "
            "that anyone was left out.\n\n"
            "This has now happened twice at the Pune plant. In the first instance a "
            "new joiner had no shift for eight days and was marked absent for all of "
            "them, which then fed into their attendance regularisation and their first "
            "salary. Their manager assumed the roster was correct because the roster "
            "screen showed them once it was reopened for editing.\n\n"
            "The operations lead notes that the draft screen and the published view "
            "disagree, and asks that publishing either include everyone currently in "
            "the department or refuse to publish and say who is missing. Silently "
            "publishing a roster that does not match the team is the part causing the "
            "damage, more than the omission itself."
        ),
        steps=(
            "1. Create a shift roster draft for a department with 20 employees\n"
            "2. Leave the draft unpublished\n"
            "3. Add a new employee to the same department\n"
            "4. Reopen the draft (the new employee is listed) and publish it\n"
            "5. Open the published roster for the affected week"
        ),
        expected=(
            "Everyone currently in the department appears on the published roster, or "
            "publishing is blocked with a message naming who has no shift assigned."
        ),
        actual=(
            "The published roster contains only the original 20 employees. The new "
            "joiner has no shift and is marked absent each day."
        ),
        severity=S1,
        impact=DEPT,
        workaround=PAINFUL,
    ),
    DetailedScenario(
        team="CoreHR",
        title=(
            "Employee custom fields revert to their previous values after a bulk "
            "department transfer"
        ),
        description=(
            "The organisation uses eight custom fields on the employee profile to hold "
            "things the standard schema does not cover: cost centre, billing code, "
            "workstation ID, badge number, and four project-tracking attributes. After "
            "running a bulk department transfer for 34 employees, all eight custom "
            "fields on those employees reverted to the values they held before an "
            "update made two weeks earlier.\n\n"
            "The audit log records the earlier update correctly, and it records the "
            "transfer, but there is no entry for the custom fields changing during the "
            "transfer. So the values changed without the audit trail showing it, which "
            "is the part the compliance team is most concerned about. Single-employee "
            "transfers through the same screen do not exhibit this.\n\n"
            "HR operations has a CSV export taken the morning of the transfer, so the "
            "correct values are recoverable, but they are reluctant to re-import until "
            "they know whether a second bulk transfer would undo it again. They have "
            "paused three further departmental restructures pending an answer."
        ),
        steps=(
            "1. Note the current values of the custom fields on a set of employees\n"
            "2. Update at least one custom field on each and save\n"
            "3. Select all of them and run a bulk department transfer\n"
            "4. Reopen any of the transferred employees and inspect the custom fields\n"
            "5. Check the audit log for that employee"
        ),
        expected=(
            "Custom field values are untouched by a department transfer, and any change "
            "to them appears in the audit log."
        ),
        actual=(
            "All custom fields hold their pre-update values, with no audit entry "
            "recording the change."
        ),
        severity=S1,
        impact=DEPT,
        workaround=PAINFUL,
    ),
    DetailedScenario(
        team="Platform",
        title="SCIM provisioning creates duplicate employees when the identity provider retries",
        description=(
            "The customer provisions employees from Azure AD over SCIM. When the "
            "provisioning endpoint takes longer than the identity provider's 30-second "
            "timeout, Azure retries the create. The retry is treated as a new employee "
            "rather than matched to the one already created, so a duplicate record "
            "appears with the same work email and a different employee ID.\n\n"
            "There are currently 23 duplicates in their tenant. The duplicates are not "
            "harmless: each one consumes a licence, appears in the org chart, receives "
            "notifications, and shows up in the reporting-manager dropdown, so managers "
            "have been assigning work to the inactive copy. Two of the duplicates have "
            "had leave requests raised against them.\n\n"
            "The SCIM specification expects the externalId attribute to be the "
            "idempotency key, and Azure is sending it consistently on both the original "
            "and the retry. The integration log shows both requests carrying the same "
            "externalId value, so the information needed to deduplicate is arriving and "
            "not being used. Error code SCIM_DUP_409 appears in the log for a handful of "
            "the attempts but not for the ones that produced duplicates."
        ),
        steps=(
            "1. Configure SCIM provisioning from Azure AD\n"
            "2. Provision a batch large enough that some requests exceed 30 seconds\n"
            "3. Allow the identity provider to retry the timed-out creates\n"
            "4. Search the employee directory for the affected work email addresses"
        ),
        expected=(
            "A retried create with a previously seen externalId updates the existing "
            "employee rather than creating a second one."
        ),
        actual=(
            "A second employee record is created with the same work email and a new "
            "employee ID, consuming a licence."
        ),
        severity=S1,
        impact=ORG,
        workaround=NONE,
    ),
    DetailedScenario(
        team="Payroll",
        title="Arrears from a backdated increment are taxed entirely in the month of payment",
        description=(
            "An increment backdated to the start of the financial year pays out as "
            "arrears in the current month. The arrears amount is being added in full to "
            "the current month's taxable income, which pushes several employees into a "
            "higher slab for that month and takes a disproportionate amount of tax in "
            "one go.\n\n"
            "Under section 89(1) relief the arrears should be spread across the months "
            "they relate to for the purpose of computing the tax liability, and Form "
            "10E should be available for the employee to file. Neither is happening: "
            "the projection screen shows the entire arrears amount against the current "
            "month, and there is no 10E workflow visible.\n\n"
            "Forty-one employees received backdated increments in this cycle. The "
            "largest single case had 214,000 rupees of arrears and paid roughly 31,000 "
            "more in tax than the spread calculation would produce. The employees will "
            "recover it when they file their returns, but they are asking payroll why "
            "their take-home dropped, and payroll does not have a good answer.\n\n"
            "The HR head has asked whether the annual tax projection for the remaining "
            "months is also wrong, since it appears to be computed from the inflated "
            "current-month figure."
        ),
        steps=(
            "1. Apply an increment to an employee with an effective date six months in "
            "the past\n"
            "2. Run the payroll cycle that pays the resulting arrears\n"
            "3. Open the employee's tax projection for the current month\n"
            "4. Compare the tax deducted against a section 89(1) spread calculation"
        ),
        expected=(
            "Arrears are attributed to the months they relate to for tax computation, "
            "and Form 10E is generated for the employee."
        ),
        actual=(
            "The full arrears amount is taxed in the month of payment and no Form 10E is produced."
        ),
        severity=S2,
        impact=DEPT,
        workaround=PAINFUL,
    ),
    DetailedScenario(
        team="Performance",
        title=(
            "Goal cascade from a department objective assigns weightage that does not total 100"
        ),
        description=(
            "When a department objective is cascaded to individual goals, each "
            "recipient is supposed to receive a share of the weightage such that their "
            "own goal sheet totals 100 percent. For teams of more than seven people the "
            "cascaded weightages are being rounded down individually, so the sheet "
            "totals 98 or 99 percent and the cycle will not allow submission.\n\n"
            "The manager can edit the weightages by hand to make them add up, but the "
            "cascade link is broken when they do, and the goal then stops rolling up "
            "into the department objective's progress. So the choice is a sheet that "
            "cannot be submitted or a goal that does not contribute to the objective it "
            "was cascaded from.\n\n"
            "This affects the two largest departments, Engineering with 34 people and "
            "Operations with 19. The performance cycle closes at the end of the month "
            "and roughly 50 goal sheets are currently stuck. The HR business partner "
            "would accept any workaround that preserves the roll-up, including rounding "
            "the remainder onto one arbitrary recipient."
        ),
        steps=(
            "1. Create a department objective with a weightage of 100\n"
            "2. Cascade it to a team of eight or more employees\n"
            "3. Open any recipient's goal sheet and sum the weightage column\n"
            "4. Attempt to submit the sheet"
        ),
        expected=(
            "Cascaded weightages total exactly 100 on each recipient's sheet, with any "
            "rounding remainder absorbed somewhere."
        ),
        actual=(
            "The sheet totals 98 or 99 and submission is blocked. Editing by hand "
            "breaks the cascade roll-up."
        ),
        severity=S2,
        impact=DEPT,
        workaround=PAINFUL,
    ),
    DetailedScenario(
        team="Workforce",
        title=(
            "Overtime hours computed against the wrong week when a shift crosses the "
            "weekly boundary"
        ),
        description=(
            "The organisation runs a Sunday-to-Saturday working week and pays overtime "
            "above 48 hours in a week. Night shifts start at 22:00 and end at 06:00 the "
            "following day. When a shift starts on Saturday night and ends Sunday "
            "morning, all eight hours are being attributed to the week the shift ends "
            "in rather than the week it starts in.\n\n"
            "The effect is that the closing week is short by eight hours and the "
            "opening week is long by eight, so employees who should have crossed the "
            "overtime threshold do not, and occasionally an employee is paid overtime "
            "they did not earn. Over a four-week cycle it roughly balances out for most "
            "people, but not for anyone who joined, left, or changed shift pattern "
            "mid-cycle, and those are the cases being escalated.\n\n"
            "The shift definition screen has a setting labelled 'attribute crossing "
            "shifts to' with options for start date and end date, and it is set to "
            "start date. Changing it and saving does not alter the computed hours, "
            "which suggests the setting is not being read by the overtime calculation "
            "even though it is being persisted."
        ),
        steps=(
            "1. Define a night shift running 22:00 to 06:00 with crossing attribution "
            "set to start date\n"
            "2. Roster an employee for Saturday night in a week where they have already "
            "worked 44 hours\n"
            "3. Run the weekly overtime computation\n"
            "4. Inspect which week the eight hours were attributed to"
        ),
        expected=(
            "The eight hours count toward the week the shift started in, taking the "
            "employee to 52 hours and four hours of overtime."
        ),
        actual=(
            "The hours are attributed to the following week. The closing week shows 44 "
            "hours and no overtime is paid."
        ),
        severity=S2,
        impact=DEPT,
        workaround=NONE,
    ),
    DetailedScenario(
        team="CoreHR",
        title="Offer letter merge fields render blank when the candidate has no middle name",
        description=(
            "Offer letters are generated from a template with merge fields for the "
            "candidate's name components. When a candidate has no middle name recorded, "
            "the whole name block renders empty rather than falling back to first and "
            "last name, so the letter goes out addressed to nobody and with the "
            "signatory block blank as well.\n\n"
            "Nine offers went out in this state before recruitment noticed. Two "
            "candidates replied asking whether the letter was intended for them. The "
            "template preview inside the editor renders correctly because the preview "
            "uses sample data that always includes a middle name, so there is no "
            "warning before sending.\n\n"
            "Recruitment has worked around it by entering a single full stop as the "
            "middle name, which makes the letter render but puts a stray full stop in "
            "the candidate's name everywhere else in the system, including their "
            "eventual employee record. They would like a fix before the next hiring "
            "wave, which starts in about three weeks and is expected to produce around "
            "sixty offers."
        ),
        steps=(
            "1. Create a candidate with a first and last name and no middle name\n"
            "2. Generate an offer letter from a template using the name merge fields\n"
            "3. Download or preview the generated letter\n"
            "4. Repeat with a candidate who has a middle name for comparison"
        ),
        expected=("The name renders as first and last name, skipping the absent middle name."),
        actual=("The entire name block and the signatory block render empty."),
        severity=S2,
        impact=FEW,
        workaround=PAINFUL,
    ),
    DetailedScenario(
        team="UI",
        title=(
            "Expense claim attachments lose their orientation when previewed on the approval screen"
        ),
        description=(
            "Receipts photographed on a phone in portrait orientation are displayed "
            "rotated 90 degrees on the expense approval screen. The stored file is "
            "correct -- downloading it and opening it locally shows the right "
            "orientation -- so the rotation is being introduced by the preview "
            "renderer, which appears to ignore the EXIF orientation tag.\n\n"
            "Approvers are rotating their heads or downloading each receipt to read the "
            "amount, which for a finance approver processing sixty claims a week is a "
            "meaningful amount of friction. Two approvals were rejected for "
            "'illegible receipt' and later found to be perfectly legible once "
            "downloaded.\n\n"
            "It affects images captured on iOS more than Android, which is consistent "
            "with EXIF orientation handling being the cause, since iOS writes the "
            "orientation tag rather than rotating the pixel data. PDFs and screenshots "
            "are unaffected. The thumbnail in the claim list is also rotated, so the "
            "problem is in whatever generates both."
        ),
        steps=(
            "1. Photograph a receipt in portrait orientation on an iPhone\n"
            "2. Attach it to an expense claim from the mobile app\n"
            "3. Submit the claim and open it on the web approval screen\n"
            "4. Compare the preview with the downloaded original"
        ),
        expected=(
            "The preview honours the EXIF orientation tag and displays the receipt "
            "upright, as the downloaded file does."
        ),
        actual=("The preview and the list thumbnail are both rotated 90 degrees clockwise."),
        severity=S3,
        impact=DEPT,
        workaround=PAINFUL,
    ),
    DetailedScenario(
        team="Platform",
        title="Scheduled report emails stop after a recipient's address bounces once",
        description=(
            "A weekly headcount report is scheduled to eleven recipients. When one "
            "recipient's mailbox bounced -- they had left the company and their address "
            "was deactivated -- the schedule stopped delivering to everyone, not just "
            "to the bounced address. Nobody was notified; the report simply stopped "
            "arriving and was noticed three weeks later when the HR director asked why "
            "they had not seen it.\n\n"
            "The schedule still shows as active on the reports screen, with a next-run "
            "timestamp that keeps advancing each week. There is no delivery history "
            "visible in the UI, so there was nothing to look at that would have "
            "revealed the failure. The only evidence is in the notification log, which "
            "requires a support ticket to access.\n\n"
            "The customer has fourteen other scheduled reports and now has no "
            "confidence that any of them are being delivered. They are asking for two "
            "things: that a single bad recipient not suppress delivery to the others, "
            "and that a failed delivery be visible somewhere they can check without "
            "raising a ticket."
        ),
        steps=(
            "1. Create a scheduled report with several recipients\n"
            "2. Deactivate one recipient's mailbox so mail to it hard-bounces\n"
            "3. Wait for two scheduled runs\n"
            "4. Ask the remaining recipients whether they received the report"
        ),
        expected=(
            "Delivery continues to every valid recipient, and the bounce is surfaced on "
            "the schedule so an administrator can remove the bad address."
        ),
        actual=(
            "Delivery stops for all recipients. The schedule reports itself as active "
            "and the failure is visible only in the internal notification log."
        ),
        severity=S3,
        impact=DEPT,
        workaround=PAINFUL,
    ),
]


_titles = [scenario.title for scenario in DETAILED_SCENARIOS]
assert len(_titles) == len(set(_titles)), "duplicate scenario titles"
assert len(DETAILED_SCENARIOS) == 10, f"expected 10 scenarios, found {len(DETAILED_SCENARIOS)}"
