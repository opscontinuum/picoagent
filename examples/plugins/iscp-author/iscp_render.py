"""Render the document set from the answers file. A pure function of its inputs.

The anti-fabrication guarantee
------------------------------
Every byte of ``ISCP.md`` is one of exactly three things, and the renderer is built so that it
*cannot* produce a fourth:

``Segment("template", ...)``
    Text copied from :mod:`iscp_template` or a :class:`iscp_questions.Question`'s
    ``placeholder`` - which is to say, from the FedRAMP template or NIST SP 800-34 Rev. 1.
``Segment("answer", ...)``
    A value the user recorded through ``iscp_answer``, stringified and nothing more.
``Segment("markup", ...)``
    Markdown structure: newlines, ``#`` prefixes, ``-`` bullets, ``|`` table pipes. Markup
    segments contain no letters or digits at all.

:func:`render_iscp` returns the segments alongside the text, and
``"".join(segment.text for segment in segments) == text`` always holds. ``tests/
test_iscp_plugin.py`` asserts all four properties, so "every rendered sentence is either
template text or a user's answer" is a checked invariant rather than a claim.

Unanswered questions render the source's own placeholder - ``<Insert CSO Name>``,
``<Enter Number>``, ``Click here to enter text.``, ``{insert}`` - never a plausible default.
:class:`RenderReport` counts them per section so the user knows what is still open.

The runbooks and the CI inventory are the *project's* own structure, not FedRAMP's, and this
module says so where it writes them. FedRAMP §4.2 states that "specific keystroke-level
procedures may be provided in an appendix"; that slot is what the runbooks fill. Nothing in
them is generated except steps derived from a Configuration Item or an explicit
``TODO(<question id>)`` - no product behaviour is ever asserted.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

import iscp_questions
import iscp_template
from iac_inventory import CI
from iscp_template import Bullets, Heading, Para, Table

#: ``{question.id}`` or ``{question.id|<alternate placeholder>}``. Braces naming anything else
#: stay literal, which is what keeps NIST's own ``{insert}`` and ``{system name}`` intact.
SLOT = re.compile(r"\{([A-Za-z0-9_.]+)(?:\|([^{}]*))?\}")

#: Markup segments must match this: structure only, never a letter or a digit. The test
#: asserts it, which is how "the renderer cannot invent a word" is enforced mechanically.
MARKUP_ONLY = re.compile(r"^[\s#|*\-:,]*$")


@dataclass(frozen=True)
class Segment:
    """One piece of the output, tagged with where it came from."""
    kind: str          # "template" | "answer" | "markup"
    text: str


@dataclass
class RenderReport:
    """What one render produced and what is still open in it."""
    files: dict[str, str] = field(default_factory=dict)          # relative path -> content
    unfilled: dict[str, list[str]] = field(default_factory=dict)  # section -> question ids
    todos: list[str] = field(default_factory=list)                # TODO(...) markers in runbooks
    segments: list[Segment] = field(default_factory=list)         # provenance of ISCP.md

    @property
    def unfilled_count(self) -> int:
        return sum(len(ids) for ids in self.unfilled.values())


class _Writer:
    """Accumulates segments. Every append goes through one of these three methods."""

    def __init__(self) -> None:
        self.segments: list[Segment] = []

    def markup(self, text: str) -> None:
        if text:
            self.segments.append(Segment("markup", text))

    def template(self, text: str) -> None:
        if text:
            self.segments.append(Segment("template", text))

    def answer(self, text: str) -> None:
        if text:
            self.segments.append(Segment("answer", text))

    def answer_value(self, value: object) -> None:
        """One answer. A list becomes one segment per item so each stays traceable to the
        answers file - joining them into a single string would produce a sentence that is in
        neither the template nor the answers, which is exactly what must not happen."""
        if isinstance(value, list):
            for position, item in enumerate(value):
                if position:
                    self.markup(", ")
                self.answer_value(item)
        else:
            self.answer(_scalar(value))

    def fill(self, text: str, answers: dict) -> None:
        """Emit ``text``, substituting slots with answers or with the source's placeholder."""
        position = 0
        for match in SLOT.finditer(text):
            self.template(text[position:match.start()])
            self._slot(match.group(1), match.group(2), answers)
            position = match.end()
        self.template(text[position:])

    def _slot(self, question_id: str, override: str | None, answers: dict) -> None:
        question = iscp_questions.question(question_id)
        if question is None:
            self.template("{" + question_id + ("|" + override if override else "") + "}")
            return
        value = answers.get(question_id)
        if _is_empty(value):
            self.template(override if override is not None else question.placeholder)
        else:
            self.answer_value(value)

    @property
    def text(self) -> str:
        return "".join(segment.text for segment in self.segments)


