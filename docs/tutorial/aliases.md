# Aliases and protection

An **alias** gives one instrument channel a short name and carries its
**protection**: limits, ramp rate and scaling. Protection belongs to the alias, not
to the instrument, so the same source can be a gate limited to ±2 V in one setup
and a heater limited to 5 V in another.

```python
c.call("define_alias", name="Vg", target="src.V1", min=-2, max=2, ramp_rate=2.0,
       unit="V", description="gate voltage")
c.call("define_alias", name="I", target="dmm.reading", unit="nA")
```

In the GUI: *Instruments & channels → Aliases → Add…*, or select an instrument
channel and click *Make alias…*.

## Fields

| field | meaning |
|---|---|
| `name` | the alias name, used everywhere: sweeps, monitor, scripts, Manual panel |
| `target` | `instrument.channel` |
| `min`, `max` | hard limits in alias units; a value outside is refused, nothing is written |
| `ramp_rate` | maximum rate of change in alias units per second (`None`: jump) |
| `ramp_step` | optional maximum step per write while ramping |
| `scale`, `offset` | alias value = raw × `scale` + `offset` (amplifier gain, divider …) |
| `unit` | shown in the front ends and data files |
| `allowed` | accepted values of a text channel; narrows the instrument's own list |
| `read_only` | refuse every write through this alias |
| `description` | free text (tooltips) |

## Limits

```python
c.call("write_channel", name="Vg", value=0.5)      # fine
c.call("write_channel", name="Vg", value=5)
# LimitError: Vg: value 5 above maximum 2
c.call("write_channel", name="Vg", value=float("nan"))
# LimitError: Vg: refusing to set NaN/inf
```

Limits are checked everywhere a value is set: Manual panel, scripts, composite
channels, and every setpoint of a sweep, all checked **before** the sweep starts.
NaN and infinity are never written.

## Ramp rates

With a `ramp_rate`, a write moves the instrument there in small, time-based
steps. Going from 0.2 V to 0.6 V at 2 V/s takes about 0.2 s:

```python
c.call("write_channel", name="Vg", value=0.6)              # ramps
c.call("write_channel", name="Vg", value=0.0, ramp=False)  # jumps (use with care)
```

Ramps are abortable. They start from where the instrument really is: after an action
or a raw write, the cached value is discarded and the start is read back. Sweeps
ramp between points, and to the first point, at the alias' rate.

## Scaling

An amplifier, a divider or a different unit:

```python
# a ×10 voltage amplifier after the DAC output: alias in volts at the sample
c.call("define_alias", name="Vbias", target="daq.ao0", scale=10, unit="V",
       min=-50, max=50, ramp_rate=5)

# a current preamplifier at 1e-7 A/V into a voltmeter: alias in amperes
c.call("define_alias", name="Isd", target="dmm.Vdc", scale=1e-7, unit="A")

# the same source channel in millivolts
c.call("define_alias", name="Vg_mV", target="src.V1", scale=1000, unit="mV",
       min=-500, max=500, ramp_rate=1000)
c.call("write_channel", name="Vg_mV", value=200)
c.call("read_channel", name="src.V1")     # 0.2
```

Limits and ramp rate are in alias units (after scaling).

## Text channels

```python
c.call("define_alias", name="dmm_mode", target="dmm.mode", allowed=["Vdc", "Idc"])
```

Only `Vdc` and `Idc` can be written through `dmm_mode`, even though the meter itself
has more modes.

## Read-only aliases

```python
c.call("define_alias", name="Iro", target="dmm.reading", read_only=True)
c.call("write_channel", name="Iro", value=1)    # ChannelError: alias Iro is read-only
```

## Changing and removing

```python
c.call("define_alias", name="Vg", target="src.V1", min=-1, max=1, ramp_rate=1, replace=True)
c.call("remove_alias", name="Vg")     # refused while a composite channel uses it
```

## Built-in channels

`@time` (Unix time) and `@elapsed` (seconds since the sweep started) can be recorded
like any channel.

## Next

[Composite channels](composites.md) build software channels on top of aliases,
for example detuning and energy axes of a double quantum dot, or a temperature in °C.
