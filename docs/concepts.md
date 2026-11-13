# Key ideas

Eight words cover almost everything in easyMeasure.

**Instrument**
:   One device in your rack, connected through a *driver* (which kind of instrument it
    is) at an *address* (where it is connected: `GPIB0::24::INSTR`, `COM3`, …). You give
    it a short name: `bg`, `lockin`, `vna`.

**Channel**
:   Anything with a name that you read or write. Every channel is read and written
    the same way (the Manual panel, a sweep, a script: `get(name)`, `set(name, value)`)
    and has an **access**: readable (r), writable (w) or both (rw), which is always
    respected. There are a few kinds:

    | kind | name | access |
    |---|---|---|
    | a quantity of an instrument | `bg.voltage`, `lockin.X`, `vna.S11` | what the instrument allows |
    | a setting of an instrument (⚙) | `lockin.sensitivity`, `dmm.function` | what the instrument allows |
    | an action of an instrument | `dmm.reset`, `lockin.auto_gain` | w: writing it runs it |
    | an [alias](guide/aliases.md) | `Vbg` | the instrument channel's |
    | a [composite channel](guide/composites.md) | `Rxx`, `nD.n` | r (a formula), rw (variables, mappings) |
    | a [parameter](guide/parameters.md) | `Cb` | rw (a value), r (a formula) |
    | a [user action](guide/user-actions.md) | `gates_to_zero` | w: writing it runs it |
    | the clock | `@time` | r |

    A channel holds a number, an array (a spectrum, a VNA trace, a camera image) or a
    text; an action holds nothing. Names are unique: no alias, composite, parameter,
    user action and instrument share a name. The channels of an instrument sit under
    its name (`bg.…`) and are written as they are, without limits or ramps.

**Alias**
:   Your name for a channel of an instrument, for example `Vbg` for `bg.voltage`. It
    carries the **protection**: minimum, maximum and ramp rate, and an optional scale
    (V → Ω, an amplifier gain). Measurements use aliases.

**Composite channel**
:   A channel computed from others. Examples:

    * carrier density and displacement field, driving two gates (`nD.n`, `nD.D`;
      [tutorial 2](tutorials/rotated-gates.md));
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
