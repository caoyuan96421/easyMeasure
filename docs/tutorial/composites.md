# Composite channels

Composite channels are software channels built on aliases, and on each other.
There are three types:

| type | what it is | example |
|---|---|---|
| **multivariate write** (`multi`) | named variables; hardware channels are formulas of them | detuning ε and energy U driving two gates |
| **composite read** (`read`) | a read-only formula of other channels | resistance `V / I` |
| **simple mapping** (`map`) | a one-to-one view of one channel, with forward and backward formulas | a setpoint in °C on a controller in K |

In the GUI: *Instruments & channels → Composites → Add…*. The editor has a type
selector, a variables table and a **Check** button, which runs every check below
without defining anything.

## Multivariate write

Several variables, whose names you choose, drive several hardware channels:

```python
c.call("define_composite", type="multi", name="gates",
       variables=[{"name": "eps", "min": -0.4, "max": 0.4, "unit": "V", "ramp_rate": 0.5},
                  {"name": "U",   "min": 0,    "max": 0.5, "unit": "V"}],
       targets={"Vx": "U + eps", "Vy": "U - eps"})
```

Each variable (`eps`, `U`) is now a channel of its own, with its own minimum,
maximum, unit and ramp rate:

```python
c.call("read_channel", name="eps")          # nan: never set yet
c.call("set_many", values={"eps": 0.1, "U": 0.3})
# {'Vx': 0.4, 'Vy': 0.2}   (the hardware values that were set)
c.call("read_channel", name="eps")          # 0.1: the value it was last set to
c.call("write_channel", name="U", value=0.1)  # eps stays 0.1 → Vx = 0.2, Vy = 0.0
```

Setting one variable recomputes only the targets that depend on it, using the
other variables' last values.

!!! note "The first setting"
    While a variable has never been set, the targets that need it cannot be
    computed, so setting `eps` alone fails with a message naming the missing `U`.
    Set the variables together the first time. In a script or from Python, use
    `set_many`. In a sweep, put them in the same dimension. In the Manual panel,
    tick **hold**, type the values, then click **Set all**.

### Limits and ramps

Everything is checked before anything is written: the variables' limits, and the
limits of every alias the targets end on. A value that would drive `Vx` beyond its
alias limit is refused, and no channel moves.

Ramps keep **every** channel within its own rate at the same time: the variable's
`ramp_rate` and each target alias' `ramp_rate`. This also holds along non-linear
relations: with `targets={"VL": "q**2"}`, moving `q` from −1 to 1 drives `VL`
1 → 0 → 1, at no point faster than `VL`'s rate.

### Formulas

Targets may use any [expression](../reference/expressions.md) of the variables,
including piecewise functions and vectors:

```python
c.call("define_composite", type="multi", name="drive",
       variables=[{"name": "amp", "min": 0, "max": 2}],
       targets={"wave": "amp*sin(linspace(0, 2*pi, 64))",       # a 64-point waveform
                "Vb":   "piecewise(amp < 1, 0.1, 0.3)",            # a step
                "Vg":   "interp(amp, [0, 1, 2], [0, 0.5, 0.6])"})   # a table
```

## Composite read

A read-only channel computed from others: aliases, variables, mappings or other reads:

```python
c.call("define_composite", type="read", name="Vdiff", expression="(Vx - Vy)/2", unit="V")
c.call("define_composite", type="read", name="R",
       expression="piecewise(abs(I) > 1e-12, Vg / I, nan)", unit="Ω")
c.call("define_composite", type="read", name="spec_dB", expression="10*log10(spectrum)")
```

Reads can be recorded in sweeps and monitored like any channel. Vector inputs
give vector results. A NaN input (e.g. a variable never set) gives NaN.

## Simple mapping

A one-to-one software view of one channel. `forward` gives the target `y` from
`x`, and `backward` gives `x` back from `y`:

```python
c.call("define_composite", type="map", name="Tset_C", target="Tset",
       forward="x + 273.15", backward="y - 273.15",
       min=-273.15, max=26.85, unit="°C")
c.call("write_channel", name="Tset_C", value=-73.15)   # Tset = 200 K
c.call("read_channel", name="Tset_C")                  # reads Tset, applies backward
```

