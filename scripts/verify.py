"""Run only in a fresh interpreter with an isolated environment; no production writes."""
from pathlib import Path
import importlib.abc
import os
import sys
import tempfile
from types import ModuleType

REPO = Path(__file__).resolve().parents[1]
HOST = Path(os.environ.get("HERMES_SOURCE", str(Path.home() / ".hermes/hermes-agent"))).resolve()
for packages in sorted((HOST / "venv/lib").glob("python*/site-packages")):
    sys.path.insert(0, str(packages))
sys.path.insert(0, str(HOST))
sys.path.insert(0, str(REPO))
scratch = Path(os.environ.get("TMPDIR", str(Path.home() / ".hermes/cache/scratch")))
scratch.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix="session-heartbeat-verify-", dir=scratch) as temp:
    root = Path(temp)
    os.environ["HOME"] = str(root / "user-home")
    Path(os.environ["HOME"]).mkdir()
    os.environ["HERMES_HOME"] = str(root / "bootstrap-home")
    os.environ["HERMES_DISABLE_LAZY_INSTALLS"] = "1"
    os.environ["HERMES_BUNDLED_PLUGINS"] = str(root / "no-bundled-plugins")
    os.environ["HERMES_ENABLE_PROJECT_PLUGINS"] = "0"
    os.environ["HERMES_TEST_PLUGIN"] = str(Path(sys.argv[2] if len(sys.argv) > 2 else REPO / "plugin").resolve())
    class OfflineImportGuard(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname in {"hermes_bootstrap", "run_agent"}:
                # Never execute entry-point PM preparation or instantiate a live agent.
                raise ModuleNotFoundError("Offline probe excludes " + fullname, name=fullname)
    sys.meta_path.insert(0, OfflineImportGuard())
    # agent.process_bootstrap imports this helper even for pure binder/dispatch use.
    # Supply only its unused network export; fail loudly if the probe tries networking.
    bootstrap = ModuleType("hermes_bootstrap")
    def no_network(*args, **kwargs):
        raise RuntimeError("Offline probe must not open a bootstrap network connection")
    setattr(bootstrap, "_happy_eyeballs_create_connection", no_network)
    sys.modules["hermes_bootstrap"] = bootstrap
    print("HOST", HOST, flush=True)
    print("PLUGIN", os.environ["HERMES_TEST_PLUGIN"], flush=True)
    print("ISOLATED_HOME", os.environ["HERMES_HOME"], flush=True)
    print("OFFLINE_GUARD: scratch HOME; bootstrap network-only rejecting shim; live-agent import blocked; lazy installs disabled", flush=True)
    if len(sys.argv) > 1 and sys.argv[1] == "doctor":
        from hermes_cli.plugins_cmd import cmd_plugin_doctor
        cmd_plugin_doctor(os.environ["HERMES_TEST_PLUGIN"], ci=True)
    else:
        import pytest
        result = pytest.main(["-q", "-s", "-p", "no:cacheprovider", "--basetemp", str(root / "pytest"), str(REPO / "tests")])
        assert sys.modules["hermes_bootstrap"] is bootstrap and "run_agent" not in sys.modules
        print("OFFLINE_GUARD_READBACK: bootstrap shim retained; no live-agent module loaded", flush=True)
        raise SystemExit(result)
