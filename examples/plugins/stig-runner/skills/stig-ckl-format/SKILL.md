---
name: stig-ckl-format
description: What the CKL checklist format is, what stig-runner changes in it and what it preserves, the four status spellings, and how to open the output in STIG Viewer 2.x and 3.x
---
# The CKL format, and what this runner does to it

CKL is the XML checklist format of DISA STIG Viewer - what STIG Viewer 3.x calls
"CKL (SV v2 format)" when exporting, and what 2.x reads natively.

## What changes and what does not

`stig-runner` writes exactly five text nodes per rule and the twelve `<ASSET>` children:

| Changed | Never changed |
|---|---|
| `<STATUS>` | every `<STIG_DATA>` pair (Vuln_Num, Rule_ID, Check_Content, Fix_Text, CCI_REF, LEGACY_ID, …) |
| `<FINDING_DETAILS>` | the whole `<STIG_INFO>` block (version, stigid, releaseinfo, uuid, …) |
| `<COMMENTS>` | `STIG_UUID` on every rule |
| `<SEVERITY_OVERRIDE>` | `TARGET_KEY` - STIG Viewer's own reference id for the target |
| `<SEVERITY_JUSTIFICATION>` | element order, indentation, escaping, line endings, the header comment |
| the twelve `<ASSET>` children except `TARGET_KEY` | |

The safety argument is that structure is never touched, not that the output is validated: the
"Checklist Schema v2.2" XSD ships inside the STIG Viewer distribution and is not published.
`stig_save` re-reads what it wrote and compares rule identity, order, `STIG_INFO` and every
`STIG_DATA` pair against the input, and marks itself an error if anything moved.

## The four statuses

| STIG Viewer UI | XML spelling |
|---|---|
| Not Reviewed | `Not_Reviewed` |
| Open | `Open` |
| Not A Finding | `NotAFinding` (no underscores) |
| Not Applicable | `Not_Applicable` |

They are case-sensitive. Anything else in a `<STATUS>` element makes `stig_load` refuse the
file rather than guess.

## Finding Details versus Comments

By convention among reviewers, and the convention this plugin follows:

- **Finding Details**: the evidence and the reasoning. What was inspected, what was found,
  where - `path:line` - and why that leads to this status. This is the field an auditor reads.
- **Comments**: reviewer notes. Who reviewed it and when, what to re-check next cycle, a
  pointer to a ticket or a mitigation in progress. Not a place for the evidence itself.

## Severity override

`<SEVERITY_OVERRIDE>` changes the severity this rule scores at for this asset; it requires a
`<SEVERITY_JUSTIFICATION>` and the runner refuses one without the other. It is a decision with
an owner - use it only when the user has told you to and has given you the justification text.

## Opening the output

- **STIG Viewer 3.x**: hamburger menu -> **Import V2 Checklist**. The 3.x User Guide V1R5
  describes this as a plain open-and-convert; it documents no requirement that the STIG be in
  your library first, and the runner never rewrites `uuid` or `STIG_UUID`, so the file carries
  exactly the identifiers STIG Viewer wrote into it. (Not confirmed by running the viewer -
  see the README's manual acceptance checklist.)
- **STIG Viewer 2.x**: open it directly.

If a checklist contains more than one `<iSTIG>`, the runner refuses it. Split it in STIG Viewer
and run one STIG at a time.

## Not produced

CKLB (STIG Viewer 3's native JSON), XCCDF results, CMRS, and eMASS uploads are all out of
scope. So is scoring: STIG Viewer computes the score from status and weight itself.
