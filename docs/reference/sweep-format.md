# Sweep definition

A sweep is a JSON-compatible dict. See the [Sweeps tutorial](../guide/sweeps.md)
for explanations and examples. `validate_sweep` returns the normalised form, with
every default filled in.

```json
{
  "name": "gate_bias_map",
  "dimensions": [
    {"points": 101,
     "sweep":  [{"channel": "Vg", "start": -0.5, "stop": 0.5},
                {"channel": "Vc", "start": 0, "stop": 1, "mapping": "start+(stop-start)*t**2"},
                {"channel": "wave", "mapping": "np.sin(np.linspace(0, 2*np.pi, 64))*t"},
                {"channel": "Vx", "values": [0, 0.1, 0.3]}],
     "record": ["I", "spectrum"],
     "pre_delay": 0.05,
     "post_delay": 0.0,
     "snake": false,
     "record_after_inner": false,
     "label": "",
     "hooks": {"pre_record": {"enabled": true, "code": "action('dmm.trigger')"}}},
    {"points": 21, "sweep": [{"channel": "Vb", "start": -0.3, "stop": 0.3}],
     "record": ["T"]}
  ],
  "pre":  {"hook": "log('starting')", "constants": [{"channel": "dmm.mode", "value": "Vdc"}],
           "record": ["T"]},
  "post": {"record": ["T"], "constants": [{"channel": "Vg", "value": 0}], "hook": ""},
  "on_read_error": "nan",
  "on_set_error": "abort",
  "run_post_on_abort": true,
  "ppt": true,
  "metadata": {"sample": "D17", "comment": "..."}
}
```

## Top level

| field | type | default | meaning |
|---|---|---|---|
| `name` | str | `"sweep"` | used in the file name and slide title |
| `dimensions` | list | (required) | at least one; index 0 is the innermost (fastest) loop |
| `pre` | dict | `{}` | pre phase: `hook` → `constants` → `record` |
| `post` | dict | `{}` | post phase: `record` → `constants` → `hook`; always runs |
| `on_read_error` | `"nan"` / `"abort"` | `"nan"` | a failed reading: store NaN, or stop the sweep |
| `on_set_error` | `"abort"` / `"skip"` | `"abort"` | a failed setting: stop the sweep, or skip the point |
| `run_post_on_abort` | bool | `true` | run the post phase after an abort too |
| `ppt` | bool | `true` | append a PowerPoint slide at the end (if a file is configured) |
| `metadata` | dict | `{}` | free key/values, stored in the header and shown on the slide |

## Dimension

| field | type | default | meaning |
|---|---|---|---|
| `points` | int ≥ 1 | (required) | number of points (at most 10 000 000; whole sweep at most 100 000 000) |
| `sweep` | list | `[]` | swept channels (below); empty = repeat `points` times |
| `record` | list of names | `[]` | channels recorded at every point of this dimension |
| `pre_delay` | s ≥ 0 | 0 | after setting, before recording |
| `post_delay` | s ≥ 0 | 0 | after recording |
| `snake` | bool | `false` | alternate the direction on each pass of the outer loop |
| `record_after_inner` | bool | `false` | (outer dimensions) run the inner dimensions before recording |
| `label` | str | `""` | a name for the dimension, shown in the editor |
| `hooks` | dict | all off | [point actions](../guide/point-actions.md): `pre_sweep`, `post_sweep`, `pre_record`, `post_record`, each `{"enabled": bool, "code": str}` (a bare string counts as enabled code) |

A channel may be recorded in only one dimension.

## Swept channel

Each entry has `channel` (an alias, composite variable, mapping or writable channel)
and one of:

| fields | setpoints |
|---|---|
| `start`, `stop` | `linspace(start, stop, points)` |
| `values` | the list (its length must be `points`) |
| `mapping` (+ optional `start`, `stop`) | an [expression](expressions.md) of `i`, `N`, `t = i/(N-1)`, `start`, `stop`, `x` (the linear value) and numpy; required for vector / array channels |

Text channels cannot be swept.

## Pre / post phase

| field | type | meaning |
|---|---|---|
| `hook` | str | Python code (script API), run first (pre) or last (post) |
| `constants` | list of `{channel, value}` | values set once; configuration channels are typical |
| `record` | list of names | channels recorded once, stored in the file's `pre` / `post` |

## Recording names

* aliases, composite channels, `instrument.channel` references (configuration
  channels included);
* `@time`: Unix time of the recording;
* `@elapsed`: seconds since the sweep started.

## Order at each point

```
pre_sweep → set swept channels → post_sweep → pre_delay → pre_record → record
          → post_record → post_delay → inner dimensions
```

With `record_after_inner`, the inner dimensions run right after `pre_delay`.

## Flow-control calls (point actions only)

| call | like | effect |
|---|---|---|
| `skip_point()` | `continue` | rest of the point skipped, its data NaN |
| `skip_line()` | `break` | the rest of this dimension's line skipped |
| `end_sweep()` | `exit` | status `terminated`; post phase runs |

## Data shape

A channel recorded in dimension *k* of a sweep with points `(N_0, …, N_{D-1})` is
stored as an array of shape `(N_k, …, N_{D-1}) + value_shape`.
