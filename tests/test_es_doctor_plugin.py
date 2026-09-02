"""es-doctor plugin: log digging, metric queries, and log<->metric<->APM correlation
against the fake Elasticsearch incident (errors + CPU + latency spike at 10:15-10:20)."""
import tempfile, unittest
from pathlib import Path
from helpers import CaptureFrontend, ScriptedProvider, call, make_runtime, run, text, tool_ctx, ROOT
from picoagent.core.loop import AgentLoop
from picoagent.plugins import loader
from picoagent.testing.fake_es import FakeES

PLUGIN = ROOT / "examples/plugins/es-doctor"
WINDOW = {"since": "2026-09-02T10:00:00Z", "until": "2026-09-02T10:30:00Z"}


class EsDoctorBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.es = FakeES().start()

    @classmethod
    def tearDownClass(cls):
        cls.es.stop()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.es.requests.clear()
        self.rt = make_runtime(self.tmp, provider=ScriptedProvider([[text("ok")]]))
        self.rt.cfg["plugins"]["es-doctor"] = {"url": self.es.url, "api_key": "abc123"}
        loader.load_plugin(PLUGIN, self.rt, loader.TrustStore(self.tmp / "home"), allow_untrusted=True)

    def tool(self, name, **args):
        return run(self.rt.tools.get(name).execute(args, tool_ctx(self.tmp)))


class RegistrationTests(EsDoctorBase):
    def test_registers_tools_skills_command_and_prompt_section(self):
        for name in ("es_cluster_health", "es_indices", "es_logs", "es_metrics", "es_correlate", "es_search", "es_request",
                     "es_shards", "es_recovery", "es_nodes", "es_hot_threads", "es_ilm", "es_snapshots",
                     "es_index_inspect", "es_templates", "es_slowlog"):
            self.assertIsNotNone(self.rt.tools.get(name), name)
        for skill in ("es-triage", "es-log-dig", "es-correlate", "es-unassigned-shards", "es-slow-cluster",
                      "es-node-pressure", "es-ilm-and-retention", "es-snapshot-and-restore", "es-mappings-and-templates"):
            self.assertEqual(self.rt.skills.get(skill).source, "plugin:es-doctor")
        self.assertIsNotNone(self.rt.commands.get("es"))
        prompt = self.rt.prompt.build()
        self.assertIn("ECS", prompt); self.assertIn("metricbeat", prompt.lower()); self.assertIn("logs-*", prompt)

    def test_api_key_header_is_sent(self):
        self.tool("es_cluster_health")
        self.assertEqual(self.es.requests[0]["headers"]["Authorization"], "ApiKey abc123")


class ClusterTests(EsDoctorBase):
    def test_health_summarises_status_and_unassigned_shards(self):
        r = self.tool("es_cluster_health")
        self.assertFalse(r.is_error); self.assertIn("yellow", r.content); self.assertIn("unassigned: 2", r.content)
        self.assertIn("disk_threshold", r.content)          # allocation explain is pulled in when not green

    def test_indices_groups_data_streams_by_signal(self):
        r = self.tool("es_indices")
        self.assertIn("logs-checkout-default", r.content); self.assertIn("metrics-system.cpu-default", r.content)
        self.assertIn("traces", r.content.lower())


class LogDigTests(EsDoctorBase):
    def test_logs_default_to_error_level_and_ecs_index_patterns(self):
        r = self.tool("es_logs", **WINDOW, service="checkout")
        req = self.es.requests[-1]
        self.assertTrue(req["path"].startswith("/logs-*,filebeat-*"), req["path"])
        self.assertIn("pool exhausted", r.content); self.assertNotIn("order placed", r.content)
        self.assertIn("checkout.app", r.content)               # dataset breakdown

    def test_logs_free_text_and_level_filters(self):
        r = self.tool("es_logs", **WINDOW, query="upstream timed out", level="error")
        self.assertIn("nginx.error", r.content); self.assertNotIn("pool exhausted", r.content)

    def test_logs_since_relative_duration_is_accepted(self):
        r = self.tool("es_logs", since="15m", level="info", size=3)
        self.assertFalse(r.is_error)
        rng = self.es.requests[-1]["body"]["query"]["bool"]["filter"][0]["range"]["@timestamp"]
        self.assertEqual(rng["gte"], "now-15m")

    def test_logs_histogram_shows_when_errors_started(self):
        r = self.tool("es_logs", **WINDOW, level="error", histogram=True, interval="5m")
        self.assertIn("10:15", r.content); self.assertNotIn("10:05 ", r.content.split("Timeline")[1] if "Timeline" in r.content else "")


class MetricTests(EsDoctorBase):
    def test_metrics_buckets_average_of_a_field(self):
        r = self.tool("es_metrics", **WINDOW, field="system.cpu.total.norm.pct", host="web-01", interval="5m")
        self.assertTrue(self.es.requests[-1]["path"].startswith("/metrics-*,metricbeat-*"))
        lines = [l for l in r.content.splitlines() if l.startswith("2026")]
        self.assertEqual(len(lines), 6)
        self.assertIn("0.9", [l for l in lines if "10:15" in l][0])

    def test_metric_aliases_expand_to_ecs_fields(self):
        self.tool("es_metrics", **WINDOW, field="cpu")
        body = self.es.requests[-1]["body"]
        self.assertEqual(body["aggs"]["over_time"]["aggs"]["value"]["avg"]["field"], "system.cpu.total.norm.pct")


class CorrelationTests(EsDoctorBase):
    def test_correlate_aligns_errors_metrics_and_apm_and_finds_the_spike(self):
        r = self.tool("es_correlate", **WINDOW, host="web-01", service="checkout", interval="1m")
        self.assertFalse(r.is_error)
        self.assertIn("10:15", r.content); self.assertIn("10:19", r.content)
        self.assertIn("cpu", r.content); self.assertIn("p50", r.content.lower() + "latency")
        self.assertRegex(r.content, r"system\.cpu\.total\.norm\.pct\s+r=0\.9\d")   # strong positive correlation
        self.assertIn("  cpu  ", r.content)                                    # alias used as column header
        self.assertIn("spike", r.content.lower())
        self.assertIn("pool exhausted", r.content)                          # top error message during spike

    def test_correlate_reports_uncorrelated_metric_honestly(self):
        r = self.tool("es_correlate", **WINDOW, host="web-01", metrics=["memory"], include_apm=False)
        self.assertRegex(r.content, r"system\.memory\.actual\.used\.pct\s+r=")
        self.assertNotIn("transaction", r.content.lower())


class GuardTests(EsDoctorBase):
    def test_destructive_requests_are_blocked_by_default(self):
        rt = make_runtime(self.tmp, provider=ScriptedProvider([[call("es_request", method="DELETE", path="/logs-*")], [text("ok")]]))
        rt.cfg["plugins"]["es-doctor"] = {"url": self.es.url}
        loader.load_plugin(PLUGIN, rt, loader.TrustStore(self.tmp / "home"), allow_untrusted=True)
        run(AgentLoop(rt).run("clean up"))
        result = rt.frontend.tool_results()[0]
        self.assertTrue(result.is_error); self.assertIn("destructive", result.content)
        self.assertFalse(any(r["method"] == "DELETE" for r in self.es.requests))

    def test_raw_search_passthrough_works(self):
        r = self.tool("es_search", index="traces-apm*", body={"size": 1, "query": {"term": {"event.outcome": "failure"}}})
        self.assertIn("POST /checkout", r.content)


if __name__ == "__main__":
    unittest.main()
