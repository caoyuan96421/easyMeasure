"""End-to-end test of the browser frontend (Chromium via Playwright).

Skipped when Playwright or its browser is not installed
(``pip install playwright && playwright install chromium``).
"""

import json
import time

import numpy as np
import pytest

pytest.importorskip("aiohttp")
sync_api = pytest.importorskip("playwright.sync_api")

from test_remote import GW, PW, gw, gw_factory, home, server  # noqa: F401  (fixtures)


@pytest.fixture
def browser():
    try:
        p = sync_api.sync_playwright().start()
        b = p.chromium.launch()
    except Exception as exc:                      # browser binaries missing
        pytest.skip("no Chromium for Playwright: %s" % exc)
    yield b
    b.close()
    p.stop()


def open_page(browser, gw, user, errors, bypass_csp=True):
    # the test harness evaluates helper JS in the page, which the page's own CSP forbids
    ctx = browser.new_context(ignore_https_errors=True, viewport={"width": 1600, "height": 1000},
                              bypass_csp=bypass_csp)
    page = ctx.new_page()
    page.on("pageerror", lambda e: errors.append("pageerror: %s" % e))
    page.on("console", lambda m: m.type == "error" and errors.append(m.text))
    page.goto(gw.url + "/")
    page.wait_for_selector("#login-user", state="visible")
    if user:
        page.fill("#login-user", user)
        page.fill("#login-password", PW[user])
        page.click("#login-button")
        page.wait_for_selector(".conn.ok", timeout=15000)
        page.wait_for_timeout(800)
    return page


def canvas_has_image(page, sel):
    return page.evaluate("""(sel) => {
        const c = document.querySelector(sel);
        if (!c || !c.width) return 0;
        const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
        const seen = new Set();
        for (let i = 0; i < d.length; i += 4 * 97) seen.add(d[i] + ',' + d[i+1] + ',' + d[i+2]);
        return seen.size;
    }""", sel)


