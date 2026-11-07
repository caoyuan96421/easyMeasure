"""Interfaces: what each driver accepts, and finding ports / devices on this computer."""

import sys
import types

import pytest

from easymeasure.backend import resources
from easymeasure.backend.resources import ResourceError, VisaBackend, interface_of
from easymeasure.common.errors import ChannelError
from easymeasure.drivers.emulators import nidaqmx_sim


@pytest.mark.parametrize("address, kind", [
    ("GPIB0::24::INSTR", "gpib"), ("gpib1::5::instr", "gpib"),
    ("USB0::0x0957::0x8B18::MY51140123::INSTR", "usb"),
    ("TCPIP0::192.168.1.9::inst0::INSTR", "tcpip"), ("TCP::10.0.0.2::5025", "tcp"),
    ("COM3", "serial"), ("ASRL4::INSTR", "serial"), ("/dev/ttyUSB0", "serial"),
    ("DAQ::Dev1", "daq"), ("SIM::k24", "sim"), ("NONE", "none"), ("", "none"),
    ("PXI0::5-0.0::INSTR", "other")])
def test_interface_of(address, kind):
    assert interface_of(address) == kind


def test_drivers_declare_interfaces_and_their_suggestions_fit(backend):
    d = backend.list_drivers()["drivers"]
    for name, info in d.items():
        assert info["interfaces"], name
        assert set(info["interfaces"]) <= set(resources.INTERFACES), name
        for sg in info["suggest"]:
            assert interface_of(sg["address"]) in info["interfaces"], (name, sg)
    assert d["keithley2400"]["interfaces"] == ["gpib", "serial", "tcp", "sim"]
    assert "usb" in d["keysight_b2901"]["interfaces"]
    assert d["ni63xx"]["interfaces"] == ["daq"] and "USB-6343" in d["ni63xx"]["idn_match"]
    assert d["sim_magnet"]["interfaces"] == ["none"]
    assert d["sim_dmm"]["interfaces"] == ["sim"]
    assert {"address": "SIM::keithley2400", "description": "built-in emulator"} in \
        d["keithley2400"]["suggest"]


def test_addresses_of_other_interfaces_are_refused(backend):
    be = backend
    with pytest.raises(ChannelError, match="keithley2400 does not take NI-DAQmx addresses"):
        be.add_instrument("smu", "keithley2400", "DAQ::Dev1")
    with pytest.raises(ChannelError, match="GPIB"):
        be.add_instrument("daq", "ni63xx", "GPIB0::3::INSTR")
    with pytest.raises(ChannelError, match="no port"):
        be.add_instrument("mag", "sim_magnet", "COM3")
    assert "smu" not in be.instruments.names() and "daq" not in be.instruments.names()
    # updating to a wrong address keeps the instrument as it was
    assert be.add_instrument("smu", "keithley2400", "SIM::ifk")["state"] == "connected"
    with pytest.raises(ChannelError):
        be.update_instrument("smu", address="USB0::1::2::3::INSTR")
    assert be.instruments.get("smu").state == "connected"
    assert be.instruments.get("smu").address == "SIM::ifk"


def test_wizard_driver_interfaces():
    import copy
    from easymeasure.drivers import _command
    d = copy.deepcopy(next(t["definition"] for t in _command.templates()
                           if t["title"] == "Generic SCPI"))
    d["name"] = "wiz_ifs"
    assert d.get("interfaces", list(resources.BYTE_PORTS)) == list(resources.BYTE_PORTS)
    d["interfaces"] = ["serial", "tcp"]
    assert _command.check_definition(d)[0] == []
    cls = _command.CommandInstrument.from_definition(d)
    assert cls.INTERFACES == ("serial", "tcp")
    assert "interfaces" in _command.render_source(d)
    d["interfaces"] = ["daq"]
    assert any("interfaces" in e for e in _command.check_definition(d)[0])
    d["interfaces"] = []
    assert any("interfaces" in e for e in _command.check_definition(d)[0])


# -- discovery --------------------------------------------------------------------------
class FakeInst:
    def __init__(self, rm, addr):
        self.rm, self.addr, self.timeout = rm, addr, 2000

    def query(self, cmd):
        assert cmd == "*IDN?"
        self.rm.queried.append(self.addr)
        idn = self.rm.idn.get(self.addr)
        if idn is None:
            raise TimeoutError("VI_ERROR_TMO")
        return idn + "\n"

    def close(self):
        pass


