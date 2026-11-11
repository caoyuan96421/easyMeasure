# Instruments

An **instrument** is a driver connected to a port. It exposes:

* **channels**: values to read and/or write. A channel is a `float` (a scalar or an
  array of any dimension, such as a spectrum or a camera image) or a `str`;
* **configuration channels**: settings such as range, sensitivity, mode or output on/off;
* **actions**: commands without arguments, such as reset, trigger or auto-phase.

The drivers that come with easyMeasure are listed under
[Supported instruments](../instruments.md); each has a reference page with every
channel.

## Adding an instrument

In the GUI: *Instruments & channels → Add…*. Choose the driver, a name and the
address. The **Address** list shows the ports and devices the backend computer has
that this driver accepts:

* instruments that identified as this driver's kind come first, marked ✓;
* then the other ports;
* then ports already in use;
* then the driver's suggestions, such as its built-in emulator.

You can also type any address. **Refresh** scans again; with *ask \*IDN?* it
also identifies GPIB / USB / LAN instruments.

From Python:

```python
c.call("add_instrument", name="lockin", driver="sr830", address="GPIB0::8::INSTR")
c.call("add_instrument", name="smu", driver="keithley2400", address="GPIB0::24::INSTR",
       options={"call_timeout": 20, "verify_writes": True})
c.call("add_instrument", name="daq", driver="ni6211", address="DAQ::Dev1")
```

The returned description has `state` (`connected`, `disconnected`, `error`) and
`last_error`. An instrument that cannot be reached is still added; fix the problem,
then reconnect it:

```python
c.call("reconnect_instrument", name="lockin")
c.call("update_instrument", name="lockin", address="GPIB0::9::INSTR")   # reconnects
c.call("disconnect_instrument", name="lockin")
c.call("remove_instrument", name="lockin")       # refused while aliases use it
```

!!! tip "No hardware at hand?"
    Every hardware driver accepts `SIM::<anything>` (NI DAQs: `DAQ::SIM`) and
    then talks to a built-in emulator of the instrument:
    `c.call("add_instrument", name="smu", driver="keithley2400", address="SIM::smu")`.

## Addresses

| interface | address examples | needs |
|---|---|---|
| `gpib` | `GPIB0::12::INSTR` | VISA library + GPIB card driver |
| `usb` (USB-TMC) | `USB0::0x0957::0x8B18::MY51141134::0::INSTR` | VISA library |
| `tcpip` (VISA LAN) | `TCPIP0::192.168.1.20::inst0::INSTR`, `TCPIP0::192.168.1.20::5025::SOCKET` | VISA library |
| `tcp` (raw socket) | `TCP::192.168.1.30::1234` (e.g. a Prologix GPIB-Ethernet adapter) | – |
| `serial` | `COM3`, `/dev/ttyUSB0`, `ASRL3::INSTR` | adapter driver |
| `daq` | `DAQ::Dev1` (the NI MAX device name), `DAQ::SIM` | NI-DAQmx |
| `sim` | `SIM::<bus name>` | – |
| `none` | `NONE` (instruments without a port, e.g. the simulated ones) | – |

Each driver lists the interfaces it accepts. An address of another interface is
refused when the instrument is added. For example, the GPIB instruments also accept
`serial` and `tcp` (RS-232, or a Prologix adapter), and the Keysight B2900 also
takes USB and LAN.

### Finding what is connected

```python
r = c.call("discover_resources", identify=True)
for f in r["found"]:
    print(f["interface"], f["address"], f.get("description", ""), f.get("idn", ""))
print(r["notes"])     # what could not be scanned (no VISA library, no NI-DAQmx …)
```

Serial ports come with their description (e.g. an FTDI adapter), VISA instruments
with their alias, and NI devices with their product type. `identify` asks each GPIB
/ USB / LAN instrument `*IDN?`, except instruments already configured here, which are
left alone.

### Several instruments behind one GPIB adapter

A Prologix GPIB-USB or GPIB-Ethernet adapter is one port shared by several
instruments. Mark them `shared` and give each its GPIB address:

```python
for name, drv, addr in (("dmm", "hp34401a", 22), ("src", "yokogawa7651", 3)):
    c.call("add_instrument", name=name, driver=drv, address="COM5",
           options={"shared": True, "gpib_addr": addr})
```

Before an instrument talks, the port is switched to it (`++addr N`). The
measurement thread has priority over monitor and GUI traffic, so monitoring
never slows a sweep down.

