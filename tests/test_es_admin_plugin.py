"""es-doctor's cluster-administration tools against the canned cluster in
``picoagent/testing/fake_es.py``: unassigned shards, node pressure, ILM errors, snapshots,
templates and the narrow slow-log write path.

Every assertion here is about what the *model* sees or what the tool *sent*, because those
are the two things a wrong answer in production comes from: a mis-read response, or a
request that asked the cluster for the wrong thing.
"""
import tempfile, unittest
from pathlib import Path
from helpers import CaptureFrontend, ScriptedProvider, call, make_runtime, run, text, tool_ctx, ROOT
from picoagent.core.loop import AgentLoop
from picoagent.plugins import loader
from picoagent.testing.fake_es import FakeES, build_cluster

PLUGIN = ROOT / "examples/plugins/es-doctor"
ADMIN_TOOLS = ("es_shards", "es_recovery", "es_nodes", "es_hot_threads", "es_ilm",
               "es_snapshots", "es_index_inspect", "es_templates", "es_slowlog")


class EsAdminBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.es = FakeES().start()

    @classmethod
    def tearDownClass(cls):
        cls.es.stop()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.es.cluster = build_cluster()        # tests mutate it (PUT _settings, serverless 404s)
        self.es.requests.clear()
        self.rt = make_runtime(self.tmp, provider=ScriptedProvider([[text("ok")]]))
        self.rt.cfg["plugins"]["es-doctor"] = {"url": self.es.url}
        loader.load_plugin(PLUGIN, self.rt, loader.TrustStore(self.tmp / "home"), allow_untrusted=True)

    def tool(self, name, _ui=None, _ctx=None, **args):
        ctx = _ctx or tool_ctx(self.tmp)
        ctx.ui = _ui
        return run(self.rt.tools.get(name).execute(args, ctx))

    def paths(self):
        return [r["path"] for r in self.es.requests]

    def last_path(self, needle):
        matching = [p for p in self.paths() if needle in p]
        self.assertTrue(matching, f"no request containing {needle!r} in {self.paths()}")
        return matching[-1]


class RegistrationTests(EsAdminBase):
    def test_all_admin_tools_are_registered(self):
        for name in ADMIN_TOOLS:
            self.assertIsNotNone(self.rt.tools.get(name), name)

    def test_admin_skills_are_registered(self):
        for skill in ("es-unassigned-shards", "es-slow-cluster", "es-node-pressure",
                      "es-ilm-and-retention", "es-snapshot-and-restore", "es-mappings-and-templates"):
            self.assertEqual(self.rt.skills.get(skill).source, "plugin:es-doctor")

    def test_prompt_section_describes_the_admin_workflow(self):
        prompt = self.rt.prompt.build()
        self.assertIn("es_shards", prompt)
        self.assertIn("ECS", prompt)                                  # the original note is still there
        self.assertIn("allocation", prompt.lower())


class ShardTests(EsAdminBase):
    def test_unassigned_shards_come_first_with_reasons_and_per_node_counts(self):
        r = self.tool("es_shards")
        self.assertFalse(r.is_error)
        body = r.content
        first_row = [ln for ln in body.splitlines() if "logs-" in ln or "metrics-" in ln][0]
        self.assertIn("UNASSIGNED", first_row)
        self.assertIn("INDEX_CREATED", body)
        self.assertIn("NODE_LEFT", body)
        self.assertIn("node-2=3", body)                               # per-node shard counts
        self.assertIn("node-1=1", body)
        self.assertIn("bytes=b", self.last_path("/_cat/shards"))
        self.assertIn("unassigned.reason", self.last_path("/_cat/shards"))

    def test_state_filter_narrows_the_table(self):
        r = self.tool("es_shards", state="UNASSIGNED")
        self.assertNotIn("traces-apm-default", r.content)
        self.assertIn("logs-nginx.error-default", r.content)

    def test_explain_with_an_index_sends_a_body_and_renders_the_deciders(self):
        r = self.tool("es_shards", index="logs-nginx.error-default", explain=True)
        explains = [req for req in self.es.requests if req["path"].startswith("/_cluster/allocation/explain")]
        self.assertEqual(len(explains), 1)
        self.assertEqual(explains[0]["method"], "POST")
        self.assertEqual(explains[0]["body"]["index"], "logs-nginx.error-default")
        self.assertEqual(explains[0]["body"]["shard"], 0)
        self.assertIs(explains[0]["body"]["primary"], False)
        self.assertIn("include_disk_info=true", explains[0]["path"])
        self.assertIn("[disk_threshold] NO", r.content)
        self.assertIn("high watermark", r.content)

    def test_explain_without_an_index_sends_no_body(self):
        self.tool("es_shards", explain=True)
        explains = [req for req in self.es.requests if req["path"].startswith("/_cluster/allocation/explain")]
        self.assertEqual(explains[0]["body"], {})

    def test_explain_error_saying_nothing_is_unassigned_is_not_a_tool_error(self):
        self.es.cluster["allocation_explain"] = {}                    # the 400 a healthy cluster gives
        r = self.tool("es_shards", index="logs-checkout-default", explain=True)
        self.assertFalse(r.is_error)
        self.assertIn("unassigned", r.content.lower())