def _is_empty(value: object) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _scalar(value: object) -> str:
    """One scalar answer as text. Lists are emitted item by item, never joined here."""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return str(value)


# --------------------------------------------------------------------------- ISCP.md

def render_iscp(answers: dict, cis: list[CI]) -> tuple[str, list[Segment], dict[str, list[str]]]:
    """The FedRAMP ISCP, its provenance, and the still-unfilled question ids per section."""
    writer = _Writer()
    for block in iscp_template.ISCP_BLOCKS:
        _block(writer, block, answers)
    return writer.text, writer.segments, unfilled_by_section(answers)


def _block(writer: _Writer, block: object, answers: dict) -> None:
    if isinstance(block, Heading):
        writer.markup("#" * block.level + " ")
        writer.fill(block.text, answers)
        writer.markup("\n\n")
    elif isinstance(block, Para):
        writer.fill(block.text, answers)
        writer.markup("\n\n")
    elif isinstance(block, Bullets):
        _bullets(writer, block, answers)
    elif isinstance(block, Table):
        _table(writer, block, answers)


def _bullets(writer: _Writer, block: Bullets, answers: dict) -> None:
    """The template's bullets, or the user's list where they supplied one."""
    supplied = answers.get(block.question) if block.question else None
    if isinstance(supplied, list) and supplied:
        for item in supplied:
            writer.markup("- ")
            writer.answer_value(item)
            writer.markup("\n")
    else:
        for item in block.items:
            writer.markup("- ")
            writer.fill(item, answers)
            writer.markup("\n")
    writer.markup("\n")


def _table(writer: _Writer, block: Table, answers: dict) -> None:
    if block.title:
        writer.markup("**")
        writer.fill(block.title, answers)
        writer.markup("**\n\n")
    columns = _effective_columns(block, answers)
    _row(writer, columns, answers)
    writer.markup("|" + " --- |" * len(columns) + "\n")
    for row in block.rows:
        _row(writer, [(cell, "template") for cell in row], answers)
    _question_rows(writer, block, columns, answers)
    writer.markup("\n")


def _effective_columns(block: Table, answers: dict) -> list[tuple[str, str]]:
    """Header cells as ``(text, kind)``.

    NIST's impact summary table is as wide as the impact categories the user chose, so those
    column *names* are the user's answers and are tagged as such - a header cell reading
    "Cost" came from the answers file, not from NIST.
    """
    if not block.columns_from:
        return [(cell, "template") for cell in block.header]
    categories = answers.get(block.columns_from)
    names = [str(row.get("Impact category", "")) for row in categories] \
        if isinstance(categories, list) else []
    names = [name for name in names if name]
    middle = [(name, "answer") for name in names] or \
        [(iscp_questions.NIST_INSERT, "template")] * 4
    return [(block.header[0], "template"), *middle, (block.header[-1], "template")]


def _question_rows(writer: _Writer, block: Table, columns: list[tuple[str, str]],
                   answers: dict) -> None:
    """The user's rows, or the source's own placeholder rows when there are none."""
    if not block.question:
        return
    supplied = answers.get(block.question)
    if isinstance(supplied, list) and supplied:
        for row in supplied:
            _row(writer, [(row.get(name, ""), "answer") for name, _ in columns], answers)
        return
    for row in block.template_rows:
        padded = list(row) + [row[-1] if row else ""] * (len(columns) - len(row))
        _row(writer, [(cell, "template") for cell in padded[:len(columns)]], answers)


def _row(writer: _Writer, cells: list[tuple[object, str]], answers: dict) -> None:
    writer.markup("|")
    for value, kind in cells:
        writer.markup(" ")
        if kind == "answer":
            writer.answer_value(value)
        else:
            writer.fill(str(value), answers)
        writer.markup(" |")
    writer.markup("\n")