## Channels

```python
c.call("read_channel", name="lockin.X")              # raw reference: instrument.channel
c.call("channel_info", name="lockin.X")
# {'name': 'lockin.X', 'kind': 'raw', 'dtype': 'float', 'shape': [], 'readable': True,
#  'writable': False, 'unit': 'V', 'config': False, 'choices': None}
```

Raw `instrument.channel` references can be read anywhere: sweeps, monitor, scripts.
To **set** a measurement channel, give it an [alias](aliases.md), which carries the
limits and ramp rate. The unprotected `set_raw()` exists only in scripts.

### Configuration channels

Settings are read and set directly, and are never aliased or swept, which keeps the
channel lists short:

```python
c.call("write_channel", name="lockin.sensitivity", value="500uv")   # → '500 µV'
c.call("write_channel", name="lockin.time_constant", value="300 ms")
c.call("write_channel", name="smu.nplc", value=10)
c.call("read_channel", name="lockin.sensitivity")
```

In the GUI: *Instrument channels & actions → Set…*. In sweeps, use them as pre/post
**constants** (see [Sweeps](sweeps.md#pre-and-post-phases)).

**Allowed values.** Many settings accept only certain values. The front ends then
show a drop-down. Writes are checked and normalised to the listed spelling. Case,
blanks, `u`/`µ` and `Ohm`/`Ω` do not matter, so `"500uv"` sets `"500 µV"`.

### Channel lists that follow the instrument

A mode switch can rename channels, change units or make a channel read-only. The
backend asks the driver for its channel list again after every configuration
write, action and reconnect, and the front ends follow:

```python
c.call("write_channel", name="smu.output", value="OFF")    # modes change with output off
c.call("write_channel", name="smu.mode", value="Current")
# voltage is now measured (read-only), current is the source,
# Icompliance [A] became Vcompliance [V]
```

An alias whose target disappeared reports an error instead of silently reading
something else. This applies, for example, to `dmm.Vdc` after the meter was switched to
`Iac`. Re-point the alias, or switch back.

## Actions

```python
c.call("run_action", ref="lockin.auto_phase")
c.call("run_action", ref="smu.reset")
```

In the GUI: select the action under *Instrument channels & actions* → *Run action*.
In scripts and sweep hooks: `action('lockin.auto_gain')`. Actions are guarded like
every other call but never retried automatically, because a trigger must not fire twice.
Afterwards the cached values of that instrument's aliases are discarded, so the
next ramp reads back where the instrument really is.

## Talking to the instrument directly

```python
c.call("raw_query", instrument="lockin", command="*IDN?")
# 'Stanford_Research_Systems,SR830,s/n48261,ver1.07'
c.call("raw_write", instrument="lockin", command="OUTX 1")
```

These are admin-only over remote access, and logged.

## Guard options

Every instrument, whatever its driver, takes these options:

| option | default | meaning |
|---|---|---|
| `call_timeout` | 10 s | deadline for every driver call |
| `retries` | 2 | retries of a failed read / write |
| `retry_delay` | 0.05 s | pause between retries |
| `reconnect_after` | 2 | consecutive failures before a reconnect |
| `timeout_retries` | 1 | retries after a timeout |
| `breaker_threshold` | 2 | hangs before a channel is quarantined |
| `breaker_cooldown` | 10 s | quarantine time (doubles on repeated trips) |
| `shared` | driver default | the port may be shared with other instruments |

Common driver options of the hardware drivers:

| option | meaning |
|---|---|
| `reset` | `*RST` on connect |
| `check_errors` | read the error queue after every setting, so a refused value raises the instrument's own message (default on) |
| `verify_writes` | SMUs: read every source value back (worth it on RS-232) |
| `gpib_addr` | GPIB address behind a Prologix adapter |
| `timeout`, `read_termination`, `baudrate` | port settings |

See [Robustness](robustness.md) for what happens when an instrument misbehaves.

## Saving the setup

Instruments, aliases, composites, the monitor list and user actions together
form the **setup**:

```python
c.call("save_setup", path="C:/lab/fridge_setup.json")
c.call("load_setup", path="C:/lab/fridge_setup.json")   # returns a list of problems ([] = fine)
```

The setup is also autosaved on every change. `python -m easymeasure.backend
--restore` starts with the last one. The file format is in the
[reference](../reference/setup-format.md).
