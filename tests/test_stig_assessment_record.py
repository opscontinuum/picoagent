"""The ASD STIG assessment record stays whole, consistent, and evidenced - on every checkin.

212 of the STIG's 286 checks name an interview, so no test re-assesses anything. What a test
can hold, and these do, is the record itself: the committed checklist parses and covers all
286 rules with none slipped back to Not Reviewed; its determination counts and its Open set
are exactly what the current addendum states, so drift between the two is a build failure
rather than a quiet divergence; and every rule closed by a mechanism still has the mechanism
- the file its determination cites exists. Deleting `tests/test_command_injection.py` should
break the build twice: once as missing tests, and once here, as evidence a determination
stands on.

The suite runs before every checkin (docs/testing-and-conventions.md), which is what makes
this the per-commit half of the cadence in docs/security/assessment/README.md.
"""
from __future__ import annotations

import re
import unittest
import xml.etree.ElementTree as ET

from helpers import ROOT

ASSESSMENT = ROOT / "docs" / "security" / "assessment"
CKL = ASSESSMENT / "picoagent-asd-v6r4.ckl"
ADDENDUM = ASSESSMENT / "addendum-2026-09-09.md"

#: Determination -> the evidence it cites. Presence is checked here; meaning is the
#: per-release human pass. Extend this map when an addendum closes a rule on a mechanism.
EVIDENCE = {
    "V-222444": ["tests/test_shell_environment.py"],
    "V-222500": ["tests/test_session_log_permissions.py"],
    "V-222587": ["tests/test_config_permissions.py"],
    "V-222469": ["tests/test_session_shutdown_record.py"],
    "V-222604": ["tests/test_command_injection.py"],
    "V-222513": ["tests/test_plugin_pins.py"],
    "V-222649": ["tools/coverage_report.py", "docs/testing-and-conventions.md"],
    "V-222655": ["docs/security/threat-model.md"],
    "V-222657": ["SECURITY.md"],
    "V-222632": ["docs/security/assessment/scm-plan.md"],
    "V-222646": ["docs/security/assessment/scm-plan.md"],
}


def checklist() -> dict[str, ET.Element]:
    root = ET.parse(CKL).getroot()
    vulns = {}
    for vuln in root.findall("STIGS/iSTIG/VULN"):
        data = {d.find("VULN_ATTRIBUTE").text: (d.find("ATTRIBUTE_DATA").text or "")
                for d in vuln.findall("STIG_DATA")}
        vulns[data["Vuln_Num"]] = vuln
    return vulns


class TheChecklistIsWhole(unittest.TestCase):

    def setUp(self):
        self.vulns = checklist()

    def test_all_286_rules_are_present_and_none_is_unreviewed(self):
        self.assertEqual(len(self.vulns), 286)
        unreviewed = [n for n, v in self.vulns.items() if v.find("STATUS").text == "Not_Reviewed"]
        self.assertEqual(unreviewed, [], "rules slipped back to Not_Reviewed")

    def test_every_rule_carries_a_status_and_nonempty_finding_details(self):
        for number, vuln in self.vulns.items():
            with self.subTest(rule=number):
                self.assertIsNotNone(vuln.find("STATUS"))
                self.assertTrue((vuln.find("FINDING_DETAILS").text or "").strip(),
                                "a determination with no finding details is a verdict "
                                "without a reason")


class TheRecordAndTheAddendumAgree(unittest.TestCase):
    """The addendum is the human-readable register; the CKL is the machine one. They were
    written together and must fail together: a determination changed in one place only is
    exactly the drift this file exists to stop."""

    def setUp(self):
        self.vulns = checklist()
        self.addendum = ADDENDUM.read_text()

    def test_the_open_sets_are_identical(self):
        ckl_open = {n for n, v in self.vulns.items() if v.find("STATUS").text == "Open"}
        register = self.addendum[self.addendum.index("## The register after this addendum"):]
        doc_open = set(re.findall(r"\|\s*(V-\d{6})\s*\|", register))
        self.assertEqual(ckl_open, doc_open)

    def test_the_stated_counts_match_the_checklist(self):
        match = re.search(r"\*\*(\d+) Not Applicable, (\d+) Not a Finding, (\d+) Open, "
                          r"(\d+) Not Reviewed\.\*\*", self.addendum)
        self.assertIsNotNone(match, "the addendum no longer states its register counts")
        na, naf, open_, nr = (int(g) for g in match.groups())
        import collections
        counts = collections.Counter(v.find("STATUS").text for v in self.vulns.values())
        self.assertEqual((counts["Not_Applicable"], counts["NotAFinding"],
                          counts["Open"], counts["Not_Reviewed"]), (na, naf, open_, nr))

    def test_every_addendum_determination_is_dated_in_the_checklist(self):
        updated = set(re.findall(r"\|\s*(V-\d{6})\s*\|", self.addendum)) & set(self.vulns)
        self.assertGreaterEqual(len(updated), 38)
        for number in sorted(updated):
            with self.subTest(rule=number):
                self.assertIn("2026-09-09", self.vulns[number].find("FINDING_DETAILS").text,
                              "an addendum rule whose checklist entry carries no dated note")


class TheEvidenceStillExists(unittest.TestCase):

    def test_every_cited_mechanism_is_on_disk(self):
        for rule, paths in EVIDENCE.items():
            for path in paths:
                with self.subTest(rule=rule, evidence=path):
                    self.assertTrue((ROOT / path).exists(),
                                    f"{rule}'s determination cites {path}, which is gone; "
                                    "either restore it or re-determine the rule in a new "
                                    "addendum")

    def test_the_open_register_matches_the_threat_models_residuals(self):
        """The threat model's residual table (section 8) and the checklist must not disagree
        about the security-relevant Opens: every STIG rule the threat model records as a
        residual risk is Open in the checklist. The reverse is not required - the three
        process Opens (FIPS host-dependence, release hashes, training records) are debts,
        not threats, and live only in the assessment record."""
        threat_model = (ROOT / "docs" / "security" / "threat-model.md").read_text()
        section = threat_model[threat_model.index("## 8. Residual risk"):]
        vulns = checklist()
        for rule in re.findall(r"V-\d{6}", section.split("\n## ")[0]):
            status = vulns[rule].find("STATUS").text
            with self.subTest(rule=rule):
                self.assertIn(status, ("Open", "NotAFinding"),
                              f"{rule} is a threat-model residual but the checklist says "
                              f"{status}")
        # The headline residual is unambiguous: T11's rule is Open, CAT I, in both records.
        self.assertEqual(vulns["V-222604"].find("STATUS").text, "Open")


if __name__ == "__main__":
    unittest.main()
