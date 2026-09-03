---
name: pdf-evidence
description: How to quote a PDF so the quotation can be checked - confirm every sentence with pdf_find before writing it down, pin the edition by md5, cite the printed page, and tell a document's italic guidance apart from its content
---
A citation exists so a reader can look the sentence up. If they cannot, it is decoration, and a
decorative citation in a compliance document is worse than none: it transfers your confidence to
the reader without transferring the evidence.

The failure this procedure prevents is specific. Asked to quote a long standard, a model produces
sentences in the document's own register, on a plausible page, that are not there. They do not
look wrong. They read *better* than the real sentence, because the real one was written by a
committee. Nobody catches them without opening the PDF.

## The rule

**Run `pdf_find` on every sentence before you write it into anything.** Not the ones you are
unsure about — every one. Certainty is not evidence about certainty; it is the symptom.

If `pdf_find` returns NOT FOUND, you have one of three situations, and they are not the same:

1. **You paraphrased.** Most common. "must" for "should", "shall" for "will", a dropped
   subordinate clause. Go back to `pdf_pages` and read what is printed.
2. **Wrong edition.** Run `pdf_info` and compare the md5 against the one your source pinned.
   A revision moves sentences and sometimes deletes them.
3. **The page is not extractable.** A scanned page yields no text, so a real quotation reports
   absent. Check with `pdf_pages`; if the page comes back empty or as gibberish, say so in the
   report as an extraction limit. Do not quote it and do not report it as refuted.

What you must not do is quote it anyway with a hedge. "The standard indicates that..." followed
by an unverified sentence is the same defect wearing a hat.

## Citing

Give the reader the number printed on the page, because that is what they can look up, and give
the file position alongside so a link works. Pass `printed_offset` and the tools render both:

    printed p.26 (PDF p.40)

Offsets are per document: 14 for NIST SP 800-34 Rev. 1, 27 for NIST SP 800-53 Rev. 5. Derive one
by running `pdf_pages` on a page whose printed number you can see and subtracting.

Record the `pdf_info` md5 with the citation. An audit that does not pin the edition it read cannot
be reproduced, and "NIST SP 800-53" names four documents.

## Guidance is not content

NIST prints its advice about how to fill a template in *italic* and the template's own content in
regular type. So does FedRAMP. A generator that treats them alike publishes the standards body's
instructions as though the organisation had written them — a sentence that will be read as a
commitment the organisation never made.

Before treating a sentence from a template as content, run `pdf_spans` with `only=italic` on its
page. If it comes back in the italic set, it is instruction about the document, not a statement
in it. Quote it as guidance or leave it out.

## When you are quoting a lot

`pdf_dump` writes one text file per page plus a `SOURCE.txt` recording the source path and md5.
Use it when a document will be quoted repeatedly: the dump can be grepped by anything, survives
without the PDF, and lets a later reviewer re-run your verification without re-extracting.

## What good looks like

    pdf_info  path=... -> md5 4086ceb..., 149 pages
    pdf_find  path=... printed_offset=14 quote="<the exact sentence>"
              -> FOUND, printed p.26 (PDF p.40)

    > "Team leaders should have a designated alternate to act as the leader if the
    > primary leader is unavailable."
    > - NIST SP 800-34 Rev. 1 Sec. 3.4.6, printed p. 26 (md5 4086ceb...)

Every part of that line was checked by a tool. That is the whole point.
