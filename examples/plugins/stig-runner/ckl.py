"""Read and write DISA STIG Viewer CKL checklists without changing anything but the answers.

The safety property
-------------------
The CKL schema ("Checklist Schema v2.2", per STIG Viewer's own validation messages) ships
inside the STIG Viewer distribution and is not published, so this module cannot validate
against it. Its safety comes from the opposite direction: **it never changes structure.** The
document is parsed, at most five text nodes per rule and the twelve ``<ASSET>`` children are
assigned to, and the tree is written back. No element is added, removed or reordered; the
``<STIG_INFO>`` block, ``TARGET_KEY``, ``STIG_UUID`` and every ``<STIG_DATA>`` pair come out
exactly as they went in. A checklist STIG Viewer accepted before the runner touched it is
therefore still the same document afterwards, with different answers in it.

Reproducing the byte layout
---------------------------
``ElementTree`` gets most of the way there and loses three things, each handled here:

* it drops comments and the declaration outside the root, so the input's first two lines are
  captured verbatim by :func:`_read_prologue` and prepended on write (STIG Viewer writes
  ``<?xml version="1.0" encoding="UTF-8"?>`` with double quotes; ElementTree would emit
  single);
* ``ET.indent`` needs to be told the indent unit, which is a tab in STIG Viewer 3.7.0 output,
  and is detected from the input rather than assumed;
* an XML parser turns ``&#xD;`` into a bare ``\\r`` and a plain serialiser writes that byte
  straight back out, silently rewriting the file. ASD V6R4 has eleven rules whose text
  carries ``&#xD;``, so :func:`_serialise` re-encodes every ``\\r`` as ``&#xD;``. After parsing
  a ``\\r`` can only have come from a character reference - XML normalises literal CRLF in
  content to LF - so the mapping back is unambiguous.

Verified: loading and re-writing an unedited 286-rule ASD V6R4 checklist exported by STIG
Viewer 3.7.0 reproduces the input byte for byte (1,658,000 bytes, SHA-equal). The test suite
proves the same property against ``picoagent.testing.fake_ckl``, which builds its bytes with
string formatting rather than ElementTree so the test is not ElementTree agreeing with itself.

No picoagent imports: this module is usable and testable on its own.
"""
from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

#: The four status spellings CKL uses. STIG Viewer's UI calls them Not Reviewed, Open,
#: Not A Finding and Not Applicable; these are the XML forms and they are case-sensitive.
STATUSES = ("Not_Reviewed", "Open", "NotAFinding", "Not_Applicable")

#: The only text nodes inside a ``<VULN>`` this module ever assigns to.
EDITABLE_TAGS = ("STATUS", "FINDING_DETAILS", "COMMENTS", "SEVERITY_OVERRIDE",
                 "SEVERITY_JUSTIFICATION")

#: The twelve ``<ASSET>`` children, in document order. ``TARGET_KEY`` is in the list because it
#: must be *preserved*; :meth:`Checklist.set_asset` refuses to write it.
ASSET_FIELDS = ("ROLE", "ASSET_TYPE", "HOST_NAME", "HOST_IP", "HOST_MAC", "HOST_FQDN",
                "TARGET_COMMENT", "TECH_AREA", "TARGET_KEY", "WEB_OR_DATABASE", "WEB_DB_SITE",
                "WEB_DB_INSTANCE")

#: STIG Viewer owns this one - it is the viewer's own reference id for the target.
READ_ONLY_ASSET_FIELDS = ("TARGET_KEY",)

_PROLOGUE = re.compile(r"\A\s*(<\?xml[^>]*\?>)?\s*(<!--.*?-->)?", re.DOTALL)
_FIRST_INDENT = re.compile(r"\n([ \t]+)<")


class CklError(Exception):
    """The file is not a checklist this module can safely round-trip.

    Raised only for structural surprises - a missing ``<CHECKLIST>``/``<ASSET>``/``<iSTIG>``,
    more than one ``<iSTIG>``, an unknown status spelling. Anything a caller could reasonably
    recover from is a return value, not this.
    """


