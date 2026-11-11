One driver for the ten NI USB X Series multifunction DAQs, in their BNC, screw
terminal, mass termination and OEM variants, through NI-DAQmx (NI's runtime required).

**`model`** is a configuration channel, and also an instrument option, so a setup
keeps it. It picks the exact model, and the channel list is rebuilt from NI's
specifications for it:

| model | analog in | out | port 0 | AI ranges | AO ranges | max rate |
|---|---|---|---|---|---|---|
| USB-6341 / 6343 | 16 / 32 SE (8 / 16 DIFF) | 2 / 4 | 8 / 32 | ±0.2…10 V (4) | ±10 V | 500 kS/s total |
| USB-6351 / 6353 | 16 / 32 SE (8 / 16 DIFF) | 2 / 4 | 8 / 32 | ±0.1…10 V (7) | ±10, ±5 V | 1.25 MS/s one input, 1 MS/s total |
| USB-6361 / 6363 | 16 / 32 SE (8 / 16 DIFF) | 2 / 4 | 8 / 32 | ±0.1…10 V (7) | ±10, ±5 V | 2 MS/s one input, 1 MS/s total |
| USB-6346 / 6349 | 8 / 32 DIFF, simultaneous | 2 | 8 | ±1…10 V (4) | ±10 V | 500 kS/s per input |
| USB-6356 | 8 DIFF, simultaneous | 2 | 8 | ±1…10 V (4) | ±10, ±5 V | 1.25 MS/s per input |
| USB-6366 | 8 DIFF, simultaneous | 2 | 8 | ±1…10 V (4) | ±10, ±5 V | 2 MS/s per input |

All have 16 PFI lines (`pfi0…15` = P1 / P2) and 4 counters.

**`model` = auto** takes what the device reports. A model that does not match the
device is refused; a device type the table does not know is trusted to be the
model you set.

**Changing the model.** Settings the new model does not support fall back, with a log
message: the terminal to RSE or DIFF, the ranges to ±10 V, and the rate lowered to fit.

**Inputs.** In DIFF the inputs are the pairs aiK / aiK+8, so `ai0…7` (and `ai16…23`
on the 32-input models). Readings work as on the [USB-6211](ni6211.md): the mean of
`samples` samples, acquiring only the inputs returned. The vector `ai` shares the
aggregate rate.

**Digital lines** are inputs until written. A written line is an output driving that
level until `release_digital`.

**Counters.** A counter (`ctrK`, started by its first reading) uses its default source,
PFI 8 / 3 / 0 / 5. That line then refuses to be a digital line.

**Emulator.** `DAQ::SIM` emulates the chosen model (`auto` → USB-6363) with a
loop-back: `aiK` sees `aoK`, and each digital line is wired to the one half a port away.
