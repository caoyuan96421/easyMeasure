"""SHG-specific output-only behavior around the upstream NI USB-6211 driver.

Keep these lab options in the add-on, leaving the upstream driver untouched.
"""
import math
from easymeasure.drivers.ni6211 import NI6211
from easymeasure.common.errors import ChannelError


class SHGNI6211(NI6211):
    DEFAULT_OPTIONS = dict(NI6211.DEFAULT_OPTIONS, start_counters=True,
                           ao_min=-10., ao_max=10., digital_readback='hardware')

    def connect(self):
        self.ao_min = float(self.options['ao_min'])
        self.ao_max = float(self.options['ao_max'])
        if not -10 <= self.ao_min < self.ao_max <= 10:
            raise ChannelError('ao_min/ao_max must satisfy -10 <= min < max <= 10 V')
        if self.options['digital_readback'] not in ('hardware', 'commanded'):
            raise ChannelError('digital_readback must be hardware or commanded')
        self.do_last = {}
        super().connect()

    def _start_counters(self):
        if self.options['start_counters']:
            super()._start_counters()

    def _ao_task(self, k):
        return self._task('ao%d' % k, lambda t: t.ao_channels.add_ao_voltage_chan(
            '%s/ao%d' % (self.device, k), min_val=self.ao_min, max_val=self.ao_max))

    def read(self, channel):
        if channel in {'do0','do1','do2','do3'} and self.options['digital_readback'] == 'commanded':
            return self.do_last.get(int(channel[-1]), math.nan)
        return super().read(channel)

    def daq_read(self, channel):
        # Current upstream dispatches through DaqBase; retain commanded DO
        # readback there too. Older versions call read() directly.
        if channel in {'do0','do1','do2','do3'} and self.options['digital_readback'] == 'commanded':
            return self.do_last.get(int(channel[-1]), math.nan)
        return super().daq_read(channel)

    def write(self, channel, value):
        if channel in ('ao0','ao1') and not self.ao_min <= float(value) <= self.ao_max:
            raise ChannelError('analog outputs are limited to %g..%g V' % (self.ao_min, self.ao_max))
        super().write(channel, value)
        if channel in {'do0','do1','do2','do3'}:
            self.do_last[int(channel[-1])] = float(bool(value))

    def daq_write(self, channel, value):
        if channel in ('ao0','ao1') and not self.ao_min <= float(value) <= self.ao_max:
            raise ChannelError('analog outputs are limited to %g..%g V' % (self.ao_min, self.ao_max))
        super().daq_write(channel, value)
        if channel in {'do0','do1','do2','do3'}:
            self.do_last[int(channel[-1])] = float(bool(value))
