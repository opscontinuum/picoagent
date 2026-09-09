# The DISA Application Security and Development STIG, for engineers building tooling

**Research date:** 6 September 2026.
**Primary artifact examined:** `U_ASD_V6R4_STIG.zip`, downloaded directly from
`dl.dod.cyber.mil`, containing `U_ASD_STIG_V6R4_Manual-xccdf.xml`, the V6R4 Overview,
and the V6R4 Revision History.

Everything in this document that carries an identifier (V-number, STIG ID, CCI, NIST
control) was read out of that package or out of DISA's CCI List, not from memory. The
"Not verified" section near the end lists what could not be confirmed.

---

## 1. What this is and who it binds

### Identity of the document

From the XCCDF benchmark header:

| Field | Value |
|---|---|
| Title | Application Security and Development Security Technical Implementation Guide |
| Benchmark id | `Application_Security_Development_STIG` |
| Version | 6 |
| Release | 4 |
| Benchmark date | 01 Oct 2025 |
| XCCDF `status` | `accepted`, dated 2025-09-09 |
| Rule count | 286 |
| Severity split | 34 CAT I (high), 230 CAT II (medium), 22 CAT III (low) |

V6R4 is the current release, confirmed three ways: `U_ASD_V6R5_STIG.zip` and
`U_ASD_V7R1_STIG.zip` both return HTTP 404 while V6R4 returns 200; the live STIG library
catalogue returns exactly one item for the "Application Security and Development" facet; and
the current quarterly compilation, `U_SRG-STIG_Library_July_2026.zip`, contains
`U_ASD_V6R4_STIG.zip` and no other ASD package.

Two dates attach to this release and they are not the same. The document date is **01
October 2025**, which appears on the Overview cover, in the Revision History release-date
column, and as the XCCDF benchmark date. The library upload date is 2025-11-26. For a
compliance artifact, cite 01 October 2025; the upload date is library metadata and no DISA
statement explains the gap.

### Scope

The Overview, section 1.1, states the applicability directly:

> "The Application Security and Development STIG is designed to be applied to all
> enterprise applications connected via the network. This includes client applications
> installed on desktop computers that establish network connections to remote systems,
> HTML and browser-based applications comprising numerous web technologies and
> architectures including Java, JavaScript, .NET, Cloud, RESTful-based, and SOA-oriented
> web services. This document is a requirement for all DOD-developed, -architected, and
> -administered enterprise applications and systems connected to DOD networks."

And the exclusion:

> "The STIG is not intended to be applied to scripts, administrative or otherwise,
> firewalls, or other network devices with application management interfaces when a
> relevant product STIG or technology SRG already exists."

Two properties matter for tooling. First, the STIG is technology-neutral by design:

> "The guidance provided is not specific to any one platform, programming language, or
> application type."

Second, it is explicitly dual-purpose. It covers in-house development *and* evaluation of
software you did not write:

> "This guide is to be used for in-house application development and to assist in the
> evaluation of the security of COTS/GOTS/OSS products and any other third-party
> applications."

That second use is why a large number of rules carry a "not applicable if you are not the
developer" escape clause. 139 of the 286 check procedures contain the phrase "not
applicable". For a development team that owns its source, most of those escapes do not
apply, and the development-process requirements in section 3 below bind fully.

### Who it binds, and by what authority

The Overview, section 1.2, cites DoD Instruction 8500.01:

> "Department of Defense Instruction (DODI) 8500.01 requires that 'all IT [information
> technology] that receives, processes, stores, displays, or transmits DOD information
> will be [...] configured [...] consistent with applicable DOD cybersecurity policies,
> standards, and architectures.' The instruction tasks that DISA 'develops and maintains
> control correlation identifiers (CCIs), security requirements guides (SRGs), security
> technical implementation guides (STIGs), and mobile code risk categories and usage
> guides that implement and are consistent with DOD cybersecurity policies, standards,
> architectures, security controls, and validation procedures [...]'. This document is
> provided under the authority of DODI 8500.01."

The V4 release memo, signed by the DISA Risk Management Executive and included in the
V6R4 package, adds the compliance obligation on the consuming side, quoting DoDI 8500.01:

> "DoD Component heads 'ensure that all DoD IT under their purview complies with
> applicable STIGs, security configuration guides, and SRGs.'"

The Overview also scopes what a STIG is *not*:

> "The existence of a STIG does not equate to DOD approval for the procurement or use of
> a product."

and

> "STIGs, along with vendor confidential documentation, also provide a basis for assessing
> compliance with cybersecurity controls/control enhancements, which supports system
> assessment and authorization (A&A) under the DOD Risk Management Framework (RMF)."

Deviations are an Authorizing Official decision, not a developer decision:

> "The evaluated risks resulting from not applying specified configuration settings must be
> approved by the responsible AO."

### Severity categories

Reproduced from Overview Table 1-1:

| Category | DISA definition |
|---|---|
| CAT I | "Any vulnerability, the exploitation of which will directly and immediately result in loss of Confidentiality, Availability, or Integrity." |
| CAT II | "Any vulnerability, the exploitation of which has a potential to result in loss of Confidentiality, Availability, or Integrity." |
| CAT III | "Any vulnerability, the existence of which degrades measures to protect against loss of Confidentiality, Availability, or Integrity." |

In the XCCDF these appear as `severity="high" | "medium" | "low"` on each `Rule`. A tool
must translate: `high` is CAT I.

### Relationship to the SRG, and to release cadence

DISA publishes technology-agnostic Security Requirements Guides and technology-specific
STIGs derived from them. The ASD STIG carries its SRG linkage in a place that is easy to
miss: **the SRG requirement ID is the `Group/title`, not an `ident`**. Every one of the 286
groups in the V6R4 XCCDF has an `SRG-APP-xxxxxx` string as its group title. For example
`V-222387` sits under `SRG-APP-000001`, and `V-222607` (SQL injection) under
`SRG-APP-000251`.

The mapping is many-to-one: 286 rules resolve to 189 distinct SRG-APP identifiers. A tool
can therefore recover the full STIG-to-SRG mapping from the manual XCCDF alone, by reading
group titles.

One detail matters for anyone reasoning about where the process requirements come from.
`SRG-APP-000516` is a catch-all, used by 57 of the 286 rules, and **almost the entire
development-process family sits under it**: V-222624, V-222632, V-222633, V-222644,
V-222646, V-222648, V-222649, V-222650, V-222651, V-222652, V-222653, V-222654, V-222655,
V-222657, and V-222673 all carry `SRG-APP-000516` as their group title. The process rules
are not derived from individually numbered SRG requirements; they hang off the general
bucket. That is worth knowing before trying to trace a process finding back to a specific
SRG requirement, because the trace terminates in a catch-all.

The derivation is actively maintained: the V5R1 revision history entry reads
"APSC-DV-000610 - Removed requirement based on SRG-APP-000353 removal", showing ASD STIG
rules being retired when the upstream SRG requirement is retired.

On cadence, the ASD Overview says only:

> "Approved changes will be made in accordance with the DISA maintenance release schedule."

It does not itself use the word "quarterly", but DISA states the quarterly cycle in the
readme it ships with the SRG-STIG Library Compilation:

> "Draft and Sunset SRGs and STIGs are excluded from the library compilation. New releases
> that occur mid-cycle must be individually downloaded until the next quarterly release of
> the library compilation is published."

The observed ASD release dates from the V6R4 Revision History are:

| Release | Date |
|---|---|
| V6R4 | 01 October 2025 |
| V6R3 | 02 April 2025 |
| V6R2 | 30 January 2025 |
| V6R1 | 24 July 2024 |
| V5R3 | 26 July 2023 |
| V5R2 | 27 October 2022 |
| V5R1 | 23 October 2020 |

The dates line up with the quarterly maintenance windows (January, April, July, October),
and the ASD STIG skips windows when it has no changes. Note the gap between April 2025 and
October 2025: there was a July 2025 maintenance release, but ASD was not in it. A tool that
pins a version must therefore re-check on a quarterly boundary and must not assume a new
ASD release every quarter.

Two changes are worth knowing because they move identifiers:

- V6R1 (24 July 2024) was a major revision: "Updated based on NIST SP 800-53 Rev. 5
  changes." That is why the version incremented to a whole number.
- Several releases note "Rule numbers updated throughout due to changes in content
  management system." The `SV-......r......_rule` revision suffix churns between releases
  even when requirement text does not. Key on the V-number or the STIG ID, never on the
  full SV-rule string.

---

## 2. The requirement families, and what each means for code

Grouping below is mine, derived from reading the 286 rule titles. DISA does not ship these
groupings; it ships a flat list keyed by STIG ID (`APSC-DV-xxxxxx`). Identifiers in each
table are verbatim from the V6R4 XCCDF.

