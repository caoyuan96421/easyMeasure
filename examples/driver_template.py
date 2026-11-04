"""Template for a new instrument driver.

Copy this file into a driver directory (listed in the backend setting
``driver_paths``) or into ``easymeasure/backend/drivers/``.  The driver name is
the file name (here: ``driver_template``).

Write the driver naively: the framework runs every call on a guarded worker
thread with a deadline, retries/reconnects on errors, validates the returned
data against the channel spec, and serialises access to (shared) ports.
"""

from easymeasure.backend.instrument import ActionSpec, ChannelSpec, Instrument
from easymeasure.common.errors import ChannelError


class MyPowerSupply(Instrument):
    DESCRIPTION = "Example SCPI power supply"

    # access: 'r', 'w' or 'rw'; dtype: 'float' or 'str'
    # shape: () scalar, (-1,) vector of any length, (-1, -1) 2-D, (3,) fixed length ...
    CHANNELS = [
        ChannelSpec("voltage", "rw", "float", (), "V", "output voltage"),
        ChannelSpec("current", "r", "float", (), "A", "measured current"),
        # config=True: a setting (range, mode, output on/off ...), set directly in the
        # GUI / sweep pre-post constants, never aliased or swept
        ChannelSpec("output", "rw", "str", (), "", "ON/OFF", config=True),
        ChannelSpec("trace", "r", "float", (-1,), "A", "buffered current trace", timeout=30.0),
    ]

    # actions: commands without arguments, implemented by _action_<name>()
    ACTIONS = [ActionSpec("reset", "*RST"), ActionSpec("trigger", "*TRG")]

    # options are user-editable per instrument; port options (timeout, baudrate,
    # read_termination ...) and framework options (call_timeout, retries,
    # reconnect_after, breaker_cooldown, shared) live in the same dict
    DEFAULT_OPTIONS = {"timeout": 2.0, "baudrate": 9600, "call_timeout": 10.0, "retries": 2}

    # set True if several instruments may share this port (GPIB bus, RS-485, ...)
    SHARED_RESOURCE = False

    def connect(self):
        idn = self.resource.query("*IDN?")
        if "MYVENDOR" not in idn.upper():
            raise IOError("unexpected instrument %r" % idn)

    def disconnect(self):
        pass

    # for shared ports: called whenever this instrument takes over the bus
    # def select(self, resource):
    #     resource.write("++addr %d" % self.options["gpib_addr"])

    def _action_reset(self):
        self.resource.write("*RST")

    def _action_trigger(self):
        self.resource.write("*TRG")

    def _read_voltage(self):
        return self.resource.query("VOLT?")        # strings are converted to float

    def _write_voltage(self, value):
        self.resource.write("VOLT %.6f" % value)   # value is already a float

    def _read_current(self):
        return self.resource.query("MEAS:CURR?")

    def _read_output(self):
        return "ON" if self.resource.query("OUTP?").strip() in ("1", "ON") else "OFF"

    def _write_output(self, value):
        if value.upper() not in ("ON", "OFF"):
            raise ChannelError("output must be ON or OFF")   # ChannelError = not retried
        self.resource.write("OUTP %s" % value.upper())

    def _read_trace(self):
        return self.resource.query("TRAC:DATA?")    # "1.0,2.0,3.0" -> numpy array
