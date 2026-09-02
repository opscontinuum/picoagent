# Continuity tooling: what it is and where it is going

This records the project owner's stated direction for the continuity plugins, so that anyone -
person or model - picking the work up later starts from the intent rather than reconstructing it
from the code. Quotations are the owner's own words, dated. Everything not quoted is inference
drawn from them, and is marked as such.

Read this before extending `iscp-author`, before starting the COOP work, and before adding any
plugin that generates a compliance document.

## 1. The goal

> "Essentially i want to be able to store the complete picture for every app in an environment.
> From how to stand it up and tear it down (terraform), configuration management (ansible),
> Runbooks, Playbooks, DR Plans, COOP, BIA, etc."
> — 2026-09-02

`iscp-author` is the first slice of that, not the end state. The unit of the system is **one
application**, and the goal is that everything describing it lives in one record instead of
scattered across an IaC repo, a CM repo, a wiki and a compliance folder that drift apart.

## 2. Why a plan that does not link to operational reality is worthless

This is the sharpest constraint on the design, and it is a critique of how contingency documents
are normally written:

> "In my view an ISCP MUST point to the DRP the terraform, ansible, the complete Architecture
> diagram for it to be relevant. If you dont know what connects to it what it connects to and
> the measure of 'available' for the app you dont know if it is working. If you dont define what
> alerts the app team needs the SRE to see you have no way to be successful in 3 9's or 4 9's"
> — 2026-09-02

NIST SP 800-34's ISCP template requires a System Description and recovery procedures. It does
**not** require links to the executable artefacts, a dependency graph, an availability
definition, or an alert catalogue. So meeting NIST's minimum produces a document that passes an
audit and fails an outage.

Three consequences, and the first is buildable today:

- **The dependency graph is already in the IaC.** "What connects to it and what it connects to"
  is the edge set that `terraform show -json` already carries, as implicit references plus
  `depends_on`. `examples/plugins/iscp-author/iac_inventory.py` extracts configuration items as
  a flat list and drops the edges. Extracting them yields the connectivity map as *derived* data
  rather than something a human asserted and nobody rechecked. *(Inference from the above.)*
- **"Available" must be a definition, not a vibe.** An SLI for the application, written down.
- **The alert catalogue is a contract**, specifically about what the application team needs the
  SRE to see. At three or four nines the dangerous case is not the alarm that fired, it is the
  signal that stopped and nobody noticed - which `opscontinuum/oci-itscp` `docs/04-monitoring.md`
  already states as "alarm on the absence of signal" and catalogues.

## 3. The plan families are different instruments

Not one thing with more fields. Getting this wrong inherits the wrong requirement set in both
directions - a COOP modelled as "ISCP plus extras" acquires ISCP requirements it does not have
and misses COOP requirements it does.

| Family | Term | Level | Governing instrument | Status |
|---|---|---|---|---|
| **ISCP** | Information System Contingency Plan | system | NIST SP 800-34 Rev. 1; FedRAMP Appendix G | built - `examples/plugins/iscp-author` |
| **ITSCP** | IT Service Continuity Plan | service | ITIL 4 / ITSCM | worked reference at `opscontinuum/oci-itscp` |
| **COOP** | Continuity of Operations Plan | organisation | HSPD-20/NSPD-51, FCD 1 | planned, not started |

> "Building a full coop is eventually on the block for the ITSCP plugin" — 2026-09-02

**Naming:** the plugin is `iscp-author` and that is correct - FedRAMP's Appendix G deliverable is
an Information System Contingency Plan. `opscontinuum/oci-itscp` is an **ITSCP**; call its plan
"the ITSCP" or "the plan", never "the ISCP". An ITSCP may *align to* NIST's ISCP structure - that
repo does, through its `docs/07-itil4-alignment.md` §1a crosswalk - without being one. Keep
NIST's own "ISCP" inside quotations and when naming NIST artefacts ("NIST's ISCP template",
"Table 3-5: ISCP TT&E Activities").

COOP's element set is its own: NIST SP 800-34 Rev. 1 (printed p.8) lists essential functions,
order of succession, delegation of authority, devolution, reconstitution, continuity facilities,
vital records management, continuity communications, risk management, human capital, budgeting
and acquisition, and test/training/exercise. Note that **order of succession and delegation of
authority are COOP elements and are not ISCP requirements** - "line of succession" appears
nowhere in 800-34 as an ISCP item, which is why it was not raised as a gap against the ITSCP.

## 4. The intended user journey

> "The user will start with what is in the current repo as an example and we will question a
> user (or query elasticsearch) to determine what the current picture is and get them to a well
> oiled machine that can failover any day of the week" — 2026-09-02

1. **Start from the worked example.** `opscontinuum/oci-itscp` is the reference for what
   "complete" looks like for one application.
2. **Establish this user's actual picture** - by interviewing them, or by querying Elasticsearch.
3. **Drive to routine failover**, evidenced rather than asserted.

### The three inputs are not equivalent

*(Inference from the above, and the design point that follows from it.)*

| Source | Answers |
|---|---|
| Terraform - `iac_inventory.py`, `terraform show -json` | what is **declared** |
| Elasticsearch - the `es-doctor` / es-admin tools | what is **observed running** |
| The interview - `iscp_questions.py` | what a human **asserts** |

Where these three disagree is not noise to reconcile quietly. It is the most valuable output the
tool can produce:

- declared but not observed → drift, or an apply that failed
- observed but not declared → shadow infrastructure
- asserted but neither declared nor observed → a stale mental model, which is what actually kills
  a failover at 03:00

So **record provenance per configuration item**, exactly as `iscp_render.Segment(kind=...)`
already tags document text as template/answer/markup and fails the suite on anything
unattributed. Same pattern, different domain.

This also reframes `es-doctor`: it is not only a cluster-troubleshooting tool, it is the
**discovery** path for environments that have one.

### "Failover any day of the week" is measurable

`oci-itscp` already models how to evidence it, which is the hard part: `runbooks/RB-04-dr-drill.md`
plus `checklists/drill-timing-sheet.md` replace *design* RTO/WRT with *measured* values after each
drill, and `evidence/` accumulates the proof. The end state is not "the documents exist" - it is
"the measured numbers match the committed tiers, and here is the drill history."

## 5. Open design question

*(Not yet decided.)* Whether the interview **seeds** from discovery or **reconciles** against it.
Seeding is friendlier: pre-fill from what Terraform and Elasticsearch already show, and ask the
human only what neither knows. Reconciling is more honest: ask independently, then show the diff.
A reasonable split is to seed the routine fields and reconcile the load-bearing ones - tier
assignment, MTD, business owner - since those are exactly where a human's wrong assumption is
expensive. Decide before building the discovery layer; it is cheaper now than later.

## 6. What exists today

| Component | Does |
|---|---|
| `examples/plugins/iscp-author` | Generates a FedRAMP-compliant ISCP, DRP and BIA with a provenance-checked renderer; derives configuration items from Terraform via `iac_inventory.py` |
| `examples/plugins/es-doctor` | Elasticsearch administration and troubleshooting entirely through the API, no SSH |
| `examples/plugins/stig-runner` | Runs a DISA ASD STIG from a CKL file against a repository, human-gated |
| `opscontinuum/oci-itscp` | The worked ITSCP reference: Oracle EBS on Exadata, Ashburn to Phoenix |

Not built: COOP generation, Ansible ingestion, the dependency-edge extraction, the SLI and alert
catalogue artefacts, and the discovery-to-interview reconciliation.
