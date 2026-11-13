# Script API

Python code runs in the backend in five places, all with the same namespace:

* queued **scripts**;
* a sweep's **pre/post hooks**;
* **point actions**;
* **user actions**;
* `run_script_now`.

The few differences are listed at the end.

## Channels

Every channel is read with `get` and written with `set`, whatever its kind: an
alias, a composite channel (`nD.n`), a parameter, a user action, `@time`, or a
channel, setting or action of an instrument (`dmm.V`, `lockin.sensitivity`,
`dmm.reset`). Its access (r, w, rw) is respected: `get` of an action or `set` of a
read-only channel raises.

### `get(name)`

Read any readable channel: an alias, a composite channel, a parameter,
`instrument.channel`, `@time` or `@elapsed`. Returns a float, a numpy array or a
string.

```python
I = get('I')
spec = get('spectrum')          # numpy array
mode = get('dmm.mode')          # 'Vdc'
```

### `set(name, value=None, ramp=True)`

Write any writable channel. An alias or composite channel keeps its limits (checked
first) and, with `ramp`, is approached at the ramp rate(s); a channel of an
instrument is written as it is (no limits, no ramp); a value parameter takes the
value. Writing an action runs it; it needs no value. The call waits for the end of
the ramp (or of the action) and returns the value set.

```python
set('Vg', 0.25)
set('Vg', 0, ramp=False)        # jump
set('lockin.sensitivity', '1e-3')
set('bg.voltage', 0)            # the instrument's channel: no limits, no ramp
set('Cb', 0.6)                  # a value parameter
set('lockin.auto_gain')         # an instrument action
set('gates_to_zero')            # a user action
```

### `set_many({name: value, …}, ramp=True)`

Set several channels together, e.g. all variables of a composite (required for
their first setting). Everything is limit-checked before anything is written, and
the ramp moves all of them together. Parameters are set first, actions run last.
Returns every value set.

```python
set_many({'gates.eps': 0.0, 'gates.U': 0.3})
```

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

### `instrument(name)`

A guarded proxy of the instrument:

| member | does |
|---|---|
| `.raw_query(cmd)` | send a command, return the reply |
| `.raw_write(cmd)` | send a command |
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
| `skip_point()`, `skip_line()`, `end_sweep()` | [flow control](../guide/point-actions.md#flow-control) | point actions |

## Output and state

| name | |
|---|---|
| `log(msg)`, `print(…)` | write to the backend log (and the GUI's log dock) |
| `warn(msg)` | the same, as a warning |
| `state` | a dict kept in the backend between scripts, hooks and user actions |
| `params` | the setup's [parameters](../guide/parameters.md), read-only: `params.Cb`, `params['Cb']`, `dict(params)` (present values). Each parameter is also a name of its own (`Cb`), with its value when the script or action started, unless that name is taken by the API |
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
