"""Browser transport and real offline HTML guards (Chrome tests skip if unavailable)."""

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from harness import browser_dom, cdp


def test_disabled_browser_does_not_discover_or_connect(monkeypatch):
    monkeypatch.setenv("HARNESS_CHROME_CDP", "0")
    monkeypatch.setattr(cdp, "_discover_port", lambda *a: pytest.fail("discovery"))
    with pytest.raises(cdp.CdpError):
        with browser_dom.Browser():
            pass


def test_model_text_is_a_literal_cdp_argument_and_action_is_never_retried():
    class Session:
        calls = []

        def call(self, method, **params):
            self.calls.append((method, params))
            raise TimeoutError("lost reply after input")

    browser = browser_dom.Browser()
    browser.session, browser.object_id = Session(), "observed-object"
    text = '"); throw new Error("injected")'
    with pytest.raises(TimeoutError):
        browser.act("TYPE_TEXT", "1", text)
    assert len(browser.session.calls) == 1
    method, params = browser.session.calls[0]
    assert method == "Runtime.callFunctionOn"
    assert text not in params["functionDeclaration"]
    assert params["arguments"][-1] == {"value": text}


def test_selects_visible_tab_and_refuses_ambiguous_windows(monkeypatch):
    states = {"/first": "hidden", "/second": "visible"}

    class Session:
        def __init__(self, machine, port, path):
            self.path = path

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def call(self, *a, **kw):
            return {"result": {"value": states[self.path]}}

    monkeypatch.setattr(cdp, "_Session", Session)
    pages = [
        {"type": "page", "url": "https://example.test", "webSocketDebuggerUrl": p} for p in states
    ]
    assert browser_dom._visible_page_path(None, 123, pages) == "/second"
    states["/first"] = "visible"
    with pytest.raises(cdp.CdpError, match="ambiguous"):
        browser_dom._visible_page_path(None, 123, pages)


@pytest.mark.parametrize(
    "url,visibility", [("chrome://settings", "visible"), ("https://example.test", "hidden")]
)
def test_observation_rejects_non_web_navigation_or_tab_switch(monkeypatch, url, visibility):
    browser = browser_dom.Browser()
    monkeypatch.setattr(
        browser, "_call", lambda *a: {"url": url, "visibility": visibility, "elements": []}
    )
    with pytest.raises(cdp.CdpError):
        browser.observe()


_HTML = b"""<!doctype html><html><head><title>Browser guard fixture</title></head><body>
<label for="city">City</label><input id="city"><input type="password" value="never-observe-this">
<input autocomplete="cc-number" value="never-observe-card">
<div style="opacity:0"><button>INVISIBLE_CONTROL</button></div>
<button id="search" onclick="document.getElementById('result').textContent='Clicked'">Search</button>
<a id="link" href="#safe">Next</a>
<select id="class"><option>Economy</option><option>Business</option></select>
<p id="result">Ready</p><div style="position:absolute;top:3000px">OFFSCREEN_SECRET_TEXT</div>
</body></html>"""


@pytest.fixture
def chrome(monkeypatch):
    binary = next(
        (
            p
            for name in ("google-chrome", "chromium", "chromium-browser")
            if (p := shutil.which(name))
        ),
        None,
    )
    if not binary:
        pytest.skip("Chrome unavailable; transport tests still run")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(_HTML)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    monkeypatch.setenv("HARNESS_CHROME_CDP", "1")
    cdp.reset_caches()
    # Snap Chromium cannot expose a profile in the host's /tmp. A caller may
    # choose a disposable parent directory visible inside the snap instead.
    with tempfile.TemporaryDirectory(dir=os.environ.get("DOTOBOT_BROWSER_TEST_TMP")) as folder:
        profile = Path(folder) / "profile"
        with (Path(folder) / "chrome.log").open("w") as log:
            process = subprocess.Popen(
                [
                    binary,
                    "--headless",
                    "--no-sandbox",
                    "--disable-gpu",
                    "--no-first-run",
                    "--remote-debugging-port=0",
                    f"--user-data-dir={profile}",
                    f"http://127.0.0.1:{server.server_port}/",
                ],
                stdout=log,
                stderr=log,
            )
            try:
                deadline = time.monotonic() + 20
                while not (profile / "DevToolsActivePort").exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                assert (profile / "DevToolsActivePort").exists(), (
                    "Chrome did not expose its test profile"
                )
                last_error = None
                while time.monotonic() < deadline:
                    try:
                        with browser_dom.Browser(user_data_dir=str(profile)) as browser:
                            if browser.observe().get("title") == "Browser guard fixture":
                                yield browser
                                return
                    except (cdp.CdpError, KeyError) as exc:
                        last_error = str(exc)
                        time.sleep(0.05)
                pytest.fail(f"Chrome fixture page did not load: {last_error}")
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                server.shutdown()
                server.server_close()
                worker.join(timeout=2)
                cdp.reset_caches()


