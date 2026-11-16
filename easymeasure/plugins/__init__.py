"""Plugins: add-ons to the measurement engine with panels and processing of their own.

Write a plugin by subclassing :class:`Plugin` (see :mod:`easymeasure.plugins.api`)
and building its panels from :mod:`easymeasure.plugins.ui`::

    from easymeasure.plugins import Plugin, Channel, ui

    class Clock(Plugin):
        TITLE = "Clock"

        def start(self):
            self.ui.add_panel("clock", "Clock", ui.group("Time", ui.seg7("t", digits=8)))
            self.every(1.0, lambda: self.ui.set("t", value=time.strftime("%H-%M-%S")))

The plugins that come with easyMeasure are in :mod:`easymeasure.plugins.builtin`;
your own go into ``<home>/plugins`` (one ``.py`` file or a folder each).
"""

from . import ui
from .api import Action, BackendAPI, Channel, Plugin, PluginStopped, Timer

__all__ = ["Plugin", "Channel", "Action", "BackendAPI", "PluginStopped", "Timer", "ui"]
