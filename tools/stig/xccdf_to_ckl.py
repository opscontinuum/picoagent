"""Build a STIG Viewer 3.x checklist from the ASD V6R4 XCCDF, then apply the assessment.

STIG Viewer generates the blank checklist in the normal workflow; there is no copy of it here,
so it is generated from the benchmark instead. The output has to be a file STIG Viewer opens
and picoagent's own ``ckl.py`` round-trips, so the layout below imitates a 3.7.0 export down to
the tab indent and the attribute order, and the determinations are applied through ``ckl.py``
rather than written directly - if the plugin cannot load what this produces, that is a failure
of this script and shows up as one.
"""
from __future__ import annotations

import io
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import sys
STIG_DIR = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
WORKTREE = Path("/home/nemokrad/projects/itscm/picoagent/.claude/worktrees/ollama-e2e-and-install-docs")
sys.path.insert(0, str(WORKTREE / "examples/plugins/stig-runner"))
sys.path.insert(0, str(WORKTREE))

from picoagent.testing.fake_ckl import ASSET_FIELDS, HEADER_COMMENT  # noqa: E402

XCCDF_NS = {"x": "http://checklists.nist.gov/xccdf/1.1", "dc": "http://purl.org/dc/elements/1.1/"}

#: ``<VULN>`` attribute order as STIG Viewer writes it. LEGACY_ID and CCI_REF repeat at the end.
SINGLE_ATTRIBUTES = ("Vuln_Num", "Severity", "Group_Title", "Rule_ID", "Rule_Ver", "Rule_Title",
                     "Vuln_Discuss", "IA_Controls", "Check_Content", "Fix_Text", "False_Positives",
                     "False_Negatives", "Documentable", "Mitigations", "Potential_Impact",
                     "Third_Party_Tools", "Mitigation_Control", "Responsibility",
                     "Security_Override_Guidance", "Check_Content_Ref", "Weight", "Class",
                     "STIGRef", "STIG_UUID")


def _escape(value: str) -> str:
    """XML text escaping, with CR as an entity because that is what STIG Viewer emits."""
    return (value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace("\r", "&#xD;"))


def _element(depth: int, tag: str, value: str) -> str:
    return f"{'\t' * depth}<{tag}>{_escape(value)}</{tag}>\n"


def _text(node, path: str) -> str:
    found = node.find(path, XCCDF_NS)
    if found is None:
        return ""
    return "".join(found.itertext()).strip()


def parse_benchmark(xccdf: Path) -> tuple[list[dict], dict[str, str]]:
    """Every ``<Group>`` as the flat attribute mapping a ``<VULN>`` needs, plus the STIG header."""
    root = ET.parse(xccdf).getroot()
    title = _text(root, "x:title")
    plain = root.find("x:plain-text[@id='release-info']", XCCDF_NS)
    release = "".join(plain.itertext()).strip() if plain is not None else ""
    stig_id = root.get("id", "")

    rules = []
    for group in root.findall("x:Group", XCCDF_NS):
        rule = group.find("x:Rule", XCCDF_NS)
        if rule is None:
            continue
        check = rule.find("x:check/x:check-content", XCCDF_NS)
        idents = rule.findall("x:ident", XCCDF_NS)
        rules.append({
            "Vuln_Num": group.get("id", ""),
            "Severity": rule.get("severity", ""),
            "Group_Title": _text(group, "x:title"),
            "Rule_ID": rule.get("id", ""),
            "Rule_Ver": _text(rule, "x:version"),
            "Rule_Title": _text(rule, "x:title"),
            "Vuln_Discuss": _text(rule, "x:description"),
            "IA_Controls": "",
            "Check_Content": "".join(check.itertext()).strip() if check is not None else "",
            "Fix_Text": _text(rule, "x:fixtext"),
            "False_Positives": "", "False_Negatives": "",
            "Documentable": "false",
            "Mitigations": "", "Potential_Impact": "", "Third_Party_Tools": "",
            "Mitigation_Control": "", "Responsibility": "", "Security_Override_Guidance": "",
            "Check_Content_Ref": "M", "Weight": rule.get("weight", "10.0"),
            "Class": "Unclassified", "STIGRef": f"{title} :: {release}",
            # Per-rule, assigned by STIG Viewer, and absent from the XCCDF - so it cannot be
            # derived here for all 286. Left empty rather than invented: a made-up UUID would
            # look authoritative to anything that keys off it. STIG Viewer fills these in.
            "STIG_UUID": "",
            "_ccis": [i.text for i in idents
                      if i.text and i.text.startswith("CCI-")],
            "_legacy": [i.text for i in idents
                        if i.text and not i.text.startswith("CCI-")],
        })

    # The iSTIG uuid identifies the benchmark, not this assessment, so it is constant for
    # ASD V6R4. Taken from the STIG Viewer 3.7.0 export the test fixture was built from rather
    # than left blank, since a reader comparing this file against a Viewer-produced one for the
    # same STIG should find the same value there.
    info = {"version": "6", "classification": "UNCLASSIFIED", "customname": "",
            "stigid": stig_id, "description": "", "releaseinfo": release, "title": title,
            "uuid": "8a2162f9-56dd-4978-8924-a5a6f633b6bb",
            "notice": "terms-of-use", "source": "Unknown"}
    return rules, info