### 2.1 Input validation and injection

The largest cluster of CAT I findings in the whole STIG.

| V-number | STIG ID | CAT | Requirement |
|---|---|---|---|
| V-222606 | APSC-DV-002530 | II | The application must validate all input. |
| V-222607 | APSC-DV-002540 | I | The application must not be vulnerable to SQL Injection. |
| V-222604 | APSC-DV-002510 | I | The application must protect from command injection. |
| V-222608 | APSC-DV-002550 | I | The application must not be vulnerable to XML-oriented attacks. |
| V-222609 | APSC-DV-002560 | I | The application must not be subject to input handling vulnerabilities. |
| V-222612 | APSC-DV-002590 | I | The application must not be vulnerable to overflow attacks. |
| V-222605 | APSC-DV-002520 | II | The application must protect from canonical representation vulnerabilities. |
| V-222602 | APSC-DV-002490 | I | The application must protect from Cross-Site Scripting (XSS) vulnerabilities. |
| V-222603 | APSC-DV-002500 | II | The application must protect from Cross-Site Request Forgery (CSRF) vulnerabilities. |
| V-222567 | APSC-DV-001995 | II | The application must not be vulnerable to race conditions. |

What this means for code: validate at every entry point, parameterise every query, encode
on output, canonicalise before you compare, and bound every buffer. What it means for
*evidence* is the more important point, and it is easy to miss. Read the check text for
V-222607:

> "Request the latest vulnerability scan test results. Verify the scan configuration is
> configured to test for SQL injection flaws. [...] If the scan results are not available
> [...] this is a finding."

The finding condition is not only "the code is vulnerable". It is also "you cannot produce
a scan result configured to look for this". Absence of evidence is a finding. V-222612 says
it plainly: "if test results are not available, this is a finding." A team with clean code
and no retained scan artifact fails these rules.

Note the STIG cites OWASP testing guides by URL in several check procedures (for example
V-222604 and V-222606). Those `owasp.org/index.php/...` wiki URLs are stale in the V6R4
text.

### 2.2 Output encoding and information leakage

| V-number | STIG ID | CAT | Requirement |
|---|---|---|---|
| V-222600 | APSC-DV-002480 | II | The application must not disclose unnecessary information to users. |
| V-222601 | APSC-DV-002485 | I | The application must not store sensitive information in hidden fields. |
| V-222610 | APSC-DV-002570 | II | Error messages must provide information necessary for corrective actions without revealing information that could be exploited by adversaries. |
| V-222611 | APSC-DV-002580 | II | The application must reveal error messages only to the ISSO, ISSM, or SA. |
| V-222444 | APSC-DV-000650 | II | The application must not write sensitive data into the application logs. |

V-222610 gives an unusually concrete pass/fail line, which makes it one of the few rules
with a testable code-level predicate:

> "If variable names, SQL strings, system path information, or source or program code are
> displayed in error messages sent to non-privileged users, this is a finding."

### 2.3 Error handling

| V-number | STIG ID | CAT | Requirement |
|---|---|---|---|
| V-222656 | APSC-DV-003235 | II | The application must not be subject to error handling vulnerabilities. |
| V-222585 | APSC-DV-002310 | I | The application must fail to a secure state if system initialization fails, shutdown fails, or aborts fail. |
| V-222586 | APSC-DV-002320 | II | In the event of a system failure, applications must preserve any information necessary to determine cause of failure. |

V-222656 is checked against "the results from static code analysis tools", and again:
"If no test results are available for review, this is a finding."

### 2.4 Authentication, session management, and credentials

The largest family by rule count. It spans V-222387 to V-222392 (session lifecycle),
V-222520 to V-222560 (identification and authentication, PIV, multifactor, FICAM), and
V-222575 to V-222584 (session identifier handling). Selected code-relevant rules:

| V-number | STIG ID | CAT | Requirement |
|---|---|---|---|
| V-222642 | APSC-DV-003110 | I | The application must not contain embedded authentication data. |
| V-222542 | APSC-DV-001740 | I | The application must only store cryptographic representations of passwords. |
| V-222543 | APSC-DV-001750 | I | The application must transmit only cryptographically-protected passwords. |
| V-222554 | APSC-DV-001850 | I | The application must not display passwords/PINs as clear text. |
| V-222577 | APSC-DV-002230 | I | The application must not expose session IDs. |
| V-222578 | APSC-DV-002240 | I | The application must destroy the session ID value and/or cookie on logoff or browser close. |
| V-222575 | APSC-DV-002210 | II | The application must set the HTTPOnly flag on session cookies. |
| V-222576 | APSC-DV-002220 | II | The application must set the secure flag on session cookies. |
| V-222579 | APSC-DV-002250 | II | Applications must use system-generated session identifiers that protect against session fixation. |
| V-222581 | APSC-DV-002270 | II | Applications must not use URL embedded session IDs. |
| V-222582 | APSC-DV-002280 | II | The application must not re-use or recycle session IDs. |
| V-222583 | APSC-DV-002290 | II | The application must generate a unique session identifier using a FIPS 140-2/140-3 approved random number generator. |
| V-222389 | APSC-DV-000070 | II | Terminate the non-privileged user session after a 15 minute idle time period. |
| V-222390 | APSC-DV-000080 | II | Terminate the admin user session after a 10 minute idle time period. |
| V-222536 | APSC-DV-001680 | I | The application must enforce a minimum 15-character password length. |
| V-222662 | APSC-DV-003280 | I | Default passwords must be changed. |

V-222642 is one of the very few rules whose check procedure sends the reviewer into the
source tree rather than into a report:

> "Review the application documentation and any available source code; this includes
> configuration files such as global.asa, if present, scripts, HTML files, and any ASCII
> files. Identify any instances of passwords, certificates, or sensitive data included in
> code."

That is a secret-scanning problem, and it is directly automatable.

### 2.5 Access control

V-222425 (CAT I, "enforce approved authorizations for logical access") and V-222430 (CAT I,
"The application must execute without excessive account permissions") are the two that most
often shape code and deployment manifests. V-222429 covers preventing non-privileged users
from executing privileged functions. V-222590 and V-222591 require isolating security
functions and maintaining a separate execution domain per process.

### 2.6 Cryptography

| V-number | STIG ID | CAT | Requirement |
|---|---|---|---|
| V-222588 | APSC-DV-002340 | I | The application must implement approved cryptographic mechanisms to prevent unauthorized modification of information at rest. |
| V-222589 | APSC-DV-002350 | I | The application must use appropriate cryptography in order to protect stored DOD information. |
| V-222596 | APSC-DV-002440 | I | The application must protect the confidentiality and integrity of transmitted information. |
| V-222570 | APSC-DV-002020 | II | The application must utilize FIPS-validated cryptographic modules when signing application components. |
| V-222571 | APSC-DV-002030 | II | The application must utilize FIPS-validated cryptographic modules when generating cryptographic hashes. |
| V-222572 | APSC-DV-002040 | II | The application must utilize FIPS-validated cryptographic modules when protecting unclassified information that requires cryptographic protection. |
| V-265634 | APSC-DV-002010 | II | The application must implement NSA-approved cryptography to protect classified information. |

The operative constraint for code is FIPS validation of the *module*, not merely the
choice of a strong algorithm. V-265634 is the one rule outside the V-2223xx to V-2226xx
block, added later than the original set.

### 2.7 Logging and auditing

Roughly 80 rules, V-222439 through V-222509 plus V-222672. They divide into: what events
must generate records (account lifecycle, privilege grant/modify/delete, security object
access, logon success and failure, session ID creation/destruction/renewal), what fields
each record must carry (timestamp mappable to UTC with one-second granularity, user
identity, outcome, originating component), and how records must be protected (off-loaded
to a separate system, integrity-protected cryptographically, protected from read, modify,
and delete).

For code, this is a design constraint on the logging layer: a structured event schema with
identity, outcome, and component fields, emitted at defined choke points, shipped off-box.
V-222444 pulls the other way: do not log secrets. Those two requirements are in tension and
the resolution belongs in a documented logging standard.

### 2.8 Dependency and supply chain

This is the family that is *thinner than expected*, and the gap is worth stating plainly
for anyone building tooling against the ASD STIG.

Searching the V6R4 XCCDF for supply-chain vocabulary returns no hits for "SBOM" or
"software bill of materials", and no hits for "supply chain". The rules that carry the
load are:

