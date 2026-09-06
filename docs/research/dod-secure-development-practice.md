# US Department of Defense: secure software development practice

Research notes for engineers building tooling against DoD and federal secure
development expectations. Compiled 6 September 2026 from primary sources
(NIST, OMB, DoD CIO, DISA, CISA, DoD Directives Division). Every document
number, version and date below was read from the document itself or from the
issuing agency's own catalogue page, unless it appears in **Not verified**.

## How to read this document

Three categories get conflated constantly in this area, and conflating them
produces wrong tooling. This document labels every obligation as one of:

- **Required by regulation or policy**: a statute, executive order, OMB
  memorandum, or DoD issuance that binds by its own force. Example:
  DoDI 8510.01 binds DoD components.
- **Required by contract**: binds only because a contract clause, an
  Authorizing Official, or a programme office says so. Example: a DoD
  Authorizing Official requiring a specific control gate as a condition of
  authorisation. There is no government-wide rule making it so.
- **Recommended practice**: guidance that describes outcomes without
  compelling them. Example: NIST SP 800-218 in its own words.

The single most important current fact: **NIST SP 800-218 (SSDF) is
recommended practice, not regulation.** It became a de facto contract
requirement for federal software sales between 2022 and January 2026 through
OMB memoranda, and that mechanism was rescinded on 23 January 2026. Details
in the landscape section.

## 1. The landscape: what binds, what advises

### 1.1 The advisory layer (NIST)

| Document | Version and date | Status |
|---|---|---|
| NIST SP 800-218, *Secure Software Development Framework (SSDF) Version 1.1: Recommendations for Mitigating the Risk of Software Vulnerabilities* | v1.1, February 2022 | Final. Recommended practice. |
| NIST SP 800-218 Rev. 1 (SSDF Version 1.2) | Initial Public Draft, published 17 December 2025, comment period closed 30 January 2026 | Draft. Not final as of this writing. |
| NIST SP 800-218A | *Secure Software Development Practices for Generative AI and Dual-Use Foundation Models*. Final per the NIST SSDF project page. | Companion profile augmenting 800-218. |
| NIST SP 800-37 Rev. 2, *Risk Management Framework for Information Systems and Organizations* | Rev. 2, December 2018 | The RMF itself. Advisory to NIST; made binding on DoD by DoDI 8510.01. |
| NIST SP 800-161 Rev. 1, *Cybersecurity Supply Chain Risk Management Practices for Systems and Organizations* | Rev. 1, May 2022, Update 1 on 1 November 2024 | Advisory. Cited by DoD SWFT materials. |
| NIST SP 800-137 / 800-137A | Information security continuous monitoring | Cited as binding-by-reference in DoDI 8510.01 paragraph on continuous monitoring. |

SSDF says of itself: "The SSDF does not prescribe how to implement each
practice. The focus is on the outcomes of the practices rather than on the
tools, techniques, and mechanisms to do so." And: "The intention of the SSDF
is not to create a checklist to follow, but to provide a basis for planning
and implementing a risk-based approach." Tooling that treats SSDF task IDs as
pass/fail gates is imposing a structure the framework explicitly disclaims.
That may still be the right product decision, but it is your decision, not
NIST's requirement.

### 1.2 The federal imposition layer, and its 2026 reversal

This is where most published summaries are now out of date.

1. **Executive Order 14028**, *Improving the Nation's Cybersecurity*, directed
   NIST to produce secure development guidance and directed OMB to require
   agencies to use only software from producers who attest to those practices.
   SSDF Appendix A maps SSDF practices to EO 14028 Section 4 clauses.
2. **OMB Memorandum M-22-18** (14 September 2022) and **OMB Memorandum
   M-23-16** (9 June 2023) implemented that: agencies had to obtain an
   attestation from software producers before using their software.
3. **CISA Secure Software Development Attestation Form** (the "Common Form"),
   released 11 March 2024, was the government-wide instrument. It is based on
   selected practices from SP 800-218 v1.1.
4. **Executive Order 14144** (16 January 2025) extended EO 14028.
5. **Executive Order 14306** (6 June 2025), *Sustaining Select Efforts to
   Strengthen the Nation's Cybersecurity and Amending Executive Order 13694 and
   Executive Order 14144*, struck the attestation subsections of EO 14144 and
   redirected NIST toward an SSDF consortium and an SSDF update (preliminary
   update due 1 December 2025, final within 120 days after).
6. **OMB Memorandum M-26-05** (23 January 2026), *Adopting a Risk-based
   Approach to Software and Hardware Security*, signed by OMB Director Russell
   T. Vought, **rescinded M-22-18 and M-23-16**.

M-26-05 says, verbatim:

> OMB Memorandum M-22-18 ... imposed unproven and burdensome software
> accounting processes that prioritized compliance over genuine security
> investments. ... Accordingly, OMB Memoranda M-22-18 and M-23-16, a companion
> policy, are hereby rescinded.

And on what survives:

> Agencies shall continue to maintain a complete inventory of software and
> hardware and develop software and hardware assurance policies and processes
> that match their risk determinations and mission needs. Agencies may choose
> to use the government-wide secure software development resources developed
> under M-22-18, such as the Secure Software Development Attestation Form.
> Agencies may also choose to adopt contractual terms that require a software
> producer to provide a current software bill of materials (SBOM) upon request.

M-26-05 then points agencies at SP 800-218, the CISA SBOM minimum elements,
and the CISA HBOM framework as references and options.

**What this means for tooling.** As of January 2026:

- SSDF attestation is **not** a government-wide requirement. It is at most a
  **contract requirement** an individual agency or programme chooses to impose.
- SBOM delivery is likewise **contract-optional**, explicitly framed as
  "agencies may choose to adopt contractual terms."
- Agency-level software and hardware inventory is **required by policy**
  (M-26-05 uses "shall"), and it is an agency obligation, not a producer one.
- Build tooling for SSDF and SBOM as **configurable contract obligations**,
  parameterised per programme. Do not hard-code them as universal gates, and
  do not assume every DoD customer still holds a signed Common Form.

### 1.3 The DoD layer

