"""The 80 distinct bugs the demo corpus is built from.

Split out of `seed_demo.py` because it is data, not logic, and because eighty
scenarios with real descriptions is a file someone reviews on its own.

## Why eighty *distinct* ones matters

The first version of this corpus had 220 tickets drawn from 64 scenarios by
weighted sampling, which meant the same scenario was raised many times **with
its text reused verbatim**. Nine tickets shared the title "Comp-off expiry not
honoured", word for word, and 200 of the 220 tickets sat in an exact-duplicate
group.

That is not what a bug tracker looks like, and it quietly flattered the
retrieval evaluation: "recall@20 = 1.000 on paraphrases" was partly measured on
pairs whose text was byte-identical, which is exact matching rather than
paraphrase matching. Both retrievers solve that perfectly, so the number said
less than it appeared to.

So: eighty scenarios, each used **exactly once**, no repeats. The near-duplicates
are the twenty hand-written variants in `evals/datasets.py`, where the wording
genuinely differs and the ground truth is known. 100 tickets total.

## What these are

Operational-support issues as Keka's customers actually report them — payroll
runs, statutory filings, attendance capture, leave accrual, onboarding,
performance cycles, expenses, the mobile app, integrations. Each description is
written the way a support agent writes one up after a call: the symptom, who it
affects, and whatever the customer noticed. Long enough to be a real retrieval
problem; specific enough that two different bugs in the same module do not read
identically.

Severity, impact and workaround are set deliberately per scenario, not
randomly. The resulting priority mix should look like a healthy tracker — a few
P1s and a long P3 tail — rather than the inflation that decision D-04 exists to
prevent.
"""

from __future__ import annotations

from app.modules.tickets.enums import Impact, Severity, Workaround

#: (team, title, description, severity, impact, workaround)
Scenario = tuple[str, str, str, Severity, Impact, Workaround]

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


def S(  # noqa: N802 - deliberately terse; it appears eighty times below
    team: str,
    title: str,
    description: str,
    severity: Severity,
    impact: Impact,
    workaround: Workaround,
) -> Scenario:
    return (team, title, description, severity, impact, workaround)


