"""stig-runner: CKL round-trip fidelity, path-contained evidence probes, and the human gate.

Three properties matter more than the rest, and each has its own class below:

* ``RoundTripTests`` - a load-then-save with no edits must reproduce the input **byte for
  byte**, and an edited save must differ only on the lines of the five editable elements.
  A checklist STIG Viewer refuses to open is worthless however good the analysis inside it is.
* ``EvidenceContainmentTests`` - the probes read a repository path the model chose, so they
  must not read outside it, must not follow a symlink out of it, and must not open ``.git``.
* ``DeterminationGateTests`` - no tool argument may record a status without the user. The
  only unattended path is ``interactive = false`` in the user's own config file.
"""
import re
import tempfile
import time
import unittest
from pathlib import Path

from helpers import CaptureFrontend, ScriptedProvider, ROOT, make_runtime, run, text, tool_ctx
from picoagent.plugins import loader
from picoagent.testing.fake_ckl import build_ckl, build_repo

PLUGIN = ROOT / "examples/plugins/stig-runner"


class AskSpy(CaptureFrontend):
    """CaptureFrontend records ``emit`` but not ``ask``; the gate tests need to assert on asks.

    Without this, "the user was never asked" is a vacuous assertion - the shared fixture has
    nothing to observe.
    """

    def __init__(self, answer=True):
        super().__init__(answer=answer)
        self.asks: list[tuple[str, str, dict]] = []

    async def ask(self, kind, prompt, **kw):
        self.asks.append((kind, prompt, kw))
        return self.answer

#: The five elements the runner is allowed to change inside a <VULN>.
EDITABLE = ("STATUS", "FINDING_DETAILS", "COMMENTS", "SEVERITY_OVERRIDE", "SEVERITY_JUSTIFICATION")


def _import_plugin_modules():
    """Import the plugin's standalone modules the way the loader does (plugin root on sys.path)."""
    import sys
    if str(PLUGIN) not in sys.path:
        sys.path.insert(0, str(PLUGIN))
    import asd_probes, ckl, evidence          # noqa: E401 - imported for the test's use
    return ckl, evidence, asd_probes


ckl, evidence, asd_probes = _import_plugin_modules()


class StigBase(unittest.TestCase):
    """A loaded plugin, a checklist on disk and a synthetic repository beside it."""

    plugin_config: dict = {}

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.ckl_path = self.tmp / "asd.ckl"
        self.raw = build_ckl()
        self.ckl_path.write_bytes(self.raw)
        self.repo = build_repo(self.tmp)
        self.rt = make_runtime(self.tmp, provider=ScriptedProvider([[text("ok")]]))
        self.rt.cfg.setdefault("plugins", {})["stig-runner"] = dict(self.plugin_config)
        loader.load_plugin(PLUGIN, self.rt, loader.TrustStore(self.tmp / "home"),
                           allow_untrusted=True)

    def tool(self, name, ui=None, **args):
        ctx = tool_ctx(self.tmp)
        ctx.ui = ui
        return run(self.rt.tools.get(name).execute(args, ctx))

    def load(self):
        return self.tool("stig_load", path=str(self.ckl_path))


# --------------------------------------------------------------------------- parsing

