# Desktop GUI

```bash
python -m easymeasure.frontend                    # connect to the running backend
python -m easymeasure.frontend --start-backend    # … starting one if none runs
```

The GUI is only a view: it talks to the backend over a socket and never blocks on
it. Close it, kill it or start several on different screens; the measurement does
not notice. A reopened GUI picks up the running sweep where it is.

![The main window](../images/qs-window.png)

The window is split in two:

* on the left, the instruments, sweep editor and queue;
* on the right, the data: the live view and any loaded files;
* on the far right, the **Monitor** and **Manual** docks;
* at the bottom, the **Log**.

The read-only *Running sweep* view can be shown from the *View* menu.

## Instruments & channels

![Instruments & channels](../images/qs-instruments.png)

The page fits the window without a scroll bar. The first row holds:

* **Instruments**: Add…, Edit…, Reconnect, Disconnect, Remove, and
  *Driver wizard…*. The Add dialog's address list shows the ports found on the
  backend computer that fit the driver (see [Instruments](instruments.md#adding-an-instrument)).
* **Channels (hardware)**: the selected instrument's channels, with their
  allowed values and the *last value* read or set (nothing is read just to fill the
  table), and its actions. Buttons: *Make alias…*, *Get*, *Set…* (writes the channel
  as it is: no limits, no ramp) and *Run action*.

Below them, four tabs:

* **Channels (aliased)**: the [aliases](aliases.md), with their limits and last values.
* **Channels (composite)**: the [composite channels](composites.md). A multivariate
  composite is followed by a row per variable, under the name it goes by everywhere
  else (`↳ nD.n`), with its range and last value.
* **User Actions**: your [user actions](user-actions.md).
* **Parameters**: the setup's [parameters](parameters.md).

Drag a tab to change their order; the order is remembered. The splitter between the
two rows moves too.

The **Setup** menu has *Save setup… / Load setup… / Merge setup…*.

### Messages

What a button did, or why it was refused, is shown in the window's **status bar**
at the bottom, not in the page: errors in red stay until the next message, the rest
clear after 10 seconds. Every message is also written to the **Log** dock, so
nothing is lost when the next one comes.

### Checking what you type

Every field that takes a formula or code has a **Check** button: parameters, composite
channels, user actions, point actions and the script editor. Check asks the backend,
without saving anything: are the names known, is there a circle, does the code
compile, do the channels it quotes exist? *OK* (or *Add to queue*) checks first, and
a dialog closes only when the check passes. Warnings (a channel that does not exist
yet, say) are shown in orange but do not stop you.

## Sweep editor

![Sweep editor](../images/qs-sweep-editor.png)

The editor has any number of dimensions, each laid out in the order of one point:

1. the actions;
2. setting the swept channels;
3. the pre-sample delay;
4. recording;
5. the post-sample delay.

An outer dimension also shows where the next inner dimension's loop runs: last, or
before recording with *record after inner dims*. The pre and post phases, error
policies and metadata are below.

The **Sweep** menu works on the sweep in the editor: *New*, *Load… / Save…* (JSON),
*Load from data file…* (the definition of a stored run), *Validate* (limit-checks every
setpoint), *Add to queue* and *Add & start*.

## Queue

![Queue](../images/qs-queue.png)

The queue panel has:

* the controls: Start, Pause, Resume, Abort, Abort all and Stop after current,
  enabled by what the queue can do now;
* the queued items, which can be reordered and removed;
* the history: *Rerun selected*, *Open data*, and *Copy to sweep / script editor*;
* a script editor.

See [The run queue](queue.md).

## Data views

The **Live data** tab follows the running sweep. *File → Open data file…* opens stored
runs in further tabs. See [Plotting](plotting.md).

## Docks

* **Monitor**: channels polled at their own intervals.
* **Manual**: every writable channel with a spin box and slider.

See [Monitor and Manual panels](monitor-manual.md).

The **Log** dock shows the backend log (all clients see the same) with level filter
and search.

## Menus

* **File**: Open data file… (Ctrl+O), Browse recent runs…, Settings… (data folder,
  PowerPoint file, display), Quit (the backend keeps running).
* **Setup**: Save setup…, Load setup…, Merge setup… (see
  [Instruments](instruments.md#saving-the-setup)).
* **Sweep**: New, Load…, Load from data file…, Save…, Validate, Add to queue, Add &
  start, for the sweep in the editor (see [Sweeps](sweeps.md#the-sweep-editor)).
  Using one brings its tab to the front.
* **Backend**: Start backend, Reconnect, Restart backend…, Show tray icon, Remote
  access…, Driver wizard…, Shut down backend….
* **View**: show or hide the Monitor, Manual and Log docks and the *Running sweep* view.

The toolbar has the queue controls, *Add slide to PPT* and *Open data…*.

## First start

The first time the GUI meets a backend, it asks for:

* the **data folder**, where every run is saved;
* the **PowerPoint file** that slides are appended to (empty = none).

Both are prefilled with the current values. *Ask me later* asks again at the next
start. Both stay editable in *File → Settings*, and a data folder that cannot be
created is refused.

## Display settings

*File → Settings → Display*:

* **font size**: applied at once, also to plot axes;
* **UI scale**: an overall scale of 50–300 %, applied on the next GUI start.

Together they suit any screen resolution. The window can be made small: toolbars
wrap, and panels scroll only when they must.

Combo boxes stay narrow so toolbars do not grow, but their drop-down lists show
long channel names in full.

## Lock

**🔓 Lock** (top right corner, Ctrl+L) locks the GUI against accidental changes.

Greyed out:

* instruments, the sweep editor and the queue;
* manual control;
* adding / removing monitors;
* *Add slide to PPT* and *Settings*;
* the *Setup* and *Sweep* menus;
* the *Backend* menu, except *Reconnect*.

Still usable:

* switching tabs;
* the plots and all their settings;
* monitor time spans;
* opening data files;
* the log.

Click again to unlock; the state is remembered across restarts. It is a guard
against slips, not access control. It affects this window only, not other GUIs,
the web page, the tray or scripts.

## Tray icon

While the backend runs, a tray icon shows its state:

| colour | state |
|---|---|
| green | idle |
| blue | running |
| orange | paused |
| red | error |
| gray | unreachable |

Its tooltip shows the current run and its progress.

The tray menu can:

* open the GUI;
* open the web page (*Open in browser*, which offers to switch on remote access
  first if it is off);
* pause, resume and abort;
* open the data folder or the log;
* shut the backend down.

A notification appears when a run ends. The tray is a separate helper process, so it
can never affect the backend. Start the backend with `--no-tray` to disable it.