class RecoveryTests(EsAdminBase):
    def test_active_only_defaults_to_true_and_shows_progress(self):
        r = self.tool("es_recovery")
        path = self.last_path("/_cat/recovery")
        self.assertIn("active_only=true", path)
        self.assertIn("h=index,shard,time,type,stage", path)
        self.assertIn("40.0%", r.content)
        self.assertIn("peer", r.content)
        self.assertNotIn("logs-checkout-default", r.content)          # the finished one is filtered out

    def test_active_only_false_includes_finished_recoveries_grouped_by_type(self):
        r = self.tool("es_recovery", active_only=False)
        self.assertIn("active_only=false", self.last_path("/_cat/recovery"))
        self.assertIn("snapshot", r.content)
        self.assertIn("peer", r.content)

    def test_no_recoveries_is_a_plain_answer_not_an_error(self):
        self.es.cluster["recovery"] = []
        r = self.tool("es_recovery")
        self.assertFalse(r.is_error)
        self.assertIn("no", r.content.lower())


class NodeTests(EsAdminBase):
    def test_summary_flags_heap_disk_and_tripped_breakers(self):
        r = self.tool("es_nodes")
        self.assertIn("node-1", r.content)
        self.assertIn("node-2", r.content)
        warnings = r.content.lower()
        self.assertIn("heap", warnings)
        self.assertIn("82", r.content)
        self.assertIn("91.30", r.content)
        self.assertIn("breaker", warnings)
        self.assertIn("parent", r.content)

    def test_healthy_node_is_not_flagged(self):
        warnings = self.tool("es_nodes").content.split("Warnings")[-1]
        self.assertNotIn("node-3", warnings)

    def test_thread_pools_put_the_rejecting_pool_first(self):
        r = self.tool("es_nodes", view="thread_pools")
        rows = [ln for ln in r.content.splitlines() if "node-" in ln]
        self.assertIn("search", rows[0])
        self.assertIn("17", rows[0])
        self.assertIn("thread_pool", self.last_path("/_cat/thread_pool"))

    def test_breakers_view_lists_limits_and_trips(self):
        r = self.tool("es_nodes", view="breakers")
        self.assertIn("fielddata", r.content)
        self.assertIn("tripped", r.content)

    def test_tasks_view_shows_the_long_search_and_the_stale_pending_task(self):
        r = self.tool("es_nodes", view="tasks")
        self.assertIn("indices:data/read/search", r.content)
        self.assertIn("400", r.content)                               # 400 s of running time
        self.assertIn("shard-started", r.content)
        self.assertIn("45.2s", r.content)

    def test_node_filter_reaches_the_url(self):
        self.tool("es_nodes", node="node-2")
        self.assertIn("node-2", self.last_path("/_nodes"))


class HotThreadTests(EsAdminBase):
    def test_plain_text_is_passed_through_with_the_sampling_parameters(self):
        r = self.tool("es_hot_threads", node="node-2", threads=5, interval="1s", type="cpu")
        self.assertFalse(r.is_error)
        self.assertIn("cpu usage by thread", r.content)
        path = self.last_path("hot_threads")
        self.assertIn("threads=5", path)
        self.assertIn("interval=1s", path)
        self.assertIn("type=cpu", path)
        self.assertIn("/node-2/", path)

    def test_long_output_is_truncated_with_a_marker(self):
        r = self.tool("es_hot_threads", _ctx=tool_ctx(self.tmp, tool_output_max_lines=6))
        self.assertIn("[truncated]", r.content)
        self.assertIn("node-1", r.content)                            # head is kept, so the first node shows


