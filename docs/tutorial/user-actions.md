# User actions

**User actions** are your own named snippets of Python, run with one click, such as
"all gates to zero", "ramp the magnet down" or "outputs off". They can control any
channel. They are part of the setup, so they are saved, loaded and autosaved with it.

```python
set_many({'VL': 0, 'VR': 0})      # limits and ramp rates apply as always
set('B', 0)
print('gates and field at 0')
```

## The Actions panel

*Instruments & channels → Actions* (desktop and web) lists your actions with their
description and last run: finished ✔ / aborted ■ / error ✘, time and duration.
Hover a failed run to see its error.

| button | does |
|---|---|
| **▶ Run** (or Enter, or double click on the web) | run the selected action now |
| **■ Abort** | stop the running action(s) at the next line, sleep or ramp step |
| **Add… / Edit…** | the editor: name, description, *ask before running*, code; *insert channel name…* puts a channel name at the cursor |
| **Remove**, **↑ ↓** | delete, reorder |

Tick **ask before running** for actions that should be confirmed first, such as
"outputs off" during a cooldown.

## From Python

```python
c.call("define_user_action", name="gates to zero",
       code="set_many({'Vx': 0, 'Vy': 0})\nset('Vg', 0)",
       description="all gates to 0 V", confirm=True)

c.call("run_user_action", name="gates to zero")
# {'name': 'gates to zero', 'status': 'finished', 'error': None, 'elapsed': 0.41, …}

c.call("list_user_actions")
c.call("define_user_action", name="zero gates", code="…", rename_from="gates to zero")
c.call("move_user_action", name="zero gates", position=0)
c.call("remove_user_action", name="zero gates")
```

`run_user_action` waits for the end and returns the result. A failure or an abort
is reported in it rather than raised.

## How they run

* **Immediately**, not through the queue, like a write from the Manual panel. A
  paused sweep does not hold an action back.
* With the same namespace as [scripts](queue-scripts.md#scripts): `get`, `set`,
  `set_many`, `set_raw`, `sleep`, `wait_until`, `action`, `instrument`, `print`,
  `log`, `np`, `math` and `state` (the same dict as scripts).
* Alias and composite limits and ramp rates apply to every `set`.
* Each run has its own abort control. **Abort**, or *abort all*, stops it at the next
  line, sleep or ramp step.
* One run of the same action at a time: running it again while it runs is refused.

Sweeps cannot be started from an action; queue them instead.

## Actions calling actions

```python
user_action('gates to zero')
set('B', 0)
```

An action can run another with `user_action('name')`. An error or abort in the inner
one stops the outer one too. An action that ends up calling itself is refused,
with the chain in the message (`action loop1 calls itself (loop1 → loop2 → loop1)`).

## Examples

**Outputs off** (with *ask before running*):

```python
for k in range(1, 9):
    set_raw('cyg.output%d' % k, 'OFF')
action('smu.reset')
```

**Bring a cryostat to base temperature and report:**

```python
set('Tset', 0.01)
wait_until(lambda: get('T') < 0.05, timeout=7200, interval=10)
log('at base: %.1f mK' % (1000 * get('T')))
```

**Park the gates where the last good sweep left them:**

```python
set_many(state.get('parking', {'Vx': 0, 'Vy': 0}))
```

## Permissions

Defining an action is writing code that runs on the backend computer, so over
[remote access](remote.md#roles) it needs the admin role. Running and aborting
actions is an operator right.