class ParseTests(StigBase):
    def test_load_reports_stig_asset_and_counts(self):
        result = self.load()
        self.assertFalse(result.is_error, result.content)
        self.assertIn("Application Security and Development", result.content)
        self.assertIn("Release: 4 Benchmark Date: 01 Oct 2025", result.content)
        self.assertIn("Not_Reviewed: 8", result.content)
        self.assertIn("V-222387..V-222657", result.content)
        self.assertEqual(result.details["rules"], 8)

    def test_stig_info_and_asset_are_parsed_in_order(self):
        checklist = ckl.load(self.ckl_path)
        self.assertEqual(list(checklist.stig_info)[:3], ["version", "classification", "customname"])
        self.assertEqual(checklist.stig_info["uuid"], "8a2162f9-56dd-4978-8924-a5a6f633b6bb")
        self.assertEqual(list(checklist.asset), list(ckl.ASSET_FIELDS))
        self.assertEqual(checklist.asset["TARGET_KEY"], "4093")

    def test_first_rule_fields(self):
        rule = ckl.load(self.ckl_path).rules[0]
        self.assertEqual(rule.vuln_num, "V-222387")
        self.assertEqual(rule.rule_id, "SV-222387r960735_rule")
        self.assertEqual(rule.rule_ver, "APSC-DV-000010")
        self.assertEqual(rule.severity, "medium")
        self.assertEqual(rule.ccis, ["CCI-000054"])
        self.assertEqual(rule.legacy_ids, ["V-69239", "SV-83861"])
        self.assertEqual(rule.documentable, "false")
        self.assertIn("logon sessions per user", rule.title)

    def test_lookup_accepts_all_three_id_styles(self):
        checklist = ckl.load(self.ckl_path)
        for identifier in ("V-222387", "SV-222387r960735_rule", "APSC-DV-000010", "apsc-dv-000010"):
            self.assertEqual(checklist.by_vuln(identifier).vuln_num, "V-222387", identifier)
        self.assertIsNone(checklist.by_vuln("V-999999"))

    def test_structural_surprises_are_errors_not_exceptions(self):
        bad = self.tmp / "bad.ckl"
        bad.write_bytes(b"<?xml version=\"1.0\"?>\n<NOTACHECKLIST></NOTACHECKLIST>")
        result = self.tool("stig_load", path=str(bad))
        self.assertTrue(result.is_error)
        self.assertIn("expected <CHECKLIST>", result.content)

    def test_multi_istig_checklists_are_refused_explicitly(self):
        doubled = self.raw.replace(b"\t\t</iSTIG>\n", b"\t\t</iSTIG>\n\t\t<iSTIG></iSTIG>\n")
        path = self.tmp / "two.ckl"
        path.write_bytes(doubled)
        with self.assertRaises(ckl.CklError) as caught:
            ckl.load(path)
        self.assertIn("multi-STIG checklists are not supported", str(caught.exception))

    def test_unknown_status_names_the_element_path(self):
        broken = self.raw.replace(b"<STATUS>Not_Reviewed</STATUS>", b"<STATUS>Reviewed</STATUS>", 1)
        path = self.tmp / "status.ckl"
        path.write_bytes(broken)
        with self.assertRaises(ckl.CklError) as caught:
            ckl.load(path)
        self.assertIn("VULN[1]", str(caught.exception))


# --------------------------------------------------------------------------- fidelity

