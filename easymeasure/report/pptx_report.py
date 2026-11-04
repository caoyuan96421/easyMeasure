"""Append a summary slide for a run to a PowerPoint file.

The file is written atomically (temp file + rename).  If the target is locked
(e.g. open in PowerPoint on Windows) the slide goes to ``<name>_pending.pptx``
next to it instead, so no result is ever lost.
"""

from __future__ import annotations

import io
import os
import threading
import time
from pathlib import Path
from typing import Optional

from .render import render_png

_LOCK = threading.Lock()


def _summary_lines(ds) -> list:
    h = ds.header
    d = h.get("definition", {})
    lines = []
    started = h.get("started")
    if started:
        lines.append("Started: " + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started)))
    ft = ds.footer or {}
    if ft.get("ended") and started:
        lines.append("Duration: %.1f min" % ((ft["ended"] - started) / 60.0))
    lines.append("Status: %s   points: %d / %d" % (ds.status, ds.points, ds.total_points))
    if ft.get("error"):
        lines.append("Error: " + str(ft["error"])[:200])
    for k, dim in enumerate(d.get("dimensions", [])):
        sw = []
        for s in dim.get("sweep", []):
            if "start" in s and "stop" in s and not s.get("mapping"):
                sw.append("%s: %g→%g" % (s["channel"], float(s["start"]), float(s["stop"])))
            elif s.get("mapping"):
                sw.append("%s: f(t)=%s" % (s["channel"], s["mapping"]))
            else:
                sw.append("%s: list" % s["channel"])
        lines.append("Dim %d (%d pts): %s | rec: %s" % (
            k + 1, dim.get("points", 0), ", ".join(sw) or "-", ", ".join(dim.get("record", [])) or "-"))
    for ph, vals in (("Pre", ds.pre), ("Post", ds.post)):
        if vals:
            txt = ", ".join("%s=%s" % (k, _short(v)) for k, v in list(vals.items())[:8])
            lines.append("%s: %s" % (ph, txt))
    consts = d.get("pre", {}).get("constants", [])
    if consts:
        lines.append("Constants: " + ", ".join("%s=%s" % (c["channel"], c["value"]) for c in consts[:8]))
    meta = d.get("metadata") or {}
    for k, v in list(meta.items())[:4]:
        lines.append("%s: %s" % (k, v))
    if h.get("file"):
        lines.append("File: " + str(h["file"]))
    return lines


def _short(v):
    try:
        import numpy as np
        if isinstance(v, (float, int)):
            return "%.6g" % v
        if isinstance(v, np.ndarray):
            return "array%s" % (v.shape,)
    except Exception:
        pass
    return str(v)[:40]


def _add_slide(prs, ds, title, png) -> None:
    from pptx.util import Inches, Pt
    layout = prs.slide_layouts[6] if len(prs.slide_layouts) > 6 else prs.slide_layouts[-1]
    slide = prs.slides.add_slide(layout)
    sw, sh = prs.slide_width, prs.slide_height
    tb = slide.shapes.add_textbox(Inches(0.3), Inches(0.15), sw - Inches(0.6), Inches(0.6))
    p = tb.text_frame.paragraphs[0]
    p.text = title or "Run %s - %s" % (ds.header.get("run", "?"), ds.header.get("name", ""))
    p.font.size = Pt(24)
    p.font.bold = True
    text_w = Inches(3.6)
    pic_left = Inches(0.3)
    pic_w = sw - text_w - Inches(0.7)
    pic = slide.shapes.add_picture(io.BytesIO(png), pic_left, Inches(0.85), width=pic_w)
    max_h = sh - Inches(1.0)
    if pic.height > max_h:                       # tall figure on a 4:3 slide
        ratio = max_h / pic.height
        pic.height = int(max_h)
        pic.width = int(pic.width * ratio)
    box = slide.shapes.add_textbox(sw - text_w - Inches(0.2), Inches(0.85), text_w,
                                   sh - Inches(1.1))
    tf = box.text_frame
    tf.word_wrap = True
    for i, line in enumerate(_summary_lines(ds)):
        para = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        para.text = line
        para.font.size = Pt(10)


