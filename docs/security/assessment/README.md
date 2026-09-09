# The ASD STIG assessment record

The DISA Application Security and Development STIG V6R4 assessment of this repository, kept
in-tree because retained evidence is what half the STIG's process family actually asks for
(V-222515: "Retain scan results for compliance verification") - an assessment that lives in
a scratch directory is an assessment that stops existing.

| File | What it is |
|---|---|
| [assessment-2026-09-07.md](assessment-2026-09-07.md) | The full narrative assessment, verbatim, as written - a point-in-time record, annotated by the addendum rather than edited |
| [findings-2026-09-07.json](findings-2026-09-07.json) | The determination data behind it, same date, same rule |
| [probe-results-2026-09-07.json](probe-results-2026-09-07.json) | The repository evidence probes the assessment drew on |
| [addendum-2026-09-09.md](addendum-2026-09-09.md) | The delta: the eight findings post-remediation, the thirty previously unreviewed rules determined, the scope note for the plugin extraction, the Open register, and the cadence policy |
| [picoagent-asd-v6r4.ckl](picoagent-asd-v6r4.ckl) | The populated checklist (STIG Viewer 2.x format), every one of the 286 rules determined: 209 Not Applicable, 71 Not a Finding, 6 Open, 0 Not Reviewed. Addendum determinations carry a dated note in their finding details |
| [scm-plan.md](scm-plan.md) | The Software Configuration Management plan (V-222632), describing the process actually in force |

**The benchmark itself is not committed.** It is DISA's artifact, redistributable but
regenerable; pin, do not vendor:

    curl -LO https://dl.dod.cyber.mil/wp-content/uploads/stigs/zip/U_ASD_V6R4_STIG.zip
    # U_ASD_STIG_V6R4_Manual-xccdf.xml, sha256:
    # aa3176db372c3ec336d9489e59d67a4b8ac5a60bd08ffef65a5d687eaf7329a3

**How this stays true** - the cadence from the addendum, in one line each: every checkin,
`tests/test_stig_assessment_record.py` re-verifies the record's integrity and its evidence
as part of the ordinary suite; every release and at the threat model's out-of-cycle
triggers, a human walks the determinations against the diff (procedure in
[tools/stig/README.md](../../../tools/stig/README.md)); quarterly, the DISA maintenance
window and the CM access review.

What the per-checkin test is not: a re-assessment. 212 of the 286 checks name an interview,
and no test interviews anyone. It holds that the checklist parses, that nothing has slipped
back to Not Reviewed, that the Open set matches the addendum's register exactly, and that
the evidence each mechanism-closed rule cites still exists - which is everything a machine
can honestly re-verify per commit, and nothing more.