class RoundTripTests(StigBase):
    def test_unedited_round_trip_is_byte_identical(self):
        """The acceptance criterion: indentation, escaping, empties and the prologue all survive.

        ``fake_ckl`` builds its bytes with string formatting rather than ElementTree, so this is
        not the serialiser agreeing with itself.
        """
        checklist = ckl.load(self.ckl_path)
        out = self.tmp / "out.ckl"
        checklist.write(out)
        self.assertEqual(out.read_bytes(), self.raw)

    def test_round_trip_preserves_carriage_return_entities(self):
        """``&#xD;`` must come back as ``&#xD;``: a parser turns it into a bare CR, and writing
        that byte back silently rewrites the file. Eleven ASD V6R4 rules carry one."""
        self.assertIn(b"&#xD;", self.raw)
        out = self.tmp / "cr.ckl"
        ckl.load(self.ckl_path).write(out)
        self.assertEqual(out.read_bytes().count(b"&#xD;"), self.raw.count(b"&#xD;"))
        self.assertNotIn(b"\r", out.read_bytes())

    def test_crlf_input_produces_crlf_output(self):
        path = self.tmp / "crlf.ckl"
        raw = build_ckl(crlf=True)
        path.write_bytes(raw)
        out = self.tmp / "crlf-out.ckl"
        ckl.load(path).write(out)
        self.assertEqual(out.read_bytes(), raw)

    def test_prologue_is_preserved_verbatim(self):
        out = self.tmp / "head.ckl"
        ckl.load(self.ckl_path).write(out)
        head = out.read_bytes().split(b"\n<CHECKLIST>")[0]
        self.assertEqual(head, b'<?xml version="1.0" encoding="UTF-8"?>\n'
                               b'<!--DISA STIG Viewer :: 3.7.0 -->')

    def test_an_edit_touches_only_the_editable_lines_of_that_rule(self):
        checklist = ckl.load(self.ckl_path)
        checklist.set_status("V-222387", "Open",
                             finding_details=r"app/http_client.py:6 uses verify=False "
                                             r"(<see> C:\logs\audit.log & the ticket)",
                             comments="reviewed 2026-09-02")
        out = self.tmp / "edited.ckl"
        checklist.write(out)

        before = self.raw.decode().splitlines()
        after = out.read_bytes().decode().splitlines()
        self.assertEqual(len(before), len(after), "line count must not change")
        differing = [line for old, new in zip(before, after) if old != new for line in (new,)]
        self.assertEqual(len(differing), 3, differing)      # STATUS, FINDING_DETAILS, COMMENTS
        for line in differing:
            self.assertTrue(any(f"<{tag}>" in line for tag in EDITABLE), line)

    def test_special_characters_in_details_are_escaped_and_survive_a_reparse(self):
        details = 'ampersand & less < greater > quote " path C:\\ProgramData\\app.log'
        checklist = ckl.load(self.ckl_path)
        checklist.set_status("V-222387", "Open", finding_details=details)
        out = self.tmp / "escaped.ckl"
        checklist.write(out)
        raw = out.read_bytes()
        self.assertIn(b"ampersand &amp; less &lt; greater &gt;", raw)
        self.assertEqual(ckl.load(out).by_vuln("V-222387").finding_details, details)

    def test_stig_info_target_key_and_rule_content_never_change(self):
        checklist = ckl.load(self.ckl_path)
        checklist.set_status("V-222387", "NotAFinding", finding_details="app/session.py:4")
        checklist.set_asset(host_name="APPSRV01")
        out = self.tmp / "kept.ckl"
        checklist.write(out)
        reloaded = ckl.load(out)
        original = ckl.load(self.ckl_path)
        self.assertEqual(reloaded.stig_info, original.stig_info)
        self.assertEqual(reloaded.asset["TARGET_KEY"], original.asset["TARGET_KEY"])
        for old, new in zip(original.rules, reloaded.rules):
            self.assertEqual((old.check_content, old.fix_text, old.discussion, old.ccis),
                             (new.check_content, new.fix_text, new.discussion, new.ccis))

    def test_target_key_cannot_be_written(self):
        checklist = ckl.load(self.ckl_path)
        with self.assertRaises(ckl.CklError):
            checklist.set_asset(target_key="9999")

    def test_three_hundred_rules_load_and_save_in_under_a_second(self):
        path = self.tmp / "big.ckl"
        path.write_bytes(build_ckl(rules=300))
        started = time.monotonic()
        checklist = ckl.load(path)
        checklist.write(self.tmp / "big-out.ckl")
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(len(checklist.rules), 300)
        self.assertEqual(len({rule.vuln_num for rule in checklist.rules}), 300)


# --------------------------------------------------------------------------- evidence