def unfilled_by_section(answers: dict) -> dict[str, list[str]]:
    """Question ids per section that still render as their source's placeholder."""
    unfilled: dict[str, list[str]] = {}
    for question in iscp_questions.QUESTIONS:
        if _is_empty(answers.get(question.id)):
            unfilled.setdefault(question.section, []).append(question.id)
    return unfilled


# --------------------------------------------------------------------------- CI inventory

def render_ci_inventory(cis: list[CI], answers: dict) -> str:
    """``CI-inventory.md``: the estate this plan protects, grouped by category and site.

    This document is the project's own; it is not a FedRAMP artefact. Its job is to be the
    source a CSP fills the Integrated Inventory Workbook from (ISCP Appendix H points at that
    workbook, and this does not replace it) and to feed BIA §3.2 and §3.3.
    """
    lines = ["# Configuration Item inventory", "",
             "Derived from infrastructure-as-code by `iscp_import_cis`. This is not a FedRAMP "
             "artefact: ISCP Appendix H points at the Integrated Inventory Workbook, and this list "
             "is what you fill that workbook from. It also supplies the drafts offered for BIA "
             "sections 3.2 and 3.3.", ""]
    site_of = _sites_by_region(answers)
    lines.append(f"{len(cis)} configuration items.")
    lines.append("")
    replicated = [ci for ci in cis if ci.replication]
    if replicated:
        lines += ["## Configuration items carrying a DR mechanism", "",
                  "| CI | Name | Type | Mechanism |", "| --- | --- | --- | --- |"]
        lines += [f"| {ci.ci_id} | {ci.name} | {ci.resource_type} | {ci.replication} |"
                  for ci in replicated]
        lines.append("")
    for category in sorted({ci.category for ci in cis}):
        members = [ci for ci in cis if ci.category == category]
        lines += [f"## {category} ({len(members)})", "",
                  "| CI | Name | Type | Region | Site | Source |",
                  "| --- | --- | --- | --- | --- | --- |"]
        for ci in sorted(members, key=lambda c: c.ci_id):
            site = ci.site or site_of.get(ci.region or "", "")
            lines.append(f"| {ci.ci_id} | {ci.name} | {ci.resource_type} | {ci.region or ''} | "
                         f"{site} | {ci.source} |")
        lines.append("")
    return "\n".join(lines) + "\n"


def _sites_by_region(answers: dict) -> dict[str, str]:
    """Region -> Primary/Alternate, taken only from the user's Table 2.5 answer."""
    rows = answers.get("sites")
    if not isinstance(rows, list):
        return {}
    return {str(row.get("Site Name", "")): str(row.get("Designation", ""))
            for row in rows if row.get("Site Name")}


# --------------------------------------------------------------------------- runbooks

#: The four runbooks and the situation each covers. Titles and the skeleton below are the
#: project's own - FedRAMP has no runbook template - and are modelled on the reference DR plan
#: the user authorised (``oci-itscp/runbooks/RB-01..RB-04``).
RUNBOOKS: tuple[tuple[str, str, str], ...] = (
    ("RB-01-switchover", "Planned Switchover",
     "A planned, rehearsed move to the alternate site with both sites healthy. No data loss is "
     "expected; the primary is shut down cleanly first."),
    ("RB-02-failover", "Unplanned Failover",
     "The primary site is lost. Data loss is possible and bounded by the replication lag at the "
     "moment of loss. This is a one-way door."),
    ("RB-03-failback", "Failback to the Primary Site",
     "Returning to the primary site after a failover. This is a project, not an incident step: "
     "replication has to be rebuilt in the opposite direction first."),
    ("RB-04-drill", "Non-Disruptive Drill",
     "Exercising this plan without failing over. Nothing in this runbook may require a real "
     "failover or affect production traffic."),
)

#: One section per runbook, in order. Every heading is fixed; the body under it is either
#: derived from a CI, copied from an answer, or an explicit TODO naming the question that
#: would fill it.
RUNBOOK_SECTIONS: tuple[str, ...] = (
    "1. Decision gate",
    "2. Capture forensics first",
    "3. Execution sequence",
    "4. Per-component steps",
    "5. Validation",
    "6. Work recovery and reconciliation",
    "7. Sign-off",
)

