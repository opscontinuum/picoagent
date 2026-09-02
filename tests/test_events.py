"""EventBus behaviour: ordering, patching, blocking, plugin isolation."""
import unittest
from helpers import run, ROOT  # noqa: F401
from picoagent.core.events import EventBus


class EventBusTests(unittest.TestCase):
    def test_handlers_run_in_registration_order(self):
        bus, seen = EventBus(), []
        bus.on("x", lambda p, c: seen.append("a"), owner="a")
        bus.on("x", lambda p, c: seen.append("b"), owner="b")
        run(bus.emit("x", {}))
        self.assertEqual(seen, ["a", "b"])

    def test_returned_dict_patches_payload_for_later_handlers(self):
        bus = EventBus()
        bus.on("x", lambda p, c: {"text": p["text"].upper()})
        bus.on("x", lambda p, c: {"text": p["text"] + "!"})
        out = run(bus.emit("x", {"text": "hi"}))
        self.assertEqual(out["text"], "HI!")

    def test_block_short_circuits_and_records_owner(self):
        bus, later = EventBus(), []
        bus.on("tool_call", lambda p, c: {"block": True, "reason": "no"}, owner="gate")
        bus.on("tool_call", lambda p, c: later.append(1), owner="other")
        out = run(bus.emit("tool_call", {"block": False}))
        self.assertTrue(out["block"]); self.assertEqual(out["blocked_by"], "gate"); self.assertEqual(later, [])

    def test_handled_action_short_circuits(self):
        bus, later = EventBus(), []
        bus.on("input", lambda p, c: {"action": "handled"})
        bus.on("input", lambda p, c: later.append(1))
        run(bus.emit("input", {"action": "continue"}))
        self.assertEqual(later, [])

    def test_async_handlers_are_awaited(self):
        bus = EventBus()
        async def h(p, c): return {"n": p["n"] + 1}
        bus.on("x", h)
        self.assertEqual(run(bus.emit("x", {"n": 1}))["n"], 2)

    def test_a_crashing_handler_does_not_stop_others(self):
        bus, seen = EventBus(), []
        def boom(p, c): raise RuntimeError("plugin bug")
        bus.on("x", boom, owner="bad")
        bus.on("x", lambda p, c: seen.append(1), owner="good")
        run(bus.emit("x", {}))
        self.assertEqual(seen, [1])

    def test_off_owner_removes_only_that_plugins_handlers(self):
        bus, seen = EventBus(), []
        bus.on("x", lambda p, c: seen.append("a"), owner="a")
        bus.on("x", lambda p, c: seen.append("b"), owner="b")
        bus.off_owner("a")
        run(bus.emit("x", {}))
        self.assertEqual(seen, ["b"])


if __name__ == "__main__":
    unittest.main()