@pytest.mark.timeout(60)
def test_real_dom_fills_selects_and_rejects_stale_covered_and_replaced_nodes(chrome):
    def evaluate(script):
        return chrome.session.call("Runtime.evaluate", expression=script, returnByValue=True)[
            "result"
        ].get("value")

    def target(page, label):
        return next(e["id"] for e in page["elements"] if e["label"] == label)

    page = chrome.observe()
    assert "never-observe" not in json.dumps(page)
    assert "OFFSCREEN_SECRET_TEXT" not in page["text"]
    assert "INVISIBLE_CONTROL" not in json.dumps(page)
    assert chrome.act("TYPE_TEXT", target(page, "City"), "London") == "ok"
    assert evaluate('document.getElementById("city").value') == "London"
    assert chrome.act("TYPE_TEXT", target(page, "City"), "Paris") == "stale"
    page = chrome.observe()
    select = next(e for e in page["elements"] if "SELECT" in e["operations"])
    assert chrome.act("SELECT", select["id"] + ":1") == "ok"
    assert evaluate('document.getElementById("class").selectedIndex') == 1

    page = chrome.observe()
    evaluate('document.getElementById("link").href = "#changed-destination"')
    assert chrome.act("CLICK", target(page, "Next")) == "stale"

    page = chrome.observe()
    evaluate('document.querySelector("input[type=password]").value = "changed-password"')
    assert chrome.act("CLICK", target(page, "Search")) == "stale"

    page = chrome.observe()
    search = target(page, "Search")
    evaluate('document.getElementById("city").value = "Changed by user"')
    assert chrome.act("CLICK", search) == "stale"
    assert evaluate('document.getElementById("result").textContent') == "Ready"

    page = chrome.observe()
    evaluate('const old = document.getElementById("search"); old.replaceWith(old.cloneNode(true))')
    assert chrome.act("CLICK", target(page, "Search")) == "stale"

    page = chrome.observe()
    evaluate(
        'const cover = document.createElement("div"); cover.id="cover"; '
        'cover.style="position:fixed;inset:0;z-index:9999"; document.body.append(cover)'
    )
    assert chrome.act("CLICK", target(page, "Search")) == "stale"
    evaluate('document.getElementById("cover").remove()')
    page = chrome.observe()
    assert chrome.act("CLICK", target(page, "Search")) == "ok"
    assert evaluate('document.getElementById("result").textContent') == "Clicked"

    evaluate('const frame = document.createElement("iframe"); document.body.append(frame)')
    assert chrome.observe()["unsupported"]


@pytest.mark.timeout(60)
def test_real_outgoing_form_binds_url_text_and_single_dispatch(chrome):
    def evaluate(script):
        return chrome.session.call("Runtime.evaluate", expression=script, returnByValue=True)["result"].get("value")

    evaluate("document.body.innerHTML = '<form onsubmit=\"window.sent=this.elements.body.value; return false\"><textarea name=body></textarea><button>Send</button></form>'")
    url = chrome.observe()["url"]
    text = 'A literal message with "quotes" and a question?'
    assert chrome.prepare_outgoing(url + "different", text) == "unsupported"
    evaluate("document.querySelector('textarea').value = 'User draft'")
    assert chrome.prepare_outgoing(url, text) == "unsupported"
    evaluate("document.querySelector('textarea').value = ''")
    assert chrome.prepare_outgoing(url, text) == "ready"
    evaluate("document.querySelector('textarea').value = 'Changed after approval check'")
    assert chrome.submit_outgoing(url, text) == "stale"
    assert evaluate("window.sent") is None
    for mutation in (
        "e.target.form.action = '/different-recipient'",
        "e.target.form.method = 'post'",
        "e.target.form.elements.recipient.value = 'different'",
        "const old=e.target.form.elements.recipient; old.replaceWith(old.cloneNode())",
    ):
        evaluate("document.body.innerHTML = '<form onsubmit=\"window.sent=this.elements.body.value; return false\"><input type=hidden name=recipient value=original><textarea name=body></textarea><button>Send</button></form>'; window.sent = null")
        evaluate("document.querySelector('textarea').oninput = e => { " + mutation + " }")
        assert chrome.prepare_outgoing(url, text) == "ready"
        assert chrome.submit_outgoing(url, text) == "stale"
        assert evaluate("window.sent") is None
    evaluate("document.querySelector('textarea').value = ''; document.querySelector('textarea').oninput = null")
    assert chrome.prepare_outgoing(url, text) == "ready"
    assert chrome.submit_outgoing(url, text) == "ok"
    assert evaluate("window.sent") == text
    assert chrome.submit_outgoing(url, text) == "stale"
    evaluate("document.querySelector('textarea').value = ''; window.sent = null; document.querySelector('textarea').oninput = e => e.target.value += ' Unapproved source'")
    assert chrome.prepare_outgoing(url, text) == "ready"
    assert chrome.submit_outgoing(url, text) == "stale"
    assert evaluate("window.sent") is None