def test_web_ui_end_to_end(browser, gw, demo, tmp_path):
    errors = []
    # the Content-Security-Policy really is in force: injected code cannot eval or load scripts
    page = open_page(browser, gw, None, errors, bypass_csp=False)
    assert page.evaluate("""() => new Promise(r => { const s = document.createElement('script');
        s.textContent = 'window.__injected = 1'; document.head.append(s);
        setTimeout(() => r(window.__injected ? 'allowed' : 'blocked'), 300); })""") == "blocked"
    assert page.evaluate("""() => { const d = document.createElement('div');
        d.innerHTML = '<img src=x onerror="window.__xss=1">'; document.body.append(d);
        return new Promise(r => setTimeout(() => r(window.__xss ? 'allowed' : 'blocked'), 500)); }""") == "blocked"
    assert page.evaluate("""() => new Promise(r => { const s = document.createElement('script');
        s.src = 'https://example.com/x.js'; s.onerror = () => r('blocked'); s.onload = () => r('loaded');
        document.head.append(s); setTimeout(() => r('blocked'), 3000); })""") == "blocked"
    errors.clear()                              # the CSP violation reports above are expected
    page.context.close()
    # wrong password
    page = open_page(browser, gw, None, errors)
    page.fill("#login-user", "olga")
    page.fill("#login-password", "not-the-password")
    page.click("#login-button")
    page.wait_for_function("document.querySelector('#login-error').textContent.length > 0")
    assert "invalid" in page.text_content("#login-error")
    errors.clear()                              # the browser logs the expected 401
    page.context.close()

    page = open_page(browser, gw, "olga", errors)
    assert "olga (operator)" in page.text_content(".user")
    # role gating: operator may set channels but not change instruments
    assert page.is_disabled("[data-cmd='add_instrument']")
    assert page.is_disabled("[data-cmd='define_alias'] >> nth=0")
    assert page.is_enabled("[data-cmd='write_channel'] >> nth=0")
    # instruments table filled from the backend
    page.wait_for_selector("text=sim_source")
    # manual panel: type a value + Enter
    row = page.locator(".man-row", has_text="Vg").first
    inp = row.locator("input.man-num")
    inp.fill("0.12")
    inp.press("Enter")
    t0 = time.time()
    while abs(demo.channels.get("Vg") - 0.12) > 1e-9 and time.time() - t0 < 10:
        time.sleep(0.1)
    assert demo.channels.get("Vg") == pytest.approx(0.12)
    # right click -> expression
    inp.click(button="right")
    page.click(".ctxmenu >> text=Set from expression")
    page.fill("dialog.modal input", "x + 0.01")
    page.click("dialog.modal button.primary")
    t0 = time.time()
    while abs(demo.channels.get("Vg") - 0.13) > 1e-9 and time.time() - t0 < 10:
        time.sleep(0.1)
    assert demo.channels.get("Vg") == pytest.approx(0.13)
    page.wait_for_timeout(500)
    assert float(inp.input_value()) == pytest.approx(0.13)       # the box shows the new value
    # monitor tile
    page.click(".monitor >> input.w-chan")
    page.fill(".monitor >> input.w-chan", "T")
    page.click(".monitor >> button:has-text('Monitor')")
    page.wait_for_selector(".mon-tile >> text=T", timeout=10000)
    page.wait_for_function("[...document.querySelectorAll('.mon-value')].some(e => /\\d/.test(e.textContent))",
                           timeout=10000)
    # sweep editor: load a file, hook code is refused for operators, clear it, run
    sweep = {"name": "web_map", "dimensions": [
        {"points": 21, "sweep": [{"channel": "Vg", "start": -0.5, "stop": 0.5}], "record": ["I"]},
        {"points": 9, "sweep": [{"channel": "Vb", "start": -0.2, "stop": 0.2}], "record": ["T"]}],
        "pre": {"hook": "log('hi')"}}
    f = tmp_path / "web_map.json"
    f.write_text(json.dumps(sweep))
    page.click(".tab:has-text('Sweep editor')")
    with page.expect_file_chooser() as fc:
        page.click(".panel.sweep button:has-text('Load…')")
    fc.value.set_files(str(f))
    page.wait_for_timeout(300)
    page.click("button:has-text('Add & start')")
    page.wait_for_function("document.querySelector('.panel.sweep > .msg').textContent.includes('admin')")
    page.click(".subtab:has-text('Pre-sweep')")
    page.fill(".phase textarea.code >> nth=0", "")
    page.click("button:has-text('Add & start')")
    page.wait_for_function("document.querySelector('.panel.sweep > .msg').textContent.includes('queued')")
    page.wait_for_function("document.querySelector('.dv-title').textContent.includes('finished')", timeout=60000)
    page.wait_for_timeout(600)
    assert "189/189" in page.text_content(".dv-title")
    assert canvas_has_image(page, ".dataview .plot-canvas") > 20        # a real heat map
    # history -> open data in a new tab
    page.click(".tab:has-text('Queue')")
    page.click(".queue table.grid >> nth=1 >> td:has-text('web_map')")
    page.click(".queue button:has-text('Open data')")
    page.wait_for_selector(".tab:has-text('.emj')")
    page.wait_for_function("[...document.querySelectorAll('.dv-title')].some(e => e.textContent.includes('web_map') && e.offsetParent)",
                           timeout=20000)
    page.wait_for_timeout(500)
    # viewer in a second browser: everything that changes something is disabled
    v = open_page(browser, gw, "vic", errors)
    assert v.is_disabled(".man-row >> input.man-num >> nth=0")
    assert v.is_disabled("[data-cmd='queue_start'] >> nth=0")
    assert v.is_enabled("[data-cmd='read_channel'] >> nth=0")
    v.click("button:has-text('Log out')")
    v.wait_for_selector("#login-user", state="visible")
    assert errors == [], errors


def test_web_settings_combos_and_mode_switch(browser, gw, demo):
    """Instrument settings in the web manual panel: drop-downs for allowed values,
    and the panel follows a mode switch (renamed / read-only channels)."""
    demo.add_instrument("smu", "keithley2400", "SIM::webk24")
    demo.add_instrument("hp", "hp34401a", "SIM::webhp")
    errors = []
    page = open_page(browser, gw, "olga", errors)
    row = page.locator(".man-row", has_text="smu.mode").first
    sel = row.locator("select")
    page.wait_for_function("""() => { const r = [...document.querySelectorAll('.man-row')]
        .find(e => e.textContent.includes('smu.mode'));
        return r && r.querySelector('select').selectedOptions[0].text === 'Voltage'; }""", timeout=10000)
    assert sel.locator("option").all_text_contents()[1:] == ["Voltage", "Current"]
    assert page.locator(".man-row", has_text="smu.Icompliance").count() == 1
    sel.select_option(label="Current")
    page.wait_for_selector(".man-row:has-text('smu.Vcompliance')", timeout=10000)
    assert page.locator(".man-row", has_text="smu.Icompliance").count() == 0
    assert demo.read_channel("smu.mode") == "Current"
    # numeric choices (34401A NPLC) show as a drop-down with units, values sent as numbers
    nplc = page.locator(".man-row", has_text="hp.nplc").first.locator("select")
    assert "0.2 PLC" in nplc.locator("option").all_text_contents()
    nplc.select_option(label="0.2 PLC")
    t0 = time.time()
    while demo.read_channel("hp.nplc") != 0.2 and time.time() - t0 < 10:
        time.sleep(0.1)
    assert demo.read_channel("hp.nplc") == 0.2
    # the DMM mode renames its reading channel; the setup table lists the new one
    page.locator(".man-row", has_text="hp.mode").first.locator("select").select_option(label="iac")
    page.wait_for_selector(".man-row:has-text('hp.ac_filter')", timeout=10000)
    page.click(".tab:has-text('Instruments')")
    page.click("td:text-is('hp')")
    page.wait_for_selector("td:text-is('Iac')", timeout=10000)
    assert page.locator("td:text-is('Vdc')").count() == 0
    assert not errors, errors
    page.context.close()


