"""Browser tests run against the real server (built UI + API) in a subprocess."""

import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
AXE = BACKEND.parent / "frontend" / "node_modules" / "axe-core" / "axe.min.js"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def base_url(tmp_path_factory):
    if not (BACKEND / "static" / "index.html").exists():
        pytest.skip("build the UI first: make build")
    port = _free_port()
    env = {
        **os.environ,
        "APP_ENV": "test",  # enables the pinned-clock seam so results never depend on when the tests run
        "FIXED_NOW": os.environ.get("E2E_NOW", "2026-10-26T10:00:00"),
        "DATABASE_URL": f"sqlite:///{tmp_path_factory.mktemp('db') / 'e2e.db'}",
        "DEMO_MODE": "true",
        "RATE_LIMIT_PER_MINUTE": "100000",
        "BACKGROUND_ENABLED": "false",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.asgi:app", "--port", str(port), "--log-level", "warning"],
        cwd=BACKEND,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    for _ in range(80):
        try:
            urllib.request.urlopen(f"{url}/healthz", timeout=1)
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.25)
    else:
        proc.kill()
        pytest.fail("server did not start")
    yield url
    proc.terminate()
    proc.wait(timeout=10)


@pytest.fixture(scope="session")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def page(browser, base_url):
    ctx = browser.new_context(
        viewport={"width": 1280, "height": 860}, bypass_csp=True
    )  # bypass_csp lets axe be injected
    pg = ctx.new_page()
    pg.errors = []
    pg.on("pageerror", lambda e: pg.errors.append(str(e)))
    pg.on("console", lambda m: pg.errors.append(m.text) if m.type == "error" and "401" not in m.text else None)
    pg.set_default_timeout(8000)
    yield pg
    ctx.close()


def axe_violations(page, levels=("serious", "critical")):
    page.add_script_tag(path=str(AXE))
    found = page.evaluate(
        "async () => (await axe.run(document, {resultTypes: ['violations']})).violations"
        ".map(v => ({id: v.id, impact: v.impact, help: v.help, nodes: v.nodes.slice(0, 3).map(n => n.target.join(' '))}))"
    )
    return [v for v in found if v["impact"] in levels]