| Document | Date | Binding character |
|---|---|---|
| DoDI 8510.01, *Risk Management Framework for DoD Systems* | Effective 19 July 2022. Approved by DoD CIO John B. Sherman. Reissues the March 2014 version, incorporates DTM 20-004. | **Required by policy** for DoD systems. |
| DoDI 5000.87, *Operation of the Software Acquisition Pathway* | Effective 2 October 2020. Approved by USD(A&S) Ellen M. Lord. Implements Section 800 of Public Law 116-92 (FY2020 NDAA). | **Required by policy** for programmes on this pathway. |
| DoDI 5000.82, *Requirements for the Acquisition of Digital Capabilities* | Effective 1 June 2023. | **Required by policy** for digital capability acquisition. |
| OSD memorandum, *Continuous Authorization To Operate (cATO)*, signed David W. McKeown, DoD Senior Information Security Officer | February 2022 | Policy guidance defining the conditions for cATO. Achieving cATO is voluntary; the conditions are not negotiable if you want one. |
| DoD CIO, *Continuous Authorization to Operate (cATO) Evaluation Criteria, DevSecOps Use Case* | 29 May 2024. Distribution Statement A. | The evaluation checklist the DoD CISO applies. **Contract/authorisation requirement** in practice. |
| DoD CIO, *DevSecOps Continuous Authorization Implementation Guide*, v1.0 | March 2024 (change history: 21 March 2024) | Marked **Distribution Statement C**, restricted to US Government agencies and their contractors. Not summarised here beyond its existence and scope. |
| *DoD Enterprise DevSecOps Fundamentals* | v2.0, March 2021; **v2.5 approved by the DoD Software Modernization Senior Steering Group on 16 October 2024** | Reference guidance. Becomes binding when an AO conditions authorisation on it. |
| *DoD Enterprise DevSecOps Activities & Tools Guidebook* | **v2.5, April 2025** | Carries an explicit REQUIRED / PREFERRED / AS REQUIRED baseline per activity. See section 3. |
| *DoD Enterprise DevSecOps Reference Design: CNCF Kubernetes* | v2.0, March 2021, approved by Nicolas Chaillan, DoD Chief Software Officer (USAF) | Reference design. Adoption of "an approved reference design" is one of the three cATO competencies. |
| DISA, *Container Hardening Process Guide* | **Version 1, Release 2, 24 August 2022** | Defines what a DoD hardened container is and the process to produce one. |
| DoD CIO memorandum, *Accelerating Secure Software* (establishes the Software Fast Track initiative), signed Katherine Arrington, performing the duties of DoD CIO | Signature block reads 2025.04.24; the SWFT RFI summary foreword states "On April 24, 2025, I signed a memo titled 'Accelerating Secure Software'" | Directive to develop a framework. The framework itself is the deliverable, see Not verified. |
| RMF Knowledge Service, *Software Acquisition Pathway Integration with Risk Management Framework* | Undated page, references DoDI 8510.01 (2022) and CNSSI 1253 (29 July 2022) | Self-described as "implementation guidance and best practices describing the policy found in DoDI 8510.01." **Recommended practice** that explains binding policy. |
| CNSSI 1253, *Categorization and Control Selection for National Security Systems* | 29 July 2022, per the citation in the RMF KS page above | Control selection for NSS. Invoked by DoDI 8510.01. |

Note on naming: DoD CIO materials published from mid-2025 onward sometimes
use "Department of War (DoW)". The SWFT RFI Combined Summary foreword uses it.
Treat DoD and DoW as the same organisation in these documents.

## 2. SSDF practice groups and what each demands of a team

SSDF v1.1 organises 19 practices and 42 tasks into four groups. Exact group
names and definitions, quoted from SP 800-218:

1. **Prepare the Organization (PO)**: "Organizations should ensure that their
   people, processes, and technology are prepared to perform secure software
   development at the organization level."
2. **Protect the Software (PS)**: "Organizations should protect all components
   of their software from tampering and unauthorized access."
3. **Produce Well-Secured Software (PW)**: "Organizations should produce
   well-secured software with minimal security vulnerabilities in its
   releases."
4. **Respond to Vulnerabilities (RV)**: "Organizations should identify residual
   vulnerabilities in their software releases and respond appropriately to
   address those vulnerabilities and prevent similar ones from occurring in the
   future."

### 2.1 Practice identifiers and names (verified against SP 800-218 v1.1)

**Prepare the Organization**

| ID | Practice |
|---|---|
| PO.1 | Define Security Requirements for Software Development |
| PO.2 | Implement Roles and Responsibilities |
| PO.3 | Implement Supporting Toolchains |
| PO.4 | Define and Use Criteria for Software Security Checks |
| PO.5 | Implement and Maintain Secure Environments for Software Development |

**Protect the Software**

| ID | Practice |
|---|---|
| PS.1 | Protect All Forms of Code from Unauthorized Access and Tampering |
| PS.2 | Provide a Mechanism for Verifying Software Release Integrity |
| PS.3 | Archive and Protect Each Software Release |

**Produce Well-Secured Software**

| ID | Practice |
|---|---|
| PW.1 | Design Software to Meet Security Requirements and Mitigate Security Risks |
| PW.2 | Review the Software Design to Verify Compliance with Security Requirements and Risk Information |
| PW.3 | Verify Third-Party Software Complies with Security Requirements |
| PW.4 | Reuse Existing, Well-Secured Software When Feasible Instead of Duplicating Functionality |
| PW.5 | Create Source Code by Adhering to Secure Coding Practices |
| PW.6 | Configure the Compilation, Interpreter, and Build Processes to Improve Executable Security |
| PW.7 | Review and/or Analyze Human-Readable Code to Identify Vulnerabilities and Verify Compliance with Security Requirements |
| PW.8 | Test Executable Code to Identify Vulnerabilities and Verify Compliance with Security Requirements |
| PW.9 | Configure Software to Have Secure Settings by Default |

**Respond to Vulnerabilities**

| ID | Practice |
|---|---|
| RV.1 | Identify and Confirm Vulnerabilities on an Ongoing Basis |
| RV.2 | Assess, Prioritize, and Remediate Vulnerabilities |
| RV.3 | Analyze Vulnerabilities to Identify Their Root Causes |

Complete task ID set present in v1.1: PO.1.1 to PO.1.3, PO.2.1 to PO.2.3,
PO.3.1 to PO.3.3, PO.4.1 to PO.4.2, PO.5.1 to PO.5.2, PS.1.1, PS.2.1,
PS.3.1 to PS.3.2, PW.1.1 to PW.1.3, PW.2.1, PW.3.1 to PW.3.2, PW.4.1 to
PW.4.5, PW.5.1 to PW.5.2, PW.6.1 to PW.6.2, PW.7.1 to PW.7.2, PW.8.1 to
PW.8.2, PW.9.1 to PW.9.2, RV.1.1 to RV.1.3, RV.2.1 to RV.2.2, RV.3.1 to
RV.3.4.

Two numbering traps for anyone building an ID validator. First, v1.1
renumbered relative to SSDF 1.0, and the document carries explicit
"PW.3.1: Moved to PO.1.3", "PW.3.2: Moved to PW.4.4", "PW.4.5: Moved to
PW.4.1 and PW.4.4" migration notes. A task ID from a v1.0-era artefact may
mean something different in v1.1. Second, there is no RV.4 and no RV.8, which
matters because DoD documents cite RV.8 (see section 3.3).

### 2.2 What each group asks of a development team, in practice

**Prepare the Organization.** Write down the security requirements the software
must meet and keep them current (PO.1.2). Assign named roles and train the
people in them (PO.2). Select, secure and integrate the toolchain, then
configure the tools to emit evidence: PO.3.3 requires you to "Configure tools
to generate artifacts of their support of secure software development
practices," with the example of using workflow and issue tracking to "create
an audit trail." Define the criteria for security checks and track them across
the lifecycle (PO.4.1), and gather the supporting data automatically (PO.4.2).
Separate and protect each development environment (PO.5.1) and harden developer
endpoints against approved hardening guides (PO.5.2).

For tooling, PO.3.3 and PO.4.2 are the interesting pair: they are the mandate
for machine-readable evidence generation, which is what everything downstream
consumes.

**Protect the Software.** Control access to source and configuration (PS.1.1).
Publish integrity verification material for consumers: PS.2.1 names
cryptographic hashes on a secured site and code signing through an established
certificate authority. Archive each release with its provenance, and PS.3.2
requires you to "Collect, safeguard, maintain, and share provenance data for
all components of each software release (e.g., in a software bill of materials
[SBOM])." PS.3.2 is where SBOM enters SSDF.

