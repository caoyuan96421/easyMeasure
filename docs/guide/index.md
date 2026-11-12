# User guide

The user guide explains each part of easyMeasure in full: every button, option and
rule. It is the place to look things up.

!!! tip "New to easyMeasure?"
    Start with the [Quick start](../quickstart.md) and the
    [tutorials](../tutorials/index.md). They walk through real measurements step by
    step, with screenshots. Come back here when you want to know everything a part
    can do.

Each page describes the GUI first. Where the same can be done from Python, it
follows in a box marked **From Python**, which you can skip. The
[Scripting and Python](../scripting/index.md) chapter explains how that works.

You can try everything with the simulated instruments, without any hardware: load
`examples/graphene_setup.json` (*Setup → Load setup…*), or start the
backend with the older demo setup, `python -m easymeasure.backend --setup
examples/demo_setup.json`.

## The parts

**Building a setup**

1. [Instruments](instruments.md): adding instruments, addresses, channels, settings,
   actions, finding what is connected.
2. [Aliases and protection](aliases.md): short names, limits, ramp rates, scaling.
3. [Composite channels](composites.md): channels computed from others, such as the
   density and displacement field of a dual-gated sample.
4. [Parameters](parameters.md): named constants (capacitances, gains) that formulas
   use by name.

**Measuring**

5. [Sweeps](sweeps.md): the sweep editor, dimensions, setpoints, recording, delays,
   pre- and post-sweep settings.
6. [Point actions and flow control](point-actions.md): code at every point;
   skipping points, ending a sweep early.
7. [The run queue](queue.md): queueing, pausing, aborting, the history.
8. [User actions](user-actions.md): your own one-click buttons ("all gates to zero").
9. [Data files](data.md): where data goes, the crash-safe journal, HDF5, loading data
   in Python.

**Watching and reporting**

10. [Desktop GUI](gui.md): the panels, the lock, settings.
11. [Plotting live and stored data](plotting.md): curves, heat maps, sliders.
12. [Monitor and Manual panels](monitor-manual.md): watching and setting channels.
13. [PowerPoint reports](powerpoint.md): a slide for every sweep.

**Working from elsewhere**

14. [Remote access and the web app](remote.md): measuring from home, accounts, roles.

**Instruments without a driver**

15. [Driver Wizard](driver-wizard.md): a driver from a form, no programming.
16. [Simulated instruments](simulation.md): trying things out, fault injection.

**Under the hood**

17. [Robustness](robustness.md): what protects a measurement from misbehaving
    hardware.
18. [Testing](testing.md): the test suite and stress tests.

Python clients and writing drivers in Python are in the
[Scripting and Python](../scripting/index.md) chapter.

## The ideas in one paragraph

An **instrument** is a driver connected to a port. It has **channels** (numbers,
arrays such as spectra, or text) and **actions**. An **alias** gives an instrument
channel a short name and carries its protection: limits, ramp rate and scale.
**Composite channels** compute instrument values from your own variables, or the
other way round. A **sweep** sets channels through any number of nested
**dimensions** and records others at every point. Sweeps and **scripts** run one at
a time from the **queue**. Every run is written to a crash-safe **data file**.
Instruments, aliases, composites, the monitor list and user actions together form the
**setup**, which is saved as a file and autosaved. [Key ideas](../concepts.md) explains
each with a picture of where it lives in the GUI.