def test_web_queue_controls_copy_and_point_actions(browser, gw, demo):
    errors = []
    page = open_page(browser, gw, "ada", errors)          # admin: may use actions (code)
    # idle, empty queue: no queue button can be pressed
    page.wait_for_function("document.querySelector('header [data-qcan=start]').disabled")
    assert page.is_disabled("header [data-qcan=abort]") and page.is_disabled("header [data-qcan=pause]")
    # dimension editor in point order; actions hidden until ticked
    page.click(".tab:has-text('Sweep editor')")
    dim = page.locator(".dim-editor").first
    legends = dim.locator("legend:visible").all_text_contents()
    assert legends[:2] == ["1. Set swept channels", "3. Record"]       # 2 = pre-sample delay
    assert dim.locator("fieldset.hook:visible").count() == 0
    dim.locator("label.check:has-text('pre-record') input").check()
    page.wait_for_timeout(100)
    vis = dim.locator("legend:visible").all_text_contents()
    assert "3. Pre-record action" in vis and "4. Record" in vis
    assert dim.locator("label:has-text('2. Pre-sample delay')").count() == 1
    dim.locator("fieldset.hook:visible textarea").fill("state['web'] = state.get('web', 0) + 1")
    # "Skip this point" is the first entry of every action's helper
    hook = dim.locator("fieldset.hook:visible")
    assert hook.locator("select option").all_text_contents()[:3] == [
        "Skip this point", "Skip this line", "Terminate the entire sweep"]
    hook.locator("select").select_option("Skip this point")
    hook.locator("button:has-text('+ action')").click()
    assert "skip_point()" in hook.locator("textarea").input_value()
    # queue a sweep (not started) and a script
    sweep = {"name": "webhooks", "dimensions": [
        {"points": 3, "sweep": [{"channel": "Vg", "start": 0, "stop": 0.1}], "record": ["I"],
         "hooks": {"pre_record": {"enabled": True, "code": "state['web'] = state.get('web', 0) + 1\n"
                                                         "if i == 1: skip_point()"}}}]}
    demo.queue_add({"kind": "sweep", "definition": sweep})
    demo.queue_add({"kind": "script", "code": "log('web script')", "name": "ws"})
    page.click(".tab:has-text('Queue')")
    page.wait_for_function("!document.querySelector('header [data-qcan=start]').disabled")
    assert page.is_disabled(".panel.queue [data-qcan=abort]")
    page.click(".panel.queue td:text-is('ws')")
    page.click(".panel.queue button:has-text('Copy to script editor')")
    assert page.input_value(".panel.queue textarea.code") == "log('web script')"
    page.click(".panel.queue td:text-is('webhooks')")
    page.click(".panel.queue button:has-text('Copy to sweep editor') >> nth=0")
    page.wait_for_timeout(200)
    dim = page.locator(".dim-editor").first
    assert dim.locator("label.check:has-text('pre-record') input").is_checked()
    # start: runs, then the queue goes back to idle by itself
    demo.scripts.state.clear()
    page.click("header [data-qcan=start]")
    assert demo.queue.wait_idle(30)
    page.wait_for_function("document.querySelector('header [data-qcan=start]').disabled", timeout=10000)
    assert demo.scripts.state.get("web") == 3
    # the live view counts the skipped point as done; its value is NaN
    page.wait_for_function("document.querySelector('.dv-title').textContent.includes('3/3')", timeout=10000)
    item = next(x for x in demo.queue_state()["history"] if x.get("name") == "webhooks")
    from easymeasure.backend.storage import DataSet
    assert np.isnan(DataSet.load(item["path"]).data["I"][1])
    assert demo.queue_state()["state"] == "idle"
    # instrument channels: last value and allowed values columns
    page.click(".tab:has-text('Instruments')")
    page.click("td:text-is('lockin')")
    hdr = page.locator(".panel.setup th").all_text_contents()
    assert "Last value" in hdr and "Allowed values" in hdr
    page.click("td:text-is('sensitivity')")
    page.click("button:has-text('Get') >> nth=0")
    page.wait_for_selector("tr:has(td:text-is('sensitivity')) td:text-is('1e-3')", timeout=10000)
    assert not errors, errors
    page.context.close()