@dataclass
class Rule:
    """One ``<VULN>``: the fields a reviewer reads, plus the live element edits write through.

    The dataclass fields are a snapshot for reading and filtering; the assignments in
    :meth:`Checklist.set_status` update both them and ``element`` so the two never drift.
    """
    vuln_num: str
    rule_id: str
    rule_ver: str
    severity: str
    title: str
    group_title: str
    discussion: str
    check_content: str
    fix_text: str
    documentable: str
    ccis: list[str]
    legacy_ids: list[str]
    status: str
    finding_details: str
    comments: str
    severity_override: str
    severity_justification: str
    element: ET.Element = field(repr=False)

    def matches(self, identifier: str) -> bool:
        """Accept any of the three ids a reviewer might quote, case-insensitively.

        Reviewers copy ``V-222387`` out of one report, ``SV-222387r960735_rule`` out of an
        XCCDF result and ``APSC-DV-000010`` out of the STIG itself; all three name this rule.
        """
        wanted = identifier.strip().lower()
        return wanted in (self.vuln_num.lower(), self.rule_id.lower(), self.rule_ver.lower())


@dataclass
class Checklist:
    """A loaded checklist and the edits made to it. Nothing is written until :meth:`write`."""
    path: Path
    tree: ET.ElementTree
    prologue: str                 #: declaration + header comment, verbatim from the input
    newline: str                  #: ``"\\n"`` or ``"\\r\\n"``, matching the input
    indent: str                   #: the input's indent unit, normally a tab
    trailing_newline: bool        #: whether the input ended with a line break
    asset: dict[str, str]
    stig_info: dict[str, str]
    rules: list[Rule]
    dirty: int = 0                #: edits made since load or last write

    # ------------------------------------------------------------------ reading

    def by_vuln(self, identifier: str) -> Rule | None:
        """Look a rule up by ``Vuln_Num``, ``Rule_ID`` or ``Rule_Ver``."""
        for rule in self.rules:
            if rule.matches(identifier):
                return rule
        return None

    def counts(self) -> dict[str, int]:
        """Rules by status, plus ``"<severity> <status>"`` pairs for per-severity progress."""
        tally: dict[str, int] = {status: 0 for status in STATUSES}
        for rule in self.rules:
            tally[rule.status] = tally.get(rule.status, 0) + 1
            key = f"{rule.severity} {rule.status}"
            tally[key] = tally.get(key, 0) + 1
        return tally

    @property
    def title(self) -> str:
        return self.stig_info.get("title", "(no title)")

    @property
    def release(self) -> str:
        version = self.stig_info.get("version", "?")
        return f"Version {version}, {self.stig_info.get('releaseinfo', '(no release info)')}"

    # ------------------------------------------------------------------ editing

    def set_status(self, identifier: str, status: str, finding_details: str | None = None,
                   comments: str | None = None, severity_override: str | None = None,
                   severity_justification: str | None = None) -> Rule:
        """Record a determination on one rule. Only the five editable text nodes change.

        ``None`` leaves a field as it was; ``""`` clears it. Raises :class:`CklError` for an
        unknown rule or status - both are programming errors here, because the tool layer
        validates the model's arguments before calling this.
        """
        if status not in STATUSES:
            raise CklError(f"unknown status {status!r}; expected one of {', '.join(STATUSES)}")
        rule = self.by_vuln(identifier)
        if rule is None:
            raise CklError(f"no rule matching {identifier!r}")
        updates = {"STATUS": status, "FINDING_DETAILS": finding_details, "COMMENTS": comments,
                   "SEVERITY_OVERRIDE": severity_override,
                   "SEVERITY_JUSTIFICATION": severity_justification}
        for tag, value in updates.items():
            if value is None:
                continue
            _set_text(rule.element, tag, value)
        rule.status = status
        for attribute, tag in (("finding_details", "FINDING_DETAILS"), ("comments", "COMMENTS"),
                               ("severity_override", "SEVERITY_OVERRIDE"),
                               ("severity_justification", "SEVERITY_JUSTIFICATION")):
            if updates[tag] is not None:
                setattr(rule, attribute, updates[tag])
        self.dirty += 1
        return rule

    def set_asset(self, **fields: str) -> dict[str, str]:
        """Fill ``<ASSET>`` children. Unknown names and ``TARGET_KEY`` raise; ``None`` skips."""
        asset = self.tree.getroot().find("ASSET")
        for name, value in fields.items():
            tag = name.upper()
            if value is None:
                continue
            if tag in READ_ONLY_ASSET_FIELDS:
                raise CklError(f"{tag} is STIG Viewer's own reference id and is never rewritten")
            if tag not in ASSET_FIELDS:
                raise CklError(f"unknown ASSET field {tag}")
            _set_text(asset, tag, str(value))
            self.asset[tag] = str(value)
        self.dirty += 1
        return dict(self.asset)

    # ------------------------------------------------------------------ writing

    def to_bytes(self) -> bytes:
        """Serialise the tree with the input's declaration, comment, indent and line endings."""
        return _serialise(self.tree.getroot(), self.prologue, self.newline, self.indent,
                          self.trailing_newline)

    def write(self, path: Path) -> Path:
        """Write to ``path`` via a temp file and ``os.replace``, so a crash cannot truncate it."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".tmp")
        temp.write_bytes(self.to_bytes())
        os.replace(temp, path)
        self.dirty = 0
        return path


# --------------------------------------------------------------------------- parsing

def _set_text(parent: ET.Element, tag: str, value: str) -> None:
    """Assign one child's text, creating nothing. A missing child means the file is not a CKL."""
    child = parent.find(tag)
    if child is None:
        raise CklError(f"expected a <{tag}> element under <{parent.tag}>")
    child.text = value or ""


