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


#: A plugin whose code sits one level down, in a subpackage of its own directory. Everything
#: here is spelled the way a plugin author would spell it; what is under test is whether the
#: shim reaches that far. A module the shim does not load itself is executed with ordinary
#: builtins, and its own ``import utils`` then resolves against ``sys.path``.
SUBPACKAGE_ENTRY = '''\
def register(api):
    api.register_command("delta", named, "names the module a submodule import produced")
    api.register_command("delta-sibling", sibling, "what a module one level down bound")
    api.register_command("delta-relative", relative, "a subpackage importing its neighbour")
    api.register_command("delta-importlib", by_importlib, "the spelling the shim cannot see")


async def named(args, rt):
    from pkg import mod                  # a submodule, not an attribute the package already has
    return mod.__name__


async def sibling(args, rt):
    from pkg import mod
    return mod.SIBLING_MARKER


async def relative(args, rt):
    from pkg.rel import RELATIVE_MARKER
    return RELATIVE_MARKER


async def by_importlib(args, rt):
    import importlib
    return importlib.import_module("utils").MARKER
'''


def write_subpackage_plugin(root: Path, name: str = "delta", marker: str = "delta-utils") -> Path:
    """A plugin that ships ``utils.py`` beside its entry and a ``pkg/`` that imports it."""
    directory = root / name
    (directory / "pkg").mkdir(parents=True)
    (directory / "plugin.toml").write_text(f'name="{name}"\nentry="{name}_entry:register"\n')
    (directory / f"{name}_entry.py").write_text(SUBPACKAGE_ENTRY)
    (directory / "utils.py").write_text(f"MARKER = {marker!r}\n")
    (directory / "pkg" / "__init__.py").write_text("")
    (directory / "pkg" / "mod.py").write_text("import utils\n\nSIBLING_MARKER = utils.MARKER\n")
    (directory / "pkg" / "rel.py").write_text(
        "from . import mod\n\nRELATIVE_MARKER = mod.SIBLING_MARKER\n")
    return directory


class SubpackageIsolationTests(unittest.TestCase):
    """The boundary has to hold below the top level of a plugin directory.

    The sibling rule was written for files sitting beside the entry module, and stopped there.
    A plugin that puts its code in a subpackage got the ordinary import machinery from the
    second level down: the subpackage's modules were executed with ordinary builtins, so their
    own ``import utils`` went to ``sys.path`` and bound whatever was installed there - foreign
    code, outside the fingerprint the user approved, which is the crossing this shim exists to
    prevent. A ``utils`` is installed on ``sys.path`` here so a leak has something to bind, and
    is named so the assertion failure says which side of the boundary answered.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.rt = make_runtime(self.tmp)
        self.trust = loader.TrustStore(self.tmp / "home")
        self.delta = write_subpackage_plugin(self.tmp)
        self.addCleanup(self._forget_loaded_modules)
        self._install_foreign_utils()

    def _install_foreign_utils(self) -> None:
        """A top-level ``utils`` on ``sys.path``, standing in for one out of site-packages."""
        foreign = self.tmp / "site-packages"
        foreign.mkdir()
        (foreign / "utils.py").write_text("MARKER = 'foreign-utils'\n")
        sys.path.insert(0, str(foreign))
        self.addCleanup(sys.path.remove, str(foreign))

    def _forget_loaded_modules(self):
        for name in [n for n in sys.modules if n == "utils" or "_delta_" in n]:
            del sys.modules[name]

    def marker(self, command: str) -> str:
        loader.load_plugin(self.delta, self.rt, self.trust, allow_untrusted=True)
        return run(self.rt.commands.get(command).handler("", self.rt))

    def test_a_submodule_can_be_imported_from_the_package_that_holds_it(self):
        """``from pkg import mod`` asks for a module, not an attribute: importing it is the
        importer's job, and an importer that only returns ``pkg`` leaves the name unbound."""
        self.assertTrue(self.marker("delta").endswith(".pkg.mod"), self.marker("delta"))

    def test_a_module_one_level_down_still_imports_the_plugins_own_sibling(self):
        self.assertEqual(self.marker("delta-sibling"), "delta-utils")

    def test_a_subpackage_reaching_sideways_relatively_stays_inside_the_plugin(self):
        """``from . import mod`` below the top level is served by the same machinery, or the
        module it produces carries ordinary builtins and leaks from there."""
        self.assertEqual(self.marker("delta-relative"), "delta-utils")

    def test_importlib_import_module_is_outside_the_boundary_and_pinned_here(self):
        """A limitation, stated rather than implied. ``importlib.import_module`` calls
        ``_bootstrap._gcd_import`` directly and never consults ``__import__``, so no shim
        installed on a module's builtins can answer it: the call is served by the standard
        machinery against ``sys.path``. Plugin authors are told not to spell it this way
        (docs/plugin-authoring.md); this test is here so the gap stays visible.
        """
        self.assertEqual(self.marker("delta-importlib"), "foreign-utils")


if __name__ == "__main__":
    unittest.main()
