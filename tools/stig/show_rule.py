"""Print one rule's title, discussion and check content from the ASD XCCDF."""
import sys
import xml.etree.ElementTree as ET

NS = {"x": "http://checklists.nist.gov/xccdf/1.1"}
XCCDF = sys.argv[2] if len(sys.argv) > 2 else "U_ASD_STIG_V6R4_Manual-xccdf.xml"
wanted = sys.argv[1]

root = ET.parse(XCCDF).getroot()
for group in root.findall("x:Group", NS):
    if group.get("id") != wanted:
        continue
    rule = group.find("x:Rule", NS)
    print("TITLE:", "".join(rule.find("x:title", NS).itertext()))
    print("\nDISCUSSION:")
    print("".join(rule.find("x:description", NS).itertext())[:1100])
    print("\nCHECK:")
    print("".join(rule.find("x:check/x:check-content", NS).itertext())[:1400])