| V-number | STIG ID | CAT | Requirement |
|---|---|---|---|
| V-222658 | APSC-DV-003240 | I | All products must be supported by the vendor or the development team. |
| V-222659 | APSC-DV-003250 | I | The application must be decommissioned when maintenance or support is no longer available. |
| V-222614 | APSC-DV-002630 | II | Security-relevant software updates and patches must be kept up to date. |
| V-222513 | APSC-DV-001430 | II | Prevent installation of patches, service packs, or application components without verification the software component has been digitally signed. |
| V-222514 | APSC-DV-001440 | II | The applications must limit privileges to change the software resident within software libraries. |
| V-222645 | APSC-DV-003140 | II | Application files must be cryptographically hashed prior to deploying to DoD operational networks. |

V-222658 is a CAT I and its check is an inventory exercise: "Identify all software
components. Review the version information and identify the vendor if COTS software.
Access the vendor website to verify the version is still supported." Against a modern
dependency tree this is a software composition analysis problem even though the STIG never
names one.

V-222614's check gives the cadence: "If application updates are not checked on at least on
a weekly basis and applied immediately or in accordance with POA&Ms, IAVMs, CTOs, DTMs or
other authoritative patching guidelines or sources, this is a finding."

V-222645 names the algorithm: "Currently, SHA256 is the DoD approved standard for
cryptographic hash functions."

The closest the STIG comes to naming dependency vulnerability scanning is inside the
discussion for V-222624, where it says automated scanning tools "test for libraries and
other software modules known to be vulnerable to attack".

---

## 3. The process requirements

This is the section that matters most for an Agile team, because these rules are not about
what the code does. They are about whether a development process exists, is documented, and
produced retained artifacts. They are also the rules most often overlooked, because they do
not show up in a scanner.

All of the following are verbatim titles from the V6R4 XCCDF. The CCI and NIST columns are
resolved against DISA's CCI List, version 2026-07-14.

### 3.1 Code review

| V-number | STIG ID | CAT | CCI | NIST SP 800-53 Rev 5 |
|---|---|---|---|---|
| V-222648 | APSC-DV-003170 | II | CCI-003187 | SA-11 (4) |
| V-222650 | APSC-DV-003190 | II | CCI-003161 | SA-10 e |
| V-222649 | APSC-DV-003180 | III | CCI-003188 | SA-11 (4) |

**V-222648, "An application code review must be performed on the application."** The
discussion defines the practice and the timing:

> "A code review is a systematic evaluation of computer source code conducted for the
> purposes of identifying and remediating the security flaws in the software."

> "The code review is conducted during the application development phase, this allows
> discovered security issues to be corrected prior to release."

> "Automated code review tools are to be used whenever reviewing application source code."

It enumerates the flaw classes the review must look for, and the check text repeats the
list as the pass condition:

> "Ensure the code review looks for all known security flaws including but not limited to:
> - format string exploits
> - memory leaks
> - buffer overflows
> - race conditions
> - sql injection
> - dead/unused/commented code
> - input validation exploits"

Finding condition: "If the organization does not conduct code reviews on the application
that attempt to identify all known and potential security issues, or if code review results
are not available for review, this is a finding."

Note "dead/unused/commented code" sits in a security flaw list alongside SQL injection.
That is a linting concern promoted to a compliance concern.

**V-222650, "Flaws found during a code review must be tracked in a defect tracking
system."** The check accepts a separate tool: "The configuration management repository may
consist of a separate application for capturing code defects." An issue tracker linked to
the repository satisfies this.

**V-222649, "Code coverage statistics must be maintained for each release of the
application."** The discussion is explicit that full coverage is not demanded, and that
risk-prioritised partial coverage is acceptable:

> "Some applications are so large that it is not feasible to test every last bit of the
> application code on one release cycle. In those instances, it is acceptable to prioritize
> and identify the modules that are critical to the applications security posture and test
> those first."

The check is thin: "Ask the application representative to provide code coverage statistics
maintained for the application. If these code coverage statistics do not exist, this is a
finding." A coverage report per release satisfies it.

### 3.2 Static and dynamic analysis, and vulnerability testing

| V-number | STIG ID | CAT | CCI | NIST SP 800-53 Rev 5 |
|---|---|---|---|---|
| V-222515 | APSC-DV-001460 | II | CCI-000366 | CM-6 b |
| V-222624 | APSC-DV-002930 | II | CCI-000256 | CA-2 (2) |
| V-222646 | APSC-DV-003150 | II | CCI-003182 | SA-11 (2) |

The Overview separates the two tool classes, and this distinction drives what a pipeline
must run. Section 4.1:

> "An application code scanner is an automated tool that analyzes application source code
> for security flaws, malicious code, and back doors. [...] Application code scanners must
> be utilized whenever possible. Particularly in the development environment where code
> that has been identified as requiring remediation can be addressed prior to release."

Section 4.2:

> "An application scanner, sometimes referred to as an active vulnerability testing tool,
> is a tool that is able to communicate with the application and test the application for
> known security vulnerabilities. [...] Application vulnerability scans must be utilized
> and conducted on a regular basis, such as after any product updates or major
> reconfigurations and prior to activating new applications in their production
> environment."

**V-222624, "The ISSO must ensure active vulnerability testing is performed."** This rule
carries the hardest deadline in the whole process set:

> "If the vulnerability scan results include critical vulnerabilities 21 business days or
> older, this is a finding."

It also mandates fuzzing specifically: "If the application test procedures and test results
do not include active vulnerability and fuzz testing, this is a finding." The discussion
defines the term: "Fuzz testing is a testing process where the application is provided
invalid, unexpected, or random data."

**V-222515, "An application vulnerability assessment must be conducted."** Requires
architectural coverage, not a single tool run: "The testing must cover all aspects and
components of the application architecture. If an application consists of a web server and
a database, then both components must be tested." The fix text adds the retention
obligation: "Retain scan results for compliance verification."

**V-222646, "At least one tester must be designated to test for security flaws in addition
to functional testing."** A named-role requirement. The check is an org-chart review: "If
the organization has not designated personnel to conduct security testing, this is a
finding."

### 3.3 Testing and release gates

| V-number | STIG ID | CAT | CCI | NIST SP 800-53 Rev 5 |
|---|---|---|---|---|
| V-222644 | APSC-DV-003130 | III | CCI-003004 | PM-14 a 2 |
| V-222647 | APSC-DV-003160 | III | CCI-003182 | SA-11 (2) |
| V-222651 | APSC-DV-003200 | II | CCI-003173 | SA-11 b |
| V-222652 | APSC-DV-003210 | II | CCI-003178 | SA-11 e |
| V-222645 | APSC-DV-003140 | II | CCI-000698 | SA-10 (1) |

**V-222644** requires test plans and procedures created and executed "Prior to each release
of the application, updates to system, or applying patches". The finding condition binds
them to release cadence: "If test plans, procedures, and results do not exist, or are not
updated for each application release, this is a finding."

**V-222651** requires a security impact assessment before every change lands: "The changes
to the application must be assessed for IA and accreditation impact prior to
implementation." The check points at the Configuration Control Board process but allows
informality: "An informal group may be tasked with impact assessment of upcoming version
changes."

**V-222652** requires that security flaws reach the plan of record: "If security flaws are
not addressed in the project plan or there is no process to introduce security flaws into
the project plan, this is a finding."

**V-222647** is an annual, not per-release, obligation: test procedures executed at least
annually to confirm the system stays in a secure state through initialization, shutdown,
and abort.

**V-222645** puts an integrity gate on the build-to-deploy handoff. The fix text describes
the division of labour: "Developers/release managers create cryptographic hash values of
application files and/or application packages prior to transitioning the application from
test to a production environment."

### 3.4 Threat modelling and secure design

| V-number | STIG ID | CAT | CCI | NIST SP 800-53 |
|---|---|---|---|---|
| V-222655 | APSC-DV-003230 | II | CCI-003256 | Rev 4 SA-15 (4); no Rev 5 index (see note) |
| V-222654 | APSC-DV-003220 | III | CCI-003233 | Rev 5 SA-15 a |
| V-222625 | APSC-DV-002950 | II | CCI-000336, CCI-000366 | CM-6 b (for CCI-000366) |

**V-222655, "Threat models must be documented and reviewed for each application release and
updated as required by design and functionality changes or when new threats are
discovered."** This is the single most process-shaping rule for an iterative team, because
it attaches to *each release*. The discussion frames it against code review:

> "Threat modeling is not an approach to reviewing code, but it does complement the
> security code review process."

The check enumerates the required document sections, which is effectively a template:

> "Review the threat model document and identify the following sections are present:
> - Identified threats
> - Potential vulnerabilities
> - Counter measures taken
> - Potential mitigations
> - Mitigations selected based on risk analysis"

It also requires consistency with the real architecture and a human sign-off: "Verify the
architecture and components of the application match with the components in the threat
model document. Verify identified threats and vulnerabilities are addressed or mitigated
and the ISSO and ISSM have reviewed and approved the document."

