"""mcp plugin: the stdio handshake, tool discovery, namespacing, and failure as a value.

Every test spawns the real fake server in ``picoagent/testing/fake_mcp.py`` as a child process,
so the JSON-RPC framing, the pipe and the process lifecycle are all under test - the parts a
transport double would replace are exactly the parts that break.
"""
import sys, tempfile, time, unittest
from pathlib import Path
from helpers import ScriptedProvider, make_runtime, run, text, tool_ctx, ROOT
from picoagent.core.tools import ReadTool
from picoagent.plugins import loader

PLUGIN = ROOT / "examples/plugins/mcp"
sys.path.insert(0, str(PLUGIN))         # the translation tests call the plugin's functions directly
import mcp_client                       # noqa: E402


def server(mode="serve", **overrides):
    """A ``[plugins.mcp.servers.<name>]`` table pointing at the fake server.

    ``PYTHONPATH`` because the plugin runs the child in the project directory, which is a temp
    dir here and knows nothing about the checkout.
    """
    return {"command": sys.executable, "args": ["-m", "picoagent.testing.fake_mcp", "--mode", mode],
            "env": {"PYTHONPATH": str(ROOT)}, **overrides}


class McpBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def load(self, servers, **plugin_config):
        rt = make_runtime(self.tmp, provider=ScriptedProvider([[text("ok")]]))
        rt.cfg["plugins"]["mcp"] = {"servers": servers, **plugin_config}
        loader.load_plugin(PLUGIN, rt, loader.TrustStore(self.tmp / "home"), allow_untrusted=True)
        self.addCleanup(lambda: run(rt.events.emit("session_end", {}, rt)))
        return rt

    def call(self, rt, name, **args):
        return run(rt.tools.get(name).execute(args, tool_ctx(self.tmp)))

    def status(self, rt):
        return run(rt.commands.get("mcp").handler("", rt))


class DiscoveryTests(McpBase):
    def test_handshake_and_tools_list_register_the_advertised_tools(self):
        rt = self.load({"demo": server()})
        for name in ("demo_echo", "demo_read", "demo_slow", "demo_boom", "demo_fail"):
            self.assertIsNotNone(rt.tools.get(name), name)
        report = self.status(rt)
        self.assertIn("fake-mcp 0.1.0", report)
        self.assertIn("protocol 2024-11-05", report)

    def test_registered_schema_is_what_the_server_advertised(self):
        rt = self.load({"demo": server()})
        self.assertEqual(rt.tools.get("demo_echo").parameters,
                         {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]})
        self.assertEqual(rt.tools.get("demo_echo").description, "Return the text you send.")

    def test_schema_no_provider_can_call_is_refused_with_a_reason(self):
        rt = self.load({"demo": server()})
        self.assertIsNone(rt.tools.get("demo_scalar"))
        self.assertIn("skipped scalar: inputSchema is not an object schema", self.status(rt))

    def test_prompt_section_tells_the_model_where_the_tools_came_from(self):
        rt = self.load({"demo": server()})
        self.assertIn("<server>_<tool>", rt.prompt.build())
        self.assertIn("demo (5)", rt.prompt.build())

    def test_two_servers_are_namespaced_apart(self):
        rt = self.load({"one": server(), "two": server()})
        self.assertIsNotNone(rt.tools.get("one_echo"))
        self.assertIsNotNone(rt.tools.get("two_echo"))
        self.assertEqual(self.call(rt, "two_echo", text="hello from two").content, "hello from two")


class CallTests(McpBase):
    def test_calling_a_tool_returns_the_servers_result(self):
        rt = self.load({"demo": server()})
        result = self.call(rt, "demo_echo", text="round trip")
        self.assertFalse(result.is_error)
        self.assertEqual(result.content, "round trip")
        self.assertEqual(result.details, {"server": "demo", "tool": "echo"})

    def test_is_error_content_comes_back_as_an_error_result(self):
        rt = self.load({"demo": server()})
        result = self.call(rt, "demo_fail")
        self.assertTrue(result.is_error)
        self.assertIn("the tool ran and failed", result.content)


class NamespacingTests(McpBase):
    def test_a_server_tool_named_read_does_not_replace_the_builtin(self):
        (self.tmp / "note.txt").write_text("from the real filesystem\n")
        rt = self.load({"demo": server()})

        self.assertIsInstance(rt.tools.get("read"), ReadTool)
        self.assertIn("from the real filesystem", self.call(rt, "read", path="note.txt").content)
        self.assertIn("fake-mcp note at /elsewhere", self.call(rt, "demo_read", path="/elsewhere").content)

    def test_a_name_already_taken_is_refused_rather_than_overridden(self):
        rt = self.load({"demo": server()})
        first = rt.tools.get("demo_echo").server
        # A second server offering the same names under the same prefix (here, the same plugin
        # loaded twice) must not take `demo_echo` away from the connection that has it.
        loader.load_plugin(PLUGIN, rt, loader.TrustStore(self.tmp / "home"), allow_untrusted=True)
        self.assertIn("is already registered", self.status(rt))
        self.assertIs(rt.tools.get("demo_echo").server, first)