class IlmTests(EsAdminBase):
    def test_errors_first_with_the_failing_step_and_reason(self):
        r = self.tool("es_ilm")
        self.assertIn("RUNNING", r.content)
        rows = [ln for ln in r.content.splitlines() if "logs-" in ln and "/" in ln]
        self.assertIn("logs-nginx.error-default", rows[0])
        self.assertIn("check-rollover-ready", r.content)
        self.assertIn("rollover_alias", r.content)
        self.assertIn("unmanaged", r.content.lower())

    def test_only_errors_reaches_the_url(self):
        self.tool("es_ilm", only_errors=True)
        self.assertIn("only_errors=true", self.last_path("_ilm/explain"))

    def test_policy_definition_is_summarised_when_asked_for(self):
        r = self.tool("es_ilm", policy="logs")
        self.assertIn("rollover", r.content)
        self.assertIn("max_age=30d", r.content)
        self.assertIn("delete", r.content)
        self.assertIn("90d", r.content)

    def test_unknown_policy_is_an_error_result_not_an_exception(self):
        r = self.tool("es_ilm", policy="does-not-exist")
        self.assertTrue(r.is_error)


class SnapshotTests(EsAdminBase):
    def test_repositories_snapshots_and_slm_are_reported(self):
        r = self.tool("es_snapshots", repository="backups")
        self.assertIn("backups", r.content)
        self.assertIn("fs", r.content)
        self.assertIn("daily-1", r.content)
        self.assertIn("PARTIAL", r.content)
        self.assertIn("nightly", r.content)                           # SLM policy
        self.assertIn("daily-2", r.content)                           # its last failure

    def test_status_is_only_requested_for_a_running_snapshot(self):
        self.tool("es_snapshots", repository="backups")
        status_paths = [p for p in self.paths() if p.endswith("/_status") or "/_status?" in p]
        self.assertEqual(len(status_paths), 1, status_paths)
        self.assertIn("daily-3", status_paths[0])
        self.assertNotIn("daily-1", " ".join(status_paths))

    def test_listing_asks_for_a_sorted_bounded_page(self):
        self.tool("es_snapshots", repository="backups", limit=2)
        path = self.last_path("/_snapshot/backups/*")
        self.assertIn("sort=start_time", path)
        self.assertIn("order=desc", path)
        self.assertIn("size=2", path)

    def test_verify_needs_a_confirmation(self):
        self.tool("es_snapshots", repository="backups", verify=True, _ui=CaptureFrontend(answer=True))
        self.assertTrue([p for p in self.paths() if p.endswith("/_verify")])

    def test_verify_is_skipped_when_the_user_declines(self):
        r = self.tool("es_snapshots", repository="backups", verify=True, _ui=CaptureFrontend(answer=False))
        self.assertFalse([p for p in self.paths() if p.endswith("/_verify")])
        self.assertFalse(r.is_error)
        self.assertIn("verif", r.content.lower())

    def test_verify_is_skipped_headless(self):
        self.tool("es_snapshots", repository="backups", verify=True)
        self.assertFalse([p for p in self.paths() if p.endswith("/_verify")])

    def test_one_snapshot_detail(self):
        r = self.tool("es_snapshots", repository="backups", snapshot="daily-2")
        self.assertIn("PARTIAL", r.content)
        self.assertIn("NoSuchFileException", r.content)