**V-222654, "The designer must create and update the Design Document for each release of
the application."** Also per-release. The check lists required content, including "All
external interfaces", "User roles required for access control and the access privileges
assigned to each role", "Categories of sensitive information processed by the application
and their specific protection plans (e.g., PII, HIPAA)", and "Restoration priority of
subsystems, processes, or information".

### 3.5 Coding standards

**V-222653, APSC-DV-003215, CAT III, CCI-003233, NIST Rev 5 SA-15 a. "The application
development team must follow a set of coding standards."**

The discussion is candid that this is largely a consistency and readability requirement,
not a security-control requirement:

> "Coding standards often cover the use of white space characters, variable naming
> conventions, function naming conventions, and comment styles."

Listed examples: "Indent style conventions, Naming conventions, Line length conventions,
Comment conventions, Programming best practices, Programming style conventions".

Two things must both be true to pass: "Ask for a coding standards document, review the
document and ask the developers if they are aware of and if they use the coding standards."
The finding condition is "If the developers do not follow a coding standard, or if a coding
standard document does not exist". A committed style configuration plus an enforcing
pre-commit or CI check is strong evidence for the first half; the document is still
required for the second.

### 3.6 Configuration management and change control

| V-number | STIG ID | CAT | CCI | NIST SP 800-53 Rev 5 |
|---|---|---|---|---|
| V-222632 | APSC-DV-003010 | II | CCI-001795 | CM-9 b |
| V-222633 | APSC-DV-003020 | II | CCI-001795 | CM-9 b |
| V-222630 | APSC-DV-002995 | II | CCI-001795 | CM-9 b |
| V-222631 | APSC-DV-003000 | II | CCI-001795 | CM-9 b |

**V-222632** requires a written Software Configuration Management plan. This has the longest
check procedure in the process family. It states the required plan contents verbatim:

> "The SCM plan should contain the following:
>
> - Description of the configuration control and change management process
> - Types of objects developed
> - Roles and responsibilities of the organization
> - Defined responsibilities
> - Actions to be performed
> - Tools used in the process
> - Techniques and methodologies
> - Initial set of baselined software components
>
> If the SCM plan does not include the above, this is a finding."

Beyond the document, the check requires a live demonstration of the repository. Each of the
following is a separate finding condition, quoted:

> "If the application representative cannot display all types of objects under CMR control,
> this is a finding."

> "The SCM plan should identify third-party tools and respective version numbers. If the SCM
> plan does not identify third-party tools, this is a finding."

> "The SCM plan should identify mechanisms for controlled access of individuals
> simultaneously updating the same application component."

> "Ask the application representative to create a build or demonstrate a current release of
> the application that can be recreated. If the application representative cannot display
> releases and application component versions, this is a finding."

> "The CMR should track change requests from beginning to end. [...] If the CMR cannot track
> change requests, this is a finding."

That last pair is the one worth noticing. A reproducible build and a change history linked
to tracked requests are both demanded, and both are ordinary properties of a version control
system plus an issue tracker. There is also an explicit new-project escape: "If the
application has just completed its first release, there may not be any change requests
logged in the CMR. In this case, this finding is not applicable."

**V-222633** requires a Configuration Control Board "that meets at least every release
cycle". The check is unusually accommodating of lightweight practice:

> "CCBs do not have to physically meet, and the CCB chair may authorize a release based on
> phone and/or e-mail conversations."

Finding condition: "If there is no evidence of CCB activity or meetings prior to the last
release cycle, this is a finding." A recorded release-approval decision per release
satisfies this.

**V-222630** requires the source repository host to be patched and STIG compliant.
**V-222631** requires repository access privileges to be reviewed "at least every three
months", with the finding keyed to the date of last review.

### 3.7 Developer training

**V-222673, APSC-DV-003400, CAT II, CCI-002052, NIST Rev 5 AT-3 (3). "The Program Manager
must verify all levels of program management, designers, developers, and testers receive
annual security training pertaining to their job function."**

The discussion is explicit that this does not overlap with general cyber awareness
training:

> "This training is in addition to DoD 8570 training requirements as DoD 8570 annual
> security training does not presently cover application SDLC security concerns."

> "The Program Manager will ensure development team members are provided training on secure
> design principles for the entire SDLC and newly discovered vulnerability types on, at
> least, an annual basis."

Evidence expected: "Examples of evidence include course completion certificates and a class
roster."

### 3.8 Vulnerability intake and response

**V-222657, APSC-DV-003236, CAT II, CCI-003289, NIST Rev 5 SA-15 (10). "The application
development team must provide an application incident response plan."**

The plan must implement a process that, verbatim from both the check and the fix:

> "- Tracks reported vulnerabilities and bugs
> - Confirms reported vulnerabilities and bugs
> - Tracks remediation effort
> - Notifies application users of available updates that address the reported issues."

The discussion adds an intake channel requirement: "should include a method for individuals
to submit potential security vulnerabilities to the development or maintenance team", and
requires that submission instructions live in the design document or configuration guide.

### 3.9 Documentation deliverables

**V-222663, APSC-DV-003285, CAT II, CCI-003124, NIST Rev 5 SA-5 a 1. "An Application
Configuration Guide must be created and included with the application."**

This one reaches into the build environment, which is easy to miss:

> "Development systems, build systems, and test systems must operate in a standardized
> environment. These settings are to be documented in the Application Configuration Guide."

Required content includes "List of development systems, build systems, and test systems",
"Versions of compilers used", "Build options when creating applications and components",
"Versions of COTS software (used as part of the application)", "Operating systems and
versions", and for web applications "which browsers and what versions are supported".

A reproducible build definition (a pinned container image, a lockfile, a CI workflow file)
supplies most of this content, but the STIG asks for it as a distributed guide shipped with
the application, not only as repository state.

### 3.10 The process requirements as a single checklist

For a team asking "what must our SDLC produce", the answer from the ASD STIG is this set of
retained artifacts:

1. A coding standards document, plus evidence developers follow it (V-222653).
2. A design document, updated every release (V-222654).
3. A threat model, updated every release, reviewed and approved by the ISSO and ISSM
   (V-222655).
4. A Software Configuration Management plan (V-222632).
5. Evidence of release-cycle change-control decisions (V-222633).
6. Quarterly repository access reviews (V-222631).
7. Code review results, covering the named flaw classes (V-222648).
8. Defect tracking records for review findings (V-222650).
9. Per-release code coverage statistics (V-222649).
10. Static analysis results (V-222656 and others reference these).
11. Vulnerability and fuzz scan results, with critical findings closed inside 21 business
    days (V-222624).
12. Architecture-complete vulnerability assessment results, retained (V-222515).
13. Per-release test plans, procedures, and results (V-222644).
14. Annual secure-state test evidence (V-222647).
15. Security impact assessment per change (V-222651).
16. Security flaws reflected in the project plan (V-222652).
17. A named security tester (V-222646).
18. Annual role-specific security training records (V-222673).
19. An application incident response plan with an external intake channel (V-222657).
20. An Application Configuration Guide covering the build and test environments (V-222663).
21. SHA-256 hashes of release artifacts (V-222645).
22. A supported-version inventory for every component (V-222658).

Nothing in that list conflicts with short iterations. The binding constraint is that
several artifacts are tied to "each release", so a team that releases weekly owes a threat
model review, a design document update, and a test plan execution weekly. Teams usually
resolve this by defining the release unit deliberately, and by making the per-release
update an incremental diff against a living document rather than a new document. The STIG
does not define "release", which leaves that definition to the program.

---

## 4. What is machine-checkable, and what needs a human

This is the line that decides what tooling can honestly claim.

### 4.1 The measured shape of the check procedures

Counts over all 286 check procedures in the V6R4 XCCDF, reproducible with a regex over
`check-content`:

| Property of the check text | Rules |
|---|---|
| Total rules | 286 |
| Instructs the reviewer to "interview" someone | 212 |
| Instructs "review the application/system documentation" | 171 |
| Turns on scan results, code review reports, or test results | 23 |
| Contains an explicit "not applicable" escape | 139 |

The headline is that 212 of 286 check procedures name an interview as part of the
assessment. The ASD STIG is written for a human assessor sitting with an application
representative. It is not written as an automated benchmark. The V6R4 zip contains only the
manual XCCDF, the Overview, the Revision History, the readme, a stylesheet, and a logo, and
DISA publishes no separate SCAP benchmark for it (see section 5.5). Unlike an operating
system STIG, there is no DISA-supplied automation content for this STIG at all.

That is the honest ceiling. A tool cannot produce an authoritative "compliant" verdict for
this STIG.

### 4.2 What a repository-inspecting agent can determine

These are cases where evidence lives in the repository and a machine can reach a defensible
conclusion.

