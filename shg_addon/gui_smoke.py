import sys, os
from pathlib import Path
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer, QSettings
from PySide6.QtGui import QFontDatabase,QFont
from shg_addon.gui import SHGWindow

QSettings.setDefaultFormat(QSettings.IniFormat)
QSettings.setPath(QSettings.IniFormat,QSettings.UserScope,str(Path(os.environ['EASYMEASURE_HOME'])/'gui-settings'))
app=QApplication([])
QFontDatabase.addApplicationFont("C:/Windows/Fonts/segoeui.ttf")
app.setFont(QFont("Segoe UI",10))
w=SHGWindow()
w.show()
def finish():
    assert w.left_tabs.tabText(0)=="SHG"
    assert any(w.data_tabs.tabText(i)=="Microscope" for i in range(w.data_tabs.count()))
    if len(sys.argv)>1:w.grab().save(sys.argv[1])
    w.timer.stop();w.preview_timer.stop();w.conn.stop();w.close()
    app.quit()
QTimer.singleShot(200,finish)
status=app.exec()
print("GUI startup and event-loop shutdown passed")
sys.exit(status)