**Produce Well-Secured Software.** Threat model and design to the requirements
(PW.1). Have the design reviewed by someone not involved in it, or by an
automated process (PW.2.1). Verify third-party components against your
requirements (PW.3), acquire and maintain well-secured components (PW.4.1),
and keep verifying them "throughout their life cycles" (PW.4.4). Follow secure
coding standards (PW.5). Harden compiler, interpreter and build settings
(PW.6). Review or analyse human-readable code, and (PW.7.2) "record and triage
all discovered issues and recommended remediations in the development team's
workflow or issue tracking system." Test the executable and do the same
recording and triage (PW.8.2). Ship secure defaults (PW.9).

PW.7.2 and PW.8.2 are the clearest statement in SSDF that finding a problem is
not the deliverable. The tracked, triaged record is.

**Respond to Vulnerabilities.** Monitor external sources and investigate
credible reports (RV.1.1). Run a vulnerability disclosure programme with the
roles and process behind it (RV.1.3). Assess, prioritise and remediate
(RV.2). Do root cause analysis and feed it back into the practices (RV.3).

## 3. Pipeline and gate requirements

### 3.1 The software factory model

DoD Enterprise DevSecOps Fundamentals defines a **software factory** inside a
**DevSecOps platform**. The v2.5 text: a DevSecOps platform "can be a
multi-tenant environment that brings together a significant portion of a
software supply chain, operating under cATO or a provisional ATO."

On provenance of the factory itself, v2.5 states that "DevSecOps platforms are
expected to be instantiated from hardened IaC code and scripts or DoD hardened
containers from a DoD artifact repository like Iron Bank." The v2.0 wording was
stronger and named Iron Bank as "the sole DoD artifact repository." The v2.5
softening from "the sole" to "a DoD artifact repository like Iron Bank" is a
real change and worth respecting in tooling: do not hard-code Iron Bank as the
only permitted source.

### 3.2 Control gates

Control gates are the central pipeline concept. The cATO Evaluation Criteria
glossary defines one: "a defined point in the project lifecycle when specific
requirements, called exit criteria, must be met to move to the next phase."

Fundamentals v2.5 describes the gate sequence concretely:

- **Merge gate.** "When a developer looks to merge their completed work into
  the main branch of the code repository, they encounter a control gate."
  Successful compile, then a pull/merge request for peer review, described as
  "the software code equivalent of two-person integrity." Review can reject on
  security flaws, architectural concerns, or missing in-code documentation.
- **Test to integration gate.** Static analysis, functional, interface and
  dynamic analysis, plus hardware-in-the-loop or software-in-the-loop where
  applicable. Pass and promote, or return to the team.
- **Integration and deployment gate.** Operational and performance tests, user
  acceptance tests, additional security compliance scans, then release.

v2.5 characterises the factory as providing "a dynamically scalable set of
pipelines with three distinct cyber survivability control gates."

On whether gates are optional: **v2.0 states that control gates are mandatory
and that automation is not required from day one.** Quoted from v2.0: "The
control gates are mandatory, but there is no expectation that they are fully
automated from the moment the software factory is instantiated. On the
contrary, because each program's requirements are unique, and as espoused by
agile practices, it is expected that initially control gates may require human
intervention." This is the single most useful sentence in the corpus for
anyone building gate tooling: a human-in-the-loop gate is a compliant gate,
and automation is a maturity backlog item.

Guardrails are a distinct concept from gates. Fundamentals describes "control
gates and guardrails that evaluate code for discrepancies and vulnerabilities
before sending it to production," and the cATO criteria ask for a description
of each guardrail and "the process that occurs when something is out of the
risk tolerance for each guardrail." Model them separately: a gate blocks
promotion, a guardrail defines a tolerance band and an escalation.

### 3.3 The REQUIRED activity baseline

The *DevSecOps Activities & Tools Guidebook* v2.5 (April 2025) is the most
directly actionable document in the set. It marks every activity as REQUIRED,
PREFERRED, or AS REQUIRED, and states: "REQUIRED activities are not
negotiable. PREFERRED and AS REQUIRED may be included at the discretion of the
Mission Owner and Authorizing Official." Each row also carries SSDF task IDs,
inputs, outputs and tool dependencies. Reference designs "do not remove a
required tool or activity, only augment."

Selected REQUIRED activities with their SSDF mappings and named outputs:

**Develop phase**

| Activity | SSDF mapping | Output |
|---|---|---|
| Code commit | PS.1.1, PW.4.4 | Version controlled source code |
| Code Commit Logging | PO.3.1, PO.3.3, PO.5.1, PO.5.2 | Code commit log |
| Code commit scan (secrets/sensitive content, blocks the commit) | RV.1.2 | Security findings and warnings |
| Database Component Test | (cited as RV.8.1, RV.8.2) | Test results |
| Documentation | PO.1.1, PO.1.2, PO.1.3, PW.7.2 | Documentation, auto-generated API documentation |

Code review is marked PREFERRED in the Develop table, not REQUIRED, which sits
oddly against the Fundamentals text describing peer review at the merge gate.
Flagging rather than reconciling.

**Build phase**

| Activity | SSDF mapping | Output |
|---|---|---|
| Security Build (compile and link) | PO.3.1, PO.3.2, PO.3.3, PO.4.1 | Binary artifacts, build report |
| Static application security test and scan | PO.3.1, PO.3.2, PO.3.3, PO.4.1, PO.4.2 | Static code scan report and recommended mitigation |
| Dependency vulnerability checking | PO.3.1, PO.3.2, PW.4.4, RV.1.1, RV.1.2, RV.1.3 | Vulnerability report (input: dependency list or BOM) |
| API Security Tests | PO.3.2, PO.4.1, PW.1.3, PW.4.4, RV1.2 | Test results |
| Functional test | PW.8.1, PW.8.2 | Test report |
| Integration test | PW.8.1, PW.8.2 | Test results |
| Regression test (includes smoke tests) | (cited as RV.8.1, RV.8.2) | Test results |
| Software Integration Test | (cited as RV.8.1, RV.8.2) | Test results |
| Component Test | (cited as RV.8.1, RV.8.2) | Test results |
| Build configuration control and audit | (cited as PS 3.2) | Version controlled build report, action items, **go/no-go decision** |
| Mission Based Cyber Risk Assessments | PW.7.2, RV.1.1, RV.1.2, RV.2.1, RV.3.1, RV.3.2, RV.3.3 | Risk assessment (inputs include NIST SP 800-53 control implementations, FIPS 199 categorization, CNSSI 1253) |
| Release packaging | PS.2.1, PS.3.1, PS.3.2 | "Released package with checksum and digital signature" |
| Store artifacts | PO.3.1, PO.3.3 | Version controlled artifacts |
| Test Audit | PO.2.1, PS 2.1, PW.1.2, PW.2.1 | Test audit log ("who performs what test at what time") |

**Release phase**

| Activity | SSDF mapping | Output |
|---|---|---|
| Release go / no-go decision | PW.2.1, RV.3.4 | Go/no-go decision; artifacts tagged with release tag on go. Inputs: design documentation, version controlled artifacts, version controlled test reports, security test and scan reports |
| SBOM Software Composition Analysis | PS.3.2 | Vulnerabilities analysis report ("Collect and analyze provenance data for all components of each release") |
| Software Factory Risk Continuous Monitoring | PS.3.2 | Alerts |
| Mission Based Cyber Risk Assessments | as above | Risk assessment |
| Operational Readiness Test | n/a | Test report |
| Test Audit | PO.2.1, PS.2.1, PW.1.2, PW.2.1 | Test audit log |

