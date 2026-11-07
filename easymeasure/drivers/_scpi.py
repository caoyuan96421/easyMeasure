"""Helpers shared by the SCPI hardware drivers (not a driver itself)."""

from __future__ import annotations

import math
from typing import Callable, List, Optional, Sequence

from ..backend.instrument import Instrument
from ..backend.resources import BYTE_PORTS, GPIB_LIKE  # noqa: F401
from ..common.errors import ChannelError

#: replies of 9.9E37 mean "overload" on most SCPI meters
OVERLOAD = 9.0e37


def to_float(reply: str) -> float:
    """Parse a numeric reply (``+1.2345E-03``; overload ``9.9E37`` -> ±inf)."""
    txt = reply.strip().strip('"')
    try:
        x = float(txt)
    except ValueError:
        raise IOError("not a number: %r" % reply[:60]) from None
    if abs(x) >= OVERLOAD:
        return math.copysign(math.inf, x)
    return x


def fmt(x: float) -> str:
    """Number for a command: enough digits, no locale surprises."""
    return "%.10g" % x


class SCPIInstrument(Instrument):
    """Base of SCPI drivers: identification, error queue, emulator for ``SIM::``.

    Subclasses set ``IDN_MATCH`` (any of these substrings must occur in ``*IDN?``,
    case-insensitively) and ``EMULATOR`` (a factory for the ``SIM::`` emulator).

    Option ``gpib_addr`` (with ``shared`` = true): the instrument sits behind a
    Prologix-style GPIB adapter together with others; ``++addr N`` selects it.
    """

    IDN_MATCH: Sequence[str] = ()
    EMULATOR: Optional[Callable] = None
    INTERFACES = BYTE_PORTS
    DEFAULT_OPTIONS = {"timeout": 5.0, "reset": False, "check_errors": True,
                       "read_termination": "\n", "write_termination": "\n"}

    idn = ""

    # -- connection -----------------------------------------------------------
    def select(self, resource) -> None:
        """Shared port behind a Prologix GPIB-USB/Ethernet adapter: with option
        ``gpib_addr`` the adapter is pointed at this instrument before each use."""
        a = self.options.get("gpib_addr")
        if a is not None:
            resource.write("++addr %d" % int(a))

    @classmethod
    def suggested_addresses(cls, driver_name: str):
        out = super().suggested_addresses(driver_name)
        if cls.EMULATOR is not None:
            out.append({"address": "SIM::" + driver_name, "description": "built-in emulator"})
        return out

    def attach_emulator(self) -> None:
        if self.address.upper().startswith("SIM::") and self.EMULATOR is not None:
            from .emulators import ensure_emulator
            ensure_emulator(self.address, int(self.options.get("gpib_addr", 0)),
                            lambda: self.EMULATOR(self.options))

    def identify(self) -> str:
        self.attach_emulator()
        self.select(self.resource)
        idn = self.resource.query("*IDN?").strip()
        if self.IDN_MATCH and not any(m.upper() in idn.upper() for m in self.IDN_MATCH):
            raise IOError("%s: unexpected instrument %r (expected %s)"
                          % (type(self).__name__, idn, " / ".join(self.IDN_MATCH)))
        self.idn = idn
        return idn

    def connect(self):
        self.identify()
        if self.options.get("reset"):
            self.w("*RST")
        self.clear_errors()

    # -- I/O ------------------------------------------------------------------------
    def w(self, cmd: str) -> None:
        self.resource.write(cmd)

    def q(self, cmd: str) -> str:
        return self.resource.query(cmd).strip()

    def qf(self, cmd: str) -> float:
        return to_float(self.q(cmd))

    def qb(self, cmd: str) -> bool:
        r = self.q(cmd).strip().upper()
        if r in ("1", "ON", "+1"):
            return True
        if r in ("0", "OFF", "+0"):
            return False
        raise IOError("not a boolean: %r" % r[:40])

    # -- errors ---------------------------------------------------------------------
    def read_errors(self) -> List[str]:
        """Drain the SCPI error queue (``SYST:ERR?``)."""
        out = []
        for _ in range(20):
            r = self.q(":SYST:ERR?")
            code = r.split(",", 1)[0].strip()
            try:
                if int(float(code)) == 0:
                    break
            except ValueError:
                raise IOError("bad error-queue reply %r" % r[:60]) from None
            out.append(r)
        return out

    def clear_errors(self) -> None:
        self.w("*CLS")

    def checked(self, *cmds: str) -> None:
        """Send commands, then make sure the instrument accepted them: a refused
        setting raises :class:`ChannelError` (not retried) with the instrument's
        own message."""
        for c in cmds:
            self.w(c)
        if self.options.get("check_errors", True):
            errs = self.read_errors()
            if errs:
                raise ChannelError("%s refused %s: %s" % (self.name, "; ".join(cmds),
                                                          "; ".join(errs)))
