"""Structural checks on the produced checklist, independent of the module that wrote it."""
import collections
import pathlib
import sys
import xml.etree.ElementTree as ET

CKL = pathlib.Path(sys.argv[1] if len(sys.argv) > 1
                   else pathlib.Path(__file__).resolve().parents[2]
                   / "docs/security/assessment/picoagent-asd-v6r4.ckl")
root = ET.parse(CKL).getroot()

print("root tag:", root.tag)
print("ASSET children:", len(root.find("ASSET")))
istigs = root.findall("STIGS/iSTIG")
print("iSTIG count:", len(istigs))
vulns = istigs[0].findall("VULN")
print("VULN count:", len(vulns))
print("VULNs missing STATUS:", sum(1 for v in vulns if v.find("STATUS") is None))

required = ("STATUS", "FINDING_DETAILS", "COMMENTS", "SEVERITY_OVERRIDE", "SEVERITY_JUSTIFICATION")
incomplete = [v for v in vulns if any(v.find(tag) is None for tag in required)]
print("VULNs missing a required tag:", len(incomplete))

sev = collections.Counter()
empty_check = 0
for vuln in vulns:
    data = {d.find("VULN_ATTRIBUTE").text: (d.find("ATTRIBUTE_DATA").text or "")
            for d in vuln.findall("STIG_DATA")}
    sev[(data.get("Severity"), vuln.find("STATUS").text)] += 1
    if not data.get("Check_Content", "").strip():
        empty_check += 1

print("VULNs with empty Check_Content:", empty_check)
print("\nseverity x status")
for key in sorted(sev, key=lambda k: (k[0] or "", k[1])):
    print(f"  {key[0]:7} {key[1]:15} {sev[key]}")
print("\nCAT I (high) total:", sum(n for k, n in sev.items() if k[0] == "high"))
