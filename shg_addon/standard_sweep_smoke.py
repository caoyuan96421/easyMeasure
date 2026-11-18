"""Offline UI checks: never connects to the user's live backend or devices."""
import copy
import sys
from pathlib import Path
from PySide6.QtCore import QSettings
from PySide6.QtGui import QFontDatabase, QFont
from PySide6.QtWidgets import QApplication
from easymeasure.frontend.connection import BackendConnection
from shg_addon.gui import SHGWindow
from shg_addon.actions import action_code
from shg_addon.sweep_tools import preset, add_action

root = Path(sys.argv[1])
QSettings.setDefaultFormat(QSettings.IniFormat)
QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(root / 'settings'))
BackendConnection.start = lambda self: None
app = QApplication([])
QFontDatabase.addApplicationFont('C:/Windows/Fonts/segoeui.ttf')
app.setFont(QFont('Segoe UI', 10))
app.setStyle('Fusion')
w = SHGWindow()
w.timer.stop()
w.preview_timer.stop()
requests = []
w.conn.request = lambda *args, **kwargs: requests.append((args, kwargs))
left = [w.left_tabs.tabText(i) for i in range(w.left_tabs.count())]
right = [w.data_tabs.tabText(i) for i in range(w.data_tabs.count())]
assert 'SHG measurement' not in left + right
assert 'Sweep editor' in left
assert 'SHG spectra + CWT' in right and 'CWT diagnostic' in right
w.update_mode([])  # Covers the deleted measurement-mode label reference.
w.open_shg_sweep()
loaded = w.editor.definition()
original = preset()
from easymeasure.backend.sweep import normalize_definition
assert normalize_definition(loaded)['dimensions'] == normalize_definition(original)['dimensions'], (loaded['dimensions'], original['dimensions'])
assert loaded['pre'] == dict(original['pre'], record=[])
assert w.left_tabs.currentWidget() is w._scrolled[w.editor]
assert not requests  # Loading a preset does not write hardware or queue a scan.

# Insertion respects the selected dimension and preserves all existing fields.
outer = copy.deepcopy(original['dimensions'][0])
outer.update(points=2, sweep=[dict(channel='V1', start=0, stop=.1)], record=[], hooks={})
w.editor.add_dimension(outer)
before = w.editor.definition()
add_action(w.editor, 'reference', 'pre_record')
after = w.editor.definition()
assert after['dimensions'][0] == before['dimensions'][0]
assert after['dimensions'][1]['hooks']['pre_record']['enabled']
assert after['dimensions'][1]['hooks']['pre_record']['code'].strip() == action_code('reference')
add_action(w.editor, 'reference', 'pre_record')
assert w.editor.definition() == after  # Repeated insert doesn't double-acquire.
assert after['pre'] == before['pre'] and after['post'] == before['post']

w.open_shg_sweep()
w.resize(1600, 1000)
w.show()
app.processEvents()
w.grab().save(str(root / 'standard_sweep.png'))
w.data_tabs.setCurrentWidget(w.shg_plot_widget)
app.processEvents()
w.grab().save(str(root / 'spectra_cwt.png'))
w.conn.stop()
w.close()
print('Offline UI: no SHG measurement tabs; standard editor, action insertion, spectra and CWT passed.')
