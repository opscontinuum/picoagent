---
name: dr-runbook-authoring
description: Turn the generated RB-01..RB-04 runbook skeletons into procedures an on-call engineer can actually execute, without asserting product behaviour that has not been verified
---
# Authoring the recovery runbooks

`iscp_render` writes four skeletons — `RB-01-switchover`, `RB-02-failover`, `RB-03-failback`,
`RB-04-drill` — each with the same seven sections: decision gate, forensics capture, execution
sequence, per-component steps, validation, work recovery and reconciliation, sign-off.

**What these are.** FedRAMP publishes no runbook template, and this project does not invent
one with a FedRAMP-looking cover. The runbooks are the appendix that ISCP section 4.2
Recovery Procedures explicitly allows: "specific keystroke-level procedures may be provided in
an appendix". NIST SP 800-34 Rev. 1 §4.5 likewise lists "detailed recovery procedures and
checklists" among a plan's appendices. Their structure is this project's own; say so if
anyone asks whether they are a "FedRAMP DRP". There is no such thing.

**What the generator put in them.** Only two kinds of line: a step derived from a
Configuration Item the IaC scan actually found, and an explicit `TODO(<id>)` naming the
answer or the human decision that would fill it. Nothing else. Your job is to replace the
TODOs.

## Every step needs five things

1. **A named component.** The generated steps carry the CI id and its Terraform address.
   Keep them — during an incident, `oci_core_volume_group.primary_data` is unambiguous and
   "the data volumes" is not.
2. **A command or a console action.** Literal, copy-pasteable, with the arguments filled.
3. **An owner role** from ISCP section 2.5 (Contingency Planning Director, Contingency
   Planning Coordinator, Hardware/Software Recovery Team, Telecommunications Team, Procurement
   and Logistics Coordinator, Security Coordinator). A role, never a person's name — people
   change and Appendix A is where names live.
4. **An expected duration.** Steps without one cannot be added up against the RTO.
5. **A verification.** What you check to know the step worked, not "it should be up".

## Cite product behaviour or mark it unverified

The generator never asserts how a product behaves, and neither should you without a source.
When a step depends on what a vendor's feature actually does — what a database failover leaves
the old primary in, whether a filesystem replication target is writable while the replication
resource still exists, whether activating a volume-group replica clones or moves —
**either cite the vendor documentation page, or mark the sentence
`(unverified: engineering judgement)`.**

This matters more than it sounds. A runbook step that is wrong about product behaviour is
discovered at the worst possible moment, and an unmarked wrong claim is indistinguishable
from a verified one.

## One-way doors go above the step that opens them

Section 1's decision gate has a placeholder for these. A one-way door is any step whose
reversal costs a project rather than a command: a failover that leaves the old primary
unusable as a standby, a replication policy deleted to unlock a target, a promoted database
that cannot be demoted without a rebuild. List them before the reader reaches them, not in a
postmortem.

## Per-runbook notes

- **RB-01 switchover** is planned and both sites are healthy: quiesce cleanly, let replication
  catch up before you cut, and expect no data loss. It should have a rollback section that
  RB-02 cannot have.
- **RB-02 failover** is unplanned. Section 2's forensics capture is not optional — the
  replication lag at the moment of loss is the data-loss statement, and it is gone once roles
  change. This runbook is a one-way door in its entirety.
- **RB-03 failback** is a project, not an incident step. Replication has to be rebuilt in the
  opposite direction, which usually means full baseline copies. Give it entry criteria that
  must all be true, and a drift-reconciliation section — anything changed at the alternate
  site during the outage has to come home too.
- **RB-04 drill** **must not require a real failover** and must not touch production traffic.
  If the only way to exercise a step is to fail over, the drill covers everything up to that
  step and says so. Isolation of the drill environment is mandatory, and a drill left running
  can block a real failover — say how to stop it first.

## Authority lives in the ISCP, not here

The decision to invoke is made by the personnel named in **ISCP Table 3.1 Personnel Authorized
to Activate the ISCP**, against the criteria in **section 3.1 Activation Criteria and
Procedure**. A runbook that grants itself invocation authority contradicts the plan it belongs
to. Reference Table 3.1; do not restate it.

## Before you call a runbook finished

- No `TODO` markers left, or every remaining one is listed to the user with the reason.
- Every step has all five things above.
- Every product-behaviour claim is cited or marked.
- The durations add up to less than the RTO in ISCP section 1.4, or the gap is stated
  explicitly as a finding rather than rounded away.
- ISCP `recovery.procedures` (section 4.2) references these runbooks — the template requires
  that reference when procedures live in an appendix, and the generator will not write it for
  you.
