# Writing plugins

A [plugin](../guide/plugins.md) is one Python file, or one folder with an
`__init__.py`, in `~/.easymeasure/plugins` (or a folder of the `plugin_paths`
setting). It defines one subclass of `Plugin`. After saving it, open **Plugins →
Manage plugins…**, click **Look for plugins again**, and enable it.

```python
import math
from easymeasure.plugins import Plugin, Channel, Action, ui


class Leakage(Plugin):
    TITLE = "Leakage watch"
    DESCRIPTION = "The gate leakage, its maximum, and a reset."
    OPTIONS = {"channel": "Ig", "interval": 1.0}

    def start(self):
        self.peak = 0.0
        self.ui.add_panel("main", "Leakage", ui.group(
            "Gate leakage",
            ui.seg7("now", digits=6, unit="nA"),
            ui.row(ui.label("max"), ui.label("", id="peak", bold=True)),
            ui.button("reset", "Reset maximum", on_click=self.reset)))
        self.provide([Channel("peak", "r", read=lambda: self.peak, unit="A")],
                     [Action("reset", self.reset)])
        self.every(self.options["interval"], self.check)

    def check(self):
        i = abs(self.backend.read(self.options["channel"]))
        if not math.isfinite(i):            # nothing measured (the output is off)
            self.ui.set("now", value="-----")
            return
        self.peak = max(self.peak, i)
        self.ui.set("now", value="%.3f" % (i * 1e9))
        self.ui.set("peak", text="%.3g A" % self.peak)

    def reset(self):
        self.peak = 0.0
```

This adds a *Leakage* panel, the channel `leakage.peak` and the action
`leakage.reset`. The plugin's name, `leakage`, is the name of its file unless `NAME`
says otherwise; it is also the name of its instrument.

## The class

