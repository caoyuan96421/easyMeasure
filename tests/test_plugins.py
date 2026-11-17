"""Plugins: discovery, the life of a plugin on its own thread, what it can and cannot
do to the backend, and that nothing it does wrong reaches the backend."""

import textwrap
import threading
import time

import pytest

from easymeasure.backend import plugins as pm
from easymeasure.backend.service import Backend
from easymeasure.common.errors import ChannelError, EasyMeasureError, InstrumentError
from easymeasure.plugins import Action, Channel, Plugin, PluginStopped, ui
from easymeasure.plugins.ui import UIError, walk


def write_plugin(home, name, code):
    d = home / "plugins"
    d.mkdir(parents=True, exist_ok=True)
    (d / ("%s.py" % name)).write_text(textwrap.dedent(code))


def register(be, cls, name=None):
    """A plugin class from the test, as if it had been found."""
    name = name or cls.NAME or cls.__name__.lower()
    be.plugins.available[name] = pm.PluginInfo(name, cls, "test", "file")
    return name


def widget(be, plugin, wid):
    p = be.plugins.describe_one(plugin)
    for panel in p["panels"]:
        for w in walk([panel["root"]]):
            if w.get("id") == wid:
                return w["props"]
    raise KeyError(wid)


def until(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


class Counter(Plugin):
    NAME = "counter"
    TITLE = "Counter"
    OPTIONS = {"step": 1}
    EVENTS = ("changed",)

    def start(self):
        self.n = 0
        self.seen = []
        self.ticks = 0
        self.ui.add_panel("main", "Counter", ui.group(
            "Count", ui.seg7("n", "0", digits=4), ui.button("up", "UP", on_click=self.up),
            ui.knob("k"), ui.select("pick", ["a", "b"], on_change=self.pick)))
        self.ui.add_tab("trace", "Counter trace", ui.plot("p"))
        self.provide([Channel("n", "rw", read=lambda: self.n, write=self.set),
                      Channel("self", "r", read=self.read_self)],
                     [Action("reset", self.reset)])
        self.every(0.02, self.tick)

    def tick(self):
        self.ticks += 1

    def up(self):
        self.set(self.n + self.options["step"])

    def set(self, v):
        self.n = int(v)
        self.ui.set("n", value=str(self.n))

    def reset(self):
        self.set(0)

    def pick(self, v):
        self.seen.append(("pick", v))

    def on_event(self, widget, event, value):
        self.seen.append((widget, event, value))
        if event == "turn":
            self.set(self.n + value)

    def on_backend_event(self, kind, data):
        self.seen.append(("backend", kind))

    def read_self(self):
        # its own channel through the backend would wait for itself: refused at once
        return self.backend.read("counter.n") + 100


# ------------------------------------------------------------------ discovery --
def test_plugins_are_found_and_broken_ones_reported(tmp_path, backend):
    be = backend
    write_plugin(be.home, "hello", """
        from easymeasure.plugins import Plugin
        class Hello(Plugin):
            TITLE = "Hello"
            DESCRIPTION = "says hello"
            API = "1.0"
    """)
    write_plugin(be.home, "broken", "import nothing_here_at_all\n")
    write_plugin(be.home, "twice", """
        from easymeasure.plugins import Plugin
        class A(Plugin): API = "1.0"
        class B(Plugin): API = "1.0"
    """)
    write_plugin(be.home, "notaplugin", "X = 1\n")
    out = be.rescan_plugins()
    names = {p["name"]: p for p in out["plugins"]}
    assert "sr830_panel" in names and names["sr830_panel"]["builtin"]
    assert names["hello"]["title"] == "Hello" and names["hello"]["state"] == "off"
    assert not names["hello"]["builtin"]
    assert any(k.endswith("broken.py") for k in out["errors"])
    assert any(k.endswith("twice.py") and "several" in v for k, v in out["errors"].items())
    assert "notaplugin" not in names
    assert str(be.home / "plugins") in out["folders"]


# ------------------------------------------------------------------ lifecycle --
def test_enable_operate_disable(backend):
    be = backend
    events = []
    be.add_listener(lambda k, d: events.append((k, d)))
    register(be, Counter)
    d = be.enable_plugin("counter")
    assert d["state"] == "running" and d["enabled"] and d["instrument"] == "counter"
    assert [p["where"] for p in d["panels"]] == ["control", "data"]
    host = be.plugins.hosts["counter"]
    assert host.thread.name == "plugin-counter" and host.thread is not threading.current_thread()
    # widgets: a callback, the plugin's on_event, a value
    be.plugin_event("counter", "up", "click")
    be.plugin_event("counter", "k", "turn", 3)
    be.plugin_event("counter", "pick", "change", "b")
    assert until(lambda: widget(be, "counter", "n")["value"] == "4")
    assert ("k", "turn", 3) in host.plugin.seen and ("pick", "b") in host.plugin.seen
    assert until(lambda: any(k == "plugin_ui" and d["plugin"] == "counter" for k, d in events))
    with pytest.raises(ChannelError, match="no widget"):
        be.plugin_event("counter", "nope", "click")
    with pytest.raises(ChannelError):
        be.plugin_event("counter", "n", "click")          # a display has no click
    # its channels and action, like any instrument's
    assert be.read_channel("counter.n") == 4
    be.write_channel("counter.n", 7)
    t = time.time()
    with pytest.raises(InstrumentError, match="waits for that very channel"):
        be.read_channel("counter.self")
    assert time.time() - t < 1.0 and be.instruments.get("counter").state == "connected"
    be.write_channel("counter.reset")
    assert be.read_channel("counter.n") == 0
    inst = {i["name"]: i for i in be.list_instruments()}
    assert inst["counter"]["plugin"] == "counter"
    # timers run on the plugin's thread
    assert until(lambda: host.plugin.ticks > 5)
    # disabled: everything of it is gone at once
    d = be.disable_plugin("counter")
    assert d["state"] == "off" and not d["enabled"] and d["panels"] == []
    assert "counter" not in be.instruments.names()
    assert not host.thread.is_alive() or until(lambda: not host.thread.is_alive())
    t = host.plugin.ticks
    time.sleep(0.1)
    assert host.plugin.ticks == t                        # no timer after it
    with pytest.raises(PluginStopped):
        host.backend.read("counter.n")
    with pytest.raises(ChannelError, match="not enabled"):
        be.plugin_event("counter", "up", "click")
    assert any(k == "plugins" for k, _ in events)


def test_plugin_instrument_is_not_the_users(backend):
    be = backend
    register(be, Counter)
    be.enable_plugin("counter")
    assert all(i["name"] != "counter" for i in be.get_setup()["instruments"])
    with pytest.raises(ChannelError, match="provided by plugin counter"):
        be.remove_instrument("counter")
    with pytest.raises(ChannelError, match="provided by plugin counter"):
        be.update_instrument("counter", options={})
    # loading a setup (replace) leaves it with the plugin
    be.load_setup(setup=be.get_setup())
    assert be.read_channel("counter.n") == 0
    # an alias of a plugin channel goes on existing when the plugin is off
    be.define_alias(name="cnt", target="counter.n")
    be.disable_plugin("counter")
    with pytest.raises(Exception):
        be.read_channel("cnt")
    be.enable_plugin("counter")
    assert be.read_channel("cnt") == 0


def test_enabled_plugins_and_options_come_back(tmp_path):
    home = tmp_path / "h"
    write_plugin(home, "remembered", """
        from easymeasure.plugins import Plugin, Channel
        class R(Plugin):
            API = "1.0"
            OPTIONS = {"value": 1.0}
            def start(self):
                self.provide([Channel("v", "r", read=lambda: self.options["value"])])
    """)
    be = Backend(home=home, log_to_console=False, settings={"folders_confirmed": True})
    try:
        be.enable_plugin("remembered")
        be.set_plugin_options("remembered", {"value": 2.5})     # restarted with them
        assert be.read_channel("remembered.v") == 2.5
        with pytest.raises(ChannelError, match="no option"):
            be.set_plugin_options("remembered", {"nope": 1})
    finally:
        be.close()
    be = Backend(home=home, log_to_console=False)
    try:
        assert be.list_plugins()["plugins"][0]["enabled"]
        be.plugins.start_enabled()                     # what the backend does at start
        assert be.read_channel("remembered.v") == 2.5
        be.disable_plugin("remembered")
    finally:
        be.close()
    be = Backend(home=home, log_to_console=False)
    try:
        be.plugins.start_enabled()
        assert "remembered" not in be.instruments.names()
    finally:
        be.close()


def test_backend_events_reach_the_plugin(backend):
    be = backend
    register(be, Counter)
    be.enable_plugin("counter")
    host = be.plugins.hosts["counter"]
    be.define_parameter(name="g", value=1.0)             # a "changed" event
    assert until(lambda: ("backend", "changed") in host.plugin.seen)
    assert not any(s == ("backend", "monitor") for s in host.plugin.seen)


# ---------------------------------------------------------- nothing escapes --
class StartFails(Plugin):
    NAME = "startfails"

    def start(self):
        self.ui.add_panel("p", "P", ui.label("x"))
        self.provide([Channel("x", "r", read=lambda: 1)])
        raise RuntimeError("cannot start")


class Faulty(Plugin):
    NAME = "faulty"

    def start(self):
        self.good = 0
        self.ui.add_panel("p", "P", ui.row(ui.button("exit", "exit", on_click=self.exit),
                                           ui.button("boom", "boom", on_click=self.boom)))
        self.every(0.02, self.bad_timer)
        self.every(0.02, self.good_timer)
        self.provide([Channel("x", "r", read=self.boom)])

    def bad_timer(self):
        raise ValueError("timer bug")

    def good_timer(self):
        self.good += 1

    def boom(self):
        raise ZeroDivisionError("boom")

    def exit(self):
        raise SystemExit("plugin tried to exit")


class Stuck(Plugin):
    NAME = "stuck"
    LOOPS = []

    def start(self):
        self.ui.add_panel("p", "P", ui.button("go", "go", on_click=self.forever))
        self.provide([Channel("x", "r", read=lambda: 1.0)])

    def forever(self):
        Stuck.LOOPS.append("in")
        while True:                         # an un-cooperative loop, using the backend
            self.backend.call("ping")
            time.sleep(0.01)

    def stop(self):
        time.sleep(60)                      # and it does not stop either


def test_a_plugin_that_fails_to_start_is_left_off(backend):
    be = backend
    register(be, StartFails)
    with pytest.raises(EasyMeasureError, match="cannot start"):
        be.enable_plugin("startfails")
    d = be.plugins.describe_one("startfails")
    assert d["state"] == "error" and "cannot start" in d["last_error"] and d["panels"] == []
    assert "startfails" not in be.instruments.names()
    assert be.ping()["pong"]


def test_exceptions_of_a_plugin_stay_with_it(backend):
    be = backend
    register(be, Faulty)
    be.enable_plugin("faulty")
    host = be.plugins.hosts["faulty"]
    assert until(lambda: host.errors > 3 and host.plugin.good > 3)
    be.plugin_event("faulty", "exit", "click")          # SystemExit: contained
    be.plugin_event("faulty", "boom", "click")
    assert until(lambda: any("boom" in e for e in host.recent_errors))
    assert any("SystemExit" in e for e in host.recent_errors)
    with pytest.raises(InstrumentError, match="boom"):
        be.read_channel("faulty.x")
    assert be.instruments.get("faulty").state == "connected"   # the call failed, not it
    g = host.plugin.good
    assert until(lambda: host.plugin.good > g + 3)       # it goes on
    d = be.plugins.describe_one("faulty")
    assert d["state"] == "running" and d["errors"] > 3
    assert be.ping()["pong"]
    be.disable_plugin("faulty")


def test_a_stuck_plugin_is_disabled_at_once_and_cut_off(backend, monkeypatch):
    be = backend
    monkeypatch.setattr(pm, "STOP_TIMEOUT", 0.5)
    monkeypatch.setattr(pm, "CALL_TIMEOUT", 0.5)
    register(be, Stuck)
    be.enable_plugin("stuck")
    host = be.plugins.hosts["stuck"]
    be.plugin_event("stuck", "go", "click")
    assert until(lambda: Stuck.LOOPS)
    # the backend does not notice: other commands, other plugins
    t = time.time()
    be.ping()
    be.list_instruments()
    assert time.time() - t < 0.5
    with pytest.raises(InstrumentError, match="did not answer"):
        be.read_channel("stuck.x")                       # its thread is busy forever
    t = time.time()
    d = be.disable_plugin("stuck")
    assert time.time() - t < 3.0
    assert d["state"] == "off" and d["panels"] == []
    assert "stuck" not in be.instruments.names()
    assert host.backend.revoked
    # the loop used the backend, so it ended with PluginStopped (the thread is free)
    assert until(lambda: not host.thread.is_alive() or host._stop.is_set())
    # enabled again: a fresh start
    be.enable_plugin("stuck")
    assert be.read_channel("stuck.x") == 1.0
    monkeypatch.setattr(pm, "STOP_TIMEOUT", 0.2)
    be.disable_plugin("stuck")


def test_a_plugin_cannot_manage_plugins_or_stop_the_backend(backend):
    be = backend

    class Greedy(Plugin):
        NAME = "greedy"

        def start(self):
            self.results = []
            for cmd in ("enable_plugin", "disable_plugin", "shutdown"):
                try:
                    self.backend.call(cmd, name="greedy")
                except EasyMeasureError as exc:
                    self.results.append(str(exc))

    register(be, Greedy)
    be.enable_plugin("greedy")
    r = be.plugins.hosts["greedy"].plugin.results
    assert len(r) == 3 and all("cannot use" in x for x in r)
    be.disable_plugin("greedy")


def test_ui_checks():
    changed, updated = [], []
    u = __import__("easymeasure.plugins.ui", fromlist=["PluginUI"]).PluginUI(
        lambda: changed.append(1), lambda w, p: updated.append((w, p)))
    with pytest.raises(UIError, match="twice"):
        u.add_panel("a", "A", ui.row(ui.led("x"), ui.led("x")))
    with pytest.raises(UIError, match="needs an id"):
        u.add_panel("a", "A", ui.row({"type": "button", "id": None, "props": {}}))
    with pytest.raises(UIError, match="not a widget"):
        u.add_panel("a", "A", {"type": "spaceship"})
    u.add_panel("a", "A", ui.column(ui.seg7("s"), ui.ledlist("l", ["1", "2"])))
    with pytest.raises(UIError, match="exists already"):
        u.add_panel("a", "A", ui.label("x"))
    with pytest.raises(UIError, match="no property"):
        u.set("s", selected=1)
    u.set("s", value="1.23")
    u.set("s", value="1.23")                       # unchanged: not sent again
    u.set("l", selected=[0, 1])
    import numpy as np
    u.set("s", value=np.float64(float("nan")))     # not finite: None
    assert updated == [("s", {"value": "1.23"}), ("l", {"selected": [0, 1]}),
                       ("s", {"value": None})]
    d = u.describe()
    assert d[0]["root"]["children"][0]["props"]["value"] is None
    assert d[0]["root"]["children"][1]["events"] == []
    u.remove("a")
    assert u.describe() == [] and len(changed) == 2


# ------------------------------------------------------------ the SR830 panel --
def test_sr830_panel_plugin(backend):
    from easymeasure.plugins.builtin.sr830_panel import parse_si, radix, seg_scaled
    assert radix("500 µV", [1, 2, 5], ["nV", "µV", "mV", "V"]) == (2, 2, 1)
    assert radix("30 ks", [1, 3], ["µs", "ms", "s", "ks"]) == (1, 1, 3)
    assert radix("1 V", [1, 2, 5], ["nV", "µV", "mV", "V"]) == (0, 0, 3)
    assert radix("junk", [1], ["V"]) == (None, None, None)
    assert parse_si("2 nV") == (2.0, "n", "V")
    assert seg_scaled(-0.000123456, 500e-6, 1e-6) == "-123.46"
    be = backend
    be.add_instrument("li", "sr830", "SIM::plli")
    be.write_channel("li.sensitivity", "500 µV")
    be.write_channel("li.time_constant", "300 ms")
    be.enable_plugin("sr830_panel")
    w = lambda wid: widget(be, "sr830_panel", wid)            # noqa: E731
    assert until(lambda: w("sens_m")["selected"] == 2)
    assert (w("sens_d")["selected"], w("sens_u")["selected"]) == (2, 1)
    assert (w("tc_m")["selected"], w("tc_d")["selected"], w("tc_u")["selected"]) == (1, 2, 1)
    assert w("which")["options"] == ["li"] and w("which")["value"] == "li"
    assert until(lambda: w("ch1")["value"] not in ("", None))
    # the keys operate the instrument
    be.plugin_event("sr830_panel", "sens_dn", "click")
    assert until(lambda: be.read_channel("li.sensitivity") == "200 µV")
    assert until(lambda: w("sens_m")["selected"] == 1)
    be.plugin_event("sr830_panel", "k_slope", "click")
    assert until(lambda: be.read_channel("li.rolloff") != "12 dB/oct")
    be.plugin_event("sr830_panel", "knob", "turn", -5)
    assert until(lambda: abs(be.read_channel("li.phase") + 5.0) < 1e-6)
    be.plugin_event("sr830_panel", "k_freq", "click")
    assert until(lambda: w("ref_sel")["selected"] == 1 and w("ref_unit")["text"].endswith("Hz"))
    # its channels and its action
    be.write_channel("sr830_panel.ch1_display", "R")
    assert be.read_channel("sr830_panel.ch1") == pytest.approx(be.read_channel("li.R"), rel=0.2)
    assert until(lambda: w("ch1_sel")["selected"] == 1)
    be.write_channel("sr830_panel.auto_sensitivity")
    r, sens = be.read_channel("li.R"), parse_si(be.read_channel("li.sensitivity"))
    from easymeasure.plugins.builtin.sr830_panel import PREFIX
    fs = sens[0] * PREFIX[sens[1]]
    assert 0.1 * fs <= r <= 0.8 * fs
    assert be.read_channel("sr830_panel.overload") == "NO"
    # the trace tab fills
    assert until(lambda: len((w("plot")["series"] or [{}])[0].get("x") or []) > 2)
    # without its instrument the panel says so
    be.disable_plugin("sr830_panel")
    be.remove_instrument("li")
    be.enable_plugin("sr830_panel")
    assert "no SR830" in w("status")["text"]
    be.plugin_event("sr830_panel", "sens_up", "click")       # nothing to operate: no error
    time.sleep(0.2)
    assert be.plugins.describe_one("sr830_panel")["errors"] == 0
    be.disable_plugin("sr830_panel")


# ------------------------------------------------------------------ versions --
def test_plugin_api_versions(backend):
    from easymeasure.plugins.api import API_VERSION, api_problem
    major, minor = map(int, API_VERSION.split("."))
    assert api_problem(API_VERSION) == ""
    assert api_problem("%d.0" % major) == ""                     # an older minor: fine
    assert "update easyMeasure" in api_problem("%d.%d" % (major, minor + 1))
    assert "must be updated" in api_problem("%d.0" % (major + 1))
    assert "must be updated" in api_problem("%d.0" % (major - 1))
    assert "does not say" in api_problem("")
    assert "not a version" in api_problem("1")
    be = backend
    for name, api in (("noapi", None), ("future", "%d.%d" % (major, minor + 1)),
                      ("oldmajor", "%d.0" % (major - 1)), ("current", API_VERSION)):
        write_plugin(be.home, name, """
            from easymeasure.plugins import Plugin
            class P(Plugin):
                %s
        """ % ("API = %r" % api if api else "pass"))
    out = be.rescan_plugins()
    names = [p["name"] for p in out["plugins"]]
    assert "current" in names and not {"noapi", "future", "oldmajor"} & set(names)
    err = {k.rsplit("/", 1)[-1]: v for k, v in out["errors"].items()}
    assert "set API" in err["noapi.py"]
    assert "update easyMeasure" in err["future.py"]
    assert "must be updated" in err["oldmajor.py"]
    assert out["api"] == API_VERSION
    cur = [p for p in out["plugins"] if p["name"] == "current"][0]
    assert cur["api"] == API_VERSION and cur["kind"] == "file"
    sr = [p for p in out["plugins"] if p["name"] == "sr830_panel"][0]
    assert sr["kind"] == "builtin" and sr["api"] == API_VERSION
    with pytest.raises(ChannelError, match="no plugin"):
        be.enable_plugin("future")


def test_installed_plugin_packages(backend, tmp_path, monkeypatch):
    """A pip-installed package names its plugins under the entry point group
    easymeasure.plugins: a Plugin class, or a module defining one."""
    from easymeasure.plugins.api import API_VERSION
    site = tmp_path / "site"
    pkg = site / "emplug_demo"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(textwrap.dedent("""
        from easymeasure.plugins import Plugin, Channel
        class Greeter(Plugin):
            TITLE = "Greeter"
            API = %r
            def start(self):
                self.provide([Channel("hello", "r", read=lambda: 42.0)])
        class Old(Plugin):
            API = "0.1"
    """ % API_VERSION))
    (pkg / "more.py").write_text(textwrap.dedent("""
        from easymeasure.plugins import Plugin
        class Second(Plugin):
            NAME = "second"
            API = %r
    """ % API_VERSION))
    dist = site / "emplug_demo-2.3.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text("Metadata-Version: 2.1\nName: emplug-demo\nVersion: 2.3\n")
    (dist / "entry_points.txt").write_text(textwrap.dedent("""
        [easymeasure.plugins]
        greeter = emplug_demo:Greeter
        second = emplug_demo.more
        old = emplug_demo:Old
        broken = emplug_demo.nothing_here:X
        notaplugin = emplug_demo:Channel
    """))
    monkeypatch.syspath_prepend(str(site))
    be = backend
    out = be.rescan_plugins()
    ps = {p["name"]: p for p in out["plugins"]}
    assert ps["greeter"]["kind"] == "package" and "emplug-demo 2.3" in ps["greeter"]["source"]
    assert ps["greeter"]["title"] == "Greeter" and "second" in ps
    errs = "\n".join("%s: %s" % kv for kv in out["errors"].items())
    assert "plugin old is not loaded" in errs and "0.1" in errs
    assert "nothing_here" in errs
    assert "neither a Plugin class nor a module" in errs
    be.enable_plugin("greeter")
    assert be.read_channel("greeter.hello") == 42.0
    be.disable_plugin("greeter")