def build_blank(rules: list[dict], info: dict[str, str], asset: dict[str, str]) -> bytes:
    """A checklist with every rule Not_Reviewed - what STIG Viewer hands a new assessment."""
    out = io.StringIO()
    out.write('<?xml version="1.0" encoding="UTF-8"?>\n')
    out.write(HEADER_COMMENT + "\n<CHECKLIST>\n\t<ASSET>\n")
    for field in ASSET_FIELDS:
        out.write(_element(2, field, asset.get(field, "")))
    out.write("\t</ASSET>\n\t<STIGS>\n\t\t<iSTIG>\n\t\t\t<STIG_INFO>\n")
    for name, value in info.items():
        out.write("\t\t\t\t<SI_DATA>\n")
        out.write(_element(5, "SID_NAME", name))
        out.write(_element(5, "SID_DATA", value))
        out.write("\t\t\t\t</SI_DATA>\n")
    out.write("\t\t\t</STIG_INFO>\n")

    for rule in rules:
        out.write("\t\t\t<VULN>\n")
        pairs = [(name, rule[name]) for name in SINGLE_ATTRIBUTES]
        pairs += [("LEGACY_ID", value) for value in rule["_legacy"]]
        pairs += [("CCI_REF", value) for value in rule["_ccis"]]
        for name, value in pairs:
            out.write("\t\t\t\t<STIG_DATA>\n")
            out.write(_element(5, "VULN_ATTRIBUTE", name))
            out.write(_element(5, "ATTRIBUTE_DATA", value))
            out.write("\t\t\t\t</STIG_DATA>\n")
        out.write(_element(4, "STATUS", "Not_Reviewed"))
        for tag in ("FINDING_DETAILS", "COMMENTS", "SEVERITY_OVERRIDE",
                    "SEVERITY_JUSTIFICATION"):
            out.write(_element(4, tag, ""))
        out.write("\t\t\t</VULN>\n")
    out.write("\t\t</iSTIG>\n\t</STIGS>\n</CHECKLIST>\n")
    return out.getvalue().encode("utf-8")


def main() -> int:
    import ckl  # the plugin's own module: the checklist has to survive its loader

    xccdf = STIG_DIR / "U_ASD_STIG_V6R4_Manual-xccdf.xml"
    rules, info = parse_benchmark(xccdf)
    print(f"parsed {len(rules)} rules from {xccdf.name}")

    asset = {"ROLE": "None", "ASSET_TYPE": "Computing", "HOST_NAME": "picoagent",
             "TARGET_KEY": "4093", "WEB_OR_DATABASE": "false",
             "TARGET_COMMENT": "picoagent - stdlib-only coding agent harness. "
                               "Assessed at branch fix/architecture-review.",
             "TECH_AREA": ""}
    blank = STIG_DIR / "picoagent-asd-v6r4-blank.ckl"
    blank.write_bytes(build_blank(rules, info, asset))
    print(f"wrote blank checklist: {blank.name} ({blank.stat().st_size:,} bytes)")

    checklist = ckl.load(blank)          # proves the generated file is loadable
    print(f"ckl.load parsed {len(checklist.rules)} VULNs, asset HOST_NAME="
          f"{checklist.asset.get('HOST_NAME')!r}")

    findings = json.loads((STIG_DIR / "picoagent-asd-v6r4-findings.json").read_text())
    by_vuln = {f["vuln_num"]: f for f in findings}
    missing = [r.vuln_num for r in checklist.rules if r.vuln_num not in by_vuln]
    extra = [v for v in by_vuln if v not in {r.vuln_num for r in checklist.rules}]
    if missing or extra:
        print(f"MISMATCH: {len(missing)} rules with no finding, {len(extra)} findings with no rule")
        print("  missing:", missing[:5], "extra:", extra[:5])
        return 1

    for rule in checklist.rules:
        found = by_vuln[rule.vuln_num]
        details = found.get("finding", "").strip()
        evidence = found.get("evidence", "").strip()
        checklist.set_status(rule.vuln_num, found["status"],
                             finding_details=details,
                             comments=evidence)

    populated = STIG_DIR / "picoagent-asd-v6r4.ckl"
    checklist.write(populated)
    print(f"wrote populated checklist: {populated.name} "
          f"({populated.stat().st_size:,} bytes)")

    # Reload from disk: the round trip is the only thing that proves the write is usable.
    reloaded = ckl.load(populated)
    counts: dict[str, int] = {}
    for rule in reloaded.rules:
        counts[rule.status] = counts.get(rule.status, 0) + 1
    print("reloaded statuses:", dict(sorted(counts.items())))
    blank_out = {r.vuln_num for r in reloaded.rules if not r.finding_details.strip()}
    print(f"rules with empty FINDING_DETAILS: {len(blank_out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
