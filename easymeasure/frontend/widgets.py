"""Small layout/widget helpers that keep the GUI freely resizable."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import (QAbstractScrollArea, QApplication, QComboBox, QLabel, QLayout, QSizePolicy, QStyle,
                               QWidgetItem)


class FlowLayout(QLayout):
    """Lays widgets out left to right and wraps onto new rows when narrow.

    Used for toolbars / button rows so they never force a large minimum width.
    """

    def __init__(self, parent=None, margin: int = 0, hspacing: int = 6, vspacing: int = 4):
        super().__init__(parent)
        self._items = []
        self._h = hspacing
        self._v = vspacing
        self.setContentsMargins(margin, margin, margin, margin)

    # QLayout interface -------------------------------------------------------
    def addItem(self, item):
        self._items.append(item)

    def addStretch(self, *_):
        """Compatibility with QBoxLayout code: a flow layout has no stretch."""

    def insertWidget(self, index, w):
        self.addChildWidget(w)
        self._items.insert(index, QWidgetItem(w))
        self.invalidate()

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientations(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, width, 0), test=True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, test=False)

    def sizeHint(self):
        # preferred: everything on one row
        w = h = 0
        for it in self._items:
            if it.widget() is not None and not it.widget().isVisibleTo(it.widget().parentWidget()):
                continue
            s = it.sizeHint()
            w += s.width() + self._h
            h = max(h, s.height())
        m = self.contentsMargins()
        return QSize(w + m.left() + m.right(), h + m.top() + m.bottom())

    def minimumSize(self):
        size = QSize()
        for it in self._items:
            size = size.expandedTo(it.minimumSize())
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _do_layout(self, rect, test):
        m = self.contentsMargins()
        r = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y, line_h = r.x(), r.y(), 0
        for it in self._items:
            w = it.widget()
            if w is not None and not w.isVisibleTo(w.parentWidget()):
                continue
            hint = it.sizeHint()
            nx = x + hint.width() + self._h
            if nx - self._h > r.right() and line_h > 0:
                x = r.x()
                y += line_h + self._v
                nx = x + hint.width() + self._h
                line_h = 0
            if not test:
                it.setGeometry(QRect(QPoint(x, y), QSize(min(hint.width(), r.width()),
                                                         hint.height())))
            x = nx
            line_h = max(line_h, hint.height())
        return y + line_h - rect.y() + m.bottom()


def shrinkable_label(label: QLabel, wrap: bool = True) -> QLabel:
    """A label whose minimum width does not depend on its (changing) text."""
    if wrap:
        label.setWordWrap(True)
    sp = label.sizePolicy()
    sp.setHorizontalPolicy(QSizePolicy.Ignored)
    label.setSizePolicy(sp)
    label.setMinimumWidth(40)
    return label


class ElidedLabel(QLabel):
    """One-line plain-text label that uses the width it is given and ends in "…" when
    it has to (full text in the tooltip); it never forces its row wider."""

    def __init__(self, text: str = "", parent=None, bold: bool = False):
        super().__init__(parent)
        self._full = ""
        if bold:
            f = self.font()
            f.setBold(True)
            self.setFont(f)
        sp = self.sizePolicy()
        sp.setHorizontalPolicy(QSizePolicy.Ignored)
        self.setSizePolicy(sp)
        self.setMinimumWidth(30)
        self.setText(text)

    def setText(self, text: str) -> None:
        self._full = text or ""
        self.setToolTip(self._full)
        self._fit()

    def fullText(self) -> str:
        return self._full

    def _fit(self) -> None:
        shown = self.fontMetrics().elidedText(self._full, Qt.ElideRight, max(0, self.width()))
        QLabel.setText(self, shown)

    def resizeEvent(self, ev) -> None:
        super().resizeEvent(ev)
        self._fit()

    def changeEvent(self, ev) -> None:
        super().changeEvent(ev)
        if ev.type() == QEvent.FontChange:
            self._fit()

    def sizeHint(self):
        sh = super().sizeHint()
        sh.setWidth(self.fontMetrics().horizontalAdvance(self._full) + 4)
        return sh


class _ShrinkFilter(QObject):
    """Application-wide: combo boxes size to a short minimum instead of their
    longest entry (long channel names, axis labels, file names ...)."""

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Polish and isinstance(obj, QAbstractScrollArea):
            # tables, lists, text editors scroll anyway: let layouts shrink them
            if obj.minimumWidth() == 0 and obj.minimumHeight() == 0:
                obj.setMinimumSize(60, 45)
        if ev.type() == QEvent.Polish and isinstance(obj, QComboBox):
            if obj.sizeAdjustPolicy() != QComboBox.AdjustToMinimumContentsLengthWithIcon:
                obj.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
                if obj.minimumContentsLength() == 0:
                    obj.setMinimumContentsLength(6)
        return False


def install_resize_friendly_defaults(app: QApplication) -> None:
    if getattr(app, "_em_shrink_filter", None) is None:
        f = _ShrinkFilter(app)
        app.installEventFilter(f)
        app._em_shrink_filter = f