def _read_prologue(raw: bytes) -> str:
    """The XML declaration and header comment, verbatim, or a STIG-Viewer-shaped default.

    ElementTree discards both (they sit outside the root), and both are part of what makes a
    file look like STIG Viewer's own output, so they are carried across as text.
    """
    head = raw[:400].decode("utf-8", errors="replace").replace("\r\n", "\n")
    match = _PROLOGUE.match(head)
    declaration = (match.group(1) if match and match.group(1)
                   else '<?xml version="1.0" encoding="UTF-8"?>')
    comment = match.group(2) if match and match.group(2) else ""
    return declaration + "\n" + (comment + "\n" if comment else "")


def _detect_indent(raw: bytes) -> str:
    """The input's indent unit, taken from its first indented line. Tab if there is none."""
    match = _FIRST_INDENT.search(raw[:4000].decode("utf-8", errors="replace"))
    return match.group(1) if match else "\t"


def _serialise(root: ET.Element, prologue: str, newline: str, indent: str,
               trailing_newline: bool) -> bytes:
    """Render the tree the way STIG Viewer writes one.

    ``short_empty_elements=False`` keeps ``<HOST_NAME></HOST_NAME>`` in long form. The ``\\r``
    substitution runs on the serialised text, where the only carriage returns present came
    from ``&#xD;`` in the source (indentation is built from ``\\n`` plus ``indent``), and it
    runs *before* the line-ending conversion so a CRLF file keeps its entities as entities.
    """
    ET.indent(root, space=indent)
    body = ET.tostring(root, encoding="unicode", short_empty_elements=False)
    text = prologue + body.replace("\r", "&#xD;")
    if trailing_newline:
        text += "\n"
    if newline != "\n":
        text = text.replace("\n", newline)
    return text.encode("utf-8")


def load(path: Path) -> Checklist:
    """Parse a CKL from disk. Raises :class:`CklError` on anything this module would not
    round-trip."""
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise CklError(f"cannot read {path}: {exc}") from exc
    return load_bytes(raw, path)


