# PowerPoint reports

When a run ends, a summary slide is appended to the PowerPoint file. This happens
whether the run ended normally, by error, by abort or by `end_sweep()`. The slide has:

* a figure with one panel per recorded numeric channel (up to six): lines for 1-D
  data, heat maps for 2-D data;
* the run summary:
    * start time, duration, status and points done;
    * the error, if any;
    * each dimension's swept channels and ranges and its recorded channels;
    * pre/post values and the pre-sweep constants;
    * metadata and the file path.

## Setting it up

*File → Settings → PowerPoint file*, or the first-start dialog:

??? example "From Python"

    ```python
    c.call("set_settings", ppt_path="D:/lab/notebook.pptx", auto_ppt=True,
           ppt_colormap="viridis")
    ```

| setting | meaning |
|---|---|
| `ppt_path` | the `.pptx` slides are appended to (`""` = no slides); created if missing |
| `auto_ppt` | append a slide automatically at the end of every run |
| `ppt_colormap` | colormap of the heat maps |

A sweep with `"ppt": false` in its definition gets no automatic slide.

## Slides on demand

*Add slide to PPT* (toolbar and data views) adds a slide for the running run, or any
loaded data file, at once:

??? example "From Python"

    ```python
    c.call("make_ppt")                                # the running / last run
    c.call("make_ppt", path="D:/data/2026-11-11/00042_map.emj")
    c.call("make_ppt", path=run, ppt_path="D:/lab/talk.pptx")   # into another file
    ```

## The file may be open in PowerPoint

On Windows the slide is inserted into the **open presentation** through
PowerPoint itself (via `pywin32`, installed automatically on Windows), and the
presentation is saved. You can keep the lab notebook open all day.

The slide goes to `<name>_pending.pptx` next to it, so it is never lost, only if
that is impossible. This happens, for example, when the file is locked by a sync
client or open on another computer.

## Remote users

Operators can add slides, and the web app can download the PowerPoint file.