Release phase description: "the software artifacts are digitally signed to
verify that they have passed build, all tests, and security scans."

**Accuracy warning for anyone consuming these mappings programmatically.** The
guidebook cites RV.8.1 and RV.8.2 for several test activities. **No RV.8
practice exists in SSDF v1.1**, which stops at RV.3. Some IDs are also
formatted inconsistently ("PS 3.2", "PS 2.1", "RV1.2"). A validator that joins
guidebook activities to SSDF task IDs will produce unresolvable references. Do
not silently drop them and do not invent a mapping; surface them as source
defects.

Another cross-document discrepancy: Fundamentals v2.5 describes RMF as a
"six-step process," while DoDI 8510.01 (the binding instrument) lays out seven
steps: Prepare, Categorize, Select, Implement, Assess, Authorize, Monitor.
DoDI 8510.01 is the authority.

### 3.4 Hardened containers and approved baselines

DISA's *Container Hardening Process Guide* V1R2 (24 August 2022) defines the
term: "A DOD hardened container is an Open Container Image (OCI)-compliant
image that is secured and made compliant with the DOD Hardened Containers
Cybersecurity Requirements." Those requirements sit in Appendix B of that
guide. Hardened containers are published in **Iron Bank**, the DoD Centralized
Artifacts Repository (DCAR); source Dockerfiles live in **Repo One**, the DoD
Centralized Container Source Code Repository (DCCSCR).

Concrete rules from the guide:

- Use a DoD-hardened or approved base image via `FROM` wherever one exists.
  The STIGed DoD-approved Universal Base Image (UBI), and the approved scratch
  and distroless images, are named.
- "Use base images that reuse the closest hardened image layer available to
  inherit its hardening." A Java application should build on hardened OpenJDK,
  not on hardened UBI directly.
- Accepted base images are named as UBI7, UBI8, scratch, distroless.
- Hardening a different OS requires that OS to be STIGed first.
- Hardeners are expected to know DISA SRG and STIG documentation, container
  vulnerability scanning, and compliance scanning with tools in the
  Chef InSpec and OpenSCAP class.

The guide also states that a standalone container is not accredited on its own:
containers must run on a DevSecOps platform compliant with the reference design
and its MVP requirements, including the Sidecar Container Security Stack, for
the certificate to field to apply. Security control inheritance is Appendix C.

**Baseline sources you will need identifiers for**: DISA Security Requirements
Guides (SRGs) and Security Technical Implementation Guides (STIGs), published
via public.cyber.mil. Individual STIG identifiers and versions were not
enumerated for this document.

## 4. Software supply chain requirements

### 4.1 Where the obligation actually comes from

- **SSDF PS.3.2** (recommended practice): collect, safeguard, maintain and
  share provenance data for all components of each release, "e.g., in a
  software bill of materials [SBOM]."
- **SSDF PS.2.1** (recommended practice): publish integrity verification
  material, hashes and code signing.
- **SSDF PW.4.1 and PW.4.4** (recommended practice): acquire well-secured
  third-party components and keep verifying them across their life cycles.
- **OMB M-26-05** (policy, 23 January 2026): agencies **may choose** to require
  a current SBOM on request by contract. For a cloud platform, the memorandum's
  footnote specifies the SBOM should cover "the runtime production
  environment." This is now the top of the federal chain for SBOM. It is
  permissive, not mandatory.
- **DoD cATO Evaluation Criteria** (29 May 2024): SBOM is a delivered artefact
  in a cATO package. See section 5.
- **cATO memorandum** (February 2022): names SBOM support as one reason to
  adopt an approved platform and pipeline.
- **DoD Enterprise DevSecOps Fundamentals v2.5**: "cATO includes the need for a
  Secure Software Supply Chain (SSSC) and requires a Software Bill of
  Materials (SBOM)."

So: SBOM is **required by contract or by authorisation condition**, in DoD
practice most concretely as a cATO package artefact. It is not required by a
government-wide regulation as of this writing.

### 4.2 SBOM content: CISA 2026 Minimum Elements

CISA's *2026 Minimum Elements for a Software Bill of Materials (SBOM)*,
published **29 July 2026** (document version 2.1), authored by CISA, NSA, FBI
and a list of international partners, replaces the 2021 NTIA minimum elements.
Version history in the document itself: 1.0 on 12 July 2021 (NTIA),
2.0 draft on 22 August 2025, 2.1 final on 29 July 2026. It is marked
TLP:CLEAR and is **guidance**, not a regulation.

The minimum elements data fields, verbatim from Appendix A:

**SBOM metadata**

| Field | Definition |
|---|---|
| SBOM Author | The name of the entity that creates the SBOM data for the target component. |
| SBOM Author Signature | A digital signature attributable to the SBOM author. |
| SBOM Data Format Name | The name of the data format used to represent the SBOM data. |
| SBOM Data Format Version | Identifier designated by the SBOM data format to specify the version of the data format. |
| SBOM Generation Context | The relative software lifecycle phase and data available at the time the SBOM author generated the SBOM. |
| SBOM Timestamp | Record of the date and time of the most recent update to the SBOM data. |
| SBOM Tool Name | The name of the tool used by the SBOM author to generate or amend the SBOM. |
| SBOM Tool Version | Identifier for the version of the tool identified in the SBOM Tool Name element. |
| SBOM Version | Identifier designated by the SBOM author to specify a change in the SBOM document from a previously identified version or to indicate that it is the first version. |

**Component data**

| Field | Definition |
|---|---|
| Component Name | The name assigned by the component producer to a software component. |
| Component Producer | The name of an entity that creates, defines, and identifies components. |
| Component Version | Identifier used by the component producer to specify a change in a software component from a previously identified version or to indicate that it is the first version. |
| Component Identifiers | Identifiers used to identify a component or serve as a look-up key for relevant databases. |
| Component Hash Value | The output generated from applying a cryptographic hash algorithm to an executable component artifact. |
| Component Hash Algorithm | The cryptographic algorithm used to compute the Component Hash Value of the software component. |
| Component License | The identifiers for the licenses under which the software component is available. |
| Component Dependency Relationship | The relationship between two components, where one component is necessary for the operation of the other. |

Changes from the 2021 NTIA baseline that affect implementations:

- New fields: SBOM Author Signature, SBOM Data Format Name, SBOM Data Format
  Version, SBOM Generation Context, SBOM Tool Name, SBOM Tool Version, SBOM
  Version, Component Hash Value, Component Hash Algorithm, Component License.
- "Supplier Name" became **Component Producer**. "Author of SBOM Data" became
  **SBOM Author**. "Depth" became **Coverage**.
- "Accommodation of Mistakes" became **Accommodation of Updates to SBOM Data**,
  on the reasoning that "recipients can expect SBOM data to be accurate."
- **Access Control was removed** as a standalone element and folded into
  Distribution and Delivery.
- **SWID tags were removed** from the list of data formats, on the grounds that
  they are "not a widely used SBOM data format for which multiple tools exist."
  "Automation Support" was replaced by **Machine-Processable Data**, moved
  under Practices and Processes.
- Component Name now allows multiple entries.

