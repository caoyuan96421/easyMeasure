Yokogawa 7651 programmable DC source. It is a source only, with no measurement. The
source channel is named after the `mode`:

| `mode` | channel | ranges |
|---|---|---|
| `Voltage` | `voltage` | 10 mV … 30 V |
| `Current` | `current` | 1 … 100 mA |

**Ranges.** With `range` = `AUTO`, the driver picks the smallest range that holds the
value. While the output is ON it only ever ranges *up*, so a sweep coming down does not
glitch the output with range changes. A fixed range refuses values beyond 120 % of it.

The compliance is `Icompliance` in voltage mode and `Vcompliance` in current mode.

**Verified settings.** Every setting is executed (`E`) and read back (`OD`). An error
flag, or a value that did not arrive, raises an error, and a command lost on the bus
is retried.
