# Key ideas

Eight words cover almost everything in easyMeasure.

**Instrument**
:   One device in your rack, connected through a *driver* (which kind of instrument it
    is) at an *address* (where it is connected: `GPIB0::24::INSTR`, `COM3`, …). You give
    it a short name: `bg`, `lockin`, `vna`.

**Channel**
:   One quantity of an instrument, written `instrument.channel`, for example `bg.voltage`,
    `lockin.X` or `vna.S11`. A channel holds a number, an array (a spectrum, a VNA trace,
    a camera image) or a text. Channels marked ⚙ are **settings**: ranges, modes,
    sensitivity, output on/off.

**Alias**
:   Your name for a channel, for example `Vbg` for `bg.voltage`. It carries the
    **protection**: minimum, maximum and ramp rate, and an optional scale (V → Ω, an
    amplifier gain). Measurements use aliases.

**Composite channel**
:   A channel computed from others. Examples:

    * carrier density and displacement field, driving two gates
      ([tutorial 2](tutorials/rotated-gates.md));
    * a resistance computed from a voltage and a current measured by two lock-ins
      ([tutorial 2](tutorials/rotated-gates.md#2-measure-the-current-too-rxx-vxx-i));
    * a temperature in °C on a controller that works in K.

**Parameter**
:   A named constant of your setup: a capacitance, a bias resistor, a gain. Formulas
    of composite channels and actions use it by name (`Cb`), so a new calibration
    is one change. It can be a formula of other parameters (`Ct/Cb`). See
    [Parameters](guide/parameters.md).

**Sweep**
:   A measurement. Some channels step through values in one or more nested
    **dimensions**, and others are **recorded** at every step. Dimension 1 is the
    innermost (fastest) loop. Every value is checked against the limits before the first
    one is set.

**Queue**
:   Sweeps wait in the queue and run one after another, so you can line up a night's
    worth of measurements. The queue also runs **scripts**: Python programs for what
    a single sweep can't do, such as a sweep at each of a list of temperatures (an
    advanced topic: [Scripting and Python](scripting/index.md)).

**Setup**
:   Everything above that describes your experiment: instruments, aliases, composite
    channels, the monitor list and your actions. It is saved automatically, and you can
    keep copies as files.

And two programs:

* the **backend** owns the instruments and runs the measurements;
* the **GUI** is a window onto it.

Close, crash or restart the GUI as you like; the measurement is not affected. Several
windows can be open at once, also from other computers through the
[web app](guide/remote.md).

## Where things are in the window

| to … | go to |
|---|---|
| add an instrument, make an alias or a composite channel | *Instruments & channels* |
| set something by hand | the *Manual* panel (right) |
| watch values continuously | the *Monitor* panel (right) |
| describe and start a measurement | *Sweep editor* tab, then the **Sweep** menu (*Validate*, *Add & start*) |
| pause, abort, see what ran | *Queue* (and the toolbar) |
| look at data | *Live data*, *File → Open data file…* |
| write or run a script | *Queue → Script* |
| save, load or merge your setup | the **Setup** menu: *Save setup… / Load setup… / Merge setup…* |
| data folder, PowerPoint file, font size | *File → Settings* |
