"""DATA ONLY: the verbatim text of the two source templates this plugin renders.

Everything in this module was transcribed from a primary source. Nothing here is
paraphrased, improved, summarised or invented, because a generated document that *looks*
like a FedRAMP ISCP but carries a structure of our own making fails assessor review and
costs the reader more time than shipping nothing would.

Sources
-------
* ``ISCP_BLOCKS`` - **FedRAMP SSP Appendix G: Information System Contingency Plan (ISCP)
  Template, version 5.0, dated 12/06/2024** ("Updated to align with OMB Memo M-24-15 and
  remove PMO references", the newest row of the template's own revision history).
  Downloaded 2026-09-02 from
  ``https://www.fedramp.gov/resources/templates/SSP-Appendix-G-Information-System-Contingency-Plan-(ISCP)-Template.docx``
  (HTTP 200, 153865 bytes, md5 298f6b1392ee21b1cded5164c2523b86) and read out of
  ``word/document.xml`` with ``zipfile`` + ``xml.etree``, keeping paragraph styles so that
  headings, table titles and the template's *instructional* paragraphs stay distinguishable.
* ``BIA_BLOCKS`` - **NIST SP 800-34 Rev. 1, Appendix B, "Sample Business Impact Analysis
  (BIA) and BIA Template"**, pages B-1 to B-4, May 2010 (errata 2010-11-11),
  ``https://nvlpubs.nist.gov/nistpubs/Legacy/SP/nistspecialpublication800-34r1.pdf``.
  FedRAMP's Appendix L is one instruction - "Insert the Business Impact Analysis here.
  Please see NIST SP 800-34, Revision 1 for more information on how to conduct a Business
  Impact Analysis." - so the BIA's structure has to come from NIST, and it does.

Two editorial rules, applied consistently, that a reviewer should check:

1. **Instructional text is dropped.** Every FedRAMP "Instructions:" box ends with "Delete
   this and all other instructional text from your final version of this document", so it is
   not reproduced. NIST's equivalent is its italic guidance ("In this template, words in
   *italics* are for guidance only and should be deleted from the final version. Regular
   (non-italic) text is intended to remain."). Boilerplate the template supplies as *final*
   text - the Three Phases description, the role duty lists, Table 2.1 Backup Types, Table 2.4
   Alternative Site Types, the eight role headings - is reproduced in full.
2. **NIST's worked examples are dropped.** Appendix B illustrates its tables with a sample
   organisation ("Pay vendor invoice", "Web Server 1 / Optiplex GX280 / 24 hours to rebuild
   or replace"). Those are illustrations, not defaults; emitting them into a real plan would
   put a fictional business process in front of an assessor. Unanswered BIA table rows render
   NIST's own ``{insert}`` placeholder instead.

Text may contain slots written ``{question.id}`` or ``{question.id|<alternate placeholder>}``.
A slot renders the user's answer, or - when unanswered - the placeholder the template itself
prints at that spot (``iscp_questions.Question.placeholder``, overridden by the text after
``|`` where the template varies its wording between two occurrences of the same field).
Braces that do not name a known question id are literal, which matters because NIST writes
its own placeholders as ``{system name}`` and ``{insert}``.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Heading:
    """A numbered heading, verbatim from the template. ``level`` is the Markdown depth."""
    level: int
    text: str


@dataclass(frozen=True)
class Para:
    """One template paragraph."""
    text: str


@dataclass(frozen=True)
class Bullets:
    """A template bullet list. ``question`` names a list-kind question that replaces it."""
    items: tuple[str, ...]
    question: str = ""


@dataclass(frozen=True)
class Table:
    """One template table.

    ``title`` is the template's own caption ("Table 2.2 Backup System Components") or "" for
    the untitled ones. ``header`` is its column header row, verbatim. ``rows`` are rows the
    template supplies as final content (Table 2.1's backup-type definitions) or as
    label/value pairs whose value cell holds a slot. ``question`` names a table-kind question
    whose rows are appended; ``template_rows`` are what the template prints in that question's
    place when it is unanswered.
    """
    title: str
    header: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...] = ()
    question: str = ""
    template_rows: tuple[tuple[str, ...], ...] = ()
    #: When set, the effective columns are Mission/Business Process + this question's first
    #: column + "Impact" - NIST's impact summary table is as wide as the categories chosen.
    columns_from: str = ""


Block = Heading | Para | Bullets | Table


# --------------------------------------------------------------------------- front matter

#: The template's cover page carries no heading of its own, so questions that land there name
#: it with this constant rather than a heading text.
COVER_REF = "Cover page"

COVER: tuple[Block, ...] = (
    Para("FedRAMP® System Security Plan (SSP)"),
    Para("Appendix G: Information System Contingency Plan (ISCP) Template"),
    Para("for {csp.name}"),
    Para("{cso.name}"),
    Para("{doc.version}"),
    Para("{doc.date}"),
    Para("info@fedramp.gov"),
    Para("fedramp.gov"),

    Heading(1, "Prepared by"),
    Table("", ("Identification of Organization that Prepared this Document", ""),
          (("Organization Name", "{prepared_by.organization}"),
           ("Street Address", "{prepared_by.street}"),
           ("Suite/Room/Building", "{prepared_by.suite}"),
           ("City, State, Zip", "{prepared_by.city_state_zip}"))),

    Heading(1, "Prepared for"),
    Table("", ("Identification of Cloud Service Provider", ""),
          (("Organization Name", "{prepared_for.organization}"),
           ("Street Address", "{prepared_for.street}"),
           ("Suite/Room/Building", "{prepared_for.suite}"),
           ("City, State, Zip", "{prepared_for.city_state_zip}"))),

    Heading(1, "Document Revision History"),
    Table("", ("Date", "Description", "Version", "Author"), (),
          "doc.revisions",
          (("<Date>", "<Revision Description>", "<Version>", "<Author>"),
           ("<Date>", "<Revision Description>", "<Version>", "<Author>"),
           ("<Date>", "<Revision Description>", "<Version>", "<Author>"))),

    Heading(1, "CONTINGENCY PLAN APPROVALS"),
    Table("", ("<Sign Here>", ""),
          (("Name", "{approval1.name}"), ("Date", "{approval1.date}"),
           ("Title", "{approval1.title}"), ("Cloud Service Provider", "{csp.name|<CSP Name>}"))),
    Table("", ("<Sign Here>", ""),
          (("Name", "{approval2.name}"), ("Date", "{approval2.date}"),
           ("Title", "{approval2.title}"), ("Cloud Service Provider", "{csp.name|<CSP Name>}"))),
    Table("", ("<Sign Here>", ""),
          (("Name", "{approval3.name}"), ("Date", "{approval3.date}"),
           ("Title", "{approval3.title}"), ("Cloud Service Provider", "{csp.name|<CSP Name>}"))),
)


# --------------------------------------------------------------------------- sections 1-6

SECTION_1: tuple[Block, ...] = (
    Heading(1, "1 Introduction and Purpose"),
    Para("Information systems are vital to {csp.name} mission/business functions; therefore, it is "
         "critical that services provided by {cso.name} are able to operate effectively without "
         "excessive interruption. This Information Security Contingency Plan (ISCP) establishes "
         "comprehensive procedures to recover {cso.name} quickly and effectively following a service "
         "disruption."),
    Para("One of the goals of an ISCP is to establish procedures and mechanisms that obviate the need to "
         "resort to performing IT functions using manual methods. If manual methods are the only "
         "alternative, however, every effort must be made to continue IT functions and processes "
         "manually."),
    Para("The nature of unprecedented disruptions can create confusion, and often predisposes an "
         "otherwise competent IT staff towards less efficient practices. In order to maintain a normal "
         "level of efficiency, it is important to decrease real-time process engineering by documenting "
         "notification and activation guidelines and procedures, recovery guidelines and procedures, and "
         "reconstitution guidelines and procedures prior to the occurrence of a disruption. During the "
         "notification/activation phase, appropriate personnel are apprised of current conditions and "
         "damage assessment begins. During the recovery phase, appropriate personnel take a course of "
         "action to recover the {cso.name} components to a site other than the one that experienced the "
         "disruption. In the final, reconstitution phase, actions are taken to restore IT system "
         "processing capabilities to normal operations."),

    Heading(2, "1.1 Applicable Laws and Regulations"),
    Para("A summary of {cso.name}-specific laws and regulations are available in the System Security "
         "Plan (SSP), Appendix L, {cso.name}-specific Required Laws and Regulations."),

    Heading(2, "1.2 FedRAMP Requirements and Guidance"),
    Para("All FedRAMP documents are available at www.fedramp.gov"),
    Bullets(("FedRAMP Incident Communications Procedure",
             "FedRAMP Continuous Monitoring Strategy Guide")),

    Heading(2, "1.3 {cso.name} and Identifier"),
    Para("This ISCP applies to the {cso.name} (Information System Abbreviation), which has a unique "
         "identifier as noted in Table 1.3 {cso.name} and Title."),
    Table("Table 1.3 {cso.name} and Title",
          ("Unique Identifier", "Cloud Service Offering Name", "Information System Abbreviation"),
          (("{cso.fedramp_id}", "{cso.name}", "{cso.abbreviation}"),)),

    Heading(2, "1.4 Scope"),
    Para("This ISCP has been developed for {cso.name}, which is classified as a "
         "{scope.impact_level} impact system, in accordance with Federal Information Processing "
         "Standards (FIPS) 199. FIPS 199 provides guidelines on determining potential impact to "
         "organizational operations and assets, and individuals through a formula that examines three "
         "security objectives: confidentiality, integrity, and availability. The procedures in this ISCP "
         "have been developed for a {scope.impact_level|<specify level>} impact system and are designed "
         "to recover the CSO within Recovery Time Objective (RTO) {scope.rto_hours} hours. The "
         "replacement or purchase of new equipment, short-term disruptions lasting less than "
         "{scope.short_term_disruption}, or loss of data at the primary facility or at the user-desktop "
         "levels is outside the scope of this plan."),
    Para("Table 1.4 Plans Outside of ISCP Scope below identifies other plans and circumstances that are "
         "related but are outside the scope of this ISCP."),
    Table("Table 1.4 Plans Outside of ISCP Scope", ("Plan Name", "Mission/Purpose"),
          (("Business Continuity Plan (BCP)",
            "Overall recovery and continuity of mission/business operations"),
           ("Continuity of Operations Plan (COOP)",
            "Overall recovery and continuity of mission/business operations"),
           ("The Occupant Emergency Plan (OEP)", "Emergency evacuation of personnel")),
          "scope.other_plans",
          (("Click here to enter text.", "Click here to enter text."),
           ("Click here to enter text.", "Click here to enter text."))),

    Heading(2, "1.5 Assumptions"),
    Para("The following assumptions have been made about the {cso.name}:"),
    Bullets(("The Uninterruptible Power Supply (UPS) will keep the system up and running after "
             "{assumptions.ups_runtime}",
             "The generators will kick in after {assumptions.generator_start} from the time of a power "
             "failure.",
             "Current backups of the application software and data are intact and available at the "
             "offsite storage facility in {assumptions.offsite_location}.",
             "The backup storage capability is approved and has been accepted by the Authorizing "
             "Official (AO).",
             "The {cso.name} is inoperable if it cannot be recovered within {scope.rto_hours} RTO hours.",
             "Key personnel have been identified and are trained annually in their roles.",
             "Key personnel are available to activate the ISCP.",
             "CSP Name defines circumstances that can inhibit recovery and reconstitution to a known "
             "state.")),
)

SECTION_2: tuple[Block, ...] = (
    Heading(1, "2 Concept of Operations"),
    Para("This section provides details about the {cso.name}, an overview of the three phases of the "
         "ISCP (Activation and Notification, Recovery, and Reconstitution), and a description of the "
         "roles and responsibilities of key personnel during contingency operations."),

    Heading(2, "2.1 System Description"),
    Para("{system.description}"),

    Heading(2, "2.2 Three Phases"),
    Para("This plan has been developed to recover and reconstitute the {cso.name} using a three-phased "
         "approach. The approach ensures that system recovery and reconstitution efforts are performed "
         "in a methodical sequence to maximize the effectiveness of the recovery and reconstitution "
         "efforts and minimize system outage time due to errors and omissions. The three system recovery "
         "phases consist of activation and notification, recovery, and reconstitution."),
    Bullets(("Activation and Notification Phase. Activation of the ISCP occurs after a disruption, "
             "outage, or disaster that may reasonably extend beyond the RTO established for a system. "
             "The outage event may result in severe damage to the facility that houses the system, "
             "severe damage or loss of equipment, or other damage that typically results in long-term "
             "loss. Once the ISCP is activated, the information system stakeholders are notified of a "
             "possible long-term outage, and a thorough outage assessment is performed for the "
             "information system. Information from the outage assessment is analyzed and may be used to "
             "modify recovery procedures specific to the cause of the outage.",
             "Recovery Phase. The Recovery phase details the activities and procedures for recovery of "
             "the affected system. Activities and procedures are written at a level such that an "
             "appropriately skilled technician can recover the system without intimate system knowledge. "
             "This phase includes notification and awareness escalation procedures for communication of "
             "recovery status to system stakeholders.",
             "Reconstitution. The Reconstitution phase defines the actions taken to test and validate "
             "system capability and functionality at the original or new permanent location. This phase "
             "consists of two major activities: validating data and operational functionality followed "
             "by deactivation of the plan.")),
    Para("During validation, the system is tested and validated as operational prior to returning "
         "operation to its normal state. Validation procedures include functionality or regression "
         "testing, concurrent processing, and/or data validation. The system is declared recovered and "
         "operational upon successful completion of validation testing."),
    Para("Deactivation includes activities to notify users of system operational status. This phase also "
         "addresses recovery effort documentation, activity log finalization, incorporation of lessons "
         "learned into plan updates, and readying resources for any future events."),

    Heading(2, "2.3 Data Backup Readiness Information"),
    Para("A common understanding of data backup definitions is necessary in order to ensure that data "
         "restoration is successful. {cso.name} recognizes different types of backups, which have "
         "different purposes, and those definitions are found in Table 2.1 Backup Types."),
    Table("Table 2.1 Backup Types", ("Backup Type", "Description"),
          (("Full Backup",
            "A full backup is the starting point for all other types of backup and contains all the data "
            "in the folders and files that are selected to be backed up. Because full backup stores all "
            "files and folders, frequent full backups result in faster and simpler restore operations."),
           ("Differential Backup",
            "Differential backup contains all files that have changed since the last FULL backup. The "
            "advantage of a differential backup is that it shortens restore time compared to a full "
            "backup or an incremental backup. However, if the differential backup is performed too many "
            "times, the size of the differential backup might grow to be larger than the baseline full "
            "backup."),
           ("Incremental Backup",
            "Incremental backup stores all files that have changed since the last FULL, DIFFERENTIAL OR "
            "INCREMENTAL backup. The advantage of an incremental backup is that it takes the least time "
            "to complete. However, during a restore operation, each incremental backup must be "
            "processed, which may result in a lengthy restore job."),
           ("Mirror Backup",
            "Mirror backup is identical to a full backup, with the exception that the files are not "
            "compressed in zip files and they cannot be protected with a password. A mirror backup is "
            "most frequently used to create an exact copy of the source data."))),
    Para("The hardware and software components used to create the {cso.name} backups are noted in Table "
         "2.2 Backup System Components."),
    Table("Table 2.2 Backup System Components", ("System/Component", "Description"),
          (("Software Used", "{backup.software}"),
           ("Hardware Used", "{backup.hardware}"),
           ("Frequency", "{backup.frequency}"),
           ("Backup Type", "{backup.type}"),
           ("Retention Period", "{backup.retention}"))),
    Para("Table 2.3 Back-Up Storage Location shows the offsite storage facility location of current "
         "backups of the {cso.name} system software and data."),
    Table("Table 2.3 Back-Up Storage Location", ("Backup Storage", ""),
          (("Site Name", "{backup.storage_site_name}"),
           ("Street Address", "{backup.storage_street}"),
           ("City, State, Zip Code", "{backup.storage_city_state_zip}"))),
    Para("Personnel who are authorized to retrieve backups from the offsite storage location, and may "
         "authorize the delivery of backups, are noted in Appendix C – Section 1: Alternate Storage Site "
         "Information."),
    Bullets(("{cso.name} maintains both an online and offline (portable) set of backup copies of the "
             "following types of data on site at their primary location:",
             "User-level information",
             "System-level information",
             "Information system documentation including security information.")),

    Heading(2, "2.4 Site Readiness Information"),
    Para("CSP Name recognizes different types of alternate sites, which are defined in Table 2.4 "
         "Alternative Site Types."),
    Table("Table 2.4 Alternative Site Types", ("Type of Site", "Description"),
          (("Cold Sites",
            "Cold Sites are typically facilities with adequate space and infrastructure (electric power, "
            "telecommunications connections, and environmental controls) to support information system "
            "recovery activities."),
           ("Warm Sites",
            "Warm Sites are partially equipped office spaces that contain some or all of the system "
            "hardware, software, telecommunications, and power sources."),
           ("Hot Sites",
            "Hot Sites are facilities appropriately sized to support system requirements and configured "
            "with the necessary system hardware, supporting infrastructure, and support personnel."),
           ("Mirrored Sites",
            "Mirrored Sites are fully redundant facilities with automated real-time information "
            "mirroring. Mirrored sites are identical to the primary site in all technical respects."))),
    Para("Alternate facilities have been established for the {cso.name} as noted in Table 2.4 "
         "Alternative Site Types."),
    Table("Table 2.5 Primary and Alternative Site Locations",
          ("Designation", "Site Name", "Site Type", "Address"), (), "sites",
          (("Primary Site", "", "", ""), ("Alternate Site", "", "", ""), ("Alternate Site", "", "", ""))),

    Heading(2, "2.5 Roles and Responsibilities"),
    Para("CSP Name establishes multiple roles and responsibilities for responding to outages, "
         "disruptions, and disasters for the {cso.name}. Individuals who are assigned roles for recovery "
         "operations collectively make up the Contingency Plan Team and are trained annually in their "
         "duties. Contingency Plan Team members are chosen based on their skills and knowledge."),
    Para("The Contingency Plan Team consists of personnel who have been selected to perform the roles "
         "and responsibilities described in the sections that follow. All team leads are considered key "
         "personnel."),

    Heading(3, "Contingency Planning Director (CPD)"),
    Para("The Contingency Planning Director (CPD) is a member of senior management and owns the "
         "responsibility for all facets of contingency and disaster recovery planning and execution."),
    Para("The CPD performs the following duties:"),
    Bullets(("Makes the decision on whether or not to activate the ISCP",
             "Provides the initial notification to activate the ISCP",
             "Reviews and approves the ISCP",
             "Reviews and approves the Business Impact Analysis (BIA)",
             "Notifies the Contingency Plan Team leads and members as necessary",
             "Advises other Contingency Plan Team leads and members as appropriate",
             "Issues a recovery declaration statement after the system has returned to normal operations",
             "Designates key personnel")),

    Heading(3, "Contingency Planning Coordinator (CPC)"),
    Para("The CPC performs the following duties:"),
    Bullets(("Develops and documents the ISCP under direction of the CPD",
             "Uses the BIA to prioritize recovery of components",
             "Updates the ISCP annually",
             "Ensures that annual ISCP training is conducted",
             "Facilitates periodic ISCP testing exercises",
             "Distributes copies of the plan to team members",
             "Authorizes travel and housing arrangements for team members",
             "Manages and monitors the overall recovery process",
             "Leads the contingency response effort once the plan has been activated",
             "Advises the Procurement and Logistics Coordinator on whether to order new equipment",
             "Receives updates and status reports from team members",
             "Sends out communications about the recovery",
             "Advises the CPD on status as necessary",
             "Designates key personnel")),

    Heading(3, "Outage and Damage Assessment Lead (ODAL)"),
    Para("The ODAL performs the following duties:"),
    Bullets(("Determines if there has been loss of life or injuries",
             "Assesses the extent of damage to the facilities and the information systems",
             "Estimates the time to recover operations",
             "Determines accessibility to facility, building, offices, and work areas",
             "Assesses the need for and adequacy of physical security/guards",
             "Advises the Security Coordinator that physical security/guards are required",
             "Identifies salvageable hardware",
             "Maintains a log/record of all salvageable equipment",
             "Supports the cleanup of the data center following an incident",
             "Develops and maintains a Damage Assessment Plan",
             "Estimates levels of outside assistance required",
             "Reports updates, status, and recommendations to the CPC",
             "Designates key personnel")),

    Heading(3, "Hardware Recovery Team (HRT)"),
    Para("The hardware recovery team performs the following duties:"),
    Bullets(("Installs hardware and connects power",
             "Runs cables and wiring as necessary",
             "Makes arrangements to move salvageable hardware to other locations as necessary",
             "Ensures electrical panels are operational",
             "Ensures that fire suppression system is operational",
             "Communicates with hardware vendors as needed (Appendix B – Vendor Contact List)",
             "Creates log of missing and required hardware",
             "Advises the PLC if new hardware should be purchased",
             "Connects network cables",
             "Connects wireless access points")),

    Heading(3, "Software Recovery Team (SRT)"),
    Para("The software recovery team performs the following duties:"),
    Bullets(("Installs software on all systems at alternate site",
             "Performs live migrations to alternate site prior to predictable disasters and outages",
             "Installs servers in the order described in the BIA (Appendix L – Business Impact Analysis)",
             "Communicate with software vendors as needed (Appendix B – Vendor Contact List)",
             "Advises the PLC if new software needs to be purchased",
             "Creates log of software installation problems",
             "Restore systems from most current backup media",
             "Maintains current system software configuration information in an off-site storage "
             "facility")),

    Heading(3, "Telecommunications Team (TC)"),
    Para("The Telecomm team performs the following duties:"),
    Bullets(("Assesses the need for alternative communications",
             "Communicates Internet connectivity requirements with providers",
             "Communicates with telephone vendors as needed",
             "Establishes communications between the alternate site and the users",
             "Coordinates transportation of salvageable telecom equipment to the alternative site",
             "Plans for procuring new hardware and telecommunication equipment",
             "Advises the PLC if new equipment needs to be purchased",
             "Retrieves communications configuration from the off-site storage facility",
             "Plans, coordinates and installs communication equipment as needed at the alternate site",
             "Maintains plan for installing and configuring VOIP",
             "Maintains current telecommunications configuration information at off-site storage "
             "facility")),

    Heading(3, "Procurement and Logistics Coordinator (PLC)"),
    Para("The PLC performs the following duties:"),
    Bullets(("Procures new equipment and supplies as necessary",
             "Prepares, coordinates, and obtains approval for all procurement requests",
             "Authorizes purchases up to {roles.plc_purchase_limit} for recovery operations",
             "Ensures that equipment and supplies are delivered to locations",
             "Coordinates deliveries",
             "Updates the CPC with status",
             "Works with the CPC to provide transportation for staff as needed",
             "Ensures details of administering emergency funds expenditures are known.",
             "Processes requests for payment of all invoices related to the incident",
             "Arranging for travel and lodging of staff to the alternate site as needed",
             "Procures telephone equipment and leased lines as needed",
             "Procures alternate communications for teams as needed")),

    Heading(3, "Security Coordinator (SC)"),
    Para("The Security Coordinator performs the following duties:"),
    Bullets(("Provides training for employees in facility emergency procedures and measures",
             "Provides physical security, access control, and safety measures to support recovery effort",
             "Cordons off the facility including offices to restrict unauthorized access",
             "Coordinates with the building management and the CPC for authorized personnel access",
             "Coordinates and manages additional physical security/guards as needed",
             "Acts as a liaison with emergency personnel, such as fire and police departments",
             "Provides assistance to officials in investigating the damaged facility/site",
             "Ensures that data room/center at alternate site has locks (access controls) on the doors",
             "Coordinates and secures the transportation of files, reports, and equipment in "
             "coordination with the CPC")),

    Heading(3, "Plan Distribution and Availability"),
    Para("During a disaster situation, the availability of the contingency plan is essential to the "
         "success of the restoration efforts. The Contingency Plan Team has immediate access to the plan "
         "upon notification of an emergency. The Contingency Plan Coordinator ensures that a copy of the "
         "most current version of the Contingency Plan is maintained at CSP Name’s facility. This plan "
         "has been distributed to all personnel listed in Appendix A – Key Personnel and Team Member "
         "Contact List."),
    Para("Contingency Plan Team members are obligated to inform the Contingency Planning Coordinator, if "
         "and when, they no longer require a copy of the plan. In addition, each recipient of the plan "
         "is obligated to return or destroy any portion of the plan that is no longer needed and upon "
         "termination from {csp.name}."),

    Heading(3, "Line of Succession/Alternates Roles"),
    Para("The {csp.name} sets forth an order of succession, in coordination with the order set forth by "
         "the organization to ensure that decision-making authority for the {cso.name} ISCP is "
         "uninterrupted."),
    Para("In order to preserve the continuity of operations, individuals designated as key personnel "
         "have been assigned an individual who can assume the key personnel’s position if the key "
         "personnel is not able to perform their duties. Alternate key personnel are named in a line of "
         "succession and are notified and trained to assume their alternate role, should the need arise. "
         "The line of succession for key personnel can be found in Appendix A – Key Personnel and Team "
         "Member Contact List."),
)

SECTION_3: tuple[Block, ...] = (
    Heading(1, "3 Activation and Notification"),
    Para("The activation and notification phase defines initial actions taken once the {cso.name} "
         "disruption has been detected or appears to be imminent. This phase includes activities to "
         "notify recovery personnel, conduct an outage assessment, and activate the ISCP."),
    Para("At the completion of the Activation and Notification Phase, key {cso.name} ISCP staff will be "
         "prepared to perform recovery measures to restore system functions."),

    Heading(2, "3.1 Activation Criteria and Procedure"),
    Para("The {cso.name} ISCP may be activated if one or more of the following criteria are met:"),
    Bullets(("The type of outage indicates {cso.name} will be down for more than {scope.rto_hours} RTO "
             "hours.",
             "The facility housing {cso.name} is damaged and may not be available within "
             "{scope.rto_hours} RTO hours",
             "Other criteria, as appropriate.")),
    Para("Personnel/roles listed in Table 3.1 Personnel Authorized to Activate the ISCP are authorized "
         "to activate the ISCP."),
    Table("Table 3.1 Personnel Authorized to Activate the ISCP",
          ("Name", "Title and ISCP Role", "Contact Information"), (), "activation.authorized",
          (("Click here to enter text.", "Click here to enter text.", "Click here to enter text."),
           ("Click here to enter text.", "Click here to enter text.", "Click here to enter text."),
           ("Click here to enter text.", "Click here to enter text.", "Click here to enter text."))),

    Heading(2, "3.2 Notification Instructions"),
    Para("{notification.procedures}"),
    Para("Contact information for key personnel is located in Appendix A – Key Personnel and Team Member "
         "Contact List."),

    Heading(2, "3.3 Outage Assessment"),
    Para("Following notification, a thorough outage assessment is necessary to determine the extent of "
         "the disruption, any damage, and expected recovery time. This outage assessment is conducted by "
         "{outage.assessor_role}. Assessment results are provided to the Contingency Planning "
         "Coordinator to assist in the coordination of the recovery effort."),
    Para("{outage.procedures}"),
)

SECTION_4: tuple[Block, ...] = (
    Heading(1, "4 Recovery"),
    Para("The recovery phase provides formal recovery operations that begin after the ISCP has been "
         "activated, outage assessments have been completed (if possible), personnel have been notified, "
         "and appropriate teams have been mobilized. Recovery phase activities focus on implementing "
         "recovery strategies to restore system capabilities, repair damage, and resume operational "
         "capabilities at the original or an alternate location. At the completion of the recovery "
         "phase, {cso.name} will be functional and capable of performing the functions identified in "
         "Section 4.1 Sequence of Recovery Operations of the plan."),

    Heading(2, "4.1 Sequence of Recovery Operations"),
    Para("The following activities occur during recovery of {cso.name}:"),
    Bullets(("Identification of recovery location (if not at original location)",
             "Identification of required resources to perform recovery procedures",
             "Retrieval of backup and system installation media",
             "Recovery of hardware and operating system (if required)",
             "Recovery of system from backup and system installation media",
             "Implementation of transaction recovery for systems that are transaction-based"),
            "recovery.sequence"),

    Heading(2, "4.2 Recovery Procedures"),
    Para("The following procedures are provided for recovery of {cso.name} at the original or "
         "established alternate location. Recovery procedures are outlined per team and must be executed "
         "in the sequence presented to maintain an efficient recovery effort."),
    Para("{recovery.procedures}"),

    Heading(2, "4.3 Recovery Escalation Notices/Awareness"),
    Para("Notifications during recovery include problem escalation to leadership and status awareness to "
         "system owners and users. This section describes the procedures for handling escalation notices "
         "that define and describe the events, thresholds, or other types of triggers that may require "
         "additional action."),
    Para("{recovery.escalation}"),
)

SECTION_5: tuple[Block, ...] = (
    Heading(1, "5 Reconstitution"),
    Para("Reconstitution is the process by which recovery activities are completed and normal system "
         "operations are resumed. If the original facility is unrecoverable, the activities in this "
         "phase can also be applied to preparing a new permanent location to support system processing "
         "requirements. A determination must be made whether the system has undergone significant change "
         "and will require reassessment and reauthorization. The phase consists of two major activities: "
         "(1) validating successful reconstitution and (2) deactivation of the plan."),
    Para("Concurrent processing is the process of running a system at two separate locations "
         "concurrently until there is a level of assurance that the recovered system is operating "
         "correctly and securely."),

    Heading(2, "5.1 Data Validation Testing"),
    Para("Validation data testing is the process of testing and validating data to ensure that data "
         "files or databases have been recovered completely at the permanent location."),
    Para("{reconstitution.data_validation}"),

    Heading(2, "5.2 Functional Validation Testing"),
    Para("Functionality testing is a process for verifying that all system functionality has been "
         "tested, and the system is ready to return to normal operations."),
    Para("{reconstitution.functional_validation}"),

    Heading(2, "5.3 Recovery Declaration"),
    Para("Upon successfully completing testing and validation, the {reconstitution.declaring_role} will "
         "formally declare recovery efforts complete, and that {cso.name} is in normal operations. "
         "{cso.name} business and technical POCs will be notified of the declaration by the Contingency "
         "Plan Coordinator. The recovery declaration statement notifies the Contingency Plan Team and "
         "executive management that the {cso.name} has returned to normal operations."),

    Heading(2, "5.4 User Notification"),
    Para("After the recovery declaration statement is made, notifications are sent to users and "
         "customers. Notifications to customers are made in accordance with predetermined notification "
         "procedures."),
    Para("{reconstitution.user_notification}"),

    Heading(2, "5.5 Cleanup"),
    Para("Cleanup is the process of cleaning up or dismantling any temporary recovery locations, "
         "restocking supplies used, returning manuals or other documentation to their original "
         "locations, and readying the system for a possible future contingency event."),
    Para("{cleanup.procedures}"),
    Table("Table 5.1 Cleanup Roles and Responsibilities", ("Role", "Cleanup Responsibilities"), (),
          "cleanup.responsibilities",
          (("Click here to enter text.", "Click here to enter text."),
           ("Click here to enter text.", "Click here to enter text."))),

    Heading(2, "5.6 Returning Backup Media"),
    Para("It is important that all backup and installation media used during recovery be returned to the "
         "off-site data storage location."),
    Para("The following procedures must be followed to return backup and installation media to its "
         "offsite data storage location: {media_return.procedures}"),

    Heading(2, "5.7 Backing-Up Restored Systems"),
    Para("As soon as reasonable following recovery, the system must be fully backed up and a new copy of "
         "the current operating system stored for future recovery efforts. This full backup is then kept "
         "with other system backups."),
    Para("The procedures for conducting a full system backup are: {restored_backup.procedures}"),

    Heading(2, "5.8 Event Documentation"),
    Para("It is important that all recovery events be well-documented, including actions taken and "
         "problems encountered during the recovery and reconstitution effort. Information on lessons "
         "learned must be included in the annual update to the ISCP. It is the responsibility of each "
         "ISCP team or person to document their actions during the recovery event."),
    Para("Table 5.2 Event Documentation Responsibility lists the responsibility of each ISCP team or "
         "person to document their actions during the recovery event."),
    Table("Table 5.2 Event Documentation Responsibility", ("Role Name", "Documentation Responsibility"),
          (), "event_doc.responsibilities",
          (("Click here to enter text.", "Activity log"),
           ("Click here to enter text.", "Functionality and data testing results"),
           ("Click here to enter text.", "Lessons learned"),
           ("Click here to enter text.", "After Action Report"))),
)

SECTION_6: tuple[Block, ...] = (
    Heading(1, "6 Contingency Plan Testing"),
    Para("Contingency Plan operational tests of the {cso.name} are performed annually. A Contingency "
         "Plan Test Report is documented after each annual test. A template for the Contingency Plan "
         "Test Report is found in Appendix F – Contingency Plan Test Report."),
    Para("{testing.procedures}"),
)


# --------------------------------------------------------------------------- appendices A-L

APPENDICES: tuple[Block, ...] = (
    Heading(1, "Appendix A Key Personnel and Team Member Contact List"),
    Para("Table A.1 Key Personnel and Team Member Contact List includes Contingency Plan Team members "
         "designated in Section 2.5 Roles and Responsibilities and the ISCP has been distributed to "
         "everyone in this list."),
    Table("Table A.1 Key Personnel and Team Member Contact List",
          ("Role", "Name and Home Address", "Email", "Phone"), (), "contacts.key_personnel",
          (("Contingency Plan Director", "Click here to enter text.", "Click here to enter text.",
            "Primary: Primary Phone. Alternate: Secondary Phone"),
           ("Alternate Contingency Plan Director", "Click here to enter text.",
            "Click here to enter text.", "Primary: Primary Phone. Alternate: Secondary Phone"),
           ("Contingency Plan Coordinator", "Click here to enter text.", "Click here to enter text.",
            "Primary: Primary Phone. Alternate: Secondary Phone"),
           ("Alternate Contingency Plan Coordinator", "Click here to enter text.",
            "Click here to enter text.", "Primary: Primary Phone. Alternate: Secondary Phone"),
           ("Outage and Damage Assessment Lead", "Click here to enter text.",
            "Click here to enter text.", "Primary: Primary Phone. Alternate: Secondary Phone"),
           ("Alternate Outage and Damage Assessment Lead", "Click here to enter text.",
            "Click here to enter text.", "Primary: Primary Phone. Alternate: Secondary Phone"),
           ("Procurement and Logistics Coordinator", "Click here to enter text.",
            "Click here to enter text.", "Primary: Primary Phone. Alternate: Secondary Phone"),
           ("Alternate Procurement and Logistics Coordinator", "Click here to enter text.",
            "Click here to enter text.", "Primary: Primary Phone. Alternate: Secondary Phone"),
           ("Click here to enter text.", "Click here to enter text.", "Click here to enter text.",
            "Primary: Primary Phone. Alternate: Secondary Phone"),
           ("Click here to enter text.", "Click here to enter text.", "Click here to enter text.",
            "Primary: Primary Phone. Alternate: Secondary Phone"))),

    Heading(1, "Appendix B Vendor Contact List"),
    Para("Table B.1 Vendor Contact List includes the vendors associated with the ISCP."),
    Table("Table B.1 Vendor Contact List",
          ("Vendor", "Product or Service License #, Contract #, Account #, or SLA", "Phone"), (),
          "contacts.vendors",
          (("Click here to enter text.", "Click here to enter text.",
            "Primary: Primary Phone. Alternate: Secondary Phone"),) * 7),

    Heading(1, "Appendix C Alternate Storage, Processing and Provisions"),
    Heading(2, "Section 1: Alternate Storage Site Information"),
    Para("Table C.1 Alternate Storage Site Information lists the alternative site location and details "
         "about schedules, data types, and contacts."),
    Table("Table C.1 Alternate Storage Site Information", ("Storage Site", ""),
          (("Address of alternate storage site", "{alt_storage.address}"),
           ("Distance from primary facility", "{alt_storage.distance}"),
           ("Is the alternate storage facility owned by the organization or is a third-party storage "
            "provider?", "{alt_storage.ownership}"),
           ("Points of contact at alternate storage location", "{alt_storage.poc}"),
           ("Delivery schedule and procedures for packaging media for delivery to alternate storage "
            "facility", "{alt_storage.delivery_schedule}"),
           ("Procedures for retrieving media from the alternate storage facility",
            "{alt_storage.retrieval_procedures}"),
           ("Names and contact information for those persons authorized to retrieve media",
            "{alt_storage.authorized_personnel}"),
           ("Potential accessibility problems to the alternate storage site in the event of a widespread "
            "disruption or disaster (e.g., roads that might be closed, anticipate traffic)",
            "{alt_storage.accessibility_problems}"),
           ("Mitigation steps to access alternate storage site in the event of a widespread disruption "
            "or disaster", "{alt_storage.mitigation_steps}"),
           ("Types of data located at alternate storage site, including databases, application software, "
            "operating systems, and other critical information system software",
            "{alt_storage.data_types}"))),

    Heading(2, "Section 2: Alternate Processing Site Information"),
    Para("Table C.2 Alternate Processing Site Information"),
    Table("Table C.2 Alternate Processing Site Information", ("Alternate Processing Site", ""),
          (("Address", "{alt_processing.address}"),
           ("Distance from primary facility", "{alt_processing.distance}"),
           ("Alternate processing site is owned by the organization or is a third-party site provider",
            "{alt_processing.ownership}"),
           ("Point of Contact", "{alt_processing.poc}"),
           ("Procedures for accessing and using the alternate processing site, and access security "
            "features of alternate processing site", "{alt_processing.access_procedures}"),
           ("Names and contact information for those persons authorized to go to alternate processing "
            "site", "{alt_processing.authorized_personnel}"),
           ("Type of Site (from Table 2-4 Alternative Site Types)", "{alt_processing.site_type}"),
           ("Mitigation steps to access alternate processing site in the event of a widespread "
            "disruption or disaster", "{alt_processing.mitigation_steps}"))),

    Heading(2, "Section 3: Alternate Telecommunications Provisions"),
    Para("Table C.3 Alternate Telecommunications Provisions show the details for the alternate "
         "communications vendors, agreements and capacity."),
    Table("Table C.3 Alternate Telecommunications Provisions", ("Alternate Telecommunications", ""),
          (("Name and contact information of alternate telecommunications vendors by priority",
            "{alt_telecom.vendors}"),
           ("Agreements currently in place with alternate communications vendors",
            "{alt_telecom.agreements}"),
           ("Contracted capacity of alternate telecommunications", "{alt_telecom.capacity}"),
           ("Names and contact information of individuals authorized to implement or use alternate "
            "telecommunications", "{alt_telecom.authorized_personnel}"))),

    Heading(1, "Appendix D Alternate Processing Procedures"),
    Para("{alt_processing_procedures}"),

    Heading(1, "Appendix E System Validation Test Plan"),
    Para("Table E.1 System Validation Test Plan shows the results of testing after the system has "
         "recovered and prior to the system being put into full operation."),
    Table("Table E.1 System Validation Test Plan",
          ("Procedure", "Expected Results", "Actual Results", "Successful?", "Performed by"), (),
          "validation.test_plan",
          (("At the Command Prompt, type in system name", "System Log-in Screen appears", "", "", ""),
           ("Log-in as user test user, using password test pass", "Initial Screen with Main Menu shows",
            "", "", ""),
           ("From menu, select 5-Generate Report", "Report Generation Screen shows", "", "", ""),
           ("Select Current Date Report  Select Weekly  Select To Screen",
            "Report is generated on screen with last successful transaction included", "", "", ""),
           ("Select Close", "Report Generation Screen Shows", "", "", ""),
           ("Select Return to Main Menu", "Initial Screen with Main Menu shows", "", "", ""),
           ("Select Log-Off", "Log-in Screen appears", "", "", ""))),

    Heading(1, "Appendix F Contingency Plan Test Report"),
    Para("Table F.1 Contingency Plan Test Report reflects a summary of the last Contingency Plan Test. "
         "The actual procedures used to test the plan are described in Section 6 Contingency Plan "
         "Testing."),
    Table("Table F.1 Contingency Plan Test Report", ("Test Information", "Description"),
          (("Name of Test", "{test_report.name}"),
           ("System Name", "{test_report.system_name}"),
           ("Date of Test", "{test_report.date}"),
           ("Team Test Lead and Point of Contact", "{test_report.lead}"),
           ("Location Where Conducted", "{test_report.location}"),
           ("Participants", "{test_report.participants}"),
           ("Components", "{test_report.components}"),
           ("Assumptions", "{test_report.assumptions}"),
           ("Objectives", "{test_report.objectives}"),
           ("Methodology", "{test_report.methodology}"),
           ("Activities and Results (Action, Expected Results, Actual Results)",
            "{test_report.activities_results}"),
           ("Post Test Action Items", "{test_report.post_test_actions}"),
           ("Lessons Learned and Analysis of Test", "{test_report.lessons_learned}"),
           ("Recommended Changes to Contingency Plan Based on Test Outcomes",
            "{test_report.recommended_changes}"))),

    Heading(1, "Appendix G Diagrams"),
    Para("Please refer to {cso.name} System Security Plan for the service’s authorization boundary, "
         "network, and data flow diagrams. Please contact {csp.poc} for access to the diagrams, as "
         "necessary."),

    Heading(1, "Appendix H Hardware and Software Inventory"),
    Para("The {cso.name} Integrated Inventory Workbook, also provided as Appendix M of the "
         "{cso.name|<CSO Name>} System Security Plan, provides the complete listing of system components "
         "addressed by this Information System Contingency Plan. Please reach to {csp.poc} for access to "
         "the latest version of the Integrated Inventory Workbook, as necessary."),

    Heading(1, "Appendix I System Interconnections with Other Services"),
    Para("Please refer to the {cso.name} SSP for information concerning all interconnections to the "
         "service, both to FedRAMP Authorized services/systems and those lacking FedRAMP authorization. "
         "Please contact {csp.poc} for access to the SSP, as necessary."),

    Heading(1, "Appendix J Test and Maintenance Schedule"),
    Para("{test_schedule}"),

    Heading(1, "Appendix K Associated Plans and Procedures"),
    Para("ISCPs for other systems that either interconnect or support the system must be identified in "
         "Table K.1 Associated Plans and Procedures."),
    Table("Table K.1 Associated Plans and Procedures", ("System Name", "Plan Name"), (),
          "associated_plans",
          (("Click here to enter text.", "Click here to enter text."),) * 3),

    Heading(1, "Appendix L Business Impact Analysis"),
)


# --------------------------------------------------------- Appendix L body: NIST Appendix B

#: NIST SP 800-34 Rev. 1 Appendix B, transcribed at one heading level deeper than NIST prints
#: it, because it is nested inside FedRAMP's "Appendix L Business Impact Analysis".
#:
#: The italic/regular split **has** been read back from the PDF, by extracting Appendix B
#: (pages B-1..B-4) with span-level font names: italic guidance is ``TimesNewRomanPS-ItalicMT``,
#: text intended to remain is ``TimesNewRomanPSMT``. An earlier note here said this could not be
#: determined and erred towards emitting less; that guess was wrong in both directions, and four
#: regular paragraphs NIST intends to keep were being dropped while one italic sentence NIST
#: tells the author to delete was being emitted. Both are fixed.
#:
#: What is reproduced is NIST's **content**, not its layout. NIST prints the impact categories
#: as a list and numbers its top-level headings "1." / "2." / "3."; this renders them as a table
#: and without the trailing period, because the required thing is that the information is
#: present and correct. Nothing here may state something NIST does not say - that rule is
#: absolute, and a sentence spliced together from two different NIST sentences broke it once.
BIA_BLOCKS: tuple[Block, ...] = (
    Heading(2, "1 Overview"),
    Para("This Business Impact Analysis (BIA) is developed as part of the contingency planning process "
         "for the {cso.name} {cso.abbreviation}. It was prepared on "
         "{bia.completion_date}."),

    Heading(3, "1.1 Purpose"),
    Para("The purpose of the BIA is to identify and prioritize system components by correlating them to "
         "the mission/business process(es) the system supports, and using this information to "
         "characterize the impact on the process(es) if the system were unavailable."),
    Para("The BIA is composed of the following three steps:"),
    Bullets(("Determine mission/business processes and recovery criticality. Mission/business processes "
             "supported by the system are identified and the impact of a system disruption to those "
             "processes is determined along with outage impacts and estimated downtime. The downtime "
             "should reflect the maximum that an organization can tolerate while still maintaining the "
             "mission.",
             "Identify resource requirements. Realistic recovery efforts require a thorough evaluation "
             "of the resources required to resume mission/business processes and related "
             "interdependencies as quickly as possible. Examples of resources that should be identified "
             "include facilities, personnel, equipment, software, data files, system components, and "
             "vital records.",
             "Identify recovery priorities for system resources. Based upon the results from the "
             "previous activities, system resources can more clearly be linked to critical "
             "mission/business processes. Priority levels can be established for sequencing recovery "
             "activities and resources.")),
    Para("This document is used to build the {cso.name} Information System Contingency Plan (ISCP) and "
         "is included as a key component of the ISCP. It also may be used to support the development of "
         "other contingency plans associated with the system, including, but not limited to, the "
         "Disaster Recovery Plan (DRP) or Cyber Incident Response Plan."),

    Heading(2, "2 System Description"),
    Para("{bia.system_description}"),

    Heading(2, "3 BIA Data Collection"),

    Heading(3, "3.1 Determine Process and System Criticality"),
    Para("Step one of the BIA process - Working with input from users, managers, mission/business "
         "process owners, and other internal or external points of contact (POC), identify the "
         "specific mission/business processes that depend on or support the information system."),
    Table("", ("Mission/Business Process", "Description"), (), "bia.processes",
          (("{insert}", "{insert}"),)),

    Heading(4, "3.1.1 Identify Outage Impacts and Estimated Downtime"),
    Para("The following impact categories represent important areas for consideration in the "
         "event of a disruption or impact."),
    Para("Impact values for assessing category impact:"),
    Table("", ("Impact category", "Severe", "Moderate", "Minimal"), (), "bia.impact_categories",
          (("{insert category name}", "{insert value}", "{insert value}", "{insert value}"),)),
    Para("The table below summarizes the impact on each mission/business process if {cso.name} were "
         "unavailable, based on the following criteria:"),
    Table("", ("Mission/Business Process", "Impact"), (), "bia.process_impacts",
          (("{insert}", "{insert}"),), "bia.impact_categories"),
    Para("Working directly with mission/business process owners, departmental staff, managers, and "
         "other stakeholders, estimate the downtime factors for consideration as a result of a "
         "disruptive event."),
    Para("Maximum Tolerable Downtime (MTD). The MTD represents the total amount of time "
         "leaders/managers are willing to accept for a mission/business process outage or disruption and "
         "includes all impact considerations. Determining MTD is important because it could leave "
         "continuity planners with imprecise direction on (1) selection of an appropriate recovery "
         "method, and (2) the depth of detail which will be required when developing recovery "
         "procedures, including their scope and content."),
    Para("Recovery Time Objective (RTO). RTO defines the maximum amount of time that a system resource "
         "can remain unavailable before there is an unacceptable impact on other system resources, "
         "supported mission/business processes, and the MTD. Determining the information system resource "
         "RTO is important for selecting appropriate technologies that are best suited for meeting the "
         "MTD."),
    Para("Recovery Point Objective (RPO). The RPO represents the point in time, prior to a disruption or "
         "system outage, to which mission/business process data must be recovered (given the most recent "
         "backup copy of the data) after an outage."),
    Para("The table below identifies the MTD, RTO, and RPO (as applicable) for the organizational "
         "mission/business processes that rely on {cso.name}."),
    Table("", ("Mission/Business Process", "MTD", "RTO", "RPO"), (), "bia.downtime",
          (("{insert}", "{insert}", "{insert}", "{insert}"),)),
    Para("{bia.downtime_drivers}"),
    Para("{bia.alternate_means}"),

    Heading(3, "3.2 Identify Resource Requirements"),
    Para("The following table identifies the resources that compose {cso.name} including hardware, "
         "software, and other resources such as data files."),
    Table("", ("System Resource/Component", "Platform/OS/Version (as applicable)", "Description"), (),
          "bia.resources", (("{insert}", "{insert}", "{insert}"),)),
    Para("It is assumed that all identified resources support the mission/business processes identified "
         "in Section 3.1 unless otherwise stated."),

    Heading(3, "3.3 Identify Recovery Priorities for System Resources"),
    Para("The table below lists the order of recovery for {cso.name} resources. The table also "
         "identifies the expected time for recovering the resource following a “worst case” (complete "
         "rebuild/repair or replacement) disruption."),
    Para("Recovery Time Objective (RTO) - RTO defines the maximum amount of time that a system "
         "resource can remain unavailable before there is an unacceptable impact on other system "
         "resources, supported mission/business processes, and the MTD. Determining the information "
         "system resource RTO is important for selecting appropriate technologies that are best "
         "suited for meeting the MTD."),
    Table("", ("Priority", "System Resource/Component", "Recovery Time Objective"), (), "bia.priorities",
          (("{insert}", "{insert}", "{insert}"),)),
    Para("{bia.alternate_strategies}"),
)


ISCP_BLOCKS: tuple[Block, ...] = (
    COVER + SECTION_1 + SECTION_2 + SECTION_3 + SECTION_4 + SECTION_5 + SECTION_6
    + APPENDICES + BIA_BLOCKS
)


#: The heading outline the renderer must produce, written out independently of ``ISCP_BLOCKS``
#: so that a test comparing the two catches a heading silently dropped, renamed or reordered.
#: Slots are shown filled with their placeholders, which is what an empty answers file renders.
EXPECTED_HEADINGS: tuple[str, ...] = (
    "Prepared by",
    "Prepared for",
    "Document Revision History",
    "CONTINGENCY PLAN APPROVALS",
    "1 Introduction and Purpose",
    "1.1 Applicable Laws and Regulations",
    "1.2 FedRAMP Requirements and Guidance",
    "1.3 <Insert CSO Name> and Identifier",
    "1.4 Scope",
    "1.5 Assumptions",
    "2 Concept of Operations",
    "2.1 System Description",
    "2.2 Three Phases",
    "2.3 Data Backup Readiness Information",
    "2.4 Site Readiness Information",
    "2.5 Roles and Responsibilities",
    "Contingency Planning Director (CPD)",
    "Contingency Planning Coordinator (CPC)",
    "Outage and Damage Assessment Lead (ODAL)",
    "Hardware Recovery Team (HRT)",
    "Software Recovery Team (SRT)",
    "Telecommunications Team (TC)",
    "Procurement and Logistics Coordinator (PLC)",
    "Security Coordinator (SC)",
    "Plan Distribution and Availability",
    "Line of Succession/Alternates Roles",
    "3 Activation and Notification",
    "3.1 Activation Criteria and Procedure",
    "3.2 Notification Instructions",
    "3.3 Outage Assessment",
    "4 Recovery",
    "4.1 Sequence of Recovery Operations",
    "4.2 Recovery Procedures",
    "4.3 Recovery Escalation Notices/Awareness",
    "5 Reconstitution",
    "5.1 Data Validation Testing",
    "5.2 Functional Validation Testing",
    "5.3 Recovery Declaration",
    "5.4 User Notification",
    "5.5 Cleanup",
    "5.6 Returning Backup Media",
    "5.7 Backing-Up Restored Systems",
    "5.8 Event Documentation",
    "6 Contingency Plan Testing",
    "Appendix A Key Personnel and Team Member Contact List",
    "Appendix B Vendor Contact List",
    "Appendix C Alternate Storage, Processing and Provisions",
    "Section 1: Alternate Storage Site Information",
    "Section 2: Alternate Processing Site Information",
    "Section 3: Alternate Telecommunications Provisions",
    "Appendix D Alternate Processing Procedures",
    "Appendix E System Validation Test Plan",
    "Appendix F Contingency Plan Test Report",
    "Appendix G Diagrams",
    "Appendix H Hardware and Software Inventory",
    "Appendix I System Interconnections with Other Services",
    "Appendix J Test and Maintenance Schedule",
    "Appendix K Associated Plans and Procedures",
    "Appendix L Business Impact Analysis",
    "1 Overview",
    "1.1 Purpose",
    "2 System Description",
    "3 BIA Data Collection",
    "3.1 Determine Process and System Criticality",
    "3.1.1 Identify Outage Impacts and Estimated Downtime",
    "3.2 Identify Resource Requirements",
    "3.3 Identify Recovery Priorities for System Resources",
)

#: Every table caption the two sources print, for the question bank's ``template_ref`` check.
TABLE_TITLES: tuple[str, ...] = tuple(
    block.title for block in ISCP_BLOCKS if isinstance(block, Table) and block.title
)


def template_corpus() -> str:
    """Every literal string this module can put into a document, concatenated.

    The provenance test joins this with the question placeholders and asserts that each
    template-kind segment the renderer emitted is a substring of the result. That is the
    mechanical form of "every rendered sentence is either template text or a user's answer".
    """
    parts: list[str] = []
    for block in ISCP_BLOCKS:
        if isinstance(block, Heading):
            parts.append(block.text)
        elif isinstance(block, Para):
            parts.append(block.text)
        elif isinstance(block, Bullets):
            parts.extend(block.items)
        elif isinstance(block, Table):
            parts.append(block.title)
            parts.extend(block.header)
            for row in block.rows + block.template_rows:
                parts.extend(row)
    return "\n".join(parts)