def _new_presentation(like: Optional[Path] = None):
    """An empty presentation with the slide size of *like* (if readable)."""
    from pptx import Presentation
    from pptx.util import Inches
    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    if like is not None and like.exists():
        try:
            ref = Presentation(str(like))
            prs.slide_width, prs.slide_height = ref.slide_width, ref.slide_height
        except Exception:
            pass
    return prs


def _insert_via_powerpoint(target: Path, single: Path) -> bool:
    """Windows: if *target* is open in PowerPoint (and therefore locked), ask the
    running PowerPoint to insert the slide(s) of *single* at the end and save.
    Returns False if PowerPoint / pywin32 is not available or the file is not open."""
    if os.name != "nt":
        return False
    try:
        import pythoncom  # type: ignore
        import win32com.client  # type: ignore
    except ImportError:
        return False
    pythoncom.CoInitialize()
    try:
        try:
            app = win32com.client.GetActiveObject("PowerPoint.Application")
        except Exception:
            return False
        want = os.path.normcase(os.path.abspath(str(target)))
        for i in range(1, app.Presentations.Count + 1):
            pres = app.Presentations.Item(i)
            try:
                full = os.path.normcase(os.path.abspath(pres.FullName))
            except Exception:
                continue
            if full != want:
                continue
            n = pres.Slides.Count
            pres.Slides.InsertFromFile(str(single), n, 1, -1)   # all slides of *single*
            pres.Save()
            return True
        return False
    finally:
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass


def append_run_slide(pptx_path, ds, title: Optional[str] = None, channels=None,
                     cmap: str = "viridis") -> Path:
    """Append a summary slide for *ds* to *pptx_path* (created if missing).

    Works while the file is open in PowerPoint: on Windows the open presentation
    is updated through PowerPoint itself and saved.  If the file is locked by
    something else (sync client, another computer), the write is retried and, as a
    last resort, the slide goes to ``<name>_pending.pptx`` - nothing is lost.
    """
    from pptx import Presentation

    target = Path(pptx_path)
    png = render_png(ds, channels=channels, cmap=cmap)
    with _LOCK:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.stem + ".tmp" + target.suffix)
        single = target.with_name(target.stem + ".slide.tmp" + target.suffix)
        try:
            for attempt in range(3):
                # 1) normal case: rewrite the file atomically
                try:
                    prs = Presentation(str(target)) if target.exists() else _new_presentation()
                    _add_slide(prs, ds, title, png)
                    prs.save(str(tmp))
                    _replace(tmp, target)
                    return target
                except PermissionError:
                    pass
                # 2) locked because it is open in PowerPoint: let PowerPoint add it
                one = _new_presentation(like=target)
                _add_slide(one, ds, title, png)
                one.save(str(single))
                try:
                    if _insert_via_powerpoint(target, single):
                        return target
                except Exception as exc:  # COM errors: fall through to retry/pending
                    import logging
                    logging.getLogger("easymeasure.report").warning(
                        "PowerPoint could not insert the slide: %s", exc)
                _sleep(0.5 * (attempt + 1))    # transient lock (sync, antivirus)
            # 3) still locked: keep the slide in a side file
            alt = target.with_name(target.stem + "_pending" + target.suffix)
            prs = Presentation(str(alt)) if alt.exists() else _new_presentation(like=target)
            _add_slide(prs, ds, title, png)
            prs.save(str(tmp))
            _replace(tmp, alt)
            return alt
        finally:
            for f in (tmp, single):
                try:
                    if f.exists():
                        f.unlink()
                except OSError:
                    pass


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _replace(src: Path, dst: Path) -> None:
    os.replace(src, dst)
