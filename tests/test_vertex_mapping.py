"""Unit tests for the Vertex plugin's message/schema mapping (no network)."""
import importlib.util, unittest
from helpers import ROOT
from picoagent.core.types import Message, ToolCall, ToolResult

spec = importlib.util.spec_from_file_location("vertex_provider", ROOT / "examples/plugins/vertex-provider/vertex_provider.py")
vertex = importlib.util.module_from_spec(spec); spec.loader.exec_module(vertex)


class SchemaCleaningTests(unittest.TestCase):
    def test_drops_keys_gemini_rejects_and_keeps_the_rest(self):
        schema = {"type": "object", "additionalProperties": False, "$schema": "x",
                  "properties": {"path": {"type": "string", "default": "a", "description": "d"},
                                 "items": {"type": "array", "items": {"type": "integer", "minimum": 0}}},
                  "required": ["path"]}
        cleaned = vertex.clean_schema(schema)
        self.assertEqual(cleaned, {"type": "object",
                                   "properties": {"path": {"type": "string", "description": "d"},
                                                  "items": {"type": "array", "items": {"type": "integer"}}},
                                   "required": ["path"]})

    def test_non_dict_input_is_returned_unchanged(self):
        self.assertEqual(vertex.clean_schema("x"), "x")


class MessageMappingTests(unittest.TestCase):
    def test_roles_and_function_calls_round_trip(self):
        names = {}
        contents = vertex.to_gemini_contents([
            Message(role="user", text="hi"),
            Message(role="assistant", text="ok", tool_calls=[ToolCall("c1", "shell", {"command": "ls"})]),
            Message(role="tool", tool_results=[ToolResult("c1", "a.txt")]),
        ], names)
        self.assertEqual([c["role"] for c in contents], ["user", "model", "user"])
        self.assertEqual(contents[1]["parts"][1]["functionCall"], {"name": "shell", "args": {"command": "ls"}})
        self.assertEqual(contents[2]["parts"][0]["functionResponse"]["name"], "shell")
        self.assertEqual(contents[2]["parts"][0]["functionResponse"]["response"]["output"], "a.txt")


if __name__ == "__main__":
    unittest.main()
