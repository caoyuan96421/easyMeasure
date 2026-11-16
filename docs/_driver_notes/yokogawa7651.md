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

The current limiter does not work on the 10 mV and 100 mV ranges; the hardware trip
at about ±0.6 V still does. `overload` reads `YES` while the output is held at the
limiter (the `E` header in the output data).

**Protocol.** This follows a 7651 driver verified on the hardware (IM 7651-01E).

- A message is commands written one after the other, with no separator, and at most
  50 characters: `F1R4S+1.5E+0E`.
- `F`, `R`, `S` and `O` wait for `E` (execute), so every change carries its `E`.
- `LA` and `LV` take effect at once. `LA` works only in voltage mode and `LV` only
  in current mode.
- The 7651 has no `*IDN?`. The model comes from the settings listing (`OS`).

**Connecting** sends `H1` and `DL0`, reads the status (`OC`) and the settings (`OS`),
and changes nothing, so an output that is on stays on. A running program is stopped
with `RU0`. A program being edited is refused: leave editing on the front panel, then
Reconnect.

**Verified settings.** Every setting is read back (`OD`). A message the 7651 rejected
(status bit 4) raises an error, and so does a value that did not arrive.

**No device clear.** On the 7651 a device clear restores the power-on panel (output
off, settings lost), so the driver never sends one, not even to recover from a failed
call.