class EvidenceContainmentTests(StigBase):
    def test_planted_evidence_is_found_with_path_and_line(self):
        self.load()
        expected = {
            "APSC-DV-000160": "app/http_client.py:6",     # TLS verification disabled
            "APSC-DV-002030": "app/hashing.py:5",         # md5 for a fingerprint
            "APSC-DV-002540": "app/queries.py:2",         # SQL built by concatenation
            "APSC-DV-003110": "config/settings.py:2",     # a credential literal in a config file
            "APSC-DV-000010": "app/session.py:4",         # a concurrent-session setting
        }
        for rule_ver, where in expected.items():
            result = self.tool("stig_evidence", vuln_num=rule_ver, repo=str(self.repo))
            self.assertFalse(result.is_error, result.content)
            self.assertIn(where, result.content, rule_ver)

    def test_manifest_and_ci_probes_report_what_they_found(self):
        self.load()
        manifest = self.tool("stig_evidence", vuln_num="APSC-DV-002630", repo=str(self.repo))
        self.assertIn("requirements.txt", manifest.content)
        self.assertIn("NO lockfile", manifest.content)
        pipeline = self.tool("stig_evidence", vuln_num="APSC-DV-002490", repo=str(self.repo))
        self.assertIn(".github/workflows/ci.yml", pipeline.content)
        self.assertIn("semgrep", pipeline.content)

    def test_credential_values_are_masked_in_excerpts(self):
        self.load()
        result = self.tool("stig_evidence", vuln_num="APSC-DV-003110", repo=str(self.repo))
        self.assertIn("hunt****", result.content)
        self.assertNotIn("hunter2hunter2", result.content)

    def test_git_directory_large_and_binary_files_are_not_scanned(self):
        self.load()
        result = self.tool("stig_evidence", vuln_num="APSC-DV-000160", repo=str(self.repo))
        self.assertNotIn(".git/", result.content)
        self.assertNotIn("big.log", result.content)          # 2 MiB, over the size cap
        self.assertNotIn("blob.bin", result.content)         # NUL bytes in the first 8 KiB
        self.assertIn("skipped (over 1 MiB)", result.content)

    def test_symlink_out_of_the_repository_is_skipped(self):
        if not (self.repo / "escape").is_symlink():
            self.skipTest("no symlink support on this platform")
        self.load()
        result = self.tool("stig_evidence", vuln_num="APSC-DV-000160", repo=str(self.repo))
        self.assertIn("resolving outside the repository root", result.content)
        self.assertNotIn("escape/", result.content)

    def test_relative_path_escaping_the_project_is_refused(self):
        self.load()
        for attempt in ("../..", "../../etc", "repo/../../.."):
            result = self.tool("stig_evidence", vuln_num="APSC-DV-000160", repo=attempt)
            self.assertTrue(result.is_error, attempt)
            self.assertIn("outside the project directory", result.content)

    def test_max_hits_is_honoured(self):
        self.load()
        capped = self.tool("stig_evidence", vuln_num="APSC-DV-000160", repo=str(self.repo),
                           max_hits=1)
        self.assertTrue(all(count <= 1 for count in capped.details["hits"]), capped.details)

    def test_unmapped_rule_says_so_and_is_not_an_error(self):
        self.load()
        result = self.tool("stig_evidence", vuln_num="APSC-DV-003236", repo=str(self.repo))
        self.assertFalse(result.is_error)
        self.assertIn("no automated probe", result.content)
        self.assertIn("do not infer a status from the absence of a probe", result.content)

    def test_evidence_output_labels_itself_as_data(self):
        self.load()
        result = self.tool("stig_evidence", vuln_num="APSC-DV-003110", repo=str(self.repo))
        self.assertIn("evidence, not a determination", result.content)
        self.assertIn("serves:", result.content)


class ProbeTableTests(unittest.TestCase):
    def test_probe_table_integrity(self):
        self.assertGreaterEqual(len(asd_probes.PROBES), 30)
        for rule_ver, probes in asd_probes.PROBES.items():
            self.assertRegex(rule_ver, r"^APSC-DV-\d{6}$")
            self.assertTrue(probes, rule_ver)
            for probe in probes:
                self.assertIn(probe.kind, ("grep", "exists", "manifest", "ci"), rule_ver)
                self.assertTrue(probe.serves.strip(), rule_ver)
                if probe.kind == "grep":
                    re.compile(probe.pattern)          # raises re.error on a bad pattern
                    self.assertTrue(probe.pattern, rule_ver)


# --------------------------------------------------------------------------- the human gate