Practices and Processes elements (distinct from data fields, described as "how
an entity engages with and documents the SBOM data"): Accommodation of Updates
to SBOM Data, Coverage, Distribution and Delivery, Explicitly Identifying
Unknown Information, Frequency, Machine-Processable Data.

Note the SWID removal against the DoD cATO Evaluation Criteria, which (in May
2024) listed SPDX, SWID and CycloneDX as acceptable formats. The criteria also
say plainly: "the format has not yet been mandated." If you support two format
families, SPDX and CycloneDX are the safe pair.

### 4.3 Signing and provenance

- Release packaging is a REQUIRED Build-phase activity whose output is a
  "Released package with checksum and digital signature."
- SSDF PS.2.1 examples: cryptographic hashes posted on a secured site, code
  signing through an established certificate authority.
- CISA 2026 adds SBOM Author Signature and component hash value plus algorithm
  as minimum elements, so the SBOM itself is now expected to be signed and to
  carry component-level cryptographic identity.

### 4.4 Dependency management

- REQUIRED Build-phase activity "Dependency vulnerability checking," input
  "Dependency list or BOM list," output "Vulnerability report."
- REQUIRED Release-phase activity "SBOM Software Composition Analysis."
- cATO criteria require you to "Maintain an archive of SBOMs for products
  passing through pipelines" and to explain how, when a new CVE appears, "the
  organization applies it to the SBOMs." That is a requirement for retrospective
  SBOM querying, not just generation at build time. Design storage accordingly.
- SSDF PW.4.4 requires verification of third-party components "throughout their
  life cycles," which is the same continuous-requery obligation from the NIST
  side.

### 4.5 Hardware

M-26-05 extends the frame to hardware and cites CISA's *A Hardware Bill of
Materials (HBOM) Framework for Supply Chain Risk Management* (September 2023).
Out of scope for software development tooling, noted because the memorandum
criticised the previous policy for having "neglected to account for threats
posed by insecure hardware."

## 5. What must be produced continuously for authorisation

### 5.1 RMF, and where continuous enters it

DoDI 8510.01 (19 July 2022) establishes RMF for DoD systems with seven steps:
Prepare, Categorize, Select, Implement, Assess, Authorize, Monitor. Two tasks
matter most here:

- **Task S-5, Continuous Monitoring Strategy (System)**: "A continuous
  monitoring strategy for the system that reflects the organizational risk
  management strategy is developed." Owner: System Owner or Common Control
  Provider.
- **Task M-6, Ongoing Authorization**: "AOs conduct ongoing authorizations
  using the results of continuous monitoring activities and communicate changes
  in risk determination and acceptance decisions." Owner: AO.

DoDI 8510.01 also requires components to "Implement continuous monitoring
activities in accordance with Office of Management and Budget Memorandum
M-14-03, NIST SP 800-137, NIST SP 800-137A, and DoDIs 8530.01 and 8531.01," and
to maintain a POA&M for known vulnerabilities per DoDI 8531.01.

Worth knowing: **the words "DevSecOps" and "cATO" do not appear in the body of
DoDI 8510.01 as issued.** Ongoing authorisation is in the instruction; cATO as
a named DoD construct comes from the February 2022 OSD memorandum and the 2024
evaluation criteria. If you are building tooling that claims DoDI 8510.01
compliance, do not attribute cATO requirements to it.

### 5.2 cATO: the three competencies

From the OSD cATO memorandum (February 2022, signed David W. McKeown, DoD
Senior Information Security Officer), the AO must demonstrate three
competencies:

1. "On-going visibility of key cybersecurity activities inside of the system
   boundary with a robust continuous monitoring of RMF controls"
2. "the ability to conduct active cyber defense in order to respond to cyber
   threats in real time"
3. "the adoption and use of an approved DevSecOps reference design"

Specific requirements stated in the memorandum:

- "For cATO, all security controls will need to be fed into a system level
  dashboard view, providing a real time and robust mechanism for AOs to view
  the environment."
- Automated monitoring "should be as near real time as feasible." Manual
  controls keep their own timelines but must be in the strategy.
- On active cyber defense: "Simply conducting scans and patching does not meet
  the threshold for active cyber defense." Systems must show near real time
  ability to deploy countermeasures.
- AOs and AODRs must maintain constant communication with CSSPs, component
  cyber operations forces, JFHQ-DoDIN and USCYBERCOM.
- Approval path: AO notifies the component CISO, then AO and component CISO
  present the request and body of evidence to the DoD CISO.
- "DoD CISO approved cATOs do not have an expiration date and will remain in
  effect as long as the required real time risk posture is maintained."
- "The cATO determination does not affect the underlying system ATO. Rather, it
  modifies requirements for re-authorizing that system's ATO."
- Revocation triggers: poor cybersecurity posture found through continuous
  monitoring or external assessment, changes in risk tolerance, or an incident
  resulting from poor adherence to practices. "A system can temporarily lose
  its cATO privilege without any loss of existing ATO."

### 5.3 The cATO package: what a team must actually produce

The *cATO Evaluation Criteria* (29 May 2024) lists what the DoD CISO looks for.
Bolded items in the source are documents or artefacts that must be delivered
with the application. Condensed, by competency:

**Continuous monitoring**

- cATO Risk Management Strategy, including established risk tolerances and
  insider/external threat tracking, in accordance with DoDI 8510.01.
- System CONMON Strategy, including a plan to continuously assess and track
  vulnerabilities on all system assets; implementation, effectiveness and
  impact measures; explicit monitoring timelines per control ("automated every
  hour, minute, second; manual once a year, etc."); coupling to auditing and
  incident response.
- System Authorization Boundary Diagram with data flows including PII, and
  detail on all external connections per the DISA Connection Process Guide.
- Business rules covering named cybersecurity roles, a vulnerability
  coordination POC, staffing aligned to detected vulnerabilities, and real-time
  AO access to testing, scanning, monitoring and performance metrics.
- Automated monitoring: a demonstrated live dashboard, alerting, and compliance
  reporting into the Continuous Monitoring & Risk Scoring (CMRS) system of
  record by automated process where possible, manual otherwise. Explicitly:
  "the software development pipeline and its environment must be monitored as
  well."
- System Authorization Package documentation from the component's RMF inventory
  tool: Security Assessment Plan (SAP), Security Assessment Report (SAR), Risk
  Assessment Report (RAR), System Security Plan (SSP), signed ATO memo, POA&Ms,
  and updates driven by the CONMON process.
- A security assessment covering the full lifecycle including the delivery
  pipeline, addressing threat modelling and vulnerability analysis, independent
  verification of assessment plans and evidence, penetration testing, attack
  surface reviews, manual code reviews, T&E scope verification, SAST, DAST and
  IAST.
- COOP/DRP with evidence of testing, incident response plan with evidence of
  exercises, detection and response capability evidence.
- Continuous vulnerability management documentation with published remediation
  timelines, findings tracked in Deficiency Reports and system-level POA&Ms,
  scanning per DoDI 8530.01. "Critical and moderate vulnerabilities are
  documented upon discovery and mitigated within a timeframe acceptable to the
  AO."
- Audit log collection, analysis, alerting, review and retention per NIST
  SP 800-53 controls, evaluated against Appendix A of OMB M-21-31.
- cATO Approval Memo from the RMF Knowledge Service template.

**Active cyber defense**

- Certified CSSP arrangement per DoDI 8530.01, with SLA for external, or
  documented methodology for internal, and evidence the CSSP is trained on
  DevSecOps for the software factories it monitors.
- "A penetration test must be completed on development and operational
  environments by a qualified third party within 90 days and annually
  thereafter," via Cyber Operations Rapid Assessment (CORA), red/blue team
  assessment, or pen testing. Anything else needs an Exception-to-Policy.
- Vulnerability and penetration assessment, AO-provided results with planned
  mitigations, updated POA&Ms, lessons-learned tracking.
- Ongoing security testing against real-world adversary tactics and techniques,
  with a strategy and budget for automated testing resources.

**Secure software supply chain and DevSecOps**

*Authorize the Platform*: identify the reference design the platform adheres
to. Provide an SBOM for the platform with a statement of how it was developed,
an automated SBOM export for applications passing through it, the SBOM format
and generation frequency, an archive of SBOMs, and an explanation of how new
CVEs are applied against them. Provide an **Activities and Tools mapping**
based on the Activities & Tools Guidebook showing required and preferred
activities against the implementation, plus POA&Ms or a roadmap for continuous
improvement, plus live demonstration of selected activities. Provide cloud
native protection across artifact scanning (SCA, SAST, DAST, IAST), cloud
configuration (CSPM, CIEM, IaC scanning) and runtime (CWPP, CDR).

*Authorize the Process*: reliance on IaC and CaC to avoid environment drift. A
description of each control gate and what triggers it to open and close, what
triggers an alert and how to respond, plus a live demonstration or dashboard
screenshots of each gate. A description of each guardrail and the process when
something falls outside its risk tolerance.

*Authorize the People*: org chart of DSO team roles, demonstrated separation of
duties and least privilege, periodic tabletop exercises with After Action
Reports, documented training and certification process, per-role training
verification, cyber workforce qualification per DoDM 8140.03, an active insider
threat working group chaired by senior leadership, and a defined
onboarding/offboarding process with evidence it applies "without regard to
their rank or position."

Training must specifically cover the applicable reference design version,
security automation tools, CI/CD control gates and promotion rules, established
risk tolerances, adjudication of findings that exceed tolerances, root cause
analysis of critical and substantive findings, continuous monitoring feedback
loops, and POA&M and dashboard practice.

**The tooling implication.** The unit of authorisation is the software factory,
not the release. Roughly: continuously emitted signals (dashboard feeds, CMRS
reporting, scan results, gate outcomes, SBOMs per build, audit logs) plus
periodically refreshed documents (strategies, SSP, SAR, RAR, POA&Ms, boundary
diagram, penetration test results at 90 days then annually) plus demonstrable
live behaviour (gates firing, dashboards in operation). A tool that produces a
report at release time and nothing between releases does not meet this.

## 6. Agile and compliance: the real tensions and how the guidance resolves them

### 6.1 Documentation expectations against working software

**The tension is acknowledged in the source.** Fundamentals v2.0 states the
DevSecOps cybersecurity culture "embraces another core Agile tenet that prefers
work[ing] software over comprehensive documentation," and criticises reliance
on "post-process paperwork evaluations." The same corpus then requires SAP,
SAR, RAR, SSP, POA&Ms, boundary diagrams, CONMON strategies, After Action
Reports and training records.

**How the guidance resolves it: substitute generated evidence for authored
documents, and demonstration for description.**

- SSDF PO.3.3 requires tools to be configured to generate artefacts of their
  support for secure development, with an audit trail from existing workflow
  and issue tracking. SSDF PO.4.2 requires the toolchain to gather the data
  automatically.
- The cATO criteria repeatedly ask for demonstration rather than narrative:
  "Demonstrate each control gate in action (this may be in a non-production
  environment) or provide screen shots of control gate output as displayed in a
  dashboard," and "Status of dashboarding activities, including a demonstration
  of the dashboard in operation."
- On training records: "Documentation need not be text documents but may be in
  online learning management tools that assessors can view."
- The cATO Evaluation Criteria states the shift plainly: cATO "moves away from
  solely a document-based, point-in-time technical security assessment
  approach (though some point-in-time documents are still required), towards
  focusing on a continuous risk determination and authorization concept."

**Honest assessment**: this is a partial resolution. The parenthetical "though
some point-in-time documents are still required" is doing real work. The
document set is not eliminated, it is reduced and re-sourced. Tooling that can
render an SSP or a POA&M from live pipeline state, rather than requiring
someone to write one, is targeting the actual gap.

### 6.2 Phase gates against continuous delivery

**The tension is acknowledged and resolved explicitly.**

Fundamentals presents the lifecycle in two forms and pre-empts the objection:
"While some may view this graphic as a waterfall process, this graphic contains
the identical set of steps depicted previously in Figure 1 as an infinite loop
but is 'unfolded' to effectively illustrate the multiplicity of continuous
feedback loops."

The substantive resolution has three parts:

1. **Gates are per-iteration, not per-programme.** They sit at merge, at
   promotion to integration, and at release, inside a loop that runs on every
   change.
2. **Gates are mandatory but need not start automated.** The v2.0 sentence
   quoted in section 3.2 is explicit: initial human intervention is expected,
   and automation goes on the backlog as the team matures.
3. **Automation is the exit from the tension.** The Activities & Tools
   Guidebook allows scaling activity complexity and frequency "upward or
   downward along a continuum to fit the specific needs of each adopting
   organization," while holding REQUIRED activities non-negotiable. You may
   tune how often and how heavily, not whether.

### 6.3 Authorisation boundaries against frequent release

**The sharpest tension, and the one with the most concrete resolution.**

Traditional RMF authorises a system, at a point in time, within a boundary. A
team deploying daily cannot re-authorise daily. DoD resolves this three ways,
which stack:

**Resolution 1: authorise the factory, not the release.** cATO applies to the
software factory and its processes, teams and storage. Fundamentals v2.5: "each
software factory will have its processes, teams, and storage reviewed,
certified, and continuously monitored to allow them to deploy applications into
a continuously monitored system. This shift greatly lessens the initial burden
of achieving an ATO for each piece of software, as the process and roll out are
certified." The cATO Evaluation Criteria: "A software factory with a cATO is
allowed to continuously develop, assess, and deploy software that meets the
risk tolerances laid out within a system authorization boundary."

The boundary does not move with each release. Release stays inside a
pre-agreed risk tolerance band, and the guardrail concept is what defines that
band.

**Resolution 2: Assess Only, plus control inheritance.** The RMF Knowledge
Service page *Software Acquisition Pathway Integration with Risk Management
Framework* states it directly: "Because software does not need to undergo the
full RMF process, the SWP utilizes the Assess Only construct, which requires
due diligence reviews of the software's function, environment, quality control,
and data usage and creation. The use of enterprise services allows mission
owners to inherit controls and reliable infrastructure, manage a smaller set of
controls, and instead focus on innovating and delivering applications."

The same page tells programmes to identify inheritable common and hybrid
controls during the Planning phase so as to "minimize mitigations that are the
software's responsibility," and states that "the testing and mitigation of the
inherited controls belongs to the platform and service providers." It also
directs programme managers to "leverage existing development environment
platforms and tools ... which already have an authorization to operate (ATO) or
cATO."

The obligation that remains: "As software is acquired and continuously
integrated into operational environments, the hosting system's Security Plan
and other documentation must be accurately updated to account for any changes
the software introduces."

**Resolution 3: reciprocity.** DoDI 8510.01: "The DoD Information Enterprise
will use cybersecurity reciprocity to reduce redundant testing, assessing,
documenting, and the associated costs in time and resources." AOs are told to
"Promote reciprocity as much as possible." Where the DoD ISRMC accepts risk on
behalf of the enterprise, "the receiving organization may not refuse to deploy
the system." The container hardening guide makes the same point for artefacts:
hardened containers "along with security accreditation reciprocity, greatly
simplifies and speeds the process of obtaining an Approval to Connect (ATC) or
Authority to Operate."

**The acquisition side reinforces it.** DoDI 5000.87 requires programmes on the
software acquisition pathway to "use modern iterative software development
methodologies (e.g., agile or lean), modern tools and techniques (e.g., ...
DevSecOps)," to deliver capability within one year of first obligation of funds
and at least annually thereafter, and states: "Automated cyber testing and
continuous monitoring of operational software will be designed and implemented
to support a cATO or an accelerated accreditation process to the maximum extent
practicable." It also says program documentation "will be tailored to what is
needed to effectively manage the program," with the decision authority
approving the tailoring. The RMF KS page raises the tempo target further: SWP
deployment "within 6 months or less," with the goal of "hours or days, not
months or years."

### 6.4 Where the guidance does not resolve the tension

Stated plainly, because pretending otherwise would produce wrong tooling:

- **Penetration testing cadence versus continuous delivery.** The cATO criteria
  require third-party penetration testing of development and operational
  environments within 90 days and annually thereafter. Nothing in the corpus
  reconciles an annual third-party assessment with daily deployment. It is an
  unresolved point-in-time control inside a continuous regime.
- **Vulnerability remediation timelines are undefined.** The criteria say
  critical and moderate vulnerabilities must be "mitigated within a timeframe
  acceptable to the AO." No number. Per-AO configurability is mandatory in
  tooling; a hard-coded SLA will be wrong somewhere.
- **The cATO approval path is itself a phase gate.** AO to component CISO to
  DoD CISO, with a body of evidence. Getting cATO is a heavyweight,
  point-in-time, human-adjudicated event. The continuous regime begins after
  it.
- **Code review is REQUIRED at the merge gate in the Fundamentals narrative but
  PREFERRED in the Activities & Tools Guidebook Develop table.** The corpus
  does not reconcile this.
- **Reciprocity is policy, not a mechanism.** DoDI 8510.01 mandates its use and
  charters the RMF TAG to "develop guidance for facilitating RMF reciprocity."
  How one AO consumes another's body of evidence in practice is not specified
  in the public documents reviewed here.
- **Documentation reduction has no floor.** DoDI 5000.87 permits tailoring but
  gives the decision authority the call, and cATO still requires the classic
  package. The team cannot know in advance how much documentation it owes.

## 7. Not verified

Items I could not confirm from a primary source, and why.

- **SWFT Framework and Implementation Plan.** The April 2025 memorandum
  directed DoD CIO to submit a SWFT Framework and Implementation Plan within 90
  days. I found the memorandum and the SWFT RFI Combined Summary on
  dodcio.defense.gov, but **no published SWFT Framework or Implementation
  Plan**. Whether it exists publicly, and what it requires, is unverified. Do
  not build against SWFT specifics.
- **The exact date of the cATO memorandum.** The memorandum PDF text I
  extracted carries no legible date. The DoD CIO filename is
  `20220204-cATO-memo-Signed-Cleared.pdf`, the RMF Knowledge Service page cites
  it as "February 2, 2022," and a media.defense.gov copy is filed under
  2022/Feb/03. I use "February 2022" and flag the day as unresolved.
- **The exact date on the "Accelerating Secure Software" memorandum.** The
  header date extracted as "APR 14 Wi" (OCR failure). The digital signature
  block reads 2025.04.24, and the SWFT RFI Combined Summary foreword states
  24 April 2025. I use April 2025.
- **Which DoD Enterprise DevSecOps Reference Designs are currently approved.**
  Fundamentals v2.0 (March 2021) states CNCF Kubernetes is "presently the only
  approved DoD Enterprise DevSecOps Reference Design." Fundamentals v2.5
  (October 2024) does not repeat that claim and refers to "DoD Approved
  Reference Designs" plural at the DoD CIO library. Documents visible in the
  library include the base Reference Design, CNCF Kubernetes, CNCF Multi-Cluster
  Kubernetes, an Azure/GitHub design, and "Pathway to a Reference Design." **I
  did not verify which of these carry current approval.** Treat the approved
  list as a runtime lookup, not a constant.
- **DoD Hardened Containers Cybersecurity Requirements themselves.** Appendix B
  of the DISA Container Hardening Process Guide. I confirmed the appendix exists
  and the guide's version (V1R2, 24 August 2022) but did not extract the
  requirement list. Individual STIG and SRG identifiers and versions were not
  enumerated.
- **Any FAR or DFARS clause imposing SSDF or SBOM.** Searches surfaced only
  law-firm and vendor commentary, no primary rule text. The DoD cATO Evaluation
  Criteria (May 2024) says SBOM "is currently undergoing regulatory action,
  Defense Information Systems Agency (DISA) Federal Acquisition Regulation (FAR)
  is the lead," which indicates a rulemaking was in progress as of that date. I
  could not verify whether any such rule was finalised, nor its number.
  **Do not assert a FAR or DFARS SSDF/SBOM clause exists.** DFARS 252.204-7012
  and CMMC concern NIST SP 800-171 protection of CUI on contractor systems, a
  different obligation from SSDF, and I did not verify their current status.
- **Whether DoD has separately re-imposed SSDF attestation after OMB M-26-05.**
  I found no DoD issuance doing so. Absence of evidence only.
- **NIST SP 800-218 Rev. 1 (SSDF 1.2) content.** I confirmed the draft's
  existence, publication date (17 December 2025) and closed comment period
  (30 January 2026). I did not read the draft, so I make no claim about its
  practice structure or whether identifiers changed.
- **NIST SP 800-218A publication date.** The SSDF project page lists it as
  final; I did not retrieve its date or identifiers.
- **NIST SP 800-53 revision.** Referenced throughout the DoD corpus without a
  revision number in the passages I read. I did not verify the current
  revision, so this document does not state one.
- **DoDI 8510.01 change status.** I read the 19 July 2022 issuance from the
  Directives Division. I did not verify whether administrative changes have been
  issued since.
- **DoDI 5000.87 change status.** Same. I read the 2 October 2020 issuance.
- **The three "cyber survivability control gates."** Fundamentals v2.5 refers
  to "three distinct cyber survivability control gates" but I did not find them
  individually named and defined. My three-gate description in section 3.2 is
  reconstructed from the narrative walkthrough (merge, test-to-integration,
  integration-to-release), not from a labelled list.
- **DoD CIO DevSecOps Continuous Authorization Implementation Guide content.**
  Retrieved but marked Distribution Statement C. Not summarised.
- **CISA SBOM for AI Minimum Elements.** Referenced in a footnote of the 2026
  SBOM document as released with G7 partners in May 2026. Not retrieved.

### Source accessibility notes

dodcio.defense.gov, media.defense.gov, esd.whs.mil and cisa.gov all reject
plain automated requests with HTTP 403. All documents cited here were retrieved
successfully with standard browser request headers, and every DoD PDF cited was
read from the file, not from a summary. The one exception is noted above:
`media.defense.gov` remained inaccessible, so the cATO memorandum was read from
the dodcio.defense.gov copy instead.

## Sources

Primary, NIST:

- [NIST SP 800-218, Secure Software Development Framework (SSDF) Version 1.1](https://csrc.nist.gov/pubs/sp/800/218/final) and the [full PDF](https://nvlpubs.nist.gov/nistpubs/SpecialPublications/NIST.SP.800-218.pdf)
- [NIST Secure Software Development Framework project page](https://csrc.nist.gov/projects/ssdf)
- [NIST SP 800-218 Rev. 1 (Draft), SSDF Version 1.2](https://csrc.nist.gov/pubs/sp/800/218/r1/ipd)
- [NIST SP 800-37 Rev. 2, Risk Management Framework for Information Systems and Organizations](https://csrc.nist.gov/pubs/sp/800/37/r2/final)
- [NIST SP 800-161 Rev. 1, Cybersecurity Supply Chain Risk Management Practices for Systems and Organizations](https://csrc.nist.gov/pubs/sp/800/161/r1/upd1/final)

Primary, executive and OMB:

- [OMB M-26-05, Adopting a Risk-based Approach to Software and Hardware Security (23 January 2026)](https://www.whitehouse.gov/wp-content/uploads/2026/01/M-26-05-Adopting-a-Risk-based-Approach-to-Software-and-Hardware-Security.pdf)
- [OMB M-23-16, Update to Memorandum M-22-18 (9 June 2023), rescinded](https://www.cisa.gov/sites/default/files/2024-07/M-23-16_Enhancing_the_Security_of_Software_Supply_Chain_via_Secure_Software_Practices.pdf)
- [Executive Order 14306, Sustaining Select Efforts to Strengthen the Nation's Cybersecurity and Amending Executive Order 13694 and Executive Order 14144 (6 June 2025)](https://www.whitehouse.gov/presidential-actions/2025/06/sustaining-select-efforts-to-strengthen-the-nations-cybersecurity-and-amending-executive-order-13694-and-executive-order-14144/)
- [Executive Order 14144, Strengthening and Promoting Innovation in the Nation's Cybersecurity (Federal Register)](https://www.federalregister.gov/documents/2025/01/17/2025-01470/strengthening-and-promoting-innovation-in-the-nations-cybersecurity)

Primary, CISA:

- [CISA Secure Software Development Attestation Form](https://www.cisa.gov/resources-tools/resources/secure-software-development-attestation-form)
- [CISA 2026 Minimum Elements for a Software Bill of Materials (SBOM), landing page](https://www.cisa.gov/resources-tools/resources/2026-minimum-elements-software-bill-materials-sbom) and [PDF](https://www.cisa.gov/sites/default/files/2026-07/2026_cisa_sbom_minimum_elements_508c.pdf)
- [CISA 2025 Minimum Elements for a Software Bill of Materials (SBOM), draft](https://www.cisa.gov/resources-tools/resources/2025-minimum-elements-software-bill-materials-sbom)

Primary, DoD:

- [DoDI 8510.01, Risk Management Framework for DoD Systems (19 July 2022)](https://www.esd.whs.mil/Portals/54/Documents/DD/issuances/dodi/851001p.pdf)
- [DoDI 5000.87, Operation of the Software Acquisition Pathway (2 October 2020)](https://www.esd.whs.mil/Portals/54/Documents/DD/issuances/dodi/500087p.pdf)
- [DoDI 5000.82, Requirements for the Acquisition of Digital Capabilities (1 June 2023)](https://www.esd.whs.mil/Portals/54/Documents/DD/issuances/dodi/500082p.pdf)
- [OSD memorandum, Continuous Authorization To Operate (cATO)](https://dodcio.defense.gov/Portals/0/Documents/Library/20220204-cATO-memo-Signed-Cleared.pdf)
- [DoD CIO, Continuous Authorization to Operate (cATO) Evaluation Criteria, DevSecOps Use Case (29 May 2024)](https://dodcio.defense.gov/Portals/0/Documents/Library/cATO-EvaluationCriteria.pdf)
- [DoD CIO, DevSecOps Continuous Authorization Implementation Guide v1.0 (March 2024, Distribution Statement C)](https://dodcio.defense.gov/Portals/0/Documents/Library/DoDCIO-ContinuousAuthorizationImplementationGuide.pdf)
- [DoD Enterprise DevSecOps Fundamentals v2.5 (approved 16 October 2024)](https://dodcio.defense.gov/Portals/0/Documents/Library/DoD%20Enterprise%20DevSecOps%20Fundamentals%20v2.5.pdf)
- [DoD Enterprise DevSecOps Fundamentals v2.0 (March 2021)](https://dl.dod.cyber.mil/wp-content/uploads/devsecops/pdf/DoDEnterpriseDevSecOpsFundamentals.pdf)
- [DoD Enterprise DevSecOps Activities & Tools Guidebook v2.5 (April 2025)](https://dodcio.defense.gov/Portals/0/Documents/Library/DevSecOpsActivitesToolsGuidebook.pdf)
- [DoD Enterprise DevSecOps Reference Design: CNCF Kubernetes v2.0 (March 2021)](https://dodcio.defense.gov/Portals/0/Documents/Library/DevSecOpsReferenceDesign.pdf)
- [DevSecOps Fundamentals Playbook v2.0 (March 2021)](https://dl.dod.cyber.mil/wp-content/uploads/devsecops/pdf/DoD-Enterprise-DevSecOps-2.0-Playbook.pdf)
- [DISA, Container Hardening Process Guide V1R2 (24 August 2022)](https://dl.dod.cyber.mil/wp-content/uploads/devsecops/pdf/Final_DevSecOps_Enterprise_Container_Hardening_Guide_1.2.pdf)
- [DoD CIO memorandum, Accelerating Secure Software (Software Fast Track, April 2025)](https://dodcio.defense.gov/Portals/0/Documents/Library/Memo-AcceleratingSecureSoftware.pdf)
- [SWFT RFI Combined Summary](https://dodcio.defense.gov/Portals/0/Documents/Library/SWFT-RFI-Combined-Summary.pdf)
- [RMF Knowledge Service, Software Acquisition Pathway Integration with Risk Management Framework](https://dodcio.defense.gov/Portals/0/Documents/Library/SWAPathwayIntegration-RMF.pdf)
- [DoD Software Modernization Implementation Plan FY25-26](https://dodcio.defense.gov/Portals/0/Documents/Library/SW-Mod-I-Plan25-26.pdf)
- [DoD CIO Library](https://dodcio.defense.gov/Library/)
- [DoD CIO, Software Fast Track Initiative announcement](https://dodcio.defense.gov/In-the-News/Article/4367436/software-fast-track-initiative/)

Secondary (used only to locate primary documents and to cross-check the
rescission timeline; no factual claim in this document rests on them alone):

- [Mayer Brown, OMB Rescinds Biden-Era Software Security Memoranda](https://www.mayerbrown.com/en/insights/publications/2026/02/omb-rescinds-biden-era-software-security-memoranda)
- [Covington, Inside Government Contracts: OMB Rescinds the "Common Form" Secure Software Attestation Requirement](https://www.insidegovernmentcontracts.com/2026/02/omb-rescinds-the-common-form-secure-software-attestation-requirement/)
- [Wiley, OMB Rescinds Secure Software Development Mandate in Favor of a Risk-Based Approach](https://www.wiley.law/alert-OMB-Rescinds-Secure-Software-Development-Mandate-in-Favor-of-a-Risk-Based-Approach)
