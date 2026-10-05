"""Build + exercise source and extracted artifact, retaining real execution logs."""
from pathlib import Path
import gzip
import hashlib
import io
import json
import os
import subprocess
import tarfile
import tempfile

repo = Path(__file__).resolve().parents[1]
host = Path(os.environ.get("HERMES_SOURCE", str(Path.home() / ".hermes/hermes-agent"))).resolve()
scratch = Path(os.environ.get("TMPDIR", str(Path.home() / ".hermes/cache/scratch"))).resolve()
scratch.mkdir(parents=True, exist_ok=True)
python = Path(os.environ.get("HERMES_PYTHON", str(host / "venv/bin/python")))
if not python.is_file():
    raise SystemExit("Hermes Python not found; set HERMES_PYTHON to the host interpreter.")
env = {"HOME": str(Path.home()), "PATH": "/usr/local/bin:/usr/bin:/bin", "TMPDIR": str(scratch), "HERMES_SOURCE": str(host), "PYTHONDONTWRITEBYTECODE": "1"}
evidence = repo / "evidence"
evidence.mkdir(exist_ok=True)


def run(name, argv):
    result = subprocess.run([str(v) for v in argv], cwd=repo, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    log = result.stdout + f"\nEXIT_CODE={result.returncode}\n"
    (evidence / (name + ".log")).write_text(log, encoding="utf-8")
    print(log, flush=True)
    if result.returncode:
        raise SystemExit(result.returncode)
    return result.stdout


run("source-tests", [python, repo / "scripts/verify.py"])
run("source-doctor", [python, repo / "scripts/verify.py", "doctor"])
files = [("plugin.yaml", repo / "plugin/plugin.yaml"), ("__init__.py", repo / "plugin/__init__.py"), ("heartbeat_tool.py", repo / "plugin/heartbeat_tool.py"), ("README.md", repo / "README.md"), ("README.ja.md", repo / "README.ja.md"), ("LICENSE", repo / "LICENSE"), ("docs/verification.md", repo / "docs/verification.md")]
archive = repo / "dist/session-heartbeat-0.1.0.tar.gz"
archive.parent.mkdir(exist_ok=True)
with archive.open("wb") as raw:
    with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
            for name, path in files:
                data = path.read_bytes()
                info = tarfile.TarInfo("session-heartbeat/" + name)
                info.size, info.mode, info.mtime = len(data), 0o644, 0
                tar.addfile(info, io.BytesIO(data))
manifest = {"artifact": str(archive), "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(), "files": {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in files}}
(evidence / "build.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
print(json.dumps(manifest, indent=2), flush=True)
with tempfile.TemporaryDirectory(prefix="session-heartbeat-artifact-", dir=scratch) as temp:
    with tarfile.open(archive) as tar:
        tar.extractall(temp, filter="data")
    artifact = Path(temp) / "session-heartbeat"
    for name, path in files:
        assert (artifact / name).read_bytes() == path.read_bytes()
    run("artifact-tests", [python, repo / "scripts/verify.py", "tests", artifact])
    run("artifact-doctor", [python, repo / "scripts/verify.py", "doctor", artifact])
run("host-revision", ["git", "-C", host, "rev-parse", "HEAD"])
run("host-worktree", ["git", "-C", host, "status", "--short"])
