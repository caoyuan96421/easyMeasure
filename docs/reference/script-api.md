# Script API

Python code runs in the backend in five places, all with the same namespace:

* queued **scripts**;
* a sweep's **pre/post hooks**;
* **point actions**;
* **user actions**;
* `run_script_now`.

The few differences are listed at the end.

## Channels

### `get(name)`

Read an alias, a composite channel, `instrument.channel`, `@time` or `@elapsed`.
Returns a float, a numpy array or a string.

```python
I = get('I')
spec = get('spectrum')          # numpy array
mode = get('dmm.mode')          # 'Vdc'
```

### `set(name, value, ramp=True)`

Protected write to an alias, composite variable, mapping or configuration channel.
Limits are checked first, and with `ramp` the value is approached at the ramp rate(s).
The call waits for the end of the ramp and returns the value set.

```python
set('Vg', 0.25)
set('Vg', 0, ramp=False)        # jump
set('lockin.sensitivity', '1e-3')
```

### `set_many({name: value, …}, ramp=True)`

Set several channels together, e.g. all variables of a composite (required for
their first setting). Everything is limit-checked before anything is written, and
the ramp moves all of them together. Returns the hardware values set.

```python
set_many({'eps': 0.0, 'U': 0.3})
```

### `set_raw('instrument.channel', value)`

An **unprotected** write to an instrument channel: no alias limits, no ramp. Use it
for channels that have no alias, with care.

## Waiting

### `sleep(seconds)`

An abortable sleep. Always use it instead of `time.sleep`, so pause and abort work.

### `wait_until(condition, timeout=60, interval=0.2)`

Polls `condition()` until it is true. Returns `True`, or `False` after `timeout`
seconds. It does not raise.

```python
if not wait_until(lambda: get('magnet.status') == 'HOLDING', timeout=600):
    log('magnet did not settle')
```

### `check()`

Honour pause / abort now. This is rarely needed, because abort is checked on every
line anyway.

## Instruments

### `action('instrument.action')`

Run an instrument action, e.g. `action('lockin.auto_gain')`.

### `instrument(name)`

A guarded proxy of the instrument:

| member | does |
|---|---|
| `.raw_query(cmd)` | send a command, return the reply |
| `.raw_write(cmd)` | send a command |
| `.read(channel)` | read a channel of this instrument |
| `.action(name)` | run an action |
| `.call(method, *args, **kwargs)` | call a driver method, guarded |
| `.state` | `connected`, `disconnected`, `error` … |

```python
smu = instrument('smu')
log(smu.raw_query('*IDN?'))
```

## Sweeps and actions

| function | does | where |
|---|---|---|
| `run_sweep(definition)` | run a sweep synchronously and return its data: a [`DataSet`](api/easymeasure/backend/storage.md) with `.status` (`finished`, `aborted`, `error`, `terminated`), `.data`, `.setpoints`, `.value_axes`, `.path` | queued scripts |
| `load_sweep(path)` | a sweep definition (a dict) from a `.json` file saved by the sweep editor, or from a data file (`.emj`, `.h5`); change it, then `run_sweep()` it | everywhere |
| `user_action(name)` | run a user action; its errors stop the caller | user actions |
| `skip_point()`, `skip_line()`, `end_sweep()` | [flow control](../guide/point-actions.md#flow-control) | point actions |

## Output and state

| name | |
|---|---|
| `log(msg)`, `print(…)` | write to the backend log (and the GUI's log dock) |
| `warn(msg)` | the same, as a warning |
| `state` | a dict kept in the backend between scripts, hooks and user actions |
| `np`, `math`, `time` | modules |

## Point-action extras

| name | |
|---|---|
| `k` | the dimension |
| `i` | the index in it |
| `index` | the indices of this and the outer dimensions |
| `setpoints` | channel → value at this point |
| `values` | (`post_record`) what was just recorded |

## Differences between the places

| | scripts | hooks | point actions | user actions |
|---|---|---|---|---|
| runs | from the queue | at the start / end of a sweep | at every point | at once, on request |
| `run_sweep` | ✓ | – | – | – |
| `user_action` | – | – | – | ✓ |
| flow control | – | – | ✓ | – |
| pause | at the next line | with the sweep | with the sweep | not affected |
| abort | queue abort | sweep abort | sweep abort | `abort_user_action` / abort all |
| remote role | admin | admin | admin | admin to define, operator to run |