**Direct code inspection, high confidence:**

- V-222642, embedded authentication data. The check itself directs source inspection across
  scripts, HTML, config files, and ASCII files. Secret scanning answers this.
- V-222444, sensitive data in logs. The check text supplies literal `grep` invocations,
  including a social security number pattern. Pattern matching over log output and over
  logging call sites answers most of it.
- V-222575 and V-222576, HTTPOnly and secure flags on session cookies. Detectable in
  framework configuration and cookie-setting call sites.
- V-222581, URL embedded session IDs. Detectable in routing and link generation.
- V-222583, session identifier generation from a FIPS-approved RNG. The RNG call site is
  greppable; whether the module is FIPS-validated is not.
- V-222570 to V-222572, use of FIPS-validated modules. The library and mode are visible in
  code; validation status requires the CMVP certificate, which is an external lookup.

**Artifact presence and freshness, high confidence:**

Most of the process family reduces to "does this artifact exist, is it current, and does it
contain the required sections". A machine can answer all three parts for artifacts kept in
the repository:

- V-222653, coding standards document exists, plus a linter or formatter configuration and
  a CI step that enforces it.
- V-222654 and V-222655, design document and threat model exist, and the threat model
  contains the five named sections. Section presence is a structural check. Whether the
  content is *correct* is not.
- V-222632, SCM plan exists and contains the enumerated elements.
- V-222650, review findings are linked to tracker issues.
- V-222649, a coverage report exists per release tag.
- V-222644, test plans and results exist per release tag.
- V-222657, an incident response plan exists and names an intake channel, for example a
  `SECURITY.md` with a reporting address.
- V-222663, the configuration guide exists and documents build systems, compiler versions,
  and build options.
- V-222658, a dependency manifest and lockfile exist, and versions can be compared against
  upstream support windows.
- V-222614, dependency currency can be measured against release feeds.
- V-222645, release artifact hashes exist.

**Evidence-quality checks, medium confidence:**

Several rules fail on stale or missing evidence rather than on code state, and staleness is
computable:

- V-222624, whether any critical finding in the latest scan output is older than 21
  business days. Given machine-readable scan output, this is arithmetic.
- V-222631, whether a repository access review is recorded within the last three months.
- V-222673, whether training records are within the last year.
- V-222647, whether secure-state test evidence is within the last year.
- V-222614, whether update checks happen at least weekly.

### 4.3 What a machine cannot determine

**Requires a human judgement about correctness, not existence.** A threat model can contain
all five required sections and still miss the threat that matters. The check for V-222655
requires the assessor to "Verify the architecture and components of the application match
with the components in the threat model document". A tool can flag drift between a
component list and a dependency graph; it cannot conclude that the threat analysis is
adequate.

**Requires a named human's approval.** V-222655 requires that "the ISSO and ISSM have
reviewed and approved the document". V-222652 requires that security flaws are "addressed
in the project plan", which is a management commitment. V-222651 requires an impact
assessment decision. A tool can check that an approval record exists and is signed; the
approval itself is a human act, and a tool must never synthesise one.

**Requires an interview.** V-222646 is assessed by reviewing the organisation chart and
interviewing staff to identify designated security testers. V-222633 is assessed by asking
who is on the Configuration Control Board and how often it meets. V-222653's second half is
"ask the developers if they are aware of and if they use the coding standards". No
repository artifact answers these.

**Requires evidence held outside the repository.** Training certificates and class rosters
(V-222673). Vendor support contracts and support ticket history (V-222658). ATO
documentation showing the CM repository host is STIG compliant (V-222630). CMVP validation
certificates for cryptographic modules.

**Requires a running system.** The error-message rules V-222610 and V-222611 are assessed
by authenticating as a non-privileged user, provoking errors, and reading what comes back,
then repeating as a privileged user. That is dynamic testing, not code reading. Static
analysis can find the risky pattern; only execution confirms what a user sees.

**Requires a scanner the repository does not contain.** Every rule in the injection family
turns on scan results. A tool can verify that a scan ran, that its configuration enabled
the relevant test class, and that no unremediated high findings remain. Determining that
the application "must not be vulnerable to SQL Injection" (V-222607, CAT I) is the
scanner's job, and the assessor's job is to read the scanner's output.

**Requires an applicability decision.** 139 rules carry a "not applicable" escape whose
trigger is usually organisational: are you the developer, do you manage development, is
this COTS, is the development team reachable for interview. A tool can propose the
applicability set from repository facts, but the determination belongs to the assessor.

### 4.4 The honest framing for a tool

A repository-inspecting agent can do three things defensibly:

1. **Determine applicability.** Decide which of the 286 rules plausibly apply given what
   the repository is, and say why.
2. **Report evidence status.** For each applicable rule, state whether the required
   artifact exists, whether it is current, and whether it contains the required structural
   elements. This maps cleanly onto the process family and is where most of the value is,
   because these are the rules teams miss.
3. **Flag code-level candidates.** Surface findings that a human should confirm, in the
   small set of rules where source inspection is the prescribed check.

What it must not do is emit an "Open" or "Not a Finding" determination as if it were an
assessor. The STIG's own vocabulary for a finding determination belongs to a human
reviewing evidence, and the check text says so 212 times.

---

## 5. The traceability chain to controls

### 5.1 The chain

```
NIST SP 800-53 control          e.g. SA-11 (4)
        ^
        | mapped by DISA's CCI List (version 2026-07-14), <reference version="5">
        |
CCI                             e.g. CCI-003187
        ^
        | referenced by <ident system="http://cyber.mil/cci"> in the XCCDF
        |
STIG rule                       V-222648 / SV-222648r961863_rule / APSC-DV-003170
        ^                       (its SRG requirement, SRG-APP-000516, is the Group title)
        | assessed by the rule's check-content
        |
Finding                         Open / Not a Finding / Not Applicable, recorded in a checklist
```

A Control Correlation Identifier is the join key. The definition, from the NIST CSRC
glossary, which attributes it to CNSSI 4009-2022:

> "Decomposition of a National Institute of Standards and Technology (NIST) control into a
> single, actionable, measurable statement."

That decomposition is what makes the chain work. NIST controls are written as prose that
bundles several distinct obligations, so a control cannot be pointed at precisely; a CCI
can. One control is therefore referenced by many CCIs, and one CCI by many STIG rules across
different technologies.

Three properties of the CCI List itself are worth knowing before building against it.

**There are two CCI List downloads on the same host, at different versions, and the older
one is easy to reach by accident.** Both return HTTP 200:

| URL | Archive contents | `metadata/version` |
|---|---|---|
| `.../stigs/zip/CCI_List.zip` | `CCI_List.xml` | **2026-07-14** (current) |
| `.../stigs/zip/U_CCI_List.zip` | `U_CCI_List.xml` | 2025-01-23 (stale) |

The `U_`-prefixed name matches the convention every STIG package uses, so it is the natural
guess, and it is the wrong one. Pin the un-prefixed `CCI_List.zip` and assert on
`metadata/version` after parsing.

Every mapping in section 5.3 was resolved against 2026-07-14 and then re-resolved against
2025-01-23 as a cross-check. **None of the mappings cited in this document differs between
the two versions.** The list grew from 5,137 to 5,149 items over that period.

**No CCI is marked published.** In the 2026-07-14 list, 5,058 items carry `status` `draft`
and 91 carry `deprecated`. Filtering on `status == "published"` returns nothing.

**Do not cite the CCI process document.** The only publicly reachable one,
`u_cci_process_v1r0.1.pdf`, is explicitly a draft dated 28 February 2011 and refers to NIST
SP 800-53 version 3 and DoDI 8500.2, both long superseded. The authoritative CCI
Specification sits on `dl.cyber.mil`, which is CAC-gated.

### 5.2 What the identifiers look like in the file

Each rule in the V6R4 XCCDF carries several identifiers, and they serve different purposes.
For V-222648:

| Identifier | Where it lives | Stability |
|---|---|---|
| `SRG-APP-000516` | `Group/title` | The upstream SRG requirement this rule instantiates |
| `V-222648` | `Group/@id` | Stable across releases |
| `SV-222648r961863_rule` | `Rule/@id` | The `r......` revision suffix changes between releases |
| `APSC-DV-003170` | `Rule/version` | Stable; this is the human-facing STIG ID |
| `CCI-003187` | `ident` with `system="http://cyber.mil/cci"` | Stable |
| `SV-84997`, `V-70375` | `ident` with `system="http://cyber.mil/legacy"` | Legacy identifiers from before the content management system migration |

The two `system` URIs matter. A parser that reads every `ident` element without checking
`@system` will treat legacy `SV-` and `V-` values as if they were CCIs. Filter on
`system="http://cyber.mil/cci"`.

