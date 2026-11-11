A SCPI instrument described entirely by its options, with no driver code. Its
channels and actions come from the options `channels` and `actions`:

```python
c.call("add_instrument", name="ps", driver="scpi_generic", address="GPIB0::5::INSTR",
       options={
         "idn_contains": "KEITHLEY",
         "init": ["*RST", ":SOUR:FUNC VOLT"],
         "channels": [
           {"name": "V", "access": "rw", "get": ":SOUR:VOLT?",
            "set": ":SOUR:VOLT {value:.6g}", "unit": "V"},
           {"name": "I", "access": "r", "get": ":MEAS:CURR?", "unit": "A", "index": 1},
           {"name": "trace", "access": "r", "get": ":TRAC:DATA?", "shape": [-1]},
           {"name": "output", "access": "rw", "dtype": "str", "get": ":OUTP?",
            "set": ":OUTP {value}", "config": True}],
         "actions": [{"name": "trigger", "command": "*TRG"},
                     {"name": "reset", "command": ["*RST", "*CLS"]}]})
```

* `index` picks one element of a comma-separated reply.
* Vector channels (`shape: [-1]`) parse comma-separated replies into arrays.
* It works with any port type: VISA, serial, TCP.

For anything more than a few channels, the
[Driver Wizard](../../tutorial/driver-wizard.md) is more convenient. It writes a real
driver file, with an error queue, allowed values, value maps, checks and a try-it step.