class DeterminationGateTests(StigBase):
    def test_headless_stig_set_is_refused(self):
        self.load()
        result = self.tool("stig_set", vuln_num="V-222387", status="NotAFinding",
                           finding_details="app/session.py:4 limits sessions")
        self.assertTrue(result.is_error)
        self.assertIn("needs an interactive session", result.content)
        self.assertEqual(ckl.load(self.ckl_path).rules[0].status, "Not_Reviewed")

    def test_accept_records_the_proposal(self):
        self.load()
        ui = AskSpy(answer="accept")
        result = self.tool("stig_set", ui=ui, vuln_num="V-222387",
                           status="NotAFinding", finding_details="app/session.py:4")
        self.assertFalse(result.is_error, result.content)
        self.assertTrue(result.details["recorded"])
        self.assertEqual(result.details["status"], "NotAFinding")

        kind, prompt, kwargs = ui.asks[0]
        self.assertEqual(kind, "select")
        self.assertIn("V-222387", prompt)
        self.assertIn("proposed status: NotAFinding", prompt)
        self.assertIn("app/session.py:4", prompt)
        self.assertEqual(kwargs["options"], ["accept", *ckl.STATUSES, "skip"])

    def test_the_user_can_choose_a_different_status(self):
        self.load()
        result = self.tool("stig_set", ui=CaptureFrontend(answer="Open"), vuln_num="V-222387",
                           status="NotAFinding", finding_details="app/session.py:4")
        self.assertEqual(result.details["status"], "Open")

    def test_skip_records_nothing_and_is_not_an_error(self):
        self.load()
        for answer in ("skip", None):
            result = self.tool("stig_set", ui=CaptureFrontend(answer=answer), vuln_num="V-222387",
                               status="NotAFinding", finding_details="x")
            self.assertFalse(result.is_error)
            self.assertFalse(result.details["recorded"])
            self.assertIn("skipped by user", result.content)

    def test_no_tool_argument_can_reach_the_unattended_flag(self):
        """The attack this guards: an argument named ``force``/``interactive``/``yes`` that lets
        the model grant itself unattended recording. None exists, and passing one changes
        nothing - the flag comes from the user's config file only."""
        import stig_runner
        for name in ("stig_set", "stig_save", "stig_asset", "stig_evidence"):
            schema = self.rt.tools.get(name).parameters.get("properties", {})
            for forbidden in stig_runner.FORBIDDEN_ARGUMENTS:
                self.assertNotIn(forbidden, schema, f"{name}.{forbidden}")

        self.load()
        result = self.tool("stig_set", vuln_num="V-222387", status="NotAFinding",
                           finding_details="x", interactive=False, force=True, yes=True)
        self.assertTrue(result.is_error)
        self.assertIn("needs an interactive session", result.content)

    def test_validation_runs_before_the_user_is_asked(self):
        self.load()
        cases = [
            (dict(vuln_num="V-222387", status="Reviewed", finding_details="x"), "unknown status"),
            (dict(vuln_num="V-222387", status="Open"), "finding_details is required"),
            (dict(vuln_num="V-222387", status="Open", finding_details="x",
                  severity_override="high"), "requires severity_justification"),
            (dict(vuln_num="V-222387", status="Open", finding_details="x",
                  severity_override="critical", severity_justification="j"),
             "must be high, medium or low"),
            (dict(vuln_num="V-999999", status="Open", finding_details="x"), "no rule matching"),
        ]
        for args, expected in cases:
            ui = AskSpy(answer="accept")
            result = self.tool("stig_set", ui=ui, **args)
            self.assertTrue(result.is_error, args)
            self.assertIn(expected, result.content)
            self.assertEqual(ui.asks, [], "an invalid proposal must never reach the user")


class UnattendedConfigTests(StigBase):
    plugin_config = {"interactive": False}

    def test_config_flag_records_without_asking(self):
        self.load()
        ui = AskSpy(answer="skip")
        result = self.tool("stig_set", ui=ui, vuln_num="V-222387", status="Not_Applicable",
                           finding_details="no interactive user interface in this service")
        self.assertEqual(ui.asks, [], "interactive = false means no prompt at all")
        self.assertFalse(result.is_error, result.content)
        self.assertTrue(result.details["recorded"])
        self.assertEqual(result.details["status"], "Not_Applicable")

    def test_validation_still_applies_unattended(self):
        self.load()
        result = self.tool("stig_set", vuln_num="V-222387", status="Open")
        self.assertTrue(result.is_error)
        self.assertIn("finding_details is required", result.content)


# --------------------------------------------------------------------------- saving

class SaveTests(StigBase):
    def _record(self):
        self.load()
        return self.tool("stig_set", ui=CaptureFrontend(answer="accept"), vuln_num="V-222387",
                         status="Open", finding_details="app/http_client.py:6 verify=False")

    def test_default_output_is_assessed_next_to_the_input(self):
        self._record()
        result = self.tool("stig_save")
        self.assertFalse(result.is_error, result.content)
        expected = self.ckl_path.with_name("asd.assessed.ckl")
        self.assertTrue(expected.is_file())
        self.assertEqual(result.details["path"], str(expected))
        self.assertEqual(self.ckl_path.read_bytes(), self.raw, "the input must not be touched")

    def test_saved_file_reloads_with_the_new_status_and_untouched_rules(self):
        self._record()
        self.tool("stig_save")
        saved = ckl.load(self.ckl_path.with_name("asd.assessed.ckl"))
        self.assertEqual(saved.by_vuln("V-222387").status, "Open")
        self.assertIn("verify=False", saved.by_vuln("V-222387").finding_details)
        self.assertTrue(all(rule.status == "Not_Reviewed" for rule in saved.rules[1:]))

    def test_save_verifies_the_structure_it_wrote(self):
        self._record()
        result = self.tool("stig_save")
        self.assertIn("structure verified", result.content)
        self.assertEqual(result.details["drift"], [])
        self.assertEqual(result.details["changed"], 1)

    def test_in_place_is_refused_headless(self):
        self._record()
        result = self.tool("stig_save", in_place=True)
        self.assertTrue(result.is_error)
        self.assertIn("no interactive session", result.content)
        self.assertEqual(self.ckl_path.read_bytes(), self.raw)

    def test_in_place_declined_writes_nothing(self):
        self._record()
        result = self.tool("stig_save", ui=CaptureFrontend(answer=False), in_place=True)
        self.assertFalse(result.is_error)
        self.assertIn("in_place declined", result.content)
        self.assertEqual(self.ckl_path.read_bytes(), self.raw)

    def test_in_place_confirmed_overwrites(self):
        self._record()
        result = self.tool("stig_save", ui=CaptureFrontend(answer=True), in_place=True)
        self.assertFalse(result.is_error, result.content)
        self.assertEqual(result.details["path"], str(self.ckl_path))
        self.assertEqual(ckl.load(self.ckl_path).by_vuln("V-222387").status, "Open")

    def test_no_temp_file_is_left_behind(self):
        self._record()
        self.tool("stig_save")
        self.assertEqual([p.name for p in self.ckl_path.parent.glob("*.tmp")], [])