#: Category -> the step wording used when a CI of that category has no DR mechanism recorded.
#: Deliberately a question, not an instruction: the plugin does not know what the component
#: needs, and saying so is better than guessing.
_CATEGORY_STEP = {
    "compute": "start or scale at the alternate site",
    "database": "promote or restore at the alternate site",
    "storage": "make writable at the alternate site",
    "network": "confirm present at the alternate site",
    "dns": "steer traffic",
    "load_balancer": "confirm healthy backends at the alternate site",
    "security": "confirm present at the alternate site",
    "dr_orchestration": "execute",
    "backup": "confirm the most recent restore point",
    "monitoring": "confirm alerting follows the workload",
    "identity": "confirm present at the alternate site",
    "other": "decide what this component needs",
}


def render_runbook(slug: str, title: str, summary: str, cis: list[CI],
                   answers: dict) -> tuple[str, list[str]]:
    """One runbook skeleton plus the list of TODO markers it still carries."""
    todos: list[str] = []
    lines = [f"# {slug.split('-', 2)[0]}-{slug.split('-')[1]} — {title}", "",
             summary, "",
             f"**System:** {_answer_or_todo(answers, 'cso.name', todos)}",
             f"**Authority to invoke:** the personnel named in ISCP Table 3.1 Personnel Authorized "
             f"to Activate the ISCP. The authority lives there, not in this runbook.",
             f"**Recovery Time Objective:** {_answer_or_todo(answers, 'scope.rto_hours', todos)} hours "
             f"(ISCP section 1.4 Scope).", "",
             "> This runbook is the appendix FedRAMP ISCP section 4.2 Recovery Procedures refers to "
             "when it says \"specific keystroke-level procedures may be provided in an appendix\". "
             "Its structure is this project's, not FedRAMP's.", "",
             "> Every step below is either derived from a configuration item or marked `TODO`. "
             "Nothing here asserts how a product behaves. When you fill a TODO with product "
             "behaviour, cite the vendor page or mark the sentence `(unverified: engineering "
             "judgement)`.", ""]

    primary, alternate = _site_names(answers, todos)
    for section in RUNBOOK_SECTIONS:
        lines += [f"## {section}", ""]
        lines += _runbook_section(section, slug, cis, answers, todos, primary, alternate)
        lines.append("")
    return "\n".join(lines) + "\n", todos


def _runbook_section(section: str, slug: str, cis: list[CI], answers: dict, todos: list[str],
                     primary: str, alternate: str) -> list[str]:
    if section.startswith("1."):
        return [f"Decide whether this is a {slug.split('-', 2)[2].replace('-', ' ')} at all. The "
                f"activation criteria are in ISCP section 3.1 Activation Criteria and Procedure; "
                f"the outage assessment that feeds them is section 3.3 Outage Assessment.", "",
                f"- Outage assessment is conducted by "
                f"{_answer_or_todo(answers, 'outage.assessor_role', todos)}.",
                f"- {_answer_or_todo(answers, 'outage.procedures', todos)}", "",
                "**One-way doors in this runbook must be listed here, above the step that opens "
                "them.** `TODO(runbook): list them.`"]
    if section.startswith("2."):
        return ["Capture what is about to be destroyed by the recovery itself: replication lag at "
                "the moment of loss, replica timestamps, the last committed transaction. This is "
                "what the data-loss statement is written from.", "",
                "`TODO(runbook): name the command or console action for each capture, and where "
                "the output is stored.`"]
    if section.startswith("3."):
        return _sequence_steps(answers, todos)
    if section.startswith("4."):
        return _component_steps(cis, primary, alternate)
    if section.startswith("5."):
        return [f"- Data validation: "
                f"{_answer_or_todo(answers, 'reconstitution.data_validation', todos)}",
                f"- Functional validation: "
                f"{_answer_or_todo(answers, 'reconstitution.functional_validation', todos)}",
                "- The detailed acceptance steps are ISCP Appendix E System Validation Test Plan."]
    if section.startswith("6."):
        return ["Work Recovery Time activities start as soon as the technical recovery is "
                "underway, not after it: reconciling in-flight work, replaying queues, and "
                "restarting scheduled jobs.", "",
                "`TODO(runbook): list the reconciliation activities and who owns each.`"]
    return [f"- Recovery is declared by "
            f"{_answer_or_todo(answers, 'reconstitution.declaring_role', todos)} "
            f"(ISCP section 5.3 Recovery Declaration).",
            f"- Users are notified: "
            f"{_answer_or_todo(answers, 'reconstitution.user_notification', todos)}",
            f"- Event documentation responsibilities are in ISCP Table 5.2 Event Documentation "
            f"Responsibility.",
            "", "| Step | Owner role | Expected duration | Verified by | Time |",
            "| --- | --- | --- | --- | --- |", "| | | | | |"]


