---
name: bia-workshop
description: Run the Business Impact Analysis that becomes ISCP Appendix L, following NIST SP 800-34 Rev. 1 Appendix B step by step - processes, impact categories, MTD/RTO/RPO, resources, recovery priorities
---
# Running the BIA workshop

FedRAMP's ISCP **Appendix L Business Impact Analysis** is a single instruction: "Insert the
Business Impact Analysis here. Please see NIST SP 800-34, Revision 1 for more information on
how to conduct a Business Impact Analysis." So the structure comes from **NIST SP 800-34
Rev. 1, Appendix B, "Sample Business Impact Analysis (BIA) and BIA Template"**, and the
generator reproduces its headings and table columns verbatim:

| NIST section | Question ids | Table columns (verbatim) |
|---|---|---|
| 1 Overview | `bia.completion_date` | — |
| 2 System Description | `bia.system_description` | — |
| 3.1 Determine Process and System Criticality | `bia.processes` | Mission/Business Process, Description |
| 3.1.1 Identify Outage Impacts and Estimated Downtime | `bia.impact_categories`, `bia.process_impacts`, `bia.downtime`, `bia.downtime_drivers`, `bia.alternate_means` | Impact category / Severe / Moderate / Minimal; Mission/Business Process / …categories… / Impact; Mission/Business Process / MTD / RTO / RPO |
| 3.2 Identify Resource Requirements | `bia.resources` | System Resource/Component, Platform/OS/Version (as applicable), Description |
| 3.3 Identify Recovery Priorities for System Resources | `bia.priorities`, `bia.alternate_strategies` | Priority, System Resource/Component, Recovery Time Objective |

NIST states the BIA in three steps (§1.1, quoted in the rendered document): determine
mission/business processes and recovery criticality; identify resource requirements; identify
recovery priorities for system resources. Run the workshop in that order.

## Step 1 — Processes (`bia.processes`)

Work with the people who own the work, not with the people who own the servers. Ask what
*business* processes stop if this system stops. One row per process, with a description a
non-engineer would recognise.

NIST's own example ("Pay vendor invoice") is an illustration and the generator deliberately
never emits it. Do not borrow it.

## Step 2 — Impact categories (`bia.impact_categories`)

NIST: "Impact categories and values should be created in order to characterize levels of
severity to the organization." It shows **Cost** as *an example of an impact category*, with
sample dollar figures. Those figures are a sample, not a default. Ask the organisation for
their own categories — cost, harm to individuals, ability to perform mission, reputation,
regulatory exposure — and their own Severe / Moderate / Minimal values.

Then `bia.process_impacts`: for each process, the impact under each category. The rendered
table is as wide as the categories the organisation chose.

## Step 3 — MTD, then RTO, then RPO (`bia.downtime`)

Ask in that order, and use NIST's definitions verbatim (they are printed in the rendered
document, §3.1.1):

- **Maximum Tolerable Downtime (MTD)** — "the total amount of time leaders/managers are
  willing to accept for a mission/business process outage or disruption". A business number.
- **Recovery Time Objective (RTO)** — "the maximum amount of time that a system resource can
  remain unavailable before there is an unacceptable impact on other system resources,
  supported mission/business processes, and the MTD."
- **Recovery Point Objective (RPO)** — "the point in time, prior to a disruption or system
  outage, to which mission/business process data must be recovered (given the most recent
  backup copy of the data) after an outage."

**The constraint to enforce:** "Because the RTO must ensure that the MTD is not exceeded, the
RTO must normally be shorter than the MTD" (NIST SP 800-34 Rev. 1, §3.2.1, p. 16). If the
answers violate that, say so and ask which number is wrong. NIST also notes that "when it is
not feasible to immediately meet the RTO and the MTD is inflexible, a Plan of Action and
Milestone should be initiated to document the situation and plan for its mitigation" — that
is the honest outcome when the architecture cannot meet the business's number, not a quietly
relaxed RTO.

Values are "expected to be specific time frames, identified in hourly increments (i.e., 8
hours, 36 hours, 97 hours, etc.)".

Then `bia.downtime_drivers` — what each number comes from (mandate, contract, workload,
performance measure) — and `bia.alternate_means`, the secondary processing or manual
work-around. **"If none exist, so state"**; "none" is the answer NIST asks for, not a gap.

The RTO agreed here is the same number that goes in ISCP section 1.4 Scope
(`scope.rto_hours`). Check they match before rendering.

## Step 4 — Resources (`bia.resources`) and priorities (`bia.priorities`)

`iscp_import_cis` will have derived Configuration Items from the Terraform or CloudFormation
in the repository. `iscp_status section=L` offers them as draft rows. **Show the drafts,
confirm them, then `iscp_answer accept_prefill=true`** — the drafts know the resource type and
region, they do not know the recovery priority or the recovery time, and those cells are left
empty on purpose for a human to fill.

NIST: "A system resource can be software, data files, servers, or other hardware and should
be identified individually or as a logical group." Grouping is allowed and usually clearer.

Finish with `bia.alternate_strategies`: "backup or spare equipment and vendor support
contracts".

## What not to do

- Do not compute an MTD, RTO or RPO from the architecture. They are the business's numbers;
  the architecture is then judged against them.
- Do not carry NIST's sample figures into a real plan.
- Do not fill a priority order the business has not agreed. Recovery order is a decision with
  consequences for whoever waits longest.
