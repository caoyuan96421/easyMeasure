"""PySide6 frontend for easyMeasure.

The GUI never talks to instruments: everything goes through the backend over a
non-blocking socket (:mod:`.connection`).  Closing or killing the GUI has no
effect on the backend.
"""
