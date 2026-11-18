"""CWT image coordinates matching MATLAB surf(matrix), with frequency ticks."""
from pathlib import Path
import numpy as np
import pyqtgraph as pg


class CWTView(pg.ImageView):
    def __init__(self, spectrum_plot, diagnostic=False, phase=False):
        plot=pg.PlotItem()
        super().__init__(view=plot)
        self.cwt_plot=plot
        self.diagnostic=diagnostic
        self.phase=phase
        plot.setLabel('bottom','Sensor pixel (1-based)')
        plot.setLabel('left','CWT row')
        plot.showAxis('right')
        plot.setLabel('right','cycles/pixel')
        plot.getAxis('left').setWidth(60)
        plot.getAxis('right').setWidth(85)
        plot.getViewBox().invertY(False)
        plot.getViewBox().setAspectLocked(False)
        # ViewBox.setXLink aligns screen coordinates; with a histogram and a
        # second Y axis that clips the numeric range. Copy numeric limits instead.
        source=spectrum_plot.getViewBox()
        source.sigXRangeChanged.connect(lambda view,limits:plot.setXRange(*limits,padding=0))
        plot.setXRange(*source.viewRange()[0],padding=0)
        self.setColorMap(pg.colormap.get('viridis'))
        self.ui.roiBtn.hide();self.ui.menuBtn.hide()

    def set_cwt(self, values):
        values=np.asarray(values)
        if values.ndim!=2:raise ValueError('CWT must have rows and sensor columns')
        rows,columns=values.shape
        bank=Path(__file__).with_name('diagnostic_bank.npz' if self.diagnostic else 'cwt_bank.npz')
        if bank.exists():
            with np.load(bank) as data:
                frequencies=data['frequencies'].copy()
                if int(data['n'])!=columns:raise ValueError('CWT pixel count differs from frequency bank')
        else:
            if self.diagnostic:raise FileNotFoundError('Diagnostic frequency bank missing')
            frequencies=.1*2**(-np.arange(34)/10)
            frequencies=frequencies[frequencies>=.01]
        if len(frequencies)!=rows:raise ValueError('CWT row count differs from frequency bank')
        # ImageItem cells begin at the supplied origin. Half-pixel offsets put
        # the first cell's center at (pixel=1, row=1), as in MATLAB surf(matrix).
        self.setImage(values,axes=dict(x=1,y=0),pos=(.5,.5),scale=(1,1),autoRange=False,
                      autoLevels=not self.phase,levels=(-np.pi,np.pi) if self.phase else None)
        self.cwt_plot.getViewBox().invertY(False)
        self.cwt_plot.setYRange(.5,rows+.5,padding=0)
        indices=np.unique(np.linspace(0,rows-1,min(rows,4)).round().astype(int))
        self.cwt_plot.getAxis('left').setTicks([[(int(i)+1,str(int(i)+1)) for i in indices]])
        self.cwt_plot.getAxis('right').setTicks([[(int(i)+1,'%.4g'%frequencies[i]) for i in indices]])
        self.frequencies=frequencies