SCENARIOS: list[Scenario] = [
    # ================================================================ Payroll
    S(
        "Payroll",
        "Payslip PDF generates blank for contractors",
        "Downloading a payslip for anyone on a contractor employment type produces "
        "a PDF with the company header and the column titles, and no earnings or "
        "deduction rows underneath. Permanent employees download correctly on the "
        "same run. The customer noticed it after moving fourteen people onto "
        "fixed-term contracts last quarter, and their finance team is now "
        "screenshotting the salary breakdown from the web view instead.",
        S1,
        DEPT,
        PAINFUL,
    ),
    S(
        "Payroll",
        "TDS computed on gross instead of taxable income",
        "Monthly tax deduction is being calculated on the full gross salary with no "
        "regard for the investment declarations employees submitted in April. "
        "Section 80C, HRA exemption and the standard deduction are all being "
        "ignored, so deductions are running roughly forty per cent higher than they "
        "should. Several employees have raised it with their reporting managers and "
        "the payroll team is holding the run.",
        S1,
        DEPT,
        NONE,
    ),
    S(
        "Payroll",
        "PF challan total does not match the contribution report",
        "The ECR file generated for upload to the EPFO portal shows a total that is "
        "short of the sum in the contribution report by exactly the employer share "
        "for employees who joined mid-month. Pro-rating appears to be applied to "
        "the employee contribution but not the employer one. The customer has been "
        "correcting the challan by hand before every upload.",
        S2,
        DEPT,
        PAINFUL,
    ),
    S(
        "Payroll",
        "Salary revision arrears missed in the next payroll run",
        "A backdated salary revision was approved on the twentieth with an effective "
        "date of the first of the previous month. The revised salary applied "
        "correctly going forward, but the arrear for the earlier period was never "
        "picked up in any subsequent run. Six employees are affected and the "
        "customer has processed the arrears as an off-cycle payment.",
        S2,
        FEW,
        PAINFUL,
    ),
    S(
        "Payroll",
        "Form 16 Part B shows the previous employer's income twice",
        "For employees who joined mid-year and declared previous employment income, "
        "Part B is adding that income to the total a second time, inflating the "
        "taxable figure and the tax already paid. The customer spotted it during "
        "the annual issuance and has stopped distribution.",
        S2,
        FEW,
        PAINFUL,
    ),
    S(
        "Payroll",
        "Professional tax deducted at the wrong state slab",
        "Employees who transferred between offices are being deducted professional "
        "tax at the slab for their original work location rather than the current "
        "one. Karnataka and Maharashtra rates differ, so the amounts are wrong in "
        "both directions depending on the direction of the transfer.",
        S2,
        FEW,
        PAINFUL,
    ),
    S(
        "Payroll",
        "Bonus payout rounds down to the nearest hundred",
        "The annual bonus component is being rounded down to the nearest hundred "
        "rupees on the payslip while the underlying computation keeps the exact "
        "figure. The difference then shows up as an unexplained gap between the "
        "payslip total and the bank transfer file.",
        S3,
        DEPT,
        EASY,
    ),
    S(
        "Payroll",
        "Loan EMI continues after the loan is fully repaid",
        "An employee advance was fully recovered in November, and the EMI deduction "
        "has continued in December and January. The loan record shows a zero "
        "outstanding balance, so the deduction schedule is not reading it. Two "
        "employees have had to be reimbursed manually.",
        S2,
        FEW,
        PAINFUL,
    ),
    S(
        "Payroll",
        "Reimbursement claim approved but not included in the payout",
        "Expense claims approved after the payroll input freeze are marked as "
        "'included in payroll' in the claim view, but the amount never appears in "
        "the payslip or the bank file. The claim then cannot be re-submitted "
        "because the system considers it settled.",
        S2,
        DEPT,
        PAINFUL,
    ),
    S(
        "Payroll",
        "Gratuity calculation ignores the partial final year",
        "For an employee leaving after six years and eight months, gratuity is being "
        "computed on six years rather than seven. The rule that a period beyond six "
        "months counts as a full year is not being applied, so the settlement is "
        "short.",
        S3,
        ONE,
        PAINFUL,
    ),
    S(
        "Payroll",
        "ESI deduction continues above the wage ceiling",
        "Employees whose gross crossed the ESI wage ceiling after an increment are "
        "still having ESI deducted. The contribution should stop at the end of the "
        "contribution period, and instead it is continuing indefinitely.",
        S3,
        FEW,
        PAINFUL,
    ),
    S(
        "Payroll",
        "Bank transfer file rejects accounts with an IFSC in lower case",
        "The NEFT upload file is generated with the IFSC exactly as entered in the "
        "employee record. Where someone typed it in lower case the bank's portal "
        "rejects the row, and the whole batch fails validation rather than the "
        "single line.",
        S2,
        DEPT,
        PAINFUL,
    ),
    S(
        "Payroll",
        "Full and final settlement shows a negative leave encashment",
        "An exiting employee with a negative leave balance is producing a settlement "
        "with leave encashment shown as a negative earning rather than a recovery "
        "deduction. The net figure is right but the payslip reads as though the "
        "company owes them a negative amount.",
        S3,
        ONE,
        EASY,
    ),
    S(
        "Payroll",
        "Payroll run stuck at ninety per cent for over an hour",
        "The monthly run reaches ninety per cent and stays there. No error is shown, "
        "the progress indicator keeps spinning, and cancelling and restarting leaves "
        "it in the same place. The customer has 1,400 employees and payday is "
        "tomorrow.",
        S1,
        ORG,
        NONE,
    ),
    S(
        "Payroll",
        "Variable pay percentage applies to the revised salary retrospectively",
        "When a salary revision is entered, the variable pay percentage recalculates "
        "against the new salary for months already paid, so the year-to-date "
        "variable figure changes retroactively and no longer matches the payslips "
        "that were issued.",
        S3,
        DEPT,
        PAINFUL,
    ),
    S(
        "Payroll",
        "Salary structure template applies the old HRA formula to new joiners",
        "A structure template was edited in June to change the HRA basis. Employees "
        "onboarded after that date are still getting the pre-June formula, which "
        "suggests the template version is being pinned at creation rather than "
        "resolved at run time.",
        S3,
        FEW,
        PAINFUL,
    ),
    # ================================================================= CoreHR
    S(
        "CoreHR",
        "Manager change does not cascade to pending leave approvals",
        "After a reporting manager is changed, leave requests that were already "
        "pending stay in the old manager's queue. The new manager cannot see them, "
        "and the old manager can still approve them even after moving to a different "
        "department. HR has been cancelling and re-raising the requests.",
        S2,
        DEPT,
        PAINFUL,
    ),
    S(
        "CoreHR",
        "Org chart shows a terminated employee as an active manager",
        "An employee who exited two months ago still appears in the organisation "
        "chart with four direct reports hanging off them. The employee record shows "
        "the correct exit date and inactive status, so only the chart is stale.",
        S3,
        ORG,
        EASY,
    ),
    S(
        "CoreHR",
        "Bulk employee import skips rows with a duplicate email silently",
        "Importing a spreadsheet of 300 new joiners reports 300 successes, but only "
        "287 records are created. The thirteen rows that shared an email with an "
        "existing employee were skipped with no line reported in the summary, so the "
        "customer only found out when the missing people could not log in.",
        S2,
        DEPT,
        PAINFUL,
    ),
    S(
        "CoreHR",
        "Probation confirmation date calculated from the offer date",
        "The probation end date is being derived from the offer acceptance date "
        "rather than the actual date of joining. For anyone whose start slipped, "
        "confirmation is being prompted weeks early and the reminder emails have "
        "already gone out to managers.",
        S3,
        FEW,
        PAINFUL,
    ),
    S(
        "CoreHR",
        "Employee document upload fails silently over five megabytes",
        "Uploading a scanned PAN or passport larger than about five megabytes shows "
        "a success toast, but the document does not appear in the employee's "
        "document list afterwards. No error is logged anywhere the customer can see.",
        S3,
        DEPT,
        PAINFUL,
    ),
    S(
        "CoreHR",
        "Custom field values lost when an employee changes department",
        "Department-specific custom fields are cleared when an employee is "
        "transferred, including fields that are shared across departments. The "
        "customer keeps a separate spreadsheet of the values and re-enters them "
        "after every transfer.",
        S3,
        FEW,
        PAINFUL,
    ),
    S(
        "CoreHR",
        "Offer letter template renders the salary annexure with placeholder tags",
        "Generated offer letters show raw tags such as {{ctc_annual}} in the salary "
        "annexure section instead of the values. The body of the letter merges "
        "correctly, so it seems specific to the annexure table.",
        S2,
        FEW,
        PAINFUL,
    ),
    S(
        "CoreHR",
        "Background verification status stuck at 'initiated' after the vendor completes",
        "The verification vendor has marked three candidates as cleared in their own "
        "portal, and Keka still shows them as initiated. The onboarding checklist "
        "will not advance, so joining formalities are blocked.",
        S2,
        FEW,
        PAINFUL,
    ),
    S(
        "CoreHR",
        "Employee ID sequence skips numbers after a failed import",
        "A failed bulk import consumed a block of employee IDs, so the next "
        "successful record jumps from EMP-2841 to EMP-2903. Finance reconciles "
        "against the ID sequence and is now querying the gaps.",
        S4,
        DEPT,
        EASY,
    ),
    S(
        "CoreHR",
        "Exit interview form cannot be submitted without an optional field",
        "The exit interview form marks the 'reason for leaving - other' box as "
        "optional, but submitting without it shows a validation error at the top of "
        "the page with no field highlighted. HR has been typing a full stop into the "
        "box to get past it.",
        S3,
        FEW,
        EASY,
    ),
    S(
        "CoreHR",
        "Confirmation letter goes to the employee's personal email only",
        "Probation confirmation letters are sent to the personal email on file "
        "rather than the work address, so the letter lands outside the company and "
        "the copy to HR never arrives. The customer's policy requires the work "
        "address.",
        S3,
        DEPT,
        PAINFUL,
    ),
    S(
        "CoreHR",
        "Employee search does not match on employee code",
        "Searching the employee directory by employee code returns nothing; only "
        "name and email match. The customer's managers refer to people by code and "
        "have to look the name up elsewhere first.",
        S3,
        ORG,
        PAINFUL,
    ),
    S(
        "CoreHR",
        "Asset allocation shows an asset assigned to two employees",
        "A laptop returned during an exit and reissued to a new joiner now appears "
        "in both employees' asset lists. The return was recorded, so the previous "
        "allocation is not being closed when a new one is created.",
        S3,
        FEW,
        EASY,
    ),
    S(
        "CoreHR",
        "Date of joining cannot be corrected once payroll has run",
        "A joining date entered incorrectly by two weeks cannot be edited after the "
        "first payroll, and the field gives no explanation beyond 'cannot modify'. "
        "The customer accepts the restriction but needs a supported correction path.",
        S3,
        ONE,
        NONE,
    ),
    S(
        "CoreHR",
        "Employee photo rotates ninety degrees after upload",
        "Photos taken on a phone appear sideways in the profile and the directory. "
        "The original file opens correctly on the desktop, so the EXIF orientation "
        "flag is being ignored on processing.",
        S4,
        ORG,
        EASY,
    ),
    # ============================================================== Workforce
    S(
        "Workforce",
        "Biometric attendance sync stopped for one location",
        "No attendance punches have arrived from the Hyderabad office since Saturday "
        "morning. The device shows employees clocking in normally and its own log "
        "has the records, so the failure is between the device and Keka. Every other "
        "location is reporting fine and the branch's attendance is now being "
        "compiled from the security register.",
        S1,
        DEPT,
        PAINFUL,
    ),
    S(
        "Workforce",
        "Leave balance short by one day after carry-forward",
        "After the year-end carry-forward ran, every employee's earned leave opening "
        "balance is exactly one day lower than the closing balance shown for the "
        "previous year. The customer reconciled forty employees by hand and the "
        "shortfall is consistent across all of them.",
        S2,
        DEPT,
        PAINFUL,
    ),
    S(
        "Workforce",
        "Weekly off marked as absent for night-shift staff",
        "Employees on the ten-at-night shift are being marked absent on their weekly "
        "off. Their shift crosses midnight, so the off day appears to be evaluated "
        "against the calendar date the shift started rather than the rostered day.",
        S2,
        DEPT,
        PAINFUL,
    ),
    S(
        "Workforce",
        "Comp-off expiry not honoured",
        "Compensatory offs earned for weekend work are expiring after thirty days "
        "even though the customer's policy allows ninety. Employees are losing "
        "accrued comp-off without warning, and the policy screen shows ninety days "
        "configured.",
        S2,
        DEPT,
        PAINFUL,
    ),
    S(
        "Workforce",
        "Attendance regularisation request cannot be raised for last month",
        "Once the month is closed for payroll, employees cannot raise a "
        "regularisation even within the seven-day window the customer's policy "
        "allows. Managers are emailing HR, who edit the record directly.",
        S3,
        DEPT,
        PAINFUL,
    ),
    S(
        "Workforce",
        "GPS check-in accepted from outside the geofence",
        "Field staff are able to check in from well outside the configured office "
        "radius. The location is captured and stored, and the radius check does not "
        "appear to be applied on the mobile app at all.",
        S2,
        DEPT,
        PAINFUL,
    ),
    S(
        "Workforce",
        "Shift roster publish overwrites individual shift changes",
        "Publishing next week's roster resets shift swaps that were approved "
        "individually during the current week. Supervisors have to re-apply every "
        "swap after each publish.",
        S2,
        DEPT,
        PAINFUL,
    ),
    S(
        "Workforce",
        "Half-day leave deducts a full day from the balance",
        "Applying for a half-day casual leave reduces the balance by a full day. The "
        "attendance record correctly shows a half day, so only the balance "
        "arithmetic is wrong.",
        S2,
        DEPT,
        PAINFUL,
    ),
    S(
        "Workforce",
        "Holiday calendar for a second location not applied",
        "Employees mapped to the Pune office are being marked absent on Pune-specific "
        "holidays. The location has its own holiday calendar configured and the "
        "employees are correctly mapped to it.",
        S3,
        DEPT,
        PAINFUL,
    ),
    S(
        "Workforce",
        "Overtime hours not accumulating for the second half of the month",
        "Overtime is captured correctly until the fifteenth and then stops "
        "accumulating for the rest of the month. The punches are present in the "
        "attendance log for the whole period.",
        S2,
        DEPT,
        PAINFUL,
    ),
    S(
        "Workforce",
        "Leave applied during notice period is auto-rejected without a reason",
        "Leave requests raised by employees serving notice are rejected immediately "
        "with no reason shown to the employee or the manager. The customer's policy "
        "permits leave during notice with approval.",
        S3,
        FEW,
        PAINFUL,
    ),
    S(
        "Workforce",
        "Attendance export missing the last day of the month",
        "The monthly attendance export ends on the thirtieth for a thirty-one day "
        "month. The web view shows the last day correctly, so the export's date "
        "range is off by one.",
        S3,
        DEPT,
        PAINFUL,
    ),
    S(
        "Workforce",
        "Maternity leave shows as unpaid in the balance summary",
        "Maternity leave is configured as paid, and the balance summary and the "
        "payslip both treat those days as loss of pay. The leave type screen shows "
        "the correct paid setting.",
        S2,
        FEW,
        PAINFUL,
    ),
    S(
        "Workforce",
        "Timesheet hours do not roll up to the project total",
        "Individual timesheet entries save correctly but the project-level total "
        "stays at the previous week's figure. Refreshing and re-opening the project "
        "makes no difference.",
        S3,
        DEPT,
        PAINFUL,
    ),
    S(
        "Workforce",
        "Late-coming penalty applied on an approved work-from-home day",
        "Employees with approved work-from-home are picking up late-coming marks "
        "because no office punch exists for that day. The WFH approval is visible on "
        "the same attendance record.",
        S3,
        DEPT,
        EASY,
    ),
    S(
        "Workforce",
        "Leave encashment request shows zero eligible days",
        "Employees with more than the encashable minimum see zero eligible days on "
        "the encashment form. The leave balance page shows the correct accrued "
        "figure for the same people.",
        S3,
        FEW,
        PAINFUL,
    ),
    # =============================================================== Platform
    S(
        "Platform",
        "SSO login loops back to the sign-in page",
        "Employees using the corporate identity provider are returned to the Keka "
        "sign-in page after authenticating successfully at the provider. The browser "
        "cycles between the callback URL and the login page until the session times "
        "out. Password login still works, so the customer has told everyone to use "
        "that for now.",
        S1,
        ORG,
        PAINFUL,
    ),
    S(
        "Platform",
        "Webhook retries stop after the first failure",
        "The employee-created webhook is delivered once, and if the receiving "
        "endpoint returns a 500 it is never retried. The delivery log shows a single "
        "attempt, so the customer's downstream directory misses new joiners "
        "whenever their service restarts.",
        S3,
        DEPT,
        PAINFUL,
    ),
    S(
        "Platform",
        "Password reset email arrives forty minutes late",
        "Reset emails are being delivered thirty to sixty minutes after the request, "
        "by which time the link has expired. Other transactional emails arrive "
        "promptly, so it appears specific to this template or its queue.",
        S3,
        DEPT,
        PAINFUL,
    ),
    S(
        "Platform",
        "API rate limit returns 500 instead of 429",
        "Exceeding the documented request rate on the employee API returns a 500 "
        "with an empty body rather than a 429 with a retry hint. The customer's "
        "integration treats it as an outage and pages their on-call engineer.",
        S3,
        FEW,
        EASY,
    ),
    S(
        "Platform",
        "Session expires after fifteen minutes despite the configured timeout",
        "Users are being signed out roughly every fifteen minutes although the "
        "tenant setting is eight hours. It happens across browsers and locations, "
        "and it started without any change on the customer's side.",
        S2,
        DEPT,
        PAINFUL,
    ),
    S(
        "Platform",
        "Audit log missing entries for changes made through the API",
        "Employee updates made through the REST API do not appear in the audit trail, "
        "while the same change through the web interface does. The customer's "
        "internal audit requires both.",
        S3,
        FEW,
        PAINFUL,
    ),
    S(
        "Platform",
        "Two-factor authentication code rejected for the first thirty seconds",
        "Authenticator codes are rejected immediately after they refresh and accepted "
        "about half a minute later, which suggests a clock skew on the verifying "
        "side. Employees are learning to wait, but new joiners get locked out.",
        S3,
        DEPT,
        PAINFUL,
    ),
    S(
        "Platform",
        "Scheduled report emailed twice every morning",
        "The daily headcount report arrives twice, a few minutes apart, with "
        "identical content. It began after the customer changed the delivery time, "
        "and the schedule screen shows a single entry.",
        S4,
        FEW,
        EASY,
    ),
    S(
        "Platform",
        "Data export request never produces a download link",
        "A full data export is accepted and shows as processing indefinitely. No "
        "email arrives and no file appears in the export history. The customer needs "
        "it for a compliance request with a deadline.",
        S2,
        DEPT,
        PAINFUL,
    ),
    S(
        "Platform",
        "Role permission change requires a sign-out to take effect",
        "Adding a permission to a role does not affect users who are already signed "
        "in until they sign out and back in. Nothing tells the administrator that, "
        "so it reads as the change not saving.",
        S3,
        DEPT,
        EASY,
    ),
    S(
        "Platform",
        "Notification preferences reset to default after a release",
        "Employees who had turned off leave-approval emails started receiving them "
        "again after the last release. Their preference screen shows the "
        "notifications as disabled, so the stored preference and the sending "
        "behaviour disagree.",
        S3,
        ORG,
        PAINFUL,
    ),
    S(
        "Platform",
        "HRIS integration creates duplicate records on re-sync",
        "Re-running the integration with the customer's ERP creates a second "
        "employee record instead of updating the existing one. Matching appears to "
        "be on an internal id rather than the employee code both systems share.",
        S1,
        DEPT,
        NONE,
    ),
    S(
        "Platform",
        "Mobile app shows yesterday's attendance after a pull to refresh",
        "The Android app keeps serving the previous day's attendance summary until it "
        "is force-closed. Pull to refresh spins and returns the stale data, so a "
        "cache is not being invalidated.",
        S3,
        ORG,
        EASY,
    ),
    S(
        "Platform",
        "Helpdesk ticket assignment email names the wrong assignee",
        "The notification for a reassigned helpdesk ticket names the previous "
        "assignee in the body while the ticket itself shows the new one. Recipients "
        "are ignoring the emails as a result.",
        S3,
        DEPT,
        EASY,
    ),
    # ===================================================================== UI
    S(
        "UI",
        "Payroll summary table headers scroll away on a laptop screen",
        "On a 1366 by 768 display the payroll summary's column headers scroll out of "
        "view, and with thirty columns the customer's payroll team loses track of "
        "which figure is which. It reads correctly on a larger monitor.",
        S3,
        DEPT,
        PAINFUL,
    ),
    S(
        "UI",
        "Date picker allows a future date for date of birth",
        "The date of birth field accepts dates in the future and saves them without "
        "complaint, producing negative ages in the directory and in age-band "
        "reports.",
        S3,
        ORG,
        EASY,
    ),
    S(
        "UI",
        "Leave application form loses the reason text on validation failure",
        "If the date range fails validation, the reason field is cleared along with "
        "the error, so the employee retypes a paragraph every attempt.",
        S3,
        ORG,
        PAINFUL,
    ),
    S(
        "UI",
        "Employee list pagination jumps back to page one after an edit",
        "Editing an employee from page seven of the directory returns the user to "
        "page one. With 1,400 employees the customer's HR team is paging forward "
        "repeatedly through a working session.",
        S3,
        DEPT,
        PAINFUL,
    ),
    S(
        "UI",
        "Currency shown without a thousands separator in the salary breakup",
        "The salary breakup screen renders 1250000 rather than 12,50,000. Employees "
        "are misreading the figures, and every other screen formats correctly.",
        S4,
        ORG,
        EASY,
    ),
    S(
        "UI",
        "Attendance calendar colours are indistinguishable for colour-blind users",
        "Present and half-day use two greens that an employee with deuteranopia "
        "cannot tell apart, and the only other cue is a tooltip on hover, which is "
        "unavailable on the mobile app.",
        S3,
        ORG,
        PAINFUL,
    ),
    S(
        "UI",
        "Bulk action checkbox selects rows on other pages invisibly",
        "The select-all checkbox in the employee list selects every matching record "
        "across all pages, and the confirmation dialog reports the count on the "
        "current page. The customer nearly issued letters to 1,400 people.",
        S2,
        DEPT,
        PAINFUL,
    ),
    S(
        "UI",
        "Print view of the payslip cuts off the deductions column",
        "Printing a payslip from the browser truncates the rightmost deductions "
        "column, and the PDF download is complete. Employees who print from the web "
        "view get an incomplete document.",
        S3,
        ORG,
        EASY,
    ),
    S(
        "UI",
        "Dropdown for reporting manager only shows the first fifty names",
        "The reporting manager selector lists fifty people and does not load more on "
        "scroll or respond to typing beyond that set, so managers later in the "
        "alphabet cannot be selected at all.",
        S3,
        DEPT,
        PAINFUL,
    ),
    # ============================================================ Performance
    S(
        "Performance",
        "Review cycle cannot close while one review is in draft",
        "The annual cycle will not move to closed because a single self-review is "
        "still in draft for an employee who has left the company. There is no way to "
        "withdraw or force-complete it, so the whole cycle is blocked for 900 "
        "employees.",
        S1,
        ORG,
        NONE,
    ),
    S(
        "Performance",
        "Goal weightage total allows more than one hundred per cent",
        "Objective weightages can be saved summing to more than one hundred per "
        "cent, and the final rating is then computed on the inflated total. Two "
        "departments have completed goal-setting this way.",
        S2,
        DEPT,
        PAINFUL,
    ),
    S(
        "Performance",
        "Peer feedback visible to the employee before the cycle is published",
        "Employees are able to read peer feedback while the cycle is still in "
        "progress, which the customer's policy forbids and which has already caused "
        "a complaint. The setting for anonymity is enabled.",
        S2,
        DEPT,
        PAINFUL,
    ),
    S(
        "Performance",
        "Rating scale labels reversed on the manager review form",
        "The five-point scale reads 'exceeds' at the low end and 'below "
        "expectations' at the high end on the manager form, while the employee's "
        "self-review shows the correct order. Some managers have submitted against "
        "the reversed scale.",
        S2,
        DEPT,
        PAINFUL,
    ),
    S(
        "Performance",
        "Check-in reminders sent to employees on long leave",
        "Weekly check-in reminders continue for employees on approved long leave, "
        "including maternity leave. The customer would like the reminders suppressed "
        "while a long-leave record is active.",
        S3,
        FEW,
        EASY,
    ),
    S(
        "Performance",
        "Calibration screen loses unsaved changes when filtering",
        "Applying a department filter during calibration discards rating changes "
        "that have not been saved, without warning. HR business partners have lost a "
        "session's work twice.",
        S3,
        FEW,
        PAINFUL,
    ),
    S(
        "Performance",
        "Nine-box grid places employees with no rating in the top box",
        "Employees whose review is incomplete appear in the top-right box of the "
        "nine-box grid rather than being excluded, which distorts the talent review "
        "the customer runs off that view.",
        S3,
        DEPT,
        PAINFUL,
    ),
    S(
        "Performance",
        "Objective progress percentage does not update from key results",
        "Updating a key result's progress leaves the parent objective at its "
        "previous percentage until the page is reloaded twice. Employees believe "
        "their update did not save and enter it again.",
        S3,
        DEPT,
        EASY,
    ),
    S(
        "Performance",
        "Review form allows submission with mandatory comments empty",
        "Comment boxes marked mandatory can be left empty and the review still "
        "submits. The customer requires written justification for the lowest and "
        "highest ratings.",
        S3,
        DEPT,
        PAINFUL,
    ),
    S(
        "Performance",
        "Previous cycle's goals copied into the new cycle with old due dates",
        "Rolling goals forward into the new cycle brings the previous due dates with "
        "them, so every copied objective is immediately overdue and the dashboard "
        "shows the department in red.",
        S3,
        DEPT,
        EASY,
    ),
]

#: Sanity checks, because this file is data and data drifts.
#:
#: The uniqueness assertion is the important one: the whole point of this
#: rewrite was that the previous corpus repeated scenario text verbatim, and a
#: duplicate title reintroduced here would quietly undo it.
_titles = [scenario[1] for scenario in SCENARIOS]
assert len(_titles) == len(set(_titles)), "duplicate scenario titles"
assert len(SCENARIOS) == 80, f"expected 80 scenarios, found {len(SCENARIOS)}"
