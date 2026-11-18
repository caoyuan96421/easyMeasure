"""Small SHG helpers attached to the stock SweepEditor, not another editor."""
import json
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QGroupBox, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QWidget, QDoubleSpinBox
import math
from shg_addon.actions import ACTIONS, action_code

PRESETS = (
    ('SHG half-wave preset', 'shg_halfwave',
     'Half-wave 28–208 degrees, 61 points, 1 s exposure'),
    ('SHG half-wave + MEMS preset', 'shg_halfwave_mems',
     '61 half-wave points at each MEMS.theta angle: −10 to +10 degrees, 21 positions'),
    ('SHG half-wave + MEMS + z preset', 'shg_halfwave_mems_z',
     'Half-wave inside MEMS inside Vz; verifies Vlock 70 V before changing Vz'),
)


def preset(name='shg_halfwave', z_start=0., z_stop=40., z_step=5.):
    if name not in {item[1] for item in PRESETS}:
        raise ValueError('Unknown SHG sweep preset: ' + str(name))
    definition = json.loads((Path(__file__).parent / 'presets' / (name + '.json')).read_text())
    if name == 'shg_halfwave_mems_z':
        start, stop, step = float(z_start), float(z_stop), float(z_step)
        if not all(math.isfinite(x) for x in (start, stop, step)) or not (0 <= start <= 40 and 0 <= stop <= 40) or step <= 0 or start == stop:
            raise ValueError('Choose different Vz start/stop values within 0..40 V and a positive step')
        intervals = abs(stop-start)/step
        if not math.isclose(intervals, round(intervals), abs_tol=1e-8, rel_tol=0) or intervals < 1 or intervals > 10000:
            raise ValueError('Vz step must divide the selected range exactly (1..10000 intervals)')
        dim = definition['dimensions'][2]
        dim['points'] = int(round(intervals))+1
        dim['sweep'][0].update(start=start, stop=stop)
    return definition


def add_action(editor, name, position):
    """Append without reloading the definition or losing metadata/editor state."""
    if position == 'pre_record':
        index = editor.dim_list.currentRow()
        if index < 0:
            raise ValueError('Select a sweep dimension first')
        dimension = editor.dims[index]
        target = dimension.hooks['pre_record'].code
        dimension.hook_checks['pre_record'].setChecked(True)
        editor.tabs.setCurrentIndex(0)
    elif position in ('pre', 'post'):
        target = getattr(editor, position).hook
        editor.tabs.setCurrentIndex(1 if position == 'pre' else 2)
    else:
        raise ValueError('Unknown action position: ' + position)
    snippet = action_code(name)
    text = target.toPlainText().rstrip()
    # Avoid silently acquiring two frames when an already inserted action is chosen.
    if snippet not in text:
        target.setPlainText((text + '\n\n' if text else '') + snippet + '\n')


def install(window):
    editor = window.editor
    box = QGroupBox('SHG actions')
    layout = QVBoxLayout(box)
    window.shg_preset_choice = QComboBox()
    window.shg_preset_choice.setAccessibleName('SHG sweep preset')
    for label, key, hint in PRESETS:
        window.shg_preset_choice.addItem(label, key)
        window.shg_preset_choice.setItemData(window.shg_preset_choice.count()-1, hint, Qt.ToolTipRole)
    layout.addWidget(window.shg_preset_choice)
    window.shg_z_settings = QWidget()
    z_row = QHBoxLayout(window.shg_z_settings)
    z_row.setContentsMargins(0, 0, 0, 0)
    window.shg_z_fields = {}
    for key, title, value in (('z_start', 'Vz start (V)', 0.), ('z_stop', 'stop (V)', 40.), ('z_step', 'step (V)', 5.)):
        z_row.addWidget(QLabel(title))
        spin = QDoubleSpinBox(); spin.setDecimals(3)
        spin.setRange(.001 if key == 'z_step' else 0., 40.)
        spin.setValue(value); z_row.addWidget(spin)
        window.shg_z_fields[key] = spin
    window.shg_z_settings.setVisible(False)
    window.shg_preset_choice.currentIndexChanged.connect(
        lambda _: window.shg_z_settings.setVisible(window.shg_preset_choice.currentData() == 'shg_halfwave_mems_z'))
    layout.addWidget(window.shg_z_settings)
    button = QPushButton('Load selected preset')
    button.setToolTip('Load the selected preset into the editor for review; does not queue or run')
    button.clicked.connect(lambda: window.open_shg_sweep(window.shg_preset_choice.currentData()))
    layout.addWidget(button)
    window.shg_action_choice = QComboBox()
    # Actions needing numerical options remain available from the SHG controls/API.
    for name, label in ACTIONS.items():
        if name not in ('corrected_move', 'autocal'):
            window.shg_action_choice.addItem(name.replace('_', ' ').capitalize(), name)
            window.shg_action_choice.setItemData(window.shg_action_choice.count()-1, label, Qt.ToolTipRole)
    window.shg_action_choice.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
    window.shg_action_choice.setMinimumContentsLength(18)
    layout.addWidget(window.shg_action_choice)
    window.shg_action_position = QComboBox()
    for label, value in [('Before recording (selected dimension)', 'pre_record'),
                         ('Before the sweep', 'pre'), ('After the sweep', 'post')]:
        window.shg_action_position.addItem(label, value)
    layout.addWidget(window.shg_action_position)
    button = QPushButton('Insert SHG action')
    button.clicked.connect(window.insert_shg_action)
    layout.addWidget(button)
    label = QLabel('The preset captures a fresh frame before each record. Edit channels, ranges, delays and constants below.')
    label.setWordWrap(True)
    layout.addWidget(label)
    editor.layout().insertWidget(1, box)