class IndexInspectTests(EsAdminBase):
    def test_mapping_explosion_is_warned_about(self):
        r = self.tool("es_index_inspect", index="logs-checkout-default", view="mappings")
        self.assertIn("41", r.content)
        self.assertIn("50", r.content)
        self.assertIn("keyword", r.content)
        self.assertIn("80%", r.content)

    def test_settings_view_keeps_the_operational_keys_and_drops_the_noise(self):
        r = self.tool("es_index_inspect", index="logs-checkout-default", view="settings")
        self.assertIn("index.refresh_interval", r.content)
        self.assertIn("index.lifecycle.name", r.content)
        self.assertIn("index.blocks.read_only_allow_delete", r.content)
        self.assertNotIn("index.uuid", r.content)
        self.assertNotIn("index.creation_date", r.content)

    def test_stats_view_reports_segments_and_cache_hit_ratio(self):
        r = self.tool("es_index_inspect", index="logs-checkout-default", view="stats")
        self.assertIn("210", r.content)                               # primary segment count
        self.assertIn("segments", r.content.lower())
        self.assertIn("deleted", r.content.lower())

    def test_missing_index_is_an_error_result(self):
        r = self.tool("es_index_inspect", index="not-there")
        self.assertTrue(r.is_error)


class TemplateTests(EsAdminBase):
    def test_all_three_kinds_are_listed(self):
        r = self.tool("es_templates")
        self.assertIn("logs-checkout", r.content)
        self.assertIn("ecs-base", r.content)
        self.assertIn("filebeat-7", r.content)
        self.assertIn("200", r.content)                               # priority

    def test_simulate_index_reports_the_winner_and_the_overlaps(self):
        r = self.tool("es_templates", simulate_index="logs-checkout-2026.09.02")
        simulate = [req for req in self.es.requests if "_simulate_index" in req["path"]]
        self.assertEqual(simulate[0]["method"], "POST")
        self.assertIn("filebeat-7", r.content)
        self.assertIn("overlap", r.content.lower())
        self.assertIn("logs", r.content)                              # the effective ILM policy

    def test_data_streams_without_a_template_are_called_out(self):
        r = self.tool("es_templates")
        self.assertIn("metrics-system.cpu-default", r.content)


class SlowlogTests(EsAdminBase):
    def test_show_reports_unset_thresholds_and_that_slow_logs_are_files(self):
        r = self.tool("es_slowlog", index="logs-checkout-default")
        self.assertFalse(r.is_error)
        self.assertIn("index.search.slowlog.threshold.query.warn", r.content)
        self.assertIn("no slow-log events found", r.content)
        self.assertIn("files on the nodes", r.content)

    def test_enable_is_refused_without_a_human(self):
        r = self.tool("es_slowlog", index="logs-checkout-default", action="enable", query_warn="2s")
        self.assertTrue(r.is_error)
        self.assertFalse([req for req in self.es.requests if req["method"] == "PUT"])

    def test_enable_puts_exactly_the_three_keys_and_round_trips(self):
        self.tool("es_slowlog", index="logs-checkout-default", action="enable", query_warn="2s",
                  fetch_warn="1s", index_warn="5s", _ui=CaptureFrontend(answer=True))
        puts = [req for req in self.es.requests if req["method"] == "PUT"]
        self.assertEqual(len(puts), 1)
        self.assertEqual(puts[0]["body"], {"index.search.slowlog.threshold.query.warn": "2s",
                                           "index.search.slowlog.threshold.fetch.warn": "1s",
                                           "index.indexing.slowlog.threshold.index.warn": "5s"})
        shown = self.tool("es_slowlog", index="logs-checkout-default")
        self.assertIn("2s", shown.content)
        self.assertIn("5s", shown.content)

    def test_disable_puts_nulls(self):
        self.tool("es_slowlog", index="logs-checkout-default", action="disable",
                  _ui=CaptureFrontend(answer=True))
        put = [req for req in self.es.requests if req["method"] == "PUT"][0]
        self.assertEqual(set(put["body"].values()), {None})
        self.assertEqual(len(put["body"]), 3)

    def test_declining_the_confirmation_writes_nothing(self):
        r = self.tool("es_slowlog", index="logs-checkout-default", action="enable", query_warn="2s",
                      _ui=CaptureFrontend(answer=False))
        self.assertFalse([req for req in self.es.requests if req["method"] == "PUT"])
        self.assertFalse(r.is_error)

    def test_allow_destructive_lets_it_run_headless(self):
        rt = make_runtime(Path(tempfile.mkdtemp()), provider=ScriptedProvider([[text("ok")]]))
        rt.cfg["plugins"]["es-doctor"] = {"url": self.es.url, "allow_destructive": True}
        loader.load_plugin(PLUGIN, rt, loader.TrustStore(self.tmp / "home"), allow_untrusted=True)
        r = run(rt.tools.get("es_slowlog").execute(
            {"index": "logs-checkout-default", "action": "enable", "query_warn": "3s"}, tool_ctx(self.tmp)))
        self.assertFalse(r.is_error)
        self.assertTrue([req for req in self.es.requests if req["method"] == "PUT"])


