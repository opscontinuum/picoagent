# iscp-author

Generate a **FedRAMP Information System Contingency Plan** from an interview, plus the
recovery runbooks and the configuration-item inventory that go with it. No network calls, no
third-party dependencies, standard library only.

```
/iscp                                  completeness summary for the human
iscp_status [section] [include_answered]   what is still open, and where each answer lands
iscp_answer id value [append] [accept_prefill]   record one validated answer
iscp_import_cis path [kind] [replace]      configuration items from Terraform / CloudFormation
iscp_render [output_dir] [documents]       write the document set
```

Skills: `iscp-interview` (how to run the interview), `bia-workshop` (how to run the Business
Impact Analysis), `dr-runbook-authoring` (how to finish the runbook skeletons).

## What it emits — and what it deliberately does not

| Output | Where its structure comes from |
|---|---|
| `ISCP.md` | FedRAMP SSP Appendix G ISCP Template **v5.0, 12/06/2024** — headings, table titles and column headers verbatim |
| `ISCP.md` Appendix L | NIST SP 800-34 Rev. 1 Appendix B, the BIA template — headings and table columns verbatim |
| `runbooks/RB-01-switchover.md` … `RB-04-drill.md` | **This project's own structure**, filling the appendix ISCP §4.2 allows |
| `CI-inventory.md`, `ci-inventory.json` | This project's own; the source a CSP fills the Integrated Inventory Workbook from |

