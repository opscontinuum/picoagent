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


class InterruptedToolBatchTests(unittest.TestCase):
    """A ``functionCall`` no ``functionResponse`` answers must not reach the wire that way.

    Gemini is as strict as OpenAI here and counts rather than matches ids: a request whose model
    turn carries two ``functionCall`` parts and whose next turn carries one ``functionResponse``
    is refused with *"Please ensure that the number of function response parts is equal to the
    number of function call parts of the function call turn"*. A Ctrl-C between the assistant
    message and its results leaves the session log with a call and no result at all, so every
    later turn replays that shape and the resumed session is wedged behind a 400 that names none
    of this. Same defect as ``provider._stand_in_results`` repairs for the OpenAI dialect,
    different wire: no ids to match on, a count that has to come out even, and the responses in
    the one user turn that follows the call turn.
    """

    def test_an_unanswered_call_is_answered_before_the_request_is_sent(self):
        contents = vertex.to_gemini_contents([
            Message(role="user", text="run it"),
            Message(role="assistant", tool_calls=[ToolCall("c1", "shell", {"command": "sleep 600"})]),
        ], {})
        self.assertEqual([c["role"] for c in contents], ["user", "model", "user"])
        self.assertEqual(contents[2]["parts"][0]["functionResponse"]["name"], "shell")

    def test_the_answer_claims_neither_success_nor_failure(self):
        """The one thing known about an interrupted call is that its outcome is not known."""
        contents = vertex.to_gemini_contents(
            [Message(role="assistant", tool_calls=[ToolCall("c1", "shell", {})])], {})
        response = contents[1]["parts"][0]["functionResponse"]["response"]
        self.assertIn("unknown", response["output"].lower())
        self.assertNotIn("failed", response["output"].lower())
        self.assertNotIn("succeeded", response["output"].lower())

    def test_the_answer_does_not_report_an_error_field_it_cannot_fill_in(self):
        """``error: false`` reads as success and ``error: true`` as failure; neither is known."""
        contents = vertex.to_gemini_contents(
            [Message(role="assistant", tool_calls=[ToolCall("c1", "shell", {})])], {})
        self.assertNotIn("error", contents[1]["parts"][0]["functionResponse"]["response"])

    def test_a_batch_that_finished_is_left_exactly_as_it_was(self):
        contents = vertex.to_gemini_contents([
            Message(role="assistant", tool_calls=[ToolCall("c1", "read", {})]),
            Message(role="tool", tool_results=[ToolResult("c1", "file contents")]),
        ], {})
        self.assertEqual([c["role"] for c in contents], ["model", "user"])
        self.assertEqual(contents[1]["parts"][0]["functionResponse"]["response"],
                         {"output": "file contents", "error": False})

    def test_a_half_answered_batch_comes_out_even_in_one_turn(self):
        """The count is what Gemini checks, and it checks it against the *turn* after the calls,
        so the stand-in has to join the real results rather than form a turn of its own."""
        calls = [ToolCall("c1", "read", {}), ToolCall("c2", "read", {})]
        contents = vertex.to_gemini_contents([
            Message(role="assistant", tool_calls=calls),
            Message(role="tool", tool_results=[ToolResult("c1", "first")]),
        ], {})
        self.assertEqual([c["role"] for c in contents], ["model", "user"])
        self.assertEqual(len(contents[0]["parts"]), len(contents[1]["parts"]))
        self.assertEqual([p["functionResponse"]["response"]["output"] for p in contents[1]["parts"]][0],
                         "first")

    def test_the_answer_sits_between_the_call_and_whatever_the_user_typed_next(self):
        """On ``-r`` the next entry is the new prompt, and a call turn answered by a plain user
        turn is the other half of the same refusal."""
        contents = vertex.to_gemini_contents([
            Message(role="assistant", tool_calls=[ToolCall("c1", "shell", {})]),
            Message(role="user", text="what happened?"),
        ], {})
        self.assertEqual(len(contents), 3)
        self.assertIn("functionResponse", contents[1]["parts"][0])
        self.assertEqual(contents[2]["parts"][-1]["text"], "what happened?")


if __name__ == "__main__":
    unittest.main()
