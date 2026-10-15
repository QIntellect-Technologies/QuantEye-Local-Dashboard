from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "evidence" / "after" / "PHASEA_REVIEW"


def dashboard_python() -> str:
    env = os.environ.get("QE_DASHBOARD_PYTHON")
    if env:
        return env
    conda_py = Path(r"C:\Users\Imran\anaconda3\envs\env310tfgpu\python.exe")
    if conda_py.exists():
        return str(conda_py)
    return shutil.which("python") or sys.executable


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest.fixture(scope="session")
def ui_server():
    dest = EVIDENCE / "ui.db"
    py = dashboard_python()
    seed = subprocess.run(
        [py, str(ROOT / "tests" / "playwright" / "seed_ui_db.py"), str(dest)],
        cwd=str(ROOT),
        check=True,
        capture_output=True,
        text=True,
    )
    port = _free_port()
    env = os.environ.copy()
    env["QINTELLECT_DB_PATH"] = str(dest)
    env["QINTELLECT_UI_REVIEW"] = "1"
    env["QINTELLECT_UI_ORG"] = "ORG-PAY-AUDIT-1"
    env["QINTELLECT_UI_BRANCH"] = "BR-PAY-AUDIT-1"
    env["FLASK_SECRET_KEY"] = "phase-a-ui-review-secret-key-32bytes"
    env["PORT"] = str(port)
    proc = subprocess.Popen(
        [py, str(ROOT / "tests" / "ui_review_server.py")],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}/?tab=payroll-view"
    for _ in range(90):
        try:
            import urllib.request
            urllib.request.urlopen(url, timeout=2)
            break
        except Exception:
            time.sleep(1)
    else:
        proc.kill()
        raise RuntimeError(f"UI server did not start: {seed.stdout} {seed.stderr}")
    yield {"url": url, "base": f"http://127.0.0.1:{port}", "db": dest}
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
