---
name: ooxml-evidence
description: How to read a customer's Word plan or Excel baseline as evidence - confirm every quotation with ooxml_find, tell a template's italic instructions from its content, and use ooxml_outline to see which sections are still empty rather than assuming a heading means an answer
---
Most organisations keep their contingency plan in Word and their control baseline in Excel. When
you are asked to audit, summarise or migrate one, you are reading a document that was written for
a human reader, by someone who had a template open, and the two most expensive mistakes are both
mistakes about *whose words these are*.

## The rule

**Run `ooxml_find` on every sentence before you write it down.** Same discipline as `pdf-evidence`.
A quotation that does not resolve to a heading or a cell is not evidence, whatever it sounds like.

`ooxml_find` reports a docx hit as a block index and the heading chain in force:

    block 214 (paragraph) under: 3 Activation and Notification > 3.1 Activation Criteria

Cite the heading, not the block index. Headings survive reformatting; indexes do not.

## Italic means "delete me", not "quote me"

Word templates mark the text the author is supposed to remove in italic. FedRAMP's ISCP template
opens with *"Delete this Template Revision History page and all other instructional text from your
final version of this document"* — in italic, along with 147 other blocks.

If you quote an italic block as the organisation's own words, you have attributed a standards
body's instructions to your customer. The tools mark it on every hit:

    [italic - template instruction, not content]

When you see that, you have three honest options: quote it explicitly as template guidance, use it
to explain what the section is *supposed* to contain, or leave it out. What you may not do is
report it as what the organisation wrote.

A paragraph that is only *partly* italic is not an instruction — an author emphasising a phrase in
real content is normal. Only a block whose every text run is italic is flagged.

## A heading is not an answer

The most common failure reading a half-filled template is treating the presence of a section as
evidence the section is complete. Run `ooxml_outline` first:

    [38] H2 Assumptions  - 10 block(s), 175 word(s)  [8 italic (instruction); 2 with placeholders]

That section has 175 words and says nothing: eight blocks are the template's instructions and the
remaining two still contain `<Insert ...>` placeholders. Reported as PASS, it is a false negative
that a real assessor will find.

Treat `EMPTY`, a high italic count, and any `<Insert ...>` or `{placeholder}` as evidence the
section is unfilled. Say so plainly; "the section exists but is unpopulated" is a finding, and a
more useful one than either PASS or a bare REFUTED.

## Workbooks

`ooxml_info` lists sheets with their used ranges. Read the header row before the data — the
FedRAMP baseline workbook puts control IDs in column C and its own parameters in G and H, and a
column read positionally without checking the header is how a report ends up citing the wrong
control.

Cite as `Sheet!Cell`:

    COVERSHEET!A1: "LEGACY NOTICE June 23, 2026 ..."

## Pin the edition

Record the `ooxml_info` md5 with your citations. Templates are revised, and "the FedRAMP ISCP
template" names several documents. Where a source pinned a hash, compare yours against it and say
so if they differ — a mismatch is a finding, not a nuisance.

## When you will quote a lot

`ooxml_dump` writes plain text — one file for a document, one per sheet for a workbook — plus a
`SOURCE.txt` recording the path and md5. Use it when a document will be quoted repeatedly: the
dump greps with anything, survives without Office, and lets a reviewer re-run your verification.
