"""Two plugins that ship a file of the same name must not share it.

A plugin's entry module already gets a namespace of its own; its siblings did not. They were
imported bare, through the process-wide ``sys.path`` and ``sys.modules``, so the first plugin
to load owned the name ``utils`` and every plugin loaded after it silently executed *that*
plugin's code. The user approved those bytes under a different fingerprint, against a
different manifest, which makes it a trust boundary rather than a naming inconvenience.

The synthetic plugins below are the smallest thing that shows it: identical filenames,
different contents, and an assertion that each plugin got its own. ``sys.path`` is checked
too, because the mechanism that caused the collision also left every plugin directory on the
path for the life of the process, where it shadows the standard library and site-packages
for anything imported later.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from helpers import make_runtime, run
from picoagent.plugins import loader

#: A plugin whose entry module imports a sibling at import time, calls one at command time,
#: and reaches outside itself for the standard library and for picoagent's own package.
ENTRY = '''\
import json

from utils import MARKER
from picoagent.core.types import ToolResult


def register(api):
    api.register_command({name!r}, handler, json.dumps({{"marker": MARKER}}))
    api.register_command({name!r} + "-lazy", lazy, "reads its sibling when called")
    assert ToolResult is not None


async def handler(args, rt):
    return MARKER


async def lazy(args, rt):
    import utils                      # the shape es-doctor uses: sibling imported on demand
    return utils.MARKER
'''


def write_plugin(root: Path, name: str, marker: str) -> Path:
    """A plugin directory whose sibling module is always called ``utils.py``."""
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "plugin.toml").write_text(f'name="{name}"\nentry="{name}_entry:register"\n')
    (directory / f"{name}_entry.py").write_text(ENTRY.format(name=name))
    (directory / "utils.py").write_text(f"MARKER = {marker!r}\n")
    return directory


class PluginIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.rt = make_runtime(self.tmp)
        self.trust = loader.TrustStore(self.tmp / "home")
        self.alpha = write_plugin(self.tmp, "alpha", "alpha-utils")
        self.beta = write_plugin(self.tmp, "beta", "beta-utils")
        self.path_before = list(sys.path)
        self.addCleanup(self._forget_loaded_modules)

    def _forget_loaded_modules(self):
        """Drop the namespaces these plugins claimed, so one test cannot answer the next."""
        mine = ("_alpha_", "_beta_", "_gamma_")
        for name in [n for n in sys.modules if n == "utils" or any(m in n for m in mine)]:
            del sys.modules[name]

    def load_both(self):
        for root in (self.alpha, self.beta):
            loader.load_plugin(root, self.rt, self.trust, allow_untrusted=True)

    def marker(self, command: str) -> str:
        return run(self.rt.commands.get(command).handler("", self.rt))

    def test_each_plugin_sees_its_own_sibling_at_import_time(self):
        self.load_both()
        self.assertEqual(self.marker("alpha"), "alpha-utils")
        self.assertEqual(self.marker("beta"), "beta-utils")

    def test_load_order_does_not_decide_whose_sibling_wins(self):
        for root in (self.beta, self.alpha):
            loader.load_plugin(root, self.rt, self.trust, allow_untrusted=True)
        self.assertEqual(self.marker("alpha"), "alpha-utils")
        self.assertEqual(self.marker("beta"), "beta-utils")

    def test_a_sibling_imported_lazily_is_still_the_plugins_own(self):
        self.load_both()
        self.assertEqual(self.marker("alpha-lazy"), "alpha-utils")
        self.assertEqual(self.marker("beta-lazy"), "beta-utils")

    def test_no_plugin_root_is_left_on_sys_path(self):
        self.load_both()
        self.assertEqual(sys.path, self.path_before)
        for root in (self.alpha, self.beta):
            self.assertNotIn(str(root), sys.path)

    def test_a_plugin_may_spell_its_sibling_imports_relatively(self):
        """The other style has to reach the same module, or a mixed plugin breaks in the middle."""
        gamma = self.tmp / "gamma"
        gamma.mkdir()
        (gamma / "plugin.toml").write_text('name="gamma"\nentry="gamma_entry:register"\n')
        (gamma / "utils.py").write_text("MARKER = 'gamma-utils'\n")
        (gamma / "gamma_entry.py").write_text(
            "from . import utils\n"
            "from .utils import MARKER\n"
            "def register(api):\n"
            "    assert utils.MARKER == MARKER\n"
            "    api.register_command('gamma', handler, MARKER)\n"
            "async def handler(args, rt):\n"
            "    return MARKER\n")
        self.load_both()
        loader.load_plugin(gamma, self.rt, self.trust, allow_untrusted=True)
        self.assertEqual(self.marker("gamma"), "gamma-utils")
        self.assertEqual(self.marker("alpha"), "alpha-utils")

    def test_a_sibling_never_takes_a_bare_top_level_name(self):
        self.load_both()
        bare = sys.modules.get("utils")
        self.assertIsNone(bare, f"a plugin's sibling claimed the top-level name 'utils': {bare}")


if __name__ == "__main__":
    unittest.main()