Across the benchmark there are 319 CCI references resolving to 225 distinct CCIs, and 30
rules reference more than one CCI. The most-referenced CCI is CCI-000172, appearing on 19
rules, which is the audit-record-generation family.

### 5.3 Resolved mappings for the process rules

Resolved against `CCI_List.xml`, metadata version `2026-07-14`, and cross-checked against
the 2025-01-23 list with no differences. The CCI definitions below are verbatim.

| STIG rule | CCI | CCI definition (verbatim, abbreviated) | Rev 5 | Rev 4 |
|---|---|---|---|---|
| V-222648 code review | CCI-003187 | "Require the developer ... to perform a manual code review of organization-defined specific code using organization-defined processes, procedures, and/or techniques." | SA-11 (4) | SA-11 (4) |
| V-222649 code coverage | CCI-003188 | "Defines the specific code for which the developer ... is required to perform a manual code review ..." | SA-11 (4) | SA-11 (4) |
| V-222650 defect tracking | CCI-003161 | "Require the developer ... to track security flaws within the system, component, or service." | SA-10 e | SA-10 e |
| V-222651 change impact | CCI-003173 | "Requires the developer ... at all post-design phases of the system development life cycle, to perform unit, integration, system, and/or regression testing/evaluation ..." | SA-11 b | SA-11 b |
| V-222652 flaw remediation | CCI-003178 | "Requires the developer ... to correct flaws identified during testing/evaluation." | SA-11 e | SA-11 e |
| V-222653 coding standards | CCI-003233 | "Require the developer ... to follow a documented development process." | SA-15 a | SA-15 |
| V-222654 design document | CCI-003233 | (same as above) | SA-15 a | SA-15 |
| V-222655 threat models | CCI-003256 | "The organization requires that developers perform threat modeling for the information system at an organization-defined breadth/depth." | none present | SA-15 (4) |
| V-222646 security tester | CCI-003182 | "Require the developer ... to perform threat modeling and vulnerability analysis during subsequent testing and evaluation ..." | SA-11 (2) | SA-11 (2) |
| V-222647 annual state test | CCI-003182 | (same as above) | SA-11 (2) | SA-11 (2) |
| V-222644 release test plans | CCI-003004 | "Implement a process for ensuring that organizational plans for conducting security testing associated with organizational systems continue to be executed." | PM-14 a 2 | PM-14 a 2 |
| V-222657 incident response | CCI-003289 | "Require the developer ... to provide an incident response plan." | SA-15 (10) | SA-15 (10) |
| V-222658 supported products | CCI-003376 | "Replace system components when support for the components is no longer available from the developer, vendor, or manufacturer." | SA-22 a | SA-22 a |
| V-222659 decommission | CCI-003376 | (same as above) | SA-22 a | SA-22 a |
| V-222632 SCM plan | CCI-001795 | "Implement a configuration management plan for the system that establishes a process for managing the configuration of the configuration items." | CM-9 b | CM-9 b |
| V-222633 CCB | CCI-001795 | (same as above) | CM-9 b | CM-9 b |
| V-222630 CM repo patched | CCI-001795 | (same as above) | CM-9 b | CM-9 b |
| V-222631 CM access review | CCI-001795 | (same as above) | CM-9 b | CM-9 b |
| V-222663 config guide | CCI-003124 | "Obtain or develop administrator documentation for the system ... that describes secure configuration ..." | SA-5 a 1 | SA-5 a 1 |
| V-222673 training | CCI-002052 | "Provide practical exercises in security training that reinforce training objectives." | AT-3 (3) | AT-3 (3) |
| V-222624 active vuln testing | CCI-000256 | "Include as part of the control assessments ... in-depth monitoring; security instrumentation; automated security test cases; vulnerability scanning; malicious user testing ..." | CA-2 (2) | CA-2 (2) |
| V-222614 patch currency | CCI-002605 | "Install security-relevant software updates within an organization-defined time period of the release of the updates." | SI-2 c | SI-2 c |
| V-222515 vuln assessment | CCI-000366 | "Implement the security configuration settings." | CM-6 b | CM-6 b |
| V-222606, V-222607, V-222602, V-222603, V-222604, V-222605, V-222608 | CCI-001310 | "Checks the validity of organization-defined information inputs to the system." | SI-10 | SI-10 |
| V-222609 input handling | CCI-002754 | "Verify that the system behaves in a predictable and documented manner that reflects organizational and system objectives when invalid inputs are received." | SI-10 (3) | SI-10 (3) |

The development-process requirements concentrate almost entirely in the SA family (SA-5,
SA-10, SA-11, SA-15, SA-22), with CM-9 for configuration management and AT-3 (3) for
training. If a program is already documenting SA-11 and SA-15 for its RMF package, the ASD
STIG process rules are the assessable expression of that same work.

**Two mapping gaps, stated plainly.** Not every CCI in the CCI List carries a
Rev 5 index.

- CCI-003256, the threat modelling CCI behind V-222655, carries only
  `version="4" index="SA-15 (4)"`. There is no Rev 5 reference in that record.
- CCI-002367, behind V-222642 (embedded authentication data, CAT I), carries only
  `version="4" index="IA-5 (7)"`. Its definition is "The organization ensures unencrypted
  static authenticators are not embedded in applications."

A tool that resolves CCIs to Rev 5 controls will produce an empty result for both rules and
must handle that case rather than dropping the rule. Verify the Rev 5 gap per CCI at parse
time rather than assuming every record has both versions.

### 5.4 A note on control baselines

The Overview states the relationship between STIG compliance and control selection
directly:

> "Although the use of the principles and guidelines in these SRGs/STIGs provides an
> environment that contributes to the security requirements of DOD systems, applicable NIST
> SP 800-53 cybersecurity controls must be applied to all systems and architectures based
> on the Committee on National Security Systems (CNSS) Instruction (CNSSI) 1253."

Passing every STIG rule is therefore not the same as satisfying the control baseline. The
baseline is selected under CNSSI 1253; the STIG is one input to assessing it.

For context on the two documents named in that sentence:

- **NIST SP 800-53B, "Control Baselines for Information Systems and Organizations"**,
  September 2020, updated 10 December 2020. Its abstract states it "provides security and
  privacy control baselines for the Federal Government. There are three security control
  baselines (one for each system impact level, low-impact, moderate-impact, and
  high-impact), as well as a privacy baseline that is applied to systems irrespective of
  impact level."
- **CNSSI 1253** is cited in SP 800-53B's own reference list as "Committee on National
  Security Systems Instruction No. 1253, Security Categorization and Control Selection for
  National Security Systems, March 2014", and SP 800-53B states that it "provides security
  categorization and control selection guidance for national security systems."

CNSSI 1253 was not read directly; it is published by CNSS, outside the source set used
here. Everything above about it comes from SP 800-53B's citation of it and from the ASD
STIG Overview. So the baseline half of the chain is: the CCI resolves to an 800-53 control,
and whether that control is in scope for a given DoD system is decided by CNSSI 1253, not by
the STIG. STIG findings are assessment evidence against controls that have already been
selected.

### 5.5 What a tool parses

From the readme shipped inside the package, describing DISA package layout:

| File | Contents |
|---|---|
| `*_Manual-xccdf.xml` | "an XCCDF XML document that contains the manual content of the SRG or STIG" |
| `Overview.pdf` | "background and other important information that could not be stored in the XCCDF document" |
| `Revision_History.pdf` | "the history of the changes to the SRG or STIG package" |
| `STIG_unclass.xsl` | stylesheet "used for converting the XCCDF document into HTML for viewing in a web browser" |

**Where to fetch from.** Three DoD hosts behave differently, and only one is usable
programmatically:

| Host | Behaviour |
|---|---|
| `dl.dod.cyber.mil` | Open. Serves every STIG zip, the CCI List, and the guidance PDFs. Fetch from here. |
| `public.cyber.mil` | HTTP 302 to `www.cyber.mil`. No content of its own any more. |
| `www.cyber.mil` | Salesforce Experience Cloud, entirely JavaScript-rendered. A plain HTTP client gets an empty page shell for every path. Readable only with a browser engine. |
| `dl.cyber.mil` | CAC-gated. Holds the authoritative CCI Specification and the quarterly release schedules. |

A tool that pins direct `dl.dod.cyber.mil` zip URLs works. A tool that scrapes the catalogue
page needs a headless browser.

The XCCDF is XCCDF 1.1, namespace `http://checklists.nist.gov/xccdf/1.1`. Rule text lives
in `Rule/description` as HTML-escaped pseudo-XML with a `<VulnDiscussion>` element and
siblings, so a parser must unescape and re-parse that field rather than reading it as
plain text.