**There is no standalone "DRP", and that is on purpose.** FedRAMP asks a cloud service
provider for exactly one contingency-planning document — SSP Appendix G, the ISCP — and
publishes no DRP template. NIST SP 800-34 Rev. 1 §2.2 defines a Disaster Recovery Plan as a
plan *type* ("applies to major, usually physical disruptions to service that deny access to
the primary facility infrastructure for an extended period") but publishes no DRP template
either: its Appendix A is ISCP templates and its Appendix B is the BIA template. A generated
document titled "Disaster Recovery Plan" with a structure of our own invention would look
authoritative, fail assessor review, and cost the reader more time than shipping nothing.

If you came here for "the DRP", what you want is the **runbook set**. ISCP §4.2 Recovery
Procedures says "specific keystroke-level procedures may be provided in an appendix. If
specific procedures are provided in an appendix, a reference to that appendix must be included
in this section", and NIST §4.5 lists "detailed recovery procedures and checklists" among a
plan's appendices. `runbooks/RB-01..RB-04` are that appendix. The generator will **not** write
the cross-reference in §4.2 for you — answer `recovery.procedures` yourself to say the
runbooks are your appendix.

Also not emitted, because the template's own Table 1.4 lists them as "Plans Outside of ISCP
Scope": a Business Continuity Plan, Continuity of Operations Plan, Occupant Emergency Plan or
crisis-communications plan. Nor an SSP.

## The anti-fabrication guarantee

Every byte of `ISCP.md` is one of exactly three things:

- **template text** — copied from the FedRAMP template or NIST SP 800-34 Rev. 1;
- **an answer** — a value recorded through `iscp_answer`, stringified and nothing more;
- **markup** — Markdown structure (`#`, `|`, `-`, newlines), which contains no letters or
  digits at all.

This is not a claim, it is a checked invariant. `iscp_render.render_iscp` returns the document
alongside a list of `Segment(kind, text)` covering every byte of it, and
`tests/test_iscp_plugin.py::ProvenanceTests` asserts that the segments reassemble into exactly
the document, that every `template` segment is a substring of the two sources, that every
`answer` segment appears in the answers file, and that every `markup` segment matches
`^[\s#|*\-:,]*$`. If someone adds a helpful sentence to the renderer, that test fails.

Two consequences worth knowing:

- **An unanswered question renders the source's own placeholder** — `<Insert CSO Name>`,
  `<Enter Number>`, `Click here to enter text.`, `Choose an item.`, NIST's `{insert}` — never a
  plausible default. `iscp_render` reports how many remain, per section.
- **The sources' worked examples are never emitted.** NIST illustrates its BIA tables with a
  sample organisation ("Pay vendor invoice", "Web Server 1 / Optiplex GX280 / 24 hours to
  rebuild or replace"). Those are illustrations; putting them in a real plan would hand an
  assessor a fictional business process. A test asserts they never appear.

The FedRAMP template's "Instructions:" boxes all end with "Delete this and all other
instructional text from your final version of this document", so they are not reproduced.
Boilerplate the template supplies as *final* text — the Three Phases description, the eight
role duty lists, Table 2.1 Backup Types, Table 2.4 Alternative Site Types — is reproduced in
full.

The runbooks and the CI inventory are outside this guarantee, because their structure is ours.
Inside them, every line is still either derived from a configuration item the IaC scan found
or an explicit `TODO(<id>)`. No product behaviour is ever asserted.

## The answers file

`contingency/answers.json` (configurable). Plain, diffable, committable:

```json
{"schema": 1, "answers": {"cso.name": "…"}, "cis": [...], "updated": "2026-09-02T…"}
```

Rendering is a pure function of this file: same input, same bytes out. Writes are atomic
(temp file + `os.replace`), and a corrupt file is reported rather than silently reset.

```toml
[plugins.iscp-author]
answers = "contingency/answers.json"
output  = "contingency/out"
```

## Configuration items from infrastructure-as-code

`iscp_import_cis` reads three inputs, all with the standard library:

- a **directory of `.tf` files** — a block scanner, not a grammar. It masks comments and
  string interiors so brace counting is safe, then recognises `resource`, `module`, `locals`
  and `provider` at the top level. `for_each` over a literal `locals` map expands to one CI
  per key; any other `count`/`for_each` records the expression as `_multiplicity`. Provider
  aliases resolve to regions when they are literal.
- **`terraform show -json` output** — preferred when you have it, because its values are
  already resolved. Structure per HashiCorp's "JSON Output Format" page.
- a **CloudFormation template in JSON**. YAML is refused with a message saying why: there is
  no YAML parser in the standard library and this plugin takes no dependencies.

**Why files and not cloud APIs.** No credentials, no network, no SDK; works air-gapped and on
Windows; and it inventories the *intended* estate the plan is written to protect. A cloud API
would inventory what exists right now — that is drift detection, a different job. Cloud
discovery is a non-goal.

Known limits, so nobody assumes otherwise: expressions are not evaluated (`var.shape` stays
the string `var.shape`); `dynamic` blocks and `for` expressions yield fewer CIs than they will
resources; unknown resource types are categorised `other` and **reported**, never guessed at.
The scanner never raises — it reports `file:line` warnings and carries on.

Relative paths may not climb out of the project. An absolute path is accepted, because a
Terraform repository often sits beside the documentation repository.

## Sources

All fetched or read on **2026-09-02**.

- **FedRAMP SSP Appendix G: Information System Contingency Plan (ISCP) Template, v5.0,
  12/06/2024.** `https://www.fedramp.gov/resources/templates/SSP-Appendix-G-Information-System-Contingency-Plan-(ISCP)-Template.docx`
  — HTTP 200, 153 865 bytes, md5 `298f6b1392ee21b1cded5164c2523b86`. Outline, body text, table
  titles and column headers extracted from `word/document.xml` with `zipfile` + `xml.etree`,
  keeping paragraph styles so instructional text stayed distinguishable. v5.0 is confirmed
  current: it is the newest row of the template's own revision history.
- **NIST SP 800-34 Rev. 1**, *Contingency Planning Guide for Federal Information Systems*, May
  2010 (errata 2010-11-11).
  `https://nvlpubs.nist.gov/nistpubs/Legacy/SP/nistspecialpublication800-34r1.pdf` — §2.2
  (plan types), §3.2.1 (MTD/RTO/RPO definitions and the "RTO must normally be shorter than the
  MTD" constraint), §4.5 (plan appendices), Appendix B pages B-1 to B-4 (the BIA template).
- **Terraform "JSON Output Format"**,
  `https://developer.hashicorp.com/terraform/internals/json-format` — the
  `values.root_module.resources[]` / `child_modules[]` structure `terraform show -json` emits.
- Structure of the runbook set is modelled on a real Oracle Cloud DR plan the user of this
  repository authorised as a reference. No content from it is reproduced.

### FedRAMP 20x

As of 2026-09-02, recovery planning under FedRAMP 20x is validated through **Key Security
Indicators** rather than the Rev 5 templates. The `KSI-RPL` family, from the FedRAMP
Consolidated Rules for 2026 (launched 2026-06-24,
`FedRAMP/2026-markdown:providers/20x/key-security-indicators/recovery-planning.md`), is:

| Indicator | Wording |
|---|---|
| `KSI-RPL-ABO` Aligning Backups with Objectives | "The alignment of machine-based information resource backups with defined recovery objectives is persistently reviewed." |
| `KSI-RPL-ARP` Aligning Recovery Plan | "The alignment of recovery plans with defined recovery objectives is persistently reviewed." |
| `KSI-RPL-RRO` Reviewing Recovery Objectives | "The desired Recovery Time Objectives (RTO) and Recovery Point Objectives (RPO) are defined and persistently reviewed for alignment with the provider's business needs and capabilities." |
| `KSI-RPL-TRC` Testing Recovery Capabilities | "The capability to recover from incidents and contingencies aligned with defined recovery objectives is persistently tested." |

**This plugin does not produce 20x output and does not claim 20x coverage.** It targets the
Rev 5 ISCP, which remains accepted. Note that FedRAMP's own documentation repository now files
the Rev 5 templates — this one included — under a `LEGACY` prefix, so check the current status
before submitting.

## Known gaps, stated rather than hidden

- **NIST Appendix B italics could not be verified.** NIST says "words in *italics* are for
  guidance only and should be deleted from the final version. Regular (non-italic) text is
  intended to remain", but the PDF's text layer does not carry that formatting. The
  conservative reading is used: only the paragraphs describing the document itself, the three
  numbered BIA steps, and the MTD/RTO/RPO definitions are reproduced; everything that reads as
  an instruction to the author is dropped. If that reading is wrong it errs towards emitting
  *less*, never towards emitting something the source does not say.
- **Word layout degrades to Markdown.** The template's label/value tables (Tables 2.2, 2.3,
  C.1–C.3, F.1, and the signature blocks) use merged cells; they render as two-column tables
  with the template's own caption as the header row. Every label and placeholder survives; the
  cell geometry does not. The signature blocks put Name/Date on one row in Word and on separate
  rows here.
- **No `.docx` output.** Markdown only; the Word template is the submission vehicle and
  copying headed sections across is mechanical.
- **Table 1.3's FedRAMP Application Number has no documented format** — the template's cell
  reads "Enter FedRAMP Application Number (This is the CSO Unique FedRAMP ID.)" and gives no
  pattern, so the field is free text with no validation.