class FakeRM:
    """Stands in for a pyvisa ResourceManager with a few instruments attached."""

    def __init__(self):
        self.idn = {"GPIB0::24::INSTR": "KEITHLEY INSTRUMENTS INC.,MODEL 2400,1234,C32",
                    "USB0::0x0957::0x8B18::MY1::INSTR": "Keysight Technologies,B2902A,MY1,3.4",
                    "GPIB0::8::INSTR": "Stanford_Research_Systems,SR830,s/n1,ver1.07"}
        self.addrs = list(self.idn) + ["GPIB0::5::INSTR", "ASRL3::INSTR", "ASRL9::INSTR"]
        self.queried = []

    def list_resources_info(self, query="?*::INSTR"):
        return {a: types.SimpleNamespace(alias="smu_left" if a == "GPIB0::24::INSTR" else None)
                for a in self.addrs}

    def open_resource(self, addr, open_timeout=0):
        return FakeInst(self, addr)


@pytest.fixture
def lab(monkeypatch):
    """A computer with VISA instruments, two COM ports and an NI USB-6343."""
    rm = FakeRM()
    monkeypatch.setattr(VisaBackend, "_rm", rm)
    ports = [types.SimpleNamespace(device="COM3", description="USB Serial Port (COM3)",
                                   manufacturer="FTDI"),
             types.SimpleNamespace(device="COM1", description="Communications Port (COM1)",
                                   manufacturer="(Standard port types)")]
    lp = types.ModuleType("serial.tools.list_ports")
    lp.comports = lambda: ports
    tools = types.ModuleType("serial.tools")
    tools.list_ports = lp
    monkeypatch.setitem(sys.modules, "serial.tools", tools)
    monkeypatch.setitem(sys.modules, "serial.tools.list_ports", lp)
    nidaqmx_sim.reset()
    nidaqmx_sim.define("Dev3", "USB-6343")
    monkeypatch.setitem(sys.modules, "nidaqmx", nidaqmx_sim)
    monkeypatch.setitem(sys.modules, "nidaqmx.system", nidaqmx_sim.system)
    yield rm
    nidaqmx_sim.reset()


def test_discover_by_interface_with_identification(backend, lab):
    be = backend
    be.add_instrument("li", "sr830", "GPIB0::8::INSTR", connect=False)     # in use here
    r = be.discover_resources()
    found = {e["address"]: e for e in r["found"]}
    assert r["notes"] == []
    assert set(found) == {"COM3", "COM1", "ASRL9::INSTR", "GPIB0::24::INSTR",
                          "USB0::0x0957::0x8B18::MY1::INSTR", "GPIB0::8::INSTR",
                          "GPIB0::5::INSTR", "DAQ::Dev3"}                  # ASRL3 = COM3
    assert found["COM3"]["interface"] == "serial" and "FTDI" in found["COM3"]["description"]
    assert found["GPIB0::24::INSTR"]["description"] == "VISA alias smu_left"
    assert found["USB0::0x0957::0x8B18::MY1::INSTR"]["interface"] == "usb"
    assert found["DAQ::Dev3"] == {"address": "DAQ::Dev3", "interface": "daq",
                                  "description": "USB-6343", "idn": "USB-6343"}
    assert found["GPIB0::8::INSTR"]["in_use"] == ["li"]
    assert lab.queried == []                                  # no *IDN? unless asked
    r = be.discover_resources(identify=True)
    found = {e["address"]: e for e in r["found"]}
    assert "MODEL 2400" in found["GPIB0::24::INSTR"]["idn"]
    assert found["GPIB0::5::INSTR"]["idn"] == ""              # did not answer
    assert "GPIB0::8::INSTR" not in lab.queried               # in use: left alone
    assert "COM3" not in lab.queried and "ASRL9::INSTR" not in lab.queried
    # only some interfaces
    r = be.discover_resources(interfaces=["daq"])
    assert [e["address"] for e in r["found"]] == ["DAQ::Dev3"]


def test_discover_explains_what_is_missing(backend, monkeypatch):
    def no_visa(cls, backend=""):
        raise ResourceError("pyvisa is required for VISA addresses")
    monkeypatch.setattr(VisaBackend, "_resource_manager", classmethod(no_visa))
    monkeypatch.setitem(sys.modules, "serial.tools", None)
    monkeypatch.setitem(sys.modules, "serial.tools.list_ports", None)
    r = backend.discover_resources()
    notes = " ".join(r["notes"])
    assert "pyvisa" in notes and "pyserial" in notes and "NI-DAQmx" in notes
