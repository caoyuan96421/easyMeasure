# The run queue

Sweeps and scripts run one at a time from the **queue**. You can line up a night's
worth of measurements and look at the results in the morning. The **Queue** tab shows
it:

![The Queue tab](../images/qs-queue.png)

* **Queued**: what waits. The first line is the item running now (`current`).
* **History**: what has run, with its status and data file.
* **Script**: the script editor (see [Queue scripts](../scripting/scripts.md)).

## Adding to the queue

In the **Sweep** menu (for the sweep in the editor):

* **Add to queue** puts the sweep at the end of the queue.
* **Add & start** does the same and starts the queue.

The sweep is validated when it is added: a sweep that would exceed a limit is refused
right there, not at 3 a.m. Later changes in the editor do not change a sweep that is
already queued.

**Start queue** runs what is queued, one item after the other. When the queue runs
empty it goes back to *idle* by itself; a sweep added after that waits for the next
**Start queue**. Use **up** / **down** to change the order of waiting items, **Remove**
to drop one, **Clear queue** to drop all.

## Pause, abort, stop

| button | effect |
|---|---|
| **Pause** / **Resume** | the running item halts at the next point (sweeps) or line (scripts), with everything left as it is, and continues from there |
| **Abort current** | ends the running item. A sweep's **post-sweep** settings still run, so the gates still go back to zero |
| **Abort all** | ends the running item, empties the queue, and stops running [user actions](user-actions.md) |
| **Stop after current** | lets the running item finish, then stops the queue |
| **stop queue on error** | when an item fails, the queue stops instead of going on with the next |

The same buttons are on the main toolbar, so they are at hand whichever tab is open.
Buttons that don't apply right now are greyed out.

## History

Every finished, aborted or failed item stays in the **History**, with its status:

* *finished*: ran to the end.
* *terminated*: ended itself, with `end_sweep()` (see
  [tutorial 3](../tutorials/stop-at-threshold.md)).
* *aborted*: you pressed Abort.
* *error*: something went wrong; the log has the reason.

Select an item, then:

* **Rerun selected** queues the same sweep or script again.
* **Copy to sweep editor** (or **Copy to script editor**) loads it, so you can run a
  variation.
* **Open data** opens its data file in a new data window.

## Scripts in the queue

A **script** is a Python program that runs from the queue like a sweep, for example
a loop that runs the same sweep at several temperatures. Scripts are an advanced
topic with their own chapter: [Queue scripts](../scripting/scripts.md).

## From Python

??? example "Queueing and controlling from a Python client"

    With a [Python client](../scripting/clients.md) `c`:

    ```python
    id1 = c.call("queue_add", item={"kind": "sweep", "definition": sweep1})
    id2 = c.call("queue_add", item={"kind": "sweep", "definition": sweep2}, start=True)
    c.call("queue_move", item_id=id2, position=0)     # run it first
    c.call("queue_remove", item_id=id1)
    c.call("pause"); c.call("resume"); c.call("abort"); c.call("abort_all")
    c.call("queue_stop")                              # stop after the current item
    c.call("queue_options", stop_on_error=True)

    st = c.call("queue_state")
    st["state"], st["current"], len(st["queued"])     # 'running', {...}, 3
    st["can"]       # which controls apply now: {'start': False, 'pause': True, ...}
    for item in st["history"]:
        print(item["id"], item["kind"], item["name"], item["status"], item.get("path"))
    ```

    The queue is `idle`, `running`, `paused` or `aborting`. The GUI and the web
    page enable their buttons from `can`.

## Permissions

Over [remote access](remote.md), operators can queue and control sweeps. Scripts, and
sweeps with code in their point actions, need the admin role, because they run Python
on the measurement computer.
