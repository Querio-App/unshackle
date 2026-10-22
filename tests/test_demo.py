import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_tableau_demo_end_to_end(tmp_path: Path):
    env = dict(os.environ)
    bin_dir = str(Path(sys.executable).parent)
    env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
    env["PYTHON"] = sys.executable
    out = tmp_path / "acme"
    run = subprocess.run(
        [str(ROOT / "examples/tableau-demo/run.sh"), str(out)],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert "pass" in run.stdout
    assert (out / "notebooks/executive-overview.html").exists()
    assert list((out / "evals").glob("*.yaml"))
    report = (out / "migration/report.md").read_text()
    assert "Parity (1/1 pass)" in report
