"""Run slow local work (file loading, PPT rendering) off the GUI thread."""

from __future__ import annotations

import traceback
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal


class _Signals(QObject):
    done = Signal(object)
    failed = Signal(str)


class _Task(QRunnable):
    def __init__(self, fn: Callable[[], Any]):
        super().__init__()
        self.fn = fn
        self.signals = _Signals()

    def run(self):
        try:
            res = self.fn()
        except Exception as exc:
            traceback.print_exc()
            self.signals.failed.emit("%s: %s" % (type(exc).__name__, exc))
        else:
            self.signals.done.emit(res)


_keep = set()


def run_in_background(fn: Callable[[], Any], on_done=None, on_fail=None) -> None:
    """Execute *fn* in the global thread pool; callbacks run on the GUI thread."""
    task = _Task(fn)
    _keep.add(task.signals)

    def _done(res):
        _keep.discard(task.signals)
        if on_done:
            on_done(res)

    def _fail(err):
        _keep.discard(task.signals)
        if on_fail:
            on_fail(err)
    task.signals.done.connect(_done)
    task.signals.failed.connect(_fail)
    QThreadPool.globalInstance().start(task)