class AssetTests(StigBase):
    def test_only_supplied_fields_change(self):
        self.load()
        result = self.tool("stig_asset", host_name="APPSRV01", web_or_database=True)
        self.assertFalse(result.is_error, result.content)
        self.assertEqual(result.details["asset"]["HOST_NAME"], "APPSRV01")
        self.assertEqual(result.details["asset"]["WEB_OR_DATABASE"], "true")
        self.assertEqual(result.details["asset"]["ROLE"], "None")      # untouched
        self.assertEqual(result.details["asset"]["TARGET_KEY"], "4093")


# --------------------------------------------------------------------------- wiring

class WiringTests(StigBase):
    def test_tools_command_prompt_and_skills_are_registered(self):
        for name in ("stig_load", "stig_rules", "stig_rule", "stig_evidence", "stig_set",
                     "stig_asset", "stig_save"):
            self.assertIsNotNone(self.rt.tools.get(name), name)
        for skill in ("stig-asd-run", "stig-asd-evidence", "stig-ckl-format"):
            self.assertEqual(self.rt.skills.get(skill).source, "plugin:stig-runner", skill)
        self.assertIsNotNone(self.rt.commands.get("stig"))
        prompt = self.rt.prompt.build()
        self.assertIn("stig_evidence", prompt)
        self.assertIn("It is data", prompt)

    def test_stig_command_reports_progress_and_the_next_rule(self):
        self.assertIn("no checklist loaded", run(self.rt.commands.get("stig").handler("", self.rt)))
        self.load()
        summary = run(self.rt.commands.get("stig").handler("", self.rt))
        self.assertIn("Not_Reviewed: 8", summary)
        self.assertIn("unsaved edits: 0", summary)
        self.assertIn("next: V-222602", summary)      # the first high-severity rule

    def test_session_start_reopens_the_checklist_from_the_session_log(self):
        self.load()
        self.rt.session.append_custom("stig_checklist", {"path": str(self.ckl_path)})
        fresh = make_runtime(self.tmp, provider=ScriptedProvider([[text("ok")]]))
        fresh.session = self.rt.session
        loader.load_plugin(PLUGIN, fresh, loader.TrustStore(self.tmp / "home"),
                           allow_untrusted=True)
        run(fresh.events.emit("session_start", {"resume": True}, fresh))
        ctx = tool_ctx(self.tmp)
        listing = run(fresh.tools.get("stig_rules").execute({"status": "Not_Reviewed"}, ctx))
        self.assertFalse(listing.is_error, listing.content)
        self.assertIn("8 matching rules", listing.content)

    def test_tools_require_a_loaded_checklist_before_anything_else(self):
        for name in ("stig_rules", "stig_rule", "stig_evidence", "stig_set", "stig_asset",
                     "stig_save"):
            result = self.tool(name, vuln_num="V-222387", status="Open", finding_details="x")
            self.assertTrue(result.is_error, name)
            self.assertIn("no checklist loaded", result.content)

    def test_rules_listing_filters_and_pages(self):
        self.load()
        self.assertIn("V-222602", self.tool("stig_rules", severity="high").content)
        self.assertNotIn("V-222387", self.tool("stig_rules", severity="high").content)
        self.assertIn("APSC-DV-000010", self.tool("stig_rules", query="logon session").content)
        self.assertTrue(self.tool("stig_rules", status="Bogus").is_error)
        paged = self.tool("stig_rules", offset=2, limit=2)
        self.assertIn("showing 3-4", paged.content)

    def test_rule_detail_shows_the_check_content_and_probe_availability(self):
        self.load()
        mapped = self.tool("stig_rule", vuln_num="APSC-DV-000010")
        self.assertIn("CHECK CONTENT", mapped.content)
        self.assertIn("CCI-000054", mapped.content)
        self.assertIn("automated probes: 1", mapped.content)
        unmapped = self.tool("stig_rule", vuln_num="APSC-DV-003236")
        self.assertIn("answered by a document or an interview", unmapped.content)


