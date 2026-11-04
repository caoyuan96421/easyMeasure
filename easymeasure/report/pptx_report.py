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


def append_run_slide(pptx_path, ds, title: Optional[str] = None, channels=None,
                     cmap: str = "viridis") -> Path:
    from pptx import Presentation
    from pptx.util import Inches, Pt

    pptx_path = Path(pptx_path)
    png = render_png(ds, channels=channels, cmap=cmap)
    with _LOCK:
        if pptx_path.exists():
            prs = Presentation(str(pptx_path))
        else:
            prs = Presentation()
            prs.slide_width = Inches(13.333)
            prs.slide_height = Inches(7.5)
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
        slide.shapes.add_picture(io.BytesIO(png), pic_left, Inches(0.85), width=pic_w)
        box = slide.shapes.add_textbox(sw - text_w - Inches(0.2), Inches(0.85), text_w,
                                       sh - Inches(1.1))
        tf = box.text_frame
        tf.word_wrap = True
        for i, line in enumerate(_summary_lines(ds)):
            para = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            para.text = line
            para.font.size = Pt(10)
        target = pptx_path
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.stem + ".tmp" + target.suffix)
        prs.save(str(tmp))
        try:
            os.replace(tmp, target)
        except PermissionError:
            alt = target.with_name(target.stem + "_pending" + target.suffix)
            os.replace(tmp, alt)
            return alt
        return target
