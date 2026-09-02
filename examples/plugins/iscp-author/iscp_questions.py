"""DATA ONLY: the question bank, one entry per fillable spot in the two source templates.

Every ``<Insert …>``, ``<Enter …>``, ``Click here to enter text.`` and ``Choose an item.`` in
the FedRAMP ISCP template, and every ``{insert …}`` in NIST SP 800-34 Rev. 1 Appendix B, maps
to exactly one ``Question`` here. Each one carries the ``placeholder`` the source prints at
that spot, which is what the renderer emits when the question is unanswered - the plugin never
substitutes a plausible default for a value the user did not give.

``template_ref`` names where the answer lands, so the interview can tell the user which table
or paragraph they are filling. Every ``template_ref`` is checked by a test against
``iscp_template.EXPECTED_HEADINGS`` and ``iscp_template.TABLE_TITLES``.

Deliberate deviations from ``docs/engineering/1.0-plan.md`` §2.2, all forced by the template
itself and all reported:

* Table 2.2 Backup System Components is a two-column ``System/Component | Description`` block
  with five fixed rows (Software Used, Hardware Used, Frequency, Backup Type, Retention
  Period), not the seven-column table the plan sketched, so it is five text questions and
  carries no CI prefill. CI prefill survives where the source really does want a resource
  list: BIA §3.2 and §3.3, and Table 2.5's sites.
* There is no ``person`` kind. Every person in this template appears as a row of Table A.1,
  Table 3.1 or Table B.1, so ``person`` would have no user and dead code is worse than a
  missing kind.
* BIA questions use section ``"L"`` (FedRAMP's appendix letter, and the section list the plan
  fixes) rather than ``L.3.1``-style sub-sections; the NIST sub-section is in ``template_ref``.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import iscp_template

#: FedRAMP's own "nothing has been entered here" token, used verbatim wherever the template
#: deletes an instruction box and leaves the author an empty section.
CLICK_HERE = "Click here to enter text."

#: NIST Appendix B's equivalent, used for unanswered BIA table cells.
NIST_INSERT = "{insert}"


@dataclass(frozen=True)
class Question:
    """One fillable spot in the template.

    ``kind`` is ``text | number | enum | table | list``. ``placeholder`` is the source's own
    text for this spot and is the *only* thing rendered when there is no answer. ``prefill``
    names a Configuration Item query (see :func:`iac_inventory.prefill_rows`) whose rows are
    offered to the user as a draft; a draft is never stored as an answer without the user
    saying so.
    """
    id: str
    section: str
    prompt: str
    template_ref: str
    placeholder: str
    kind: str = "text"
    required: bool = False
    unit: str = ""
    options: tuple[str, ...] = ()
    columns: tuple[str, ...] = ()
    enum_columns: dict[str, tuple[str, ...]] = field(default_factory=dict)
    prefill: str = ""


#: Site types, verbatim from Table 2.4 Alternative Site Types (plural, as the template writes
#: them), reused by Table 2.5's Site Type column and Table C.2's "Choose an item." row.
SITE_TYPES = ("Cold Sites", "Warm Sites", "Hot Sites", "Mirrored Sites")

#: The six checkboxes the template prints in Table F.1's Objectives row.
TEST_OBJECTIVES = (
    "Assess effectiveness of system recovery at alternate site",
    "Assess effectiveness of coordination among recovery teams",
    "Assess systems functionality using alternate equipment",
    "Assess performance of alternate equipment",
    "Assess effectiveness of procedures",
    "Assess effectiveness of notification procedures",
)

_T13 = "Table 1.3 <Insert CSO Name> and Title"
#: The template's cover page carries no heading of its own; ``iscp_template.COVER_REF``
#: names it so ``template_ref`` still resolves to something a test can check.
_COVER = iscp_template.COVER_REF
_PREPARED_BY = "Prepared by"
_PREPARED_FOR = "Prepared for"


QUESTIONS: tuple[Question, ...] = (
    # ------------------------------------------------------------------ 1.3 identity
    Question("csp.name", "1.3", "Cloud Service Provider (company) name", _COVER,
             "<Insert CSP Name>", required=True),
    Question("cso.name", "1.3", "Cloud Service Offering name", _T13, "<Insert CSO Name>",
             required=True),
    Question("cso.abbreviation", "1.3", "Information System Abbreviation for the offering", _T13,
             "ISA"),
    Question("cso.fedramp_id", "1.3",
             "FedRAMP Application Number (the CSO's unique FedRAMP ID), if one has been issued", _T13,
             "Enter FedRAMP Application Number (This is the CSO Unique FedRAMP ID.)"),
    Question("csp.poc", "1.3",
             "CSP designated point of contact for access to the SSP, diagrams and inventory workbook",
             "Appendix G Diagrams",
             "<Insert CSP Designated Point of Contact>"),
    Question("doc.version", "1.3", "Version of this ISCP document", _COVER, "<Insert Version X.X>"),
    Question("doc.date", "1.3", "Date of this ISCP document (MM/DD/YYYY)", _COVER,
             "<Insert MM/DD/YYYY>"),
    Question("prepared_by.organization", "1.3", "Organization that prepared this document", _PREPARED_BY,
             "<Enter Company/Organization>"),
    Question("prepared_by.street", "1.3", "Street address of the preparing organization", _PREPARED_BY,
             "<Enter Street Address>"),
    Question("prepared_by.suite", "1.3", "Suite/room/building of the preparing organization", _PREPARED_BY,
             "<Enter Suite/Room/Building>"),
    Question("prepared_by.city_state_zip", "1.3", "City, state and ZIP of the preparing organization",
             _PREPARED_BY, "<Enter City, State, and Zip Code>"),
    Question("prepared_for.organization", "1.3", "Cloud Service Provider this document is prepared for",
             _PREPARED_FOR, "<Enter Company/Organization>"),
    Question("prepared_for.street", "1.3", "Street address of the Cloud Service Provider", _PREPARED_FOR,
             "<Enter Street Address>"),
    Question("prepared_for.suite", "1.3", "Suite/room/building of the Cloud Service Provider", _PREPARED_FOR,
             "<Enter Suite/Room/Building>"),
    Question("prepared_for.city_state_zip", "1.3", "City, state and ZIP of the Cloud Service Provider",
             _PREPARED_FOR, "<Enter City, State, and Zip Code>"),
    Question("doc.revisions", "1.3", "Revision history of this ISCP", "Document Revision History",
             "<Revision Description>", kind="table",
             columns=("Date", "Description", "Version", "Author")),
    Question("approval1.name", "1.3", "First approver's name", "CONTINGENCY PLAN APPROVALS",
             "<Enter Name>"),
    Question("approval1.title", "1.3", "First approver's title", "CONTINGENCY PLAN APPROVALS",
             "<Enter Title>"),
    Question("approval1.date", "1.3", "First approver's signature date", "CONTINGENCY PLAN APPROVALS",
             "<Date>"),
    Question("approval2.name", "1.3", "Second approver's name", "CONTINGENCY PLAN APPROVALS",
             "<Enter Name>"),
    Question("approval2.title", "1.3", "Second approver's title", "CONTINGENCY PLAN APPROVALS",
             "<Enter Title>"),
    Question("approval2.date", "1.3", "Second approver's signature date", "CONTINGENCY PLAN APPROVALS",
             "<Date>"),
    Question("approval3.name", "1.3", "Third approver's name", "CONTINGENCY PLAN APPROVALS",
             "<Enter Name>"),
    Question("approval3.title", "1.3", "Third approver's title", "CONTINGENCY PLAN APPROVALS",
             "<Enter Title>"),
    Question("approval3.date", "1.3", "Third approver's signature date", "CONTINGENCY PLAN APPROVALS",
             "<Date>"),

    # ------------------------------------------------------------------ 1.4 scope
    Question("scope.impact_level", "1.4",
             "FIPS 199 impact level of the system (this is the categorisation already recorded in the "
             "SSP, not a new judgement)", "1.4 Scope", "<specify impact level>",
             kind="enum", required=True, options=("Low", "Moderate", "High")),
    Question("scope.rto_hours", "1.4",
             "Recovery Time Objective for the CSO, in hours. This is the business's number from the "
             "BIA, not an estimate of what the architecture can do", "1.4 Scope", "<Enter Number>",
             kind="number", required=True, unit="hours"),
    Question("scope.short_term_disruption", "1.4",
             "Duration below which a disruption is 'short-term' and out of this plan's scope",
             "1.4 Scope", "<Enter Number>"),
    Question("scope.other_plans", "1.4",
             "Other plans and circumstances related to but outside this ISCP's scope, beyond the BCP, "
             "COOP and OEP the template already lists", "Table 1.4 Plans Outside of ISCP Scope",
             CLICK_HERE, kind="table", columns=("Plan Name", "Mission/Purpose")),

    # ------------------------------------------------------------------ 1.5 assumptions
    Question("assumptions.ups_runtime", "1.5", "How long the UPS keeps the system running",
             "1.5 Assumptions", "<Enter Number>"),
    Question("assumptions.generator_start", "1.5",
             "How long after a power failure the generators start", "1.5 Assumptions", "<Enter Number>"),
    Question("assumptions.offsite_location", "1.5",
             "City and state of the offsite storage facility holding current backups", "1.5 Assumptions",
             "<Enter City, Enter State>"),

    # ------------------------------------------------------------------ 2.1 system description
    Question("system.description", "2.1",
             "General description of the system architecture and components, consistent with the SSP. "
             "Diagrams go in Appendix G", "2.1 System Description", CLICK_HERE),

    # ------------------------------------------------------------------ 2.3 backup readiness
    Question("backup.software", "2.3", "Backup software used",
             "Table 2.2 Backup System Components", CLICK_HERE),
    Question("backup.hardware", "2.3", "Backup hardware used",
             "Table 2.2 Backup System Components", CLICK_HERE),
    Question("backup.frequency", "2.3", "Backup frequency", "Table 2.2 Backup System Components",
             CLICK_HERE),
    Question("backup.type", "2.3",
             "Backup type in use (see Table 2.1 Backup Types: Full, Differential, Incremental, Mirror)",
             "Table 2.2 Backup System Components", CLICK_HERE),
    Question("backup.retention", "2.3", "Backup retention period",
             "Table 2.2 Backup System Components", CLICK_HERE),
    Question("backup.storage_site_name", "2.3", "Name of the offsite backup storage site",
             "Table 2.3 Back-Up Storage Location", CLICK_HERE),
    Question("backup.storage_street", "2.3", "Street address of the offsite backup storage site",
             "Table 2.3 Back-Up Storage Location", CLICK_HERE),
    Question("backup.storage_city_state_zip", "2.3",
             "City, state and ZIP of the offsite backup storage site",
             "Table 2.3 Back-Up Storage Location", CLICK_HERE),

    # ------------------------------------------------------------------ 2.4 site readiness
    Question("sites", "2.4",
             "Primary and alternate site locations. Site Type must be one of Table 2.4's four types",
             "Table 2.5 Primary and Alternative Site Locations", CLICK_HERE, kind="table",
             columns=("Designation", "Site Name", "Site Type", "Address"),
             enum_columns={"Site Type": SITE_TYPES}, prefill="regions"),

    # ------------------------------------------------------------------ 2.5 roles
    Question("roles.plc_purchase_limit", "2.5",
             "Purchase limit the Procurement and Logistics Coordinator may authorise for recovery "
             "operations", "2.5 Roles and Responsibilities", "Enter $ amount"),

    # ------------------------------------------------------------------ 3 activation
    Question("activation.authorized", "3.1",
             "Personnel authorised to activate the ISCP",
             "Table 3.1 Personnel Authorized to Activate the ISCP", CLICK_HERE, kind="table",
             columns=("Name", "Title and ISCP Role", "Contact Information")),
    Question("notification.procedures", "3.2",
             "Notification procedure: who makes the initial notification, the order in which personnel "
             "are notified, and the method (call tree, email blast, paging system, ...)",
             "3.2 Notification Instructions", CLICK_HERE),
    Question("outage.assessor_role", "3.3", "Role that conducts the outage assessment",
             "3.3 Outage Assessment", "<Insert Role Name>"),
    Question("outage.procedures", "3.3",
             "Outage assessment procedure: determining cause, potential for further damage, physical "
             "area affected, equipment status and inventory, items to replace, time to restore",
             "3.3 Outage Assessment", CLICK_HERE),

    # ------------------------------------------------------------------ 4 recovery
    Question("recovery.sequence", "4.1",
             "Sequence of recovery operations for this system. The template's six-step default is "
             "rendered until you replace it", "4.1 Sequence of Recovery Operations", CLICK_HERE,
             kind="list"),
    Question("recovery.procedures", "4.2",
             "General procedures for recovering the system from backup media, per team. If the "
             "keystroke-level steps live in the generated runbooks, say so here - the template requires "
             "this section to reference the appendix that holds them",
             "4.2 Recovery Procedures", CLICK_HERE),
    Question("recovery.escalation", "4.3",
             "Escalation notice procedures during recovery: the events, thresholds or triggers that "
             "require additional action, and who owns each",
             "4.3 Recovery Escalation Notices/Awareness", CLICK_HERE),

    # ------------------------------------------------------------------ 5 reconstitution
    Question("reconstitution.data_validation", "5.1",
             "Procedures for testing and validating that data is correct and current as of the last "
             "available backup, and who performs them", "5.1 Data Validation Testing", CLICK_HERE),
    Question("reconstitution.functional_validation", "5.2",
             "Procedures for testing the functional and operational aspects of the system",
             "5.2 Functional Validation Testing", CLICK_HERE),
    Question("reconstitution.declaring_role", "5.3",
             "Role that formally declares recovery efforts complete", "5.3 Recovery Declaration",
             "<Insert Role Name>"),
    Question("reconstitution.user_notification", "5.4",
             "Procedures for notifying users and customers after the recovery declaration, consistent "
             "with the SLAs and contracts", "5.4 User Notification", CLICK_HERE),
    Question("cleanup.procedures", "5.5", "Cleanup procedures and tasks", "5.5 Cleanup", CLICK_HERE),
    Question("cleanup.responsibilities", "5.5", "Who is responsible for which cleanup task",
             "Table 5.1 Cleanup Roles and Responsibilities", CLICK_HERE, kind="table",
             columns=("Role", "Cleanup Responsibilities")),
    Question("media_return.procedures", "5.6",
             "Procedures for returning retrieved backup or installation media to offsite storage",
             "5.6 Returning Backup Media", "<Inset Procedures>"),
    Question("restored_backup.procedures", "5.7",
             "Procedures for conducting a full system backup after recovery",
             "5.7 Backing-Up Restored Systems", "<Inset Procedures>"),
    Question("event_doc.responsibilities", "5.8",
             "Which role documents what during a recovery event",
             "Table 5.2 Event Documentation Responsibility", CLICK_HERE, kind="table",
             columns=("Role Name", "Documentation Responsibility")),

    # ------------------------------------------------------------------ 6 testing
    Question("testing.procedures", "6",
             "The procedures used to test this plan. Appendix F's summary refers back to this section "
             "for them", "6 Contingency Plan Testing", CLICK_HERE),

    # ------------------------------------------------------------------ appendices
    Question("contacts.key_personnel", "A",
             "Key personnel and Contingency Plan Team members, including alternates. Everyone here must "
             "receive a copy of the ISCP", "Table A.1 Key Personnel and Team Member Contact List",
             CLICK_HERE, kind="table", columns=("Role", "Name and Home Address", "Email", "Phone")),
    Question("contacts.vendors", "B", "Vendors associated with this ISCP",
             "Table B.1 Vendor Contact List", CLICK_HERE, kind="table",
             columns=("Vendor", "Product or Service License #, Contract #, Account #, or SLA", "Phone")),

    Question("alt_storage.address", "C.1", "Address of the alternate storage site",
             "Table C.1 Alternate Storage Site Information", CLICK_HERE),
    Question("alt_storage.distance", "C.1",
             "Distance of the alternate storage site from the primary facility",
             "Table C.1 Alternate Storage Site Information", CLICK_HERE),
    Question("alt_storage.ownership", "C.1",
             "Is the alternate storage facility owned by the organization or a third-party provider?",
             "Table C.1 Alternate Storage Site Information", CLICK_HERE),
    Question("alt_storage.poc", "C.1", "Points of contact at the alternate storage location",
             "Table C.1 Alternate Storage Site Information", CLICK_HERE),
    Question("alt_storage.delivery_schedule", "C.1",
             "Delivery schedule and media packaging procedures for the alternate storage facility",
             "Table C.1 Alternate Storage Site Information", CLICK_HERE),
    Question("alt_storage.retrieval_procedures", "C.1",
             "Procedures for retrieving media from the alternate storage facility",
             "Table C.1 Alternate Storage Site Information", CLICK_HERE),
    Question("alt_storage.authorized_personnel", "C.1",
             "Names and contact information for those authorised to retrieve media",
             "Table C.1 Alternate Storage Site Information", CLICK_HERE),
    Question("alt_storage.accessibility_problems", "C.1",
             "Potential accessibility problems reaching the alternate storage site during a widespread "
             "disruption", "Table C.1 Alternate Storage Site Information", CLICK_HERE),
    Question("alt_storage.mitigation_steps", "C.1",
             "Mitigation steps for reaching the alternate storage site during a widespread disruption",
             "Table C.1 Alternate Storage Site Information", CLICK_HERE),
    Question("alt_storage.data_types", "C.1",
             "Types of data held at the alternate storage site (databases, application software, "
             "operating systems, other critical software)",
             "Table C.1 Alternate Storage Site Information", CLICK_HERE),

    Question("alt_processing.address", "C.2", "Address of the alternate processing site",
             "Table C.2 Alternate Processing Site Information", CLICK_HERE),
    Question("alt_processing.distance", "C.2",
             "Distance of the alternate processing site from the primary facility",
             "Table C.2 Alternate Processing Site Information", CLICK_HERE),
    Question("alt_processing.ownership", "C.2",
             "Is the alternate processing site owned by the organization or a third-party provider?",
             "Table C.2 Alternate Processing Site Information", CLICK_HERE),
    Question("alt_processing.poc", "C.2", "Point of contact for the alternate processing site",
             "Table C.2 Alternate Processing Site Information", CLICK_HERE),
    Question("alt_processing.access_procedures", "C.2",
             "Procedures for accessing and using the alternate processing site, and its access security "
             "features", "Table C.2 Alternate Processing Site Information", CLICK_HERE),
    Question("alt_processing.authorized_personnel", "C.2",
             "Names and contact information for those authorised to go to the alternate processing site",
             "Table C.2 Alternate Processing Site Information", CLICK_HERE),
    Question("alt_processing.site_type", "C.2",
             "Type of alternate processing site, from Table 2.4 Alternative Site Types",
             "Table C.2 Alternate Processing Site Information", "Choose an item.", kind="enum",
             options=SITE_TYPES),
    Question("alt_processing.mitigation_steps", "C.2",
             "Mitigation steps for reaching the alternate processing site during a widespread disruption",
             "Table C.2 Alternate Processing Site Information", CLICK_HERE),

    Question("alt_telecom.vendors", "C.3",
             "Alternate telecommunications vendors, by priority, with contact information",
             "Table C.3 Alternate Telecommunications Provisions", CLICK_HERE),
    Question("alt_telecom.agreements", "C.3",
             "Agreements currently in place with alternate communications vendors",
             "Table C.3 Alternate Telecommunications Provisions", CLICK_HERE),
    Question("alt_telecom.capacity", "C.3", "Contracted capacity of alternate telecommunications",
             "Table C.3 Alternate Telecommunications Provisions", CLICK_HERE),
    Question("alt_telecom.authorized_personnel", "C.3",
             "Individuals authorised to implement or use alternate telecommunications",
             "Table C.3 Alternate Telecommunications Provisions", CLICK_HERE),

    Question("alt_processing_procedures", "D",
             "Alternate manual or technical processing procedures that let the business unit keep some "
             "processing going without the system (manual forms, queued input, ...). If none exist, say "
             "so", "Appendix D Alternate Processing Procedures", CLICK_HERE),

    Question("validation.test_plan", "E",
             "System acceptance procedures run after recovery and before the system returns to users. "
             "The template's sample plan is rendered until you replace it",
             "Table E.1 System Validation Test Plan", CLICK_HERE, kind="table",
             columns=("Procedure", "Expected Results", "Actual Results", "Successful?", "Performed by")),

    Question("test_report.name", "F", "Name of the last contingency plan test",
             "Table F.1 Contingency Plan Test Report", CLICK_HERE),
    Question("test_report.system_name", "F", "System name recorded on the test report",
             "Table F.1 Contingency Plan Test Report", CLICK_HERE),
    Question("test_report.date", "F", "Date of the test", "Table F.1 Contingency Plan Test Report",
             CLICK_HERE),
    Question("test_report.lead", "F", "Team test lead and point of contact",
             "Table F.1 Contingency Plan Test Report", CLICK_HERE),
    Question("test_report.location", "F", "Location where the test was conducted",
             "Table F.1 Contingency Plan Test Report", CLICK_HERE),
    Question("test_report.participants", "F", "Test participants",
             "Table F.1 Contingency Plan Test Report", CLICK_HERE),
    Question("test_report.components", "F", "Components exercised by the test",
             "Table F.1 Contingency Plan Test Report", CLICK_HERE),
    Question("test_report.assumptions", "F", "Assumptions the test made",
             "Table F.1 Contingency Plan Test Report", CLICK_HERE),
    Question("test_report.objectives", "F", "Test objectives (select all that apply)",
             "Table F.1 Contingency Plan Test Report",
             "Select all that apply: " + " ".join(TEST_OBJECTIVES), kind="list",
             options=TEST_OBJECTIVES),
    Question("test_report.methodology", "F", "Test methodology",
             "Table F.1 Contingency Plan Test Report", CLICK_HERE),
    Question("test_report.activities_results", "F",
             "Activities and results (action, expected results, actual results)",
             "Table F.1 Contingency Plan Test Report", CLICK_HERE),
    Question("test_report.post_test_actions", "F", "Post-test action items",
             "Table F.1 Contingency Plan Test Report", CLICK_HERE),
    Question("test_report.lessons_learned", "F", "Lessons learned and analysis of the test",
             "Table F.1 Contingency Plan Test Report", CLICK_HERE),
    Question("test_report.recommended_changes", "F",
             "Recommended changes to the contingency plan based on the test outcomes",
             "Table F.1 Contingency Plan Test Report", CLICK_HERE),

    Question("test_schedule", "J",
             "Schedule for reviewing and testing this plan, covering every ISCP security control "
             "requirement", "Appendix J Test and Maintenance Schedule", CLICK_HERE),

    Question("associated_plans", "K",
             "ISCPs for other systems that interconnect with or support this one",
             "Table K.1 Associated Plans and Procedures", CLICK_HERE, kind="table",
             columns=("System Name", "Plan Name")),

    # ------------------------------------------------------------------ Appendix L: the BIA
    Question("bia.completion_date", "L", "Date this BIA was prepared",
             "Appendix L Business Impact Analysis (NIST SP 800-34 Rev. 1 Appendix B §1 Overview)",
             "{insert BIA completion date}"),
    Question("bia.system_description", "L",
             "BIA system description: architecture and functionality, operating environment, physical "
             "location, general location of users, partnerships with external organizations",
             "Appendix L Business Impact Analysis (NIST §2 System Description)", CLICK_HERE),
    Question("bia.processes", "L",
             "Mission/business processes that depend on or support this system",
             "Appendix L Business Impact Analysis (NIST §3.1 Determine Process and System Criticality)",
             NIST_INSERT, kind="table", columns=("Mission/Business Process", "Description")),
    Question("bia.impact_categories", "L",
             "Impact categories and the values that mark Severe, Moderate and Minimal for each. The "
             "cost example in NIST's template is a sample, not a default - use your organisation's own "
             "categories",
             "Appendix L Business Impact Analysis (NIST §3.1.1 Identify Outage Impacts and Estimated "
             "Downtime)", NIST_INSERT, kind="table",
             columns=("Impact category", "Severe", "Moderate", "Minimal")),
    Question("bia.process_impacts", "L",
             "Impact on each mission/business process, per impact category, if the system were "
             "unavailable",
             "Appendix L Business Impact Analysis (NIST §3.1.1 Identify Outage Impacts and Estimated "
             "Downtime)", NIST_INSERT, kind="table", columns=("Mission/Business Process", "Impact")),
    Question("bia.downtime", "L",
             "MTD, RTO and RPO for each mission/business process, in hourly increments. RTO must "
             "normally be shorter than MTD (NIST SP 800-34 Rev. 1 §3.2.1). These are the business's "
             "numbers",
             "Appendix L Business Impact Analysis (NIST §3.1.1 Identify Outage Impacts and Estimated "
             "Downtime)", NIST_INSERT, kind="table",
             columns=("Mission/Business Process", "MTD", "RTO", "RPO")),
    Question("bia.downtime_drivers", "L",
             "Drivers for the MTD, RTO and RPO values above (mandate, workload, performance measure, "
             "...)",
             "Appendix L Business Impact Analysis (NIST §3.1.1 Identify Outage Impacts and Estimated "
             "Downtime)", CLICK_HERE),
    Question("bia.alternate_means", "L",
             "Alternate means (secondary processing or manual work-around) for recovering the "
             "mission/business processes that rely on the system. If none exist, so state",
             "Appendix L Business Impact Analysis (NIST §3.1.1 Identify Outage Impacts and Estimated "
             "Downtime)", CLICK_HERE),
    Question("bia.resources", "L",
             "System resources that compose the system: hardware, software and other resources such as "
             "data files", "Appendix L Business Impact Analysis (NIST §3.2 Identify Resource "
             "Requirements)", NIST_INSERT, kind="table",
             columns=("System Resource/Component", "Platform/OS/Version (as applicable)", "Description"),
             prefill="cis:*"),
    Question("bia.priorities", "L",
             "Order of recovery for system resources, with the expected time to recover each following "
             "a worst-case disruption",
             "Appendix L Business Impact Analysis (NIST §3.3 Identify Recovery Priorities for System "
             "Resources)", NIST_INSERT, kind="table",
             columns=("Priority", "System Resource/Component", "Recovery Time Objective"),
             prefill="cis:*"),
    Question("bia.alternate_strategies", "L",
             "Alternate strategies in place to meet the expected RTOs: backup or spare equipment, "
             "vendor support contracts",
             "Appendix L Business Impact Analysis (NIST §3.3 Identify Recovery Priorities for System "
             "Resources)", CLICK_HERE),
)


#: The template sections the interview walks, in the template's own order. Fixed by
#: ``docs/engineering/1.0-plan.md`` §2.2; a test asserts every question's section is one of
#: these and that every one of these has at least one question.
SECTIONS: tuple[str, ...] = (
    "1.3", "1.4", "1.5", "2.1", "2.3", "2.4", "2.5", "3.1", "3.2", "3.3", "4.1", "4.2", "4.3",
    "5.1", "5.2", "5.3", "5.4", "5.5", "5.6", "5.7", "5.8", "6",
    "A", "B", "C.1", "C.2", "C.3", "D", "E", "F", "J", "K", "L",
)

BY_ID: dict[str, Question] = {question.id: question for question in QUESTIONS}


def question(question_id: str) -> Question | None:
    return BY_ID.get(question_id)


def for_section(section: str) -> list[Question]:
    """Questions in one section, in template order. ``section`` is matched exactly."""
    return [q for q in QUESTIONS if q.section == section]
