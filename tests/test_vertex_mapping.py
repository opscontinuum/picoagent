"""Unit tests for the Vertex plugin's message/schema mapping (no network)."""
import importlib.util, json, unittest
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

    def test_a_repeated_id_needs_no_dedup_here_because_the_pairing_is_positional(self):
        """The OpenAI mapping answers one stand-in per *distinct* unanswered id, because two
        ``role: tool`` entries sharing a ``tool_call_id`` are their own 400. Gemini has no ids on
        the wire: one response part per call part, in order. Two calls sharing an id are two call
        parts and two response parts, so the count Gemini actually checks comes out even and
        deduplicating would be the thing that broke it."""
        calls = [ToolCall("dup", "read", {}), ToolCall("dup", "read", {})]
        contents = vertex.to_gemini_contents([Message(role="assistant", tool_calls=calls)], {})
        self.assertEqual(len(contents[1]["parts"]), len(contents[0]["parts"]))

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


class OrphanToolResultTests(unittest.TestCase):
    """A result whose call is not in the history must not be sent as a ``functionResponse``.

    Gemini pairs a response turn with the *call turn before it* and refuses the request when the
    counts differ, so a ``functionResponse`` with no ``functionCall`` ahead of it is the same 400
    as an unanswered call, arriving from the other side. The loop never writes that shape, but a
    plugin rewriting history through the ``context`` event can (compaction that keeps a recent
    tool result and drops the assistant message that asked for it), and so can a log whose
    assistant entry was lost. The information is still worth showing the model; the wire format
    has nowhere to put it as a *response*, so it goes as text.
    """

    def counts(self, contents):
        """Every turn carrying responses, as (responses in it, calls in the turn before it)."""
        pairs = []
        for index, turn in enumerate(contents):
            responses = sum("functionResponse" in part for part in turn["parts"])
            if not responses:
                continue
            before = contents[index - 1]["parts"] if index else []
            pairs.append((responses, sum("functionCall" in part for part in before)))
        return pairs

    def test_a_result_nothing_ever_called_is_not_sent_as_a_response(self):
        contents = vertex.to_gemini_contents([
            Message(role="user", text="hi"),
            Message(role="tool", tool_results=[ToolResult("gone", "the file said hello")]),
        ], {})
        self.assertEqual(self.counts(contents), [], "no response part may sit without its call")

    def test_a_stray_result_after_a_model_turn_that_called_nothing_is_not_one_either(self):
        contents = vertex.to_gemini_contents([
            Message(role="user", text="hi"),
            Message(role="assistant", text="here you go"),
            Message(role="tool", tool_results=[ToolResult("gone", "the file said hello")]),
        ], {})
        self.assertEqual(self.counts(contents), [])

    def test_what_the_tool_reported_still_reaches_the_model(self):
        """Dropping it would lose the one thing the plugin meant to keep."""
        contents = vertex.to_gemini_contents([
            Message(role="user", text="hi"),
            Message(role="tool", tool_results=[ToolResult("gone", "the file said hello")]),
        ], {})
        self.assertIn("the file said hello", json.dumps(contents))

    def test_the_model_is_told_whose_words_those_are(self):
        """It arrives in the user's turn, so it has to say it is a tool's output and not theirs."""
        contents = vertex.to_gemini_contents([
            Message(role="tool", tool_results=[ToolResult("gone", "the file said hello")]),
        ], {"gone": "read"})
        rendered = contents[0]["parts"][0]["text"]
        self.assertIn("picoagent", rendered.lower())
        self.assertIn("read", rendered)

    def test_a_paired_result_is_untouched_by_any_of_this(self):
        contents = vertex.to_gemini_contents([
            Message(role="assistant", tool_calls=[ToolCall("c1", "read", {})]),
            Message(role="tool", tool_results=[ToolResult("c1", "file contents")]),
        ], {})
        self.assertEqual(self.counts(contents), [(1, 1)])


if __name__ == "__main__":
    unittest.main()