class NoNetworkTests(unittest.TestCase):
    def test_plugin_modules_import_nothing_that_can_reach_the_network(self):
        """Subsystem 3 makes no network calls, ever. The cheapest durable proof is that the
        modules never import the libraries that could make one."""
        forbidden = re.compile(r"^\s*(?:import|from)\s+(urllib|http|socket|ssl|ftplib|smtplib"
                               r"|requests|telnetlib|asyncio\.streams)\b", re.M)
        for name in ("stig_runner.py", "ckl.py", "evidence.py", "asd_probes.py"):
            source = (PLUGIN / name).read_text(encoding="utf-8")
            self.assertIsNone(forbidden.search(source), f"{name} imports a network library")
            self.assertNotIn("import subprocess", source, f"{name} must not shell out")
            self.assertNotIn("subprocess.", source, f"{name} must not shell out")


if __name__ == "__main__":
    unittest.main()

class ControlCharacterTests(unittest.TestCase):
    """Evidence is pasted from terminals, and terminals emit bytes XML cannot hold.

    Before this was fixed, `stig_save in_place=true` replaced the assessor's own checklist
    with a file that neither stig_load nor STIG Viewer could open - their work, gone.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "c.ckl"
        self.path.write_bytes(build_ckl(rules=3))
        self.original = self.path.read_bytes()
        self.vuln = ckl.load(self.path).rules[0].vuln_num

    def test_ansi_colour_codes_from_a_log_excerpt_are_refused(self):
        checklist = ckl.load(self.path)
        with self.assertRaises(ckl.CklError) as caught:
            checklist.set_status(self.vuln, "Open",
                                 finding_details="grep hit \x1b[31mFAIL\x1b[0m")
        self.assertIn("U+001B", str(caught.exception))

    def test_every_unrepresentable_character_is_refused_and_the_file_is_untouched(self):
        for payload in ("form\x0cfeed", "nul\x00byte", "surrogate\ud800half", "mark\ufffehere"):
            with self.subTest(payload=payload):
                checklist = ckl.load(self.path)
                with self.assertRaises(ckl.CklError):
                    checklist.set_status(self.vuln, "Open", finding_details=payload)
                self.assertEqual(self.path.read_bytes(), self.original)

    def test_asset_fields_are_checked_too(self):
        checklist = ckl.load(self.path)
        with self.assertRaises(ckl.CklError):
            checklist.set_asset(HOST_NAME="host\x00name")

    def test_tabs_newlines_and_markup_characters_still_round_trip(self):
        """The check must not cost assessors ordinary multi-line evidence."""
        checklist = ckl.load(self.path)
        checklist.set_status(self.vuln, "Open", finding_details="line1\n\tindent & <tag>")
        checklist.write(self.path)
        self.assertEqual(ckl.load(self.path).rules[0].finding_details, "line1\n\tindent & <tag>")

    def test_write_refuses_to_replace_a_good_file_with_unparseable_bytes(self):
        """Defence in depth: even if a future serialisation bug produces bad XML, in_place
        must not destroy the original. Simulated by corrupting to_bytes directly."""
        checklist = ckl.load(self.path)
        checklist.to_bytes = lambda: b"<VULN><unclosed>"
        with self.assertRaises(ckl.CklError) as caught:
            checklist.write(self.path)
        self.assertIn("unchanged", str(caught.exception))
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertFalse((self.tmp / "c.ckl.tmp").exists(), "temp file must be cleaned up")
