# Tutorial

The tutorial goes through easyMeasure one part at a time. Every page has examples
you can run against the simulated demo setup:

```bash
python -m easymeasure.backend --setup examples/demo_setup.json
```

The examples are given in Python with a [`BackendClient`](clients.md):

```python
from easymeasure.client import BackendClient
c = BackendClient.connect()
```

Every `c.call("command", …)` is also a button or form somewhere in the GUI, and
the pages say where. In-process, the same commands are methods of the `Backend`
object (`be.write_channel(name="Vg", value=0.2)`).

## The parts

**Building a setup**

1. [Instruments](instruments.md): drivers, addresses, channels, configuration channels,
   actions, finding instruments.
2. [Aliases and protection](aliases.md): names, limits, ramp rates, scaling.
3. [Composite channels](composites.md): multivariate writes, computed reads,
   one-to-one mappings.

**Measuring**

4. [Sweeps](sweeps.md): N-D measurements, mappings, vector channels, pre/post
   phases.
5. [Point actions and flow control](point-actions.md): code at every point, `skip_point`,
   `skip_line`, `end_sweep`.
6. [Run queue and scripts](queue-scripts.md): queueing, pausing, aborting, scripts.
7. [User actions](user-actions.md): your own one-click actions ("all gates to zero").
8. [Data files](data.md): the crash-safe journal, HDF5, loading data in Python.

**Watching and reporting**

9. [Desktop GUI](gui.md): the panels, lock, settings.
10. [Plotting live and stored data](plotting.md): curves, heat maps, sliders.
11. [Monitor and Manual panels](monitor-manual.md): watching and setting channels.
12. [PowerPoint reports](powerpoint.md).

**Working from elsewhere**

13. [Python clients](clients.md): scripts, notebooks, events.
14. [Remote access and the web app](remote.md): accounts, roles, certificates.

**Instruments without a driver**

15. [Driver Wizard](driver-wizard.md): a driver from a form.
16. [Writing a driver](writing-drivers.md): a driver in Python.
17. [Simulated instruments](simulation.md): trying things out, fault injection.

**Under the hood**

18. [Robustness](robustness.md): what protects a measurement from buggy hardware.
19. [Testing](testing.md): the test suite and stress tests.

## Concepts in one paragraph

An **instrument** is a driver connected to a port; it has **channels** (float scalars
or arrays, or text) and **actions**. An **alias** gives an instrument channel a
short name and carries the protection: limits, ramp rate and scale. **Composite channels**
compute hardware values from software variables or the other way round. A
**sweep** sets channels through N nested dimensions and records others at every
point. Sweeps and **scripts** run one at a time from the **queue**. Every run is
written to a crash-safe **data file**. Instruments, aliases, composites, monitor
list and user actions together form the **setup**, which is saved as JSON and autosaved.
