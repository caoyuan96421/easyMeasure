import os
import subprocess
import sys
from shg_addon.configuration import ROOT

def main():
    os.environ["EASYMEASURE_HOME"]=str(ROOT/"state"/"backend")
    os.environ["PYTHONPATH"]=str(ROOT.parent)+os.pathsep+os.environ.get("PYTHONPATH","")
    from easymeasure.backend.server import read_runtime_file,backend_alive
    info=read_runtime_file()
    if not info or not backend_alive(info):
        subprocess.Popen([sys.executable,"-m","shg_addon.backend"],cwd=str(ROOT.parent),
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0,
            stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    from PySide6.QtWidgets import QApplication
    from shg_addon.gui import SHGWindow
    app=QApplication(sys.argv); app.setStyle("Fusion")
    w=SHGWindow(); w.show(); return app.exec()

if __name__=="__main__": sys.exit(main())
