# Writing a driver

For instruments the [Driver Wizard](driver-wizard.md) cannot describe, such as binary
protocols, vendor DLLs or unusual readouts, write a driver in Python. A driver is one
file with a class deriving from
[`Instrument`](../reference/api/easymeasure/backend/instrument.md).

**Write the driver naively.** The framework runs every call on a guarded worker
thread with a deadline, retries and reconnects on errors, validates the returned
data against the channel spec, and serialises access to shared ports.

## Where drivers live

| directory | |
|---|---|
| `easymeasure/drivers/` | the built-in drivers |
| `<home>/drivers/` (`~/.easymeasure/drivers/`) | drivers made with the wizard; your own can go here too |
| the `driver_paths` setting | any further directories |

The driver name is the file name: `my_supply.py` is the driver `my_supply`. A driver
that fails to import is reported in `list_drivers()["errors"]`; it is never fatal.
After adding or changing a file, use *rescan* (`c.call("rescan_drivers")`).

## A complete driver

This is `examples/driver_template.py`, abridged:

```python
from easymeasure.backend.instrument import ActionSpec, ChannelSpec, Instrument
from easymeasure.common.errors import ChannelError


class MyPowerSupply(Instrument):
    DESCRIPTION = "Example SCPI power supply"

    # access: 'r', 'w' or 'rw'; dtype: 'float' or 'str'
    # shape: () scalar, (-1,) vector of any length, (-1, -1) 2-D, (3,) fixed length …
    CHANNELS = [
        ChannelSpec("voltage", "rw", "float", (), "V", "output voltage"),
        ChannelSpec("current", "r", "float", (), "A", "measured current"),
        ChannelSpec("output", "rw", "str", (), "", "ON/OFF", config=True,
                    allowed=["ON", "OFF"]),
        ChannelSpec("range", "rw", "str", (), "", "current range", config=True,
                    allowed=["10 mA", "100 mA", "1 A"]),
        ChannelSpec("trace", "r", "float", (-1,), "A", "buffered current trace", timeout=30.0),
    ]
    ACTIONS = [ActionSpec("reset", "*RST"), ActionSpec("trigger", "*TRG")]
    DEFAULT_OPTIONS = {"timeout": 2.0, "baudrate": 9600, "call_timeout": 10.0, "retries": 2}
    INTERFACES = ("gpib", "usb", "tcpip", "tcp", "serial", "sim")
    IDN_MATCH = ("MYVENDOR,PS",)        # recognise it when ports are scanned
    SHARED_RESOURCE = False             # True for a bus several instruments share

    def connect(self):
        idn = self.resource.query("*IDN?")
        if "MYVENDOR" not in idn.upper():
            raise IOError("unexpected instrument %r" % idn)

    def _action_reset(self):
        self.resource.write("*RST")

    def _action_trigger(self):
        self.resource.write("*TRG")

    def _read_voltage(self):
        return self.resource.query("VOLT?")        # strings are converted to float

    def _write_voltage(self, value):
        if value < 0:
            raise ChannelError("negative voltages are not supported")   # not retried
        self.resource.write("VOLT %.6f" % value)   # value is already a float

    def _read_current(self):
        return self.resource.query("MEAS:CURR?")

    def _read_output(self):
        return "ON" if self.resource.query("OUTP?").strip() in ("1", "ON") else "OFF"

    def _write_output(self, value):
        self.resource.write("OUTP %s" % value)     # already "ON" or "OFF"

    def _read_trace(self):
        return self.resource.query("TRAC:DATA?")    # "1.0,2.0,3.0" → numpy array
```

## The pieces

### Channels

`ChannelSpec(name, access, dtype, shape, unit, description, timeout=None,
config=False, allowed=None)`:

| argument | meaning |
|---|---|
| `access` | `"r"`, `"w"` or `"rw"` |
| `dtype` | `"float"` (scalars and arrays) or `"str"` |
| `shape` | `()` scalar; `(-1,)` vector of any length; `(64,)` fixed; `(-1, -1)` image; `None` anything |
| `unit`, `description` | shown in the front ends, stored in data files |
| `timeout` | per-channel deadline, for slow readings |
| `config` | a setting: set directly, never aliased or swept |
| `allowed` | accepted values; writes are checked and normalised, front ends show a drop-down |

Implement `_read_<name>()` and `_write_<name>(value)`, or override
`read(channel)` / `write(channel, value)` for all channels at once. Return values
are converted and checked against the spec: strings like `"1.5"` or `"1,2,3"`
become numbers and arrays, and a wrong shape is an error.

### Actions

`ActionSpec(name, description, timeout=None)`, implemented by `_action_<name>()`,
or by overriding `action(name)`. Actions are never retried automatically.

### Options

`DEFAULT_OPTIONS` are the defaults of the instrument's options. Users edit them per
instrument, and they are passed to the port. Port options include `timeout`,
`baudrate`, `read_termination` and `write_termination`. Framework options are listed
under [guard options](robustness.md#guard-options). Read them as `self.options[...]`.

### I/O

Inside driver methods, `self.resource` is the port, already inside a transaction:

| method | does |
|---|---|
| `write(cmd)` | send a command (text or bytes) |
| `read()` | read one reply (text) |
| `query(cmd, delay=0)` | write, then read |
| `read_raw(size=None)` | read bytes |
| `clear()` | clear the port |

The same calls work on VISA, serial, raw TCP and the simulated buses. For
non-message-based hardware such as a DAQ card or a vendor DLL, use the vendor's
library directly in `connect` / `_read_…`, with the address `NONE` or `DAQ::…`.

### Errors

* Raise `ChannelError` for a refused value (out of range, not supported). It is
  reported, and not retried.
* Any other exception counts as an I/O problem. It is retried and, after
  `reconnect_after` failures, the instrument is reconnected.

### Channel lists that follow the instrument

Override `get_channels()` (and `get_actions()`) to return a list that depends on the
state the driver knows, for example the mode it last set. The backend calls it again
after every configuration write, action and reconnect, and the front ends follow.
Keep it free of I/O:

```python
def get_channels(self):
    src = "current" if self.mode == "Current" else "voltage"
    meas = "voltage" if src == "current" else "current"
    return [ChannelSpec(src, "rw", "float", (), "A" if src == "current" else "V"),
            ChannelSpec(meas, "r", "float", (), "V" if meas == "voltage" else "A"),
            ChannelSpec("mode", "rw", "str", (), "", config=True,
                        allowed=["Voltage", "Current"])]
```

### Shared ports

Set `SHARED_RESOURCE = True` (or the instrument option `shared`), and implement
`select` to make this instrument the active one on the bus whenever it takes over:

```python
def select(self, resource):
    resource.write("++addr %d" % self.options["gpib_addr"])
```

### Address suggestions and recognition

`INTERFACES` limits which addresses are accepted. `IDN_MATCH` strings mark scanned
devices whose `*IDN?` contains them. A `suggested_addresses` classmethod can offer
addresses without a scan, such as a built-in emulator:

```python
@classmethod
def suggested_addresses(cls, driver_name):
    return [{"address": "SIM::" + driver_name, "description": "built-in emulator"}]
```

## Testing a driver without the instrument

The hardware drivers come with emulators (`easymeasure/drivers/emulators/`) that
implement the instrument's command table from its programming manual. The tests
check that every command a driver sends exists in that table. Model a new driver's
tests on `tests/test_drivers.py` and `tests/test_cygnus.py`. The simplest check is:

```python
be.add_instrument("ps", "my_supply", "SIM::ps")
assert be.read_channel(name="ps.voltage") == 0.0
```

See the [driver reference pages](../reference/drivers/index.md) for how the built-in
drivers are organised, and [`Instrument`](../reference/api/easymeasure/backend/instrument.md)
for the full base class.