class FailureTests(McpBase):
    def test_a_server_that_exits_immediately_registers_nothing_and_does_not_raise(self):
        rt = self.load({"broken": server("exit-immediately")})
        self.assertEqual([name for name in rt.tools.names() if name.startswith("broken_")], [])
        report = self.status(rt)
        self.assertIn("broken: not connected", report)
        self.assertIn("refusing to start", report)          # the child's stderr, not a stack trace

    def test_a_call_to_a_server_that_has_exited_is_an_error_result(self):
        rt = self.load({"gone": server("exit-after-list")})
        self.assertIsNotNone(rt.tools.get("gone_echo"))     # discovery happened before it died
        result = self.call(rt, "gone_echo", text="anyone there")
        self.assertTrue(result.is_error)
        self.assertIn("MCP server 'gone'", result.content)
        self.assertIn("exited", result.content)

    def test_a_jsonrpc_error_response_is_an_error_result(self):
        rt = self.load({"demo": server()})
        result = self.call(rt, "demo_boom")
        self.assertTrue(result.is_error)
        self.assertIn("MCP server 'demo'", result.content)
        self.assertIn("-32602", result.content)
        self.assertIn("the server refused this call", result.content)

    def test_a_call_that_outlives_its_timeout_returns_instead_of_hanging(self):
        rt = self.load({"slowpoke": server(timeout=0.3)})
        started = time.monotonic()
        result = self.call(rt, "slowpoke_slow", seconds=30)
        self.assertLess(time.monotonic() - started, 10)     # the 30s sleep is never waited out
        self.assertTrue(result.is_error)
        self.assertIn("MCP server 'slowpoke': no response to tools/call within 0.3s", result.content)

    def test_a_command_that_is_not_installed_is_reported_not_raised(self):
        rt = self.load({"absent": {"command": "picoagent-no-such-mcp-server-xyz"}})
        self.assertIn("absent: not connected", self.status(rt))
        self.assertIn("cannot run", self.status(rt))


class TranslationTests(unittest.TestCase):
    """The pure half: schema translation, name sanitising, and content blocks that are not text."""

    def test_a_missing_schema_becomes_a_tool_with_no_arguments(self):
        self.assertEqual(mcp_client.to_parameters(None), {"type": "object", "properties": {}})

    def test_a_schema_without_a_type_is_read_as_an_object(self):
        schema = {"properties": {"path": {"type": "string"}}}
        self.assertEqual(mcp_client.to_parameters(schema),
                         {"type": "object", "properties": {"path": {"type": "string"}}})

    def test_a_non_object_schema_is_refused(self):
        self.assertIsNone(mcp_client.to_parameters({"type": "array", "items": {"type": "string"}}))
        self.assertIsNone(mcp_client.to_parameters("path"))

    def test_a_required_list_of_the_wrong_shape_is_dropped_not_forwarded(self):
        translated = mcp_client.to_parameters({"type": "object", "properties": {}, "required": "path"})
        self.assertNotIn("required", translated)

    def test_names_are_reduced_to_what_a_function_calling_api_accepts(self):
        self.assertEqual(mcp_client.tool_name("notes", "search.files"), "notes_search_files")
        self.assertEqual(len(mcp_client.tool_name("n" * 40, "t" * 40)), 64)

    def test_blocks_that_cannot_be_text_are_described_rather_than_dropped(self):
        rendered = mcp_client.render_block({"type": "image", "mimeType": "image/png", "data": "iVBOR"})
        self.assertIn("image/png", rendered)
        resource = {"type": "resource", "resource": {"uri": "file:///notes/a.md", "text": "hello"}}
        self.assertEqual(mcp_client.render_block(resource), "[resource file:///notes/a.md]\nhello")
        self.assertIn("unsupported", mcp_client.render_block({"type": "hologram"}))

    def test_a_structured_only_result_is_not_reported_as_empty(self):
        text_out, is_error = mcp_client.render_result({"content": [], "structuredContent": {"count": 2}})
        self.assertIn('"count": 2', text_out)
        self.assertFalse(is_error)


class LifecycleTests(McpBase):
    def test_session_end_stops_the_child_process(self):
        rt = make_runtime(self.tmp, provider=ScriptedProvider([[text("ok")]]))
        rt.cfg["plugins"]["mcp"] = {"servers": {"demo": server()}}
        loader.load_plugin(PLUGIN, rt, loader.TrustStore(self.tmp / "home"), allow_untrusted=True)
        connection = rt.tools.get("demo_echo").server
        self.assertTrue(connection.alive)

        run(rt.events.emit("session_end", {}, rt))
        self.assertFalse(connection.alive)


if __name__ == "__main__":
    unittest.main()