Reading the mapping really reads the hardware, so a change made to the instrument directly
is seen.

### The inverse check

When a mapping is defined, `forward` and `backward` are checked to be each other's
inverse over the whole `[min, max]` range. The range is sampled densely (also
logarithmically for wide ranges) and checked within `tolerance`, which defaults to
1e-6 × range. The error names the worst point:

```python
c.call("check_composite", type="map", name="sq", target="Vb",
       forward="x**2", backward="sqrt(y)", min=-1, max=1)
# ChannelError: … at x = -1 forward gives y = 1 and backward gives 1 back …
```

Piecewise mappings are fine, as long as they are one-to-one:

```python
c.call("define_composite", type="map", name="Vg_nl", target="Vg",
       forward="piecewise(x < 0, 2*x, 0.5*x)",
       backward="y/2 if y < 0 else 2*y", min=-1, max=1)
```

A mapping may also have its own `ramp_rate` (in its units per second), and a
`tolerance` for formulas that are inverse only to some precision:

```python
c.call("define_composite", type="map", name="Vg_mV", target="Vg",
       forward="x/1000", backward="1000*y", min=-2000, max=2000, unit="mV",
       ramp_rate=500, tolerance=1e-6)
```

If the mapping's range reaches beyond the target alias' limits, the definition
returns a warning. Values beyond the alias limits are still refused when set.

## Nesting

Composites can drive each other. A mapping can target a variable, and a multivariate
write can target another composite's variables or a mapping:

```python
c.call("define_composite", type="map", name="eps_mV", target="eps",
       forward="x/1000", backward="1000*y", min=-400, max=400, unit="mV")
c.call("define_composite", type="multi", name="outer", variables=["s"],
       targets={"U": "0.2 + s", "eps_mV": "100*s"})
c.call("write_channel", name="s", value=0.1)
# U = 0.3, eps = 0.01 V → Vx = 0.31, Vy = 0.29
```

On every write:

* every hardware channel is computed once, even when it is reached through several paths;
* two requests in one write that give the same channel different values are refused;
* everything is limit-checked before anything is written;
* ramps keep every alias, variable and mapping within its own rate.

### Circular dependencies

Loops are refused when they would be defined, and the old definition is kept:

```python
c.call("define_composite", type="read", name="r1", expression="Vx + 1")
c.call("define_composite", type="read", name="r2", expression="r1 * 2")
c.call("define_composite", type="read", name="r1", expression="r2 + 1", replace=True)
# ChannelError: circular dependency: r1 → r2 → r1
```

A runtime guard also stops a loop that slipped in some other way within a fraction
of a second, so a call can never hang the backend. A channel cannot be removed or
redefined away while another composite uses it.

## Sweeping composites

Variables and mappings can be swept like aliases. Variables of one composite that
are swept **in the same dimension** are set together at every point, so even the
first point works before they have ever been set:

```json
{"points": 51, "sweep": [{"channel": "eps", "start": -0.2, "stop": 0.2},
                         {"channel": "U",   "start": 0.1,  "stop": 0.3}],
 "record": ["Vx", "Vy", "Vdiff"]}
```

All combinations are limit-checked before the sweep starts.

## In scripts

```python
set_many({"eps": 0.0, "U": 0.25})
set("eps", 0.1)                 # U stays 0.25
print(get("Vdiff"), get("Tset_C"))
```

## Checking without defining

`check_composite` takes the same arguments as `define_composite` and returns the
channels it would define and any warnings, without changing anything:

```python
c.call("check_composite", type="multi", name="lazy", variables=["p", "q"],
       targets={"Vb": "p"})
# {'channels': ['p', 'q'], 'warnings': ['composite lazy: variable q drives no target'], …}
```

## Reference

| type | fields |
|---|---|
| `multi` | `name`, `variables` (names or `{name, min, max, unit, ramp_rate, description}`), `targets` `{channel: formula}`, `description` |
| `read` | `name`, `expression`, `unit`, `description` |
| `map` | `name`, `target`, `forward` (of `x`), `backward` (of `y`), `min`, `max` (required), `unit`, `ramp_rate`, `tolerance`, `description` |

Setups made before composites had types still load. A one-variable composite with
`targets` as formulas of `x` becomes a multivariate write whose variable has the
composite's name.
