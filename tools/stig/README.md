# STIG tooling

Three scripts for working the assessment record in `docs/security/assessment/`, stdlib only,
plus the re-assessment procedure.

| Script | Use |
|---|---|
| `validate_ckl.py [ckl]` | Structural checks on the checklist (defaults to the committed one): 286 rules, every required tag present, severity-by-status table. Run after any edit to the CKL |
| `show_rule.py V-222xxx [xccdf]` | Print one rule's title, discussion, check and fix from the DISA benchmark (download per `docs/security/assessment/README.md`) |
| `xccdf_to_ckl.py [dir]` | Regenerate a blank checklist from the benchmark and populate it from a findings JSON; used to produce the original record |

## Re-assessment procedure (per release, and at threat-model triggers)

1. `git log <last-assessed-commit>..HEAD --oneline` and read the diff against the current
   determinations - the addendum's scope note names the last full pass.
2. For every rule whose determination cites code or a test, confirm the citation still
   holds; the suite's `test_stig_assessment_record.py` has already checked existence, so
   this pass is about meaning.
3. New entry points, privilege decisions, or files picoagent reads that something else
   writes get walked against the full rule set (`show_rule.py` per rule), not just the
   currently-Open six.
4. Record changed determinations by appending a dated note to the rule's finding details
   and a row to a new dated addendum - never by rewriting an old record.
5. Check DISA's quarterly window for a new ASD release (V6R4 skips quarters; absence of a
   release is normal). A new release means re-keying: V-numbers are stable, `SV-...r..._rule`
   suffixes are not.
6. Run `validate_ckl.py`, then the suite, before the commit.