**There is no SCAP benchmark for the ASD STIG.** For STIGs that have one, DISA publishes it
as a separate download alongside the manual STIG, named
`U_<name>_<version>_STIG_SCAP_1-3_Benchmark.zip`, containing a SCAP 1.3 source data stream
that bundles XCCDF 1.2 with OVAL definitions. Probing that naming pattern for ASD V6R4
returns HTTP 404 for both the `SCAP_1-3` and `SCAP_1-2` forms, and the manual package
contains no OVAL content. This is consistent with the nature of the STIG: OVAL expresses
machine-evaluable system state, and 212 of the 286 ASD checks are interviews. There is no
DISA-supplied automation content to run against an application.

For checklist results, DISA's STIG Viewer 3.x consumes XCCDF STIGs and writes checklists in
several formats. From the STIG Viewer 3.x User Guide: "Possible formats are HTML, CSV, CMRS
XML, CKL (SV v2 format), and CKLB", with the note "CKLB format can now be imported into
eMASS". `.ckl` is the STIG Viewer 2.x checklist format and `.cklb` the 3.x format. The
guide also states the viewer "Imports automated review SCAP or XCCDF Results into the
checklist, populating the checklist with the automated results. The manual portion of the
review can be completed and added to the automated results." For the ASD STIG there are no
automated results to import, so the whole checklist is the manual portion.

---

## 6. Adjacent frameworks

Covered briefly, and only where verification was possible. See "Not verified" for what was
not confirmed in this research.

### 6.1 NIST SP 800-53 revision alignment

Verified from the ASD STIG's own Revision History: V6R1, released 24 July 2024, was a major
revision whose stated purpose was that a large set of rules was "Updated based on NIST SP
800-53 Rev. 5 changes", and "Because this is a major revision, version numbers were
incremented to the next whole number." So the current V6 line is Rev 5 aligned, with the
CCI-003256 exception noted in section 5.3.

Verified from the CCI List itself: records carry `reference` elements with `version="4"`
and `version="5"` attributes, so both Rev 4 and Rev 5 mappings are present in the same
file, per CCI, and a tool must select by `@version`. Rev 5 is the primary mapping: DISA's
own rendering stylesheet shipped in the CCI zip, `cci2html.xsl`, defaults to sorting the
list by the Rev 5 index.

The Rev-4-only gap described in section 5.3 is not confined to the two ASD cases. Across
the whole 2026-07-14 CCI List, 2,267 CCIs carry both revisions, 1,580 are Rev 5 only, and
734 are Rev 4 only. Any tool that resolves CCIs to Rev 5 alone will silently drop rules.

### 6.2 DoD Instruction 8500.01 and the RMF flowdown

Verified from primary DISA documents in the package, quoted in section 1 above. The
flowdown as the ASD STIG itself describes it:

1. DoDI 8500.01 requires DoD IT to be configured consistently with DoD cybersecurity
   policies, and tasks DISA with producing CCIs, SRGs, and STIGs.
2. DoDI 8500.01 requires DoD Component heads to "ensure that all DoD IT under their purview
   complies with applicable STIGs, security configuration guides, and SRGs".
3. Control baselines are selected under CNSSI 1253 against NIST SP 800-53.
4. CCIs decompose those controls into single obligations.
5. STIG rules implement CCIs and carry check procedures.
6. Findings from those check procedures "support system assessment and authorization (A&A)
   under the DOD Risk Management Framework (RMF)", and residual risk from unapplied
   settings "must be approved by the responsible AO".

For a development team the practical consequence is that the process artifacts in section
3.10 are not internal hygiene. They are assessment evidence that flows into an
authorisation decision.

The specific text of DoDI 8510.01 was not retrieved in this research; see "Not verified".

### 6.3 Relationship to the SRG

DISA states the SRG-to-STIG relationship in a block carried in every published SRG package.
From the Application Server SRG V4R5 Overview, section 1.1.1:

> "Security Requirements Guides are collections of requirements applicable to a given
> technology family. They represent an intermediate step between Control Correlation
> Identifiers (CCIs) and Security Technical Implementation Guides (STIGs). CCIs represent
> discrete, measurable, and actionable items sourced from Information Assurance (IA)
> controls defined in a policy, such as the National Institute of Standards and Technology
> (NIST) Special Publication (SP) 800-53. STIGs provide product-specific information for
> validating and attaining compliance with requirements defined in the SRG for that
> product's technology area."

The same section defines the hierarchy:

> "There are four core SRGs: Application, Network, Operating System, and Policy. Each
> addresses the applicable CCIs in the context of the technology family. Subordinate to the
> core SRGs, Technology SRGs are developed to address the technologies at a more granular
> level."

And section 1.3:

> "The SRG defines the requirements for various technology families, and the STIGs are the
> technical implementation guidelines for specific products. A single SRG/STIG is not
> all-inclusive for a given system... For a given system, compliance with all (multiple)
> SRGs/STIGs applicable to a system is required."

That last sentence matters for scoping. The ASD STIG is never the whole obligation for an
application. The Overview says as much in its own section 1.1: it "is meant for use in
conjunction with the Enclave, Network Infrastructure, Application Server, Database, Browser,
and appropriate Operating System (OS) STIGs and relevant technology Security Requirement
Guides (SRGs)."

**There is no separately published Application Security and Development SRG.** This was
checked three ways: the STIGs Document Library catalogue contains no such entry; the Sunset
Products library does not list one, which is where DISA puts retired products; and the
current quarterly compilation contains no ASD SRG. Do not write that the ASD SRG was retired
or folded into the STIG, because no DISA statement supports that and it is absent from the
sunset library. The accurate statement is that DISA does not publish one.

The ASD STIG's group titles are bare core-Application identifiers (`SRG-APP-000001` through
`SRG-APP-000625`), not the compound form DISA uses for subordinate Technology SRGs
(`SRG-APP-000001-COL-000001`). So the ASD STIG keys directly to the core Application SRG,
and that core SRG is itself not published as a public download. The practical consequence
for tooling: the SRG identifier in a group title is a stable join key, but there is no public
document to resolve it against. Treat it as an opaque grouping label.

Do not silently substitute the neighbouring documents, which are different technology areas:
`Application Server SRG`, `Application Programming Interface (API) SRG`, and
`General Application Draft SRG` (a draft, not a published requirement).

### 6.4 NIST SP 800-218 and DoD DevSecOps guidance

Not verified in this research. See "Not verified".

---

## 7. Not verified

Stated plainly, because in a compliance context an unverified claim is worse than an
acknowledged gap.

1. **NIST SP 800-218 (SSDF).** Not fetched or confirmed in this research. The version
   number, publication date, the exact names of its practice groups, the status of SP
   800-218A, and any stated linkage between SSDF and NIST SP 800-53 or executive-branch
   software security directives are all unverified here. Nothing in the ASD STIG V6R4
   package examined references SP 800-218, so no relationship between the two is asserted.

2. **DoD Enterprise DevSecOps Reference Design and DoD software modernisation guidance.**
   Not fetched or confirmed. Document titles, version numbers, dates, and any statements
   they make about STIG compliance in CI/CD pipelines or continuous authorisation are
   unverified here.

3. **DoD Instruction 8510.01.** Not retrieved. Its exact title, effective date, latest
   change date, and its specific language on RMF and STIG use were not confirmed. Only DoDI
   8500.01 is quoted above, and those quotes are second-hand: they are DISA quoting 8500.01
   inside the ASD STIG Overview and the V4 release memo, not the text of 8500.01 read
   directly.

4. **DoDI 8500.01 was not fetched by the author of this document.** `esd.whs.mil`, the
   official DoD issuances repository, returns HTTP 403 to automated fetching, as does
   `dodcio.defense.gov`. A delegated research pass reports having extracted the instruction
   itself and matching both quoted passages to Section 4 (Policy) paragraph h item (1) and
   to Enclosure 2 paragraph 2.b. That corroboration is reported here because it is useful,
   but it is second-hand to this document and no retrieval URL for it is available, so it is
   listed as unverified rather than as a source. What is directly verified is DISA's
   rendering of the language, quoted identically in two DISA documents written years apart.
   Anyone citing 8500.01 in an audit should pull it themselves through a browser.

5. **Whether an Application Security and Development SRG ever existed and was retired.**
   That DISA does not currently publish one is verified (section 6.3). Whether one existed
   historically cannot be established: no document, no DISA statement, and no entry in the
   sunset library where DISA says retired products go. Assert only "not separately
   published", never "retired" or "folded into the STIG".

6. **The core Application SRG as a retrievable document.** DISA names Application as one of
   four core SRGs and the ASD STIG's group titles key to it, but no public package exists.
   Whether it is internal content management data or restricted to CAC holders could not be
   determined.