| attribute | |
|---|---|
| `NAME` | identifier (default: the file's name): letters, digits, `_` |
| `TITLE`, `DESCRIPTION`, `VERSION` | shown in the Plugins dialog |
| `OPTIONS` | options and their defaults; the user changes them in the dialog, the plugin reads `self.options`, and is restarted when they change |
| `EVENTS` | backend events it wants in `on_backend_event`, e.g. `("sweep_point", "instrument_state")`, or `"*"` for everything but log records (name `"log"` to get those); the list is in [Python clients](clients.md#events) |

| method | runs |
|---|---|
| `start()` | when it is enabled: build the panels, provide channels, start timers. If it raises, the plugin is left off |
| `stop()` | when it is disabled. Panels, channels and timers are removed by the framework anyway |
| `on_event(widget, event, value)` | a widget without its own callback was used |
| `on_backend_event(kind, data)` | a backend event of a kind in `EVENTS` |

What the plugin can use:

| | |
|---|---|
| `self.backend.call(cmd, **args)` | any backend command a front end can send ([the command reference](../reference/index.md)), except managing plugins and shutting the backend down |
| `self.backend.read(name)`, `.write(name, value)`, `.action(name)` | short-hands: a channel, an alias, `instrument.channel` … |
| `self.backend.instruments(driver=None)` | the instruments (of one driver) |
| `self.every(seconds, fn)`, `self.after(seconds, fn)` | timers; they return an object with `cancel()` |
| `self.provide(channels, actions)` | its channels and actions (again: the new lists replace the old) |
| `self.ui` | its panels and tabs (below) |
| `self.options`, `self.log`, `self.name` | |

## One thread, one thing at a time

Everything of a plugin runs on its own thread, one call after the other: `start`,
`stop`, timers, widget callbacks, backend events, and the reads and writes of its
channels. So a plugin needs no locks. A slow call delays the rest of the plugin, but
nothing else:

* a timer is planned again when it has finished, so a slow one never piles up;
* a channel read waits up to 10 s for the plugin's thread, then fails like an
  instrument that does not answer.

Exceptions are caught, logged and shown in the Plugins dialog, and the plugin goes
on. A plugin that never returns is cut off when it is disabled. From then on, every
`self.backend` call raises `PluginStopped`, which also ends a loop that uses the
backend.

A channel of the plugin must not read the plugin's own channels through the backend.
The plugin's instrument is busy with that very read, so the call would wait for
itself; the backend refuses it at once. Use the plugin's own variables instead.

## Panels and tabs

`self.ui.add_panel(id, title, root)` adds a panel beside the Instruments, Sweep
editor and Queue tabs. `self.ui.add_tab(id, title, root)` adds a tab beside Live
data. `self.ui.remove(id)` takes one away again.

A panel is a tree of widgets, described as data, so the desktop GUI and the web app
draw the same panel, also when they connect later. Widgets that change, or that the
user operates, have an `id`. Their properties change with
`self.ui.set(id, prop=value, …)`. The changes reach the front ends together, a few
times a second.

| widget | properties | events |
|---|---|---|
| `row(*children)`, `column(*children)` | `gap`, `align` (`start`, `center`, `end`, `stretch`) | |
| `group(title, *children, dark=False)` | `title`; `dark`: the dark face of a front panel | |
| `spacer(size=0)` | empty space; 0 takes what is left | |
| `label(text)` | `text`, `size` (`small`, `normal`, `large`), `bold`, `color` | |
| `seg7(id, value, digits, size)` | seven-segment digits: `value` (text: digits, `-`, `.`, and letters a display can show), `digits`, `color`, `size` (pixels), `unit`, `label` | |
| `led(id, label, on)` | `on`, `color`, `label` | |
| `ledlist(id, options, selected)` | one LED per option; `selected`: an index, a list of indices, or `None`; `orient` (`vertical`, `horizontal`), `label` | |
| `bar(id, value, min, max)` | a bar graph, from 0 (the centre when `min` < 0) to `value` | |
| `plot(id, title, xlabel, ylabel, series)` | curves: `series` = `[{"name", "x": [...], "y": [...]}]`; set it again to update | |
| `button(id, text, on_click=…)` | `text`, `label` (a caption above), `enabled`, `color` | `click` |
| `knob(id, label, on_turn=…)` | a knob without end stops: `on_turn(steps)` with ±1 per step | `turn` |
| `select(id, options, value, on_change=…)` | `options`, `value`, `label`, `enabled` | `change` |
| `number(id, value, on_change=…)` | `value`, `label`, `unit`, `min`, `max`, `step`, `decimals`, `enabled` | `change` |

Every widget also has `visible`. Colours are `red`, `green`, `amber`, `blue`,
`white`, `yellow` or `#rrggbb`.

Several `ledlist`s side by side show a value in parts, as an instrument does. The
built-in SR830 panel shows a sensitivity as mantissa (1, 2, 5) · decade (×1, ×10,
×100) · unit (nV, µV, mV, V). Its source,
`easymeasure/plugins/builtin/sr830_panel.py`, is a complete example: a front panel
operating an instrument through the backend, with channels, an action, a knob, and
a data tab.

## Channels and actions

```python
self.provide(
    [Channel("ratio", "r", read=self.ratio, unit=""),
     Channel("mode", "rw", read=lambda: self.mode, write=self.set_mode, dtype="str",
             allowed=["fast", "slow"], config=True)],
    [Action("calibrate", self.calibrate, "measure the offset", timeout=60)])
```

`Channel(name, access, read=…, write=…, dtype="float", unit, description, allowed,
config)` is like a driver's `ChannelSpec`, with the functions that read and write it.
`Action(name, run, description, timeout)` is a button in the Manual panel. A user
action, or a point action of a sweep, can run it too.

They appear as the instrument `<plugin name>`, which the user cannot remove or change,
and which is not saved with the setup. Its channels are read and written on the
plugin's thread. An error there fails that call only.

## Testing a plugin

Test a plugin against simulated instruments, without the GUI:

```python
from easymeasure.backend.service import Backend

be = Backend(home="test_home")             # test_home/plugins/leakage.py
be.add_instrument("smu", "keithley2400", "SIM::smu")
be.write_channel("smu.voltage", 0.5)
be.write_channel("smu.output", "ON")
be.define_alias(name="Ig", target="smu.current")
be.enable_plugin("leakage")
be.plugin_event("leakage", "reset", "click")   # what a click on the button sends
print(be.read_channel("leakage.peak"))
be.close()
```