def test_web_composite_editor_and_manual_groups(browser, gw, demo):
    """The web composite editor (types, variables table, Check), the composites
    table and the manual panel's hold + Set all for multivariate composites."""
    import math
    errors = []
    page = open_page(browser, gw, "ada", errors)
    page.click(".tab:has-text('Instruments')")
    page.wait_for_selector("td:text-is('Simple mapping')", timeout=10000)
    assert page.locator("td:text-is('Multivariate write')").count() == 1
    assert page.locator("td:text-is('Composite read')").count() == 1
    comp_group = page.locator(".group", has_text="Composites").last
    # a mapping that is not one-to-one: Check says where
    comp_group.locator("button:text-is('Add…')").click()
    dlg = page.locator("dialog.modal")
    dlg.locator(".comp-name").fill("bad")
    dlg.locator(".comp-type").select_option("map")
    assert dlg.locator(".comp-forward").is_visible() and not dlg.locator(".comp-targets").is_visible()
    dlg.locator(".comp-target").fill("Vb")
    dlg.locator(".comp-forward").fill("x**2")
    dlg.locator(".comp-backward").fill("sqrt(y)")
    dlg.locator(".comp-min").fill("-1")
    dlg.locator(".comp-max").fill("1")
    dlg.locator(".comp-check").click()
    page.wait_for_function("document.querySelector('.comp-info').textContent.includes('not the inverse')",
                           timeout=10000)
    # OK with it: the backend refuses, the dialog stays open with the message
    dlg.locator("button.primary").click()
    page.wait_for_timeout(500)
    # a two-variable write instead
    dlg.locator(".comp-type").select_option("multi")
    dlg.locator(".comp-name").fill("gates")
    rows = dlg.locator("tr.comp-var")
    for i, v in enumerate(("eps", "-0.4", "0.4", "V")):
        rows.nth(0).locator("input").nth(i).fill(v)
    dlg.locator(".comp-add-var").click()
    for i, v in enumerate(("U", "0", "0.5")):
        rows.nth(1).locator("input").nth(i).fill(v)
    dlg.locator(".comp-targets").fill("Vx = U + eps\nVy = U - eps")
    dlg.locator(".comp-check").click()
    page.wait_for_function("document.querySelector('.comp-info').textContent.includes('defines eps, U')",
                           timeout=10000)
    dlg.locator("button.primary").click()
    page.wait_for_selector("td:text-is('gates')", timeout=10000)
    c = next(c for c in demo.list_channels()["composites"] if c["name"] == "gates")
    assert [v["name"] for v in c["variables"]] == ["eps", "U"] and c["variables"][0]["min"] == -0.4
    assert "bad" not in [c["name"] for c in demo.list_channels()["composites"]]
    # manual panel: the group header, hold, Set all
    grp = page.locator(".man-group[data-composite='gates']")
    grp.wait_for(timeout=10000)
    grp.locator("input[type=checkbox]").check()
    for n, v in (("eps", "0.1"), ("U", "0.2")):
        e = page.locator(".man-row:has(.man-label:text-is('%s')) input.man-num" % n).first
        e.fill(v)
        e.press("Enter")
    page.wait_for_timeout(300)
    assert math.isnan(demo.channels.get("eps"))           # held, not sent
    grp.locator("button:text-is('Set all')").click()
    t0 = time.time()
    while demo.channels.last_value("U") != 0.2 and time.time() - t0 < 10:
        time.sleep(0.1)
    assert demo.channels.get("Vx") == pytest.approx(0.3) and demo.channels.get("Vy") == pytest.approx(0.1)
    assert not errors, errors
    page.context.close()