7. **DISA's own prose definition of a CCI.** DISA's definition page at
   `public.cyber.mil/stigs/cci/` issues a 302 to `www.cyber.mil`, a JavaScript-rendered
   site, and the authoritative CCI Specification is on `dl.cyber.mil`, which is CAC-gated.
   The definition quoted in section 5.1 is the NIST CSRC glossary entry, attributed there to
   CNSSI 4009-2022. Cite it that way, not as "DISA says".

8. **Whether a V6R5 exists as restricted or unpublished content.** Only the public position
   is provable: as of 6 September 2026 no V6R5 is published. Likewise, whether an ASD update
   is queued for a future maintenance release cannot be checked, because the quarterly
   "STIGs to Be Released" summaries are CAC-gated.

9. **Why the ASD STIG's document date (01 October 2025) and its library upload date
   (2025-11-26) differ.** Both values were observed; no DISA statement explains the gap. For
   a compliance artifact use 01 October 2025, which is the value on the Overview cover, in
   the Revision History release-date column, and in the XCCDF benchmark date. The upload
   date is library metadata.

10. **CNSSI 1253.** Not read. It is published by CNSS at `cnss.gov`, outside the source set
   used here. Its title, number, and March 2014 date in section 5.4 are taken from NIST SP
   800-53B's reference list, not from the instruction itself, and it is not confirmed
   whether a version later than March 2014 exists. NIST SP 800-53B itself was fetched and
   its title, dates, and abstract are quoted directly.

11. **The internal serialisation of `.cklb`.** The STIG Viewer 3.x User Guide names CKL and
    CKLB as export formats, and that is what section 5.5 quotes. It does not state the
    on-disk format of a `.cklb` file. It is widely assumed to be JSON; that assumption was
    not confirmed against a DISA source, so no claim is made about it here.

12. **Rule counts across releases.** The 286-rule count, the 34/230/22 severity split, and
    all statistics in section 4.1 are computed from V6R4 only and will drift with each
    release.

13. **The severity of rules cited without a table entry.** Where a V-number appears in prose
    in section 2 without an accompanying table row, its STIG ID and CAT were read from the
    same XCCDF, but readers building tooling should re-derive rather than trust
    transcription.

---

## 8. Sources

Primary sources actually fetched and parsed for this document:

- [U_ASD_V6R4_STIG.zip, DISA, Application Security and Development STIG V6R4](https://dl.dod.cyber.mil/wp-content/uploads/stigs/zip/U_ASD_V6R4_STIG.zip)
  The package that all rule identifiers, titles, discussions, check procedures, fix texts,
  and counts in this document come from. Contains:
  - `U_ASD_V6R4_Manual_STIG/U_ASD_STIG_V6R4_Manual-xccdf.xml`, benchmark date 01 Oct 2025,
    XCCDF status `accepted` 2025-09-09.
  - `U_ASD_V6R4_Overview.pdf`, "Application Security and Development (ASD) Security
    Technical Implementation Guide (STIG) Overview", Version 6 Release 4, 01 October 2025,
    "Developed by DISA for the DOD". Source of all Overview quotations.
  - `U_ASD_V6R4_Revision_History.pdf`, Version 6 Release 4, 01 October 2025. Source of the
    release date table and the Rev 5 alignment note.
  - `U_ASD_STIG_V4_Release_Memo.pdf`, DISA memorandum for distribution, signed William A.
    Keely, Risk Management Executive, referencing DoDI 8500.01 dated March 14, 2014. Source
    of the "DoD Component heads" quotation.
  - `U_Readme_SRG_and_STIG.pdf`, "SRG and STIG Readme", Version 3 Release 5, 17 April 2023.
    Source of the package file layout table.

- [CCI_List.zip, DISA Control Correlation Identifier List](https://dl.dod.cyber.mil/wp-content/uploads/stigs/zip/CCI_List.zip)
  Contains `CCI_List.xml`, metadata `<version>2026-07-14</version>`. Source of every CCI
  definition and every NIST SP 800-53 Rev 4 and Rev 5 mapping in section 5.3, and of the
  status and revision-coverage counts.

- [U_CCI_List.zip, the older parallel CCI List download](https://dl.dod.cyber.mil/wp-content/uploads/stigs/zip/U_CCI_List.zip)
  Contains `U_CCI_List.xml`, metadata `<version>2025-01-23</version>`. Fetched and parsed as
  a cross-check against the current list; no mapping cited in this document differs between
  the two. Documented in section 5.1 as a trap, not recommended as a source.

- [DoD Cyber Exchange Public, STIG downloads](https://public.cyber.mil/stigs/downloads/)
  Fetched, returned a 302 to `https://www.cyber.mil/stigs/downloads/`, which returns a
  JavaScript application shell rather than a readable catalogue. Recorded in section 5.5 as
  a constraint on tooling, not used as a source.

Primary sources retrieved by a delegated research pass rather than by the author of this
document. All are on DISA's open download host or NIST's, and their URLs are given so a
reader can re-pull them. They are flagged separately because the author did not open them:

- [U_Application_Server_V4R5_SRG.zip, Application Server SRG V4R5](https://dl.dod.cyber.mil/wp-content/uploads/stigs/zip/U_Application_Server_V4R5_SRG.zip)
  Its Overview is the source of the SRG-to-STIG relationship quotations, the four-core-SRGs
  quotation, and the section 1.3 multiple-STIG-compliance quotation in section 6.3.

- [U_SRG-STIG_Library_July_2026.zip, the current quarterly SRG-STIG library compilation](https://dl.dod.cyber.mil/wp-content/uploads/stigs/zip/U_SRG-STIG_Library_July_2026.zip)
  Basis for the claim that V6R4 is the only ASD package in the current compilation, and that
  no ASD SRG ships in it.

- [SRG-STIG Library Compilation README, Version 2 Release 1](https://dl.dod.cyber.mil/wp-content/uploads/stigs/pdf/U_STIG_Library-zip_Readme_V2R1.pdf)
  Source of the quotation establishing the quarterly library release cycle in section 1.

- [STIG Viewer 3.x User Guide](https://dl.dod.cyber.mil/wp-content/uploads/stigs/pdf/U_STIG_Viewer_3-x_User_Guide_V1R8.pdf)
  Source of the CKL and CKLB export-format quotations and the SCAP/XCCDF results import
  behaviour in section 5.5.

- [NIST SP 800-53B, Control Baselines for Information Systems and Organizations](https://nvlpubs.nist.gov/nistpubs/SpecialPublications/NIST.SP.800-53B.pdf)
  Source of the baseline definitions and the CNSSI 1253 citation in section 5.4.

- [NIST CSRC Glossary, Control Correlation Identifier](https://csrc.nist.gov/glossary/term/control_correlation_identifier)
  Source of the CCI definition quoted in section 5.1, attributed there to CNSSI 4009-2022.

Non-primary sources consulted during discovery only, and not relied on for any factual
claim in this document:

- [Cyber Trackr, Application Security and Development STIG V6R1](https://cyber.trackr.live/stig/Application_Security_and_Development/6/1)
  A third-party mirror of DISA content. Used only to locate the STIG and confirm the
  identifier ranges before the DISA package was obtained. Every identifier in this document
  was subsequently re-read from the DISA XCCDF.
- [DISA January 2025 maintenance release notice](https://dl.dod.cyber.mil/wp-content/uploads/stigs/pdf/January-2025-STIGs-To-Be-Released.pdf)
  and [April 2025 maintenance release notice](https://dl.dod.cyber.mil/wp-content/uploads/stigs/pdf/April_2025_STIGs_To_Be_Released.pdf).
  DISA-hosted, but the PDF text could not be extracted in this environment. The same
  release information was obtained from the V6R4 Revision History instead.

### Reproducing the analysis

Every count in this document is derived from the XCCDF and can be regenerated:

```bash
curl -LO https://dl.dod.cyber.mil/wp-content/uploads/stigs/zip/U_ASD_V6R4_STIG.zip
unzip U_ASD_V6R4_STIG.zip
python3 - <<'PY'
import xml.etree.ElementTree as ET, re, collections
ns = {'x': 'http://checklists.nist.gov/xccdf/1.1'}
root = ET.parse('U_ASD_V6R4_Manual_STIG/U_ASD_STIG_V6R4_Manual-xccdf.xml').getroot()
sev = collections.Counter()
interview = docs = 0
for group in root.findall('x:Group', ns):
    rule = group.find('x:Rule', ns)
    sev[rule.get('severity')] += 1
    check = rule.find('x:check/x:check-content', ns).text or ''
    if re.search(r'\binterview\b', check, re.I):
        interview += 1
    if re.search(r'review the (application|system) documentation', check, re.I):
        docs += 1
print(sum(sev.values()), sev, 'interview:', interview, 'docs:', docs)
PY
```
