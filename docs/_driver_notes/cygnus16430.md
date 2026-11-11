Cygnus 16430: an 8-channel supply, 0–150 V and 0–40 mA per output, with an output
relay each, over USB serial (firmware `cygnus-16430-firmware`).

**Address**: its USB serial port, e.g. `COM5` or `/dev/ttyACM0`; any baud rate
works.

**Outputs.** Each output is a voltage setpoint with a current limit: constant voltage
until the limit, then constant current. Each has its own relay, and nothing changes on
connect.

| per output k = 1…8 | |
|---|---|
| `voltage<k>` | setpoint, 0 … 150 V |
| `Vmeas<k>`, `Imeas<k>` | measured voltage and current |
| `Ilimit<k>` | current limit, 0 … 40 mA (setting) |
| `output<k>` | relay ON / OFF (setting) |

**Vectors**: `voltAll` and `IlimitAll` write all 8 at once (all are checked first,
nothing is set if one is out of range); `VmeasAll` and `ImeasAll` read all 8. Monitor
`VmeasAll` to see all eight readings as curves.

**ADC and filters**: `Vrate` and `Irate` set the ADC SF (13–255); `Vfilter` and
`Ifilter` set the IIR filter α.

**Actions**: `all_off`, `reset`, `auto_calibrate` (refused while an output is on).

**Commands.** The driver speaks the firmware's exact commands (`VOLT n,v`,
`OUTP n,ON`, `MEAS:CURR? n` …). It reads the firmware's one-slot error register
(`SYST:ERR?`) after every setting, so a refused value raises the firmware's message.

**Fresh readings.** Readings are round-robin over the 8 channels, every
`8·24·SF/32768` s (76 ms at SF 13). After a change of a channel's setpoint, limit or
relay, its next reading waits for fresh conversions. With an IIR filter it also waits
for about 99 % of the step. The option `settle` = false turns this off.

**Filters.** The filters cannot be read back, so the driver sets them from its
options on connect.

**Emulator.** `SIM::cygnus` emulates the firmware's parser with resistive loads
(option `load_ohm`, default 10 kΩ).
