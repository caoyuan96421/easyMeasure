# Setup file

A setup contains:

* the instruments;
* the parameters;
* the aliases;
* the composite channels;
* the monitor list;
* the user actions.

It is saved as JSON (`save_setup`), loaded with `load_setup` or
`python -m easymeasure.backend --setup file.json`, and autosaved on every change to
`<home>/autosave_setup.json` (restored by `--restore`).

```json
{
  "format": "easyMeasure-setup-1",
  "instruments": [
    {"name": "src", "driver": "sim_source", "address": "SIM::GPIB0", "options": {"gpib_addr": 1}},
    {"name": "smu", "driver": "keithley2400", "address": "GPIB0::24::INSTR",
     "options": {"call_timeout": 20, "verify_writes": true}}
  ],
  "aliases": [
    {"name": "Vg", "target": "src.V1", "min": -2, "max": 2, "ramp_rate": 2.0, "unit": "V",
     "description": "gate"},
    {"name": "I", "target": "dmm.reading", "unit": "nA"}
  ],
  "composites": [
    {"type": "multi", "name": "gates",
     "variables": [{"name": "eps", "min": -0.4, "max": 0.4, "unit": "V", "ramp_rate": 0.5},
                   {"name": "U", "min": 0, "max": 0.5, "unit": "V"}],
     "targets": {"Vx": "U + eps", "Vy": "U - eps"}},
    {"type": "read", "name": "Vdiff", "expression": "(Vx - Vy)/2", "unit": "V"},
    {"type": "map", "name": "Tset_C", "target": "Tset", "forward": "x + 273.15",
     "backward": "y - 273.15", "min": -273.15, "max": 26.85, "unit": "°C"}
  ],
  "monitor": [{"channel": "T", "interval": 1.0}, {"channel": "I", "interval": 0.5}],
  "actions": [
    {"name": "gates to zero", "code": "set_many({'Vx': 0, 'Vy': 0})",
     "description": "all gates to 0 V", "confirm": false}
  ]
}
```

## Sections

| key | entries |
|---|---|
| `format` | always `"easyMeasure-setup-1"` |
| `instruments` | `{name, driver, address, options}`; see each [driver](drivers/index.md) for its options |
| `parameters` | `{name, value \| formula, unit, description}`; see [Parameters](../guide/parameters.md) |
| `aliases` | `{name, target, min, max, ramp_rate, ramp_step, scale, offset, unit, allowed, read_only, description}`; see [Aliases](../guide/aliases.md#the-fields) |
| `composites` | `{type: multi \| read \| map, …}`; see [Composite channels](../guide/composites.md#reference) |
| `monitor` | `{channel, interval}` |
| `actions` | `{name, code, description, confirm}`; see [User actions](../guide/user-actions.md) |

## Loading

`load_setup` returns a list of problems. An empty list means everything loaded:

* instruments that cannot connect are added anyway and reported;
* parameters are loaded together, whatever their order; one that is wrong (a circle,
  an unknown name) is reported and left out;
* aliases whose instrument is missing are reported;
* composites are loaded in dependency order, whatever their order in the file;
* a user action whose code does not compile is reported.

With `replace=False`, the setup is added to what is there instead of replacing it.

A composite needs its `type`. The variables of a `multi` composite are channels named
after it: above, `gates.eps` and `gates.U`. Setups written before this naming used the
bare variable names in sweeps, scripts and other formulas; rename them there
(`eps` → `gates.eps`).