def _sequence_steps(answers: dict, todos: list[str]) -> list[str]:
    """The user's Section 4.1 sequence, or a TODO. Never a sequence of our own invention."""
    sequence = answers.get("recovery.sequence")
    if isinstance(sequence, list) and sequence:
        return [f"{number}. {step}" for number, step in enumerate(sequence, start=1)]
    todos.append("recovery.sequence")
    return ["`TODO(recovery.sequence): answer ISCP section 4.1 Sequence of Recovery Operations and "
            "re-render; the ordered sequence is copied here.`"]


def _component_steps(cis: list[CI], primary: str, alternate: str) -> list[str]:
    """One step per CI, derived from what the IaC actually says. Nothing else."""
    if not cis:
        return ["`TODO(iscp_import_cis): no configuration items have been imported, so there are "
                "no per-component steps. Run iscp_import_cis against your Terraform or "
                "CloudFormation sources.`"]
    lines: list[str] = []
    for category in sorted({ci.category for ci in cis}):
        members = sorted((ci for ci in cis if ci.category == category), key=lambda c: c.ci_id)
        lines += [f"### {category}", "",
                  "| CI | Component | Step | Owner role | Verification |",
                  "| --- | --- | --- | --- | --- |"]
        for ci in members:
            if ci.replication:
                step = f"Activate `{ci.replication}` for `{ci.address}`"
            else:
                step = (f"{_CATEGORY_STEP.get(category, _CATEGORY_STEP['other']).capitalize()}: "
                        f"`{ci.address}`")
            target = alternate or "TODO(sites)"
            lines.append(f"| {ci.ci_id} | {ci.name} | {step} in {target} | TODO(contacts.key_personnel) "
                         f"| TODO(runbook) |")
        lines.append("")
    lines.append(f"Primary site: {primary or 'TODO(sites)'}. Alternate site: "
                 f"{alternate or 'TODO(sites)'}.")
    return lines


def _site_names(answers: dict, todos: list[str]) -> tuple[str, str]:
    """Primary and alternate site names from Table 2.5, or empty strings and a TODO."""
    rows = answers.get("sites")
    if not isinstance(rows, list) or not rows:
        todos.append("sites")
        return "", ""
    primary = next((str(row.get("Site Name", "")) for row in rows
                    if str(row.get("Designation", "")).startswith("Primary")), "")
    alternate = next((str(row.get("Site Name", "")) for row in rows
                      if str(row.get("Designation", "")).startswith("Alternate")), "")
    if not (primary and alternate):
        todos.append("sites")
    return primary, alternate


def _answer_or_todo(answers: dict, question_id: str, todos: list[str]) -> str:
    value = answers.get(question_id)
    if _is_empty(value):
        todos.append(question_id)
        return f"TODO({question_id})"
    return _scalar(value)


# --------------------------------------------------------------------------- the document set

DOCUMENTS = ("iscp", "runbooks", "ci_inventory")


def render_all(answers: dict, cis: list[CI], documents: tuple[str, ...] = DOCUMENTS) -> RenderReport:
    """Every requested document as ``relative path -> content``. Same input, same bytes."""
    report = RenderReport()
    if "iscp" in documents:
        text, segments, unfilled = render_iscp(answers, cis)
        report.files["ISCP.md"] = text
        report.segments = segments
        report.unfilled = unfilled
    else:
        report.unfilled = unfilled_by_section(answers)
    if "runbooks" in documents:
        for slug, title, summary in RUNBOOKS:
            content, todos = render_runbook(slug, title, summary, cis, answers)
            report.files[f"runbooks/{slug}.md"] = content
            report.todos.extend(todos)
    if "ci_inventory" in documents:
        report.files["CI-inventory.md"] = render_ci_inventory(cis, answers)
        report.files["ci-inventory.json"] = json.dumps(
            {"schema": 1, "cis": [ci.to_dict() for ci in cis]}, indent=2, sort_keys=True) + "\n"
    return report


def load_cis(raw: list) -> list[CI]:
    """CI dataclasses from the answers file's ``cis`` list."""
    return [CI.from_dict(item) for item in raw if isinstance(item, dict)]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