def load_bytes(raw: bytes, path: Path) -> Checklist:
    """Parse a CKL already in memory. ``path`` is carried for messages and for :meth:`write`.

    Kept separate from :func:`load` so a caller can compare a file against bytes it captured
    before writing without touching the disk twice.
    """
    path = Path(path)
    try:
        root = ET.fromstring(raw.decode("utf-8-sig"))
    except (ET.ParseError, UnicodeDecodeError) as exc:
        raise CklError(f"{path} is not well-formed XML: {exc}") from exc

    if root.tag != "CHECKLIST":
        raise CklError(f"root element is <{root.tag}>, expected <CHECKLIST> - not a CKL file")
    asset_element = root.find("ASSET")
    if asset_element is None:
        raise CklError("no <ASSET> element: CHECKLIST/ASSET is required by the CKL layout")
    istigs = root.findall("STIGS/iSTIG")
    if not istigs:
        raise CklError("no <iSTIG> element under CHECKLIST/STIGS")
    if len(istigs) > 1:
        raise CklError(f"{len(istigs)} <iSTIG> elements: multi-STIG checklists are not supported; "
                       "split it in STIG Viewer and run one STIG at a time")

    asset = {tag: (asset_element.findtext(tag) or "") for tag in ASSET_FIELDS
             if asset_element.find(tag) is not None}
    stig_info = {}
    for si_data in istigs[0].findall("STIG_INFO/SI_DATA"):
        stig_info[si_data.findtext("SID_NAME") or ""] = si_data.findtext("SID_DATA") or ""

    rules = [_parse_vuln(element, index) for index, element in enumerate(istigs[0].findall("VULN"))]
    tree = ET.ElementTree(root)
    return Checklist(path=path, tree=tree, prologue=_read_prologue(raw),
                     newline="\r\n" if b"\r\n" in raw else "\n", indent=_detect_indent(raw),
                     trailing_newline=raw.endswith(b"\n"), asset=asset, stig_info=stig_info,
                     rules=rules)


def _parse_vuln(element: ET.Element, index: int) -> Rule:
    """Turn one ``<VULN>`` into a :class:`Rule`, keeping the element for write-through edits."""
    attributes: dict[str, list[str]] = {}
    for stig_data in element.findall("STIG_DATA"):
        name = stig_data.findtext("VULN_ATTRIBUTE") or ""
        attributes.setdefault(name, []).append(stig_data.findtext("ATTRIBUTE_DATA") or "")

    def one(name: str) -> str:
        return attributes.get(name, [""])[0]

    where = f"CHECKLIST/STIGS/iSTIG/VULN[{index + 1}]"
    status = element.findtext("STATUS")
    if status is None:
        raise CklError(f"{where} has no <STATUS> element")
    if status not in STATUSES:
        raise CklError(f"{where} has status {status!r}; expected one of {', '.join(STATUSES)}")
    for tag in EDITABLE_TAGS:
        if element.find(tag) is None:
            raise CklError(f"{where} has no <{tag}> element")

    return Rule(vuln_num=one("Vuln_Num"), rule_id=one("Rule_ID"), rule_ver=one("Rule_Ver"),
                severity=one("Severity"), title=one("Rule_Title"), group_title=one("Group_Title"),
                discussion=one("Vuln_Discuss"), check_content=one("Check_Content"),
                fix_text=one("Fix_Text"), documentable=one("Documentable"),
                ccis=attributes.get("CCI_REF", []), legacy_ids=attributes.get("LEGACY_ID", []),
                status=status, finding_details=element.findtext("FINDING_DETAILS") or "",
                comments=element.findtext("COMMENTS") or "",
                severity_override=element.findtext("SEVERITY_OVERRIDE") or "",
                severity_justification=element.findtext("SEVERITY_JUSTIFICATION") or "",
                element=element)