class GuardTests(EsAdminBase):
    def test_destructive_es_request_is_still_blocked(self):
        rt = make_runtime(self.tmp, provider=ScriptedProvider(
            [[call("es_request", method="PUT", path="/logs-checkout-default/_settings")], [text("ok")]]))
        rt.cfg["plugins"]["es-doctor"] = {"url": self.es.url}
        loader.load_plugin(PLUGIN, rt, loader.TrustStore(self.tmp / "home"), allow_untrusted=True)
        run(AgentLoop(rt).run("change it"))
        blocked = rt.frontend.tool_results()[0]
        self.assertTrue(blocked.is_error)
        self.assertIn("destructive", blocked.content)

    def test_the_guard_does_not_block_es_slowlog(self):
        rt = make_runtime(self.tmp, provider=ScriptedProvider(
            [[call("es_slowlog", index="logs-checkout-default")], [text("ok")]]))
        rt.cfg["plugins"]["es-doctor"] = {"url": self.es.url}
        loader.load_plugin(PLUGIN, rt, loader.TrustStore(self.tmp / "home"), allow_untrusted=True)
        run(AgentLoop(rt).run("show me the slow log settings"))
        self.assertNotIn("destructive", rt.frontend.tool_results()[0].content)


class ServerlessTests(EsAdminBase):
    """Elastic Cloud Serverless answers 400/404 for most of these endpoints. The tool has to
    hand that back as an error result, not blow up."""

    def test_a_404_becomes_an_error_result(self):
        self.es.cluster["ilm_policies"] = {}
        r = self.tool("es_ilm", policy="logs")
        self.assertTrue(r.is_error)
        self.assertIn("404", r.content)

    def test_missing_slm_does_not_sink_the_snapshot_report(self):
        self.es.cluster["slm_policies"] = {}
        r = self.tool("es_snapshots", repository="backups")
        self.assertFalse(r.is_error)
        self.assertIn("daily-1", r.content)


if __name__ == "__main__":
    unittest.main()


class AllocationExplainWordingTests(unittest.TestCase):
    """The 400 a healthy cluster returns changed wording in ES 7.16.

    The plugin matched "unassigned shard", which only appears in the <=7.15 text, and the fake
    served that same old text - so the tests agreed with each other and with no cluster anyone
    still runs. Against a real 7.16+ healthy cluster, es_shards explain=true reported an HTTP
    400 error instead of "nothing is unassigned".
    """

    import sys as _sys
    _sys.path.insert(0, str(ROOT / "examples/plugins/es-doctor"))
    import es_admin as _es_admin

    def test_both_the_old_and_current_wording_read_as_healthy(self):
        for label, message in [
                ("<=7.15", "unable to find any unassigned shards to explain [Request[x=true]]"),
                ("7.16+", "unable to find any shards to explain [Request[x=true]] in the routing table")]:
            with self.subTest(version=label):
                self.assertTrue(self._es_admin._is_nothing_to_explain(message))

    def test_an_unrelated_400_is_still_a_real_error(self):
        self.assertFalse(self._es_admin._is_nothing_to_explain("index_not_found_exception"))
        self.assertFalse(self._es_admin._is_nothing_to_explain("illegal_argument_exception: bad shard"))

    def test_the_fake_serves_the_current_wording(self):
        """Otherwise the fake silently re-pins the bug it was meant to catch."""
        import inspect
        from picoagent.testing import fake_es
        source = inspect.getsource(fake_es)
        self.assertIn("unable to find any shards to explain", source)
        self.assertNotIn("unable to find any unassigned shards to explain", source)

    def test_the_circuit_breaker_key_matches_elasticsearch(self):
        """ES calls it inflight_requests; the fake invented in_flight_requests."""
        breakers = build_cluster()["node_stats"]
        found = set()
        for node in breakers.get("nodes", {}).values():
            found |= set(node.get("breakers", {}))
        if found:
            self.assertIn("inflight_requests", found)
            self.assertNotIn("in_flight_requests", found)
