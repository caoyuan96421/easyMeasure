"""Instrument emulators: simulated hardware that speaks the real command language.

The hardware drivers (``keithley2400``, ``keysight_b2901`` ...) are tested
against these emulators.  The command tables are written from the instruments'
programming manuals in SCPI notation (``[:SOURce[1]]:VOLTage[:LEVel]``), and a
command the manual does not define is answered like a real instrument would:
an entry in the error queue and no reply.  So the tests check the exact command
strings a driver sends, not just that it "works with itself".

A driver given a ``SIM::<name>`` address attaches its emulator to that simulated
bus (see :func:`ensure_emulator`), so a setup can be tried out without hardware.
"""

from __future__ import annotations

import re
from collections import deque
from typing import Callable, List, Optional, Tuple

from ..simbus import FaultInjector, SimDevice, get_bus


def ensure_emulator(address: str, addr: int, factory: Callable[[], SimDevice]) -> SimDevice:
    """Attach an emulator at ``addr`` of the ``SIM::`` bus unless one is there already
    (an emulated instrument keeps its state across reconnects, like real hardware)."""
    bus = get_bus(address.split("::", 1)[1])
    with bus.lock:
        dev = bus.devices.get(int(addr))
        if dev is None:
            dev = factory()
            bus.devices[int(addr)] = dev
        return dev


# ---------------------------------------------------------------------------
# SCPI header patterns
# ---------------------------------------------------------------------------
def _keyword_re(word: str) -> str:
    """``VOLTage`` -> matches exactly ``VOLT`` or ``VOLTAGE`` (any case)."""
    short = "".join(ch for ch in word if ch.isupper() or ch.isdigit() or ch == "*")
    long_ = word.upper()
    alts = {re.escape(short), re.escape(long_)}
    return "(?:%s)" % "|".join(sorted(alts, key=len, reverse=True))


def compile_pattern(pattern: str) -> "re.Pattern":
    """Compile an SCPI header such as ``[:SOURce{n}]:VOLTage[:LEVel][:IMMediate]``.

    ``[...]`` is optional, ``{n}`` captures an optional numeric suffix (channel).
    The leading colon is optional, as in SCPI.  A trailing ``?`` makes it a query.
    """
    out: List[str] = []
    i = 0
    query = pattern.endswith("?")
    if query:
        pattern = pattern[:-1]
    if not pattern.startswith((":", "[")):
        pattern = ":" + pattern            # headers are matched with a leading colon
    while i < len(pattern):
        c = pattern[i]
        if c == "[":
            out.append("(?:")
        elif c == "]":
            out.append(")?")
        elif c == ":":
            out.append(":")
        elif c == "{":
            j = pattern.index("}", i)
            out.append(r"(\d*)")
            i = j
        else:
            j = i
            while j < len(pattern) and (pattern[j].isalnum() or pattern[j] in "*"):
                j += 1
            out.append(_keyword_re(pattern[i:j]))
            i = j - 1
        i += 1
    body = "".join(out)
    return re.compile(r"^%s%s$" % (body, r"\?" if query else ""), re.IGNORECASE)


class SCPIError(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__("%d,\"%s\"" % (code, msg))
        self.code = code
        self.msg = msg


def split_args(text: str) -> List[str]:
    """``1, "VOLT", (@1,2)`` -> ['1', '"VOLT"', '(@1,2)'] (commas inside quotes/parens kept)."""
    out, cur, depth, quote = [], [], 0, ""
    for ch in text:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "'\"":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            out.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    if cur or out:
        out.append("".join(cur).strip())
    return [a for a in out if a != ""] if out != [""] else []


def unquote(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "'\"":
        return s[1:-1]
    return s


def parse_bool(s: str) -> bool:
    v = s.strip().upper()
    if v in ("ON", "1"):
        return True
    if v in ("OFF", "0"):
        return False
    raise SCPIError(-224, "Illegal parameter value")


def parse_num(s: str, lo: float = None, hi: float = None, specials: Optional[dict] = None) -> float:
    v = s.strip().upper()
    if specials and v in specials:
        return specials[v]
    for k, full in (("MIN", "MINIMUM"), ("MAX", "MAXIMUM"), ("DEF", "DEFAULT")):
        if v in (k, full):
            if specials and k in specials:
                return specials[k]
            raise SCPIError(-224, "Illegal parameter value")
    try:
        x = float(v)
    except ValueError:
        raise SCPIError(-104, "Data type error") from None
    if (lo is not None and x < lo) or (hi is not None and x > hi):
        raise SCPIError(-222, "Data out of range")
    return x


def fmt_num(x: float) -> str:
    return "%+.8E" % x


class SCPIEmulator(SimDevice):
    """Command dispatcher with an error queue, ``;`` separated messages and the
    IEEE-488.2 common commands."""

    def __init__(self, idn: str, faults: Optional[FaultInjector] = None):
        super().__init__(faults)
        self.idn = idn
        self.errors: deque = deque(maxlen=30)
        self.esr = 0
        self.handlers: List[Tuple["re.Pattern", Callable]] = []
        self.log: List[str] = []           # every command received (tests inspect it)
        self.unknown: List[str] = []       # commands the manual does not define
        self.add("*IDN?", lambda a, n: self.idn)
        self.add("*RST", lambda a, n: self.reset())
        self.add("*CLS", lambda a, n: self._cls())
        self.add("*OPC?", lambda a, n: "1")
        self.add("*WAI", lambda a, n: None)
        self.add("*ESR?", lambda a, n: self._read_esr())
        self.add(":SYSTem:ERRor[:NEXT]?", lambda a, n: self._next_error())

    # -- to override --------------------------------------------------------------
    def reset(self) -> None:
        """*RST: restore the power-on state."""

    # -- helpers ----------------------------------------------------------------
    def add(self, pattern: str, fn: Callable[[List[str], str], Optional[str]]) -> None:
        """Register ``fn(args, suffix)``; it returns the reply (queries) or None."""
        self.handlers.append((compile_pattern(pattern), fn))

    def push_error(self, code: int, msg: str) -> None:
        self.errors.append((code, msg))
        self.esr |= 32 if -199 <= code <= -100 else 16 if -299 <= code <= -200 else 8

    def _cls(self):
        self.errors.clear()
        self.esr = 0

    def _read_esr(self) -> str:
        v, self.esr = self.esr, 0
        return str(v)

    def _next_error(self) -> str:
        if not self.errors:
            return '+0,"No error"'
        code, msg = self.errors.popleft()
        return '%+d,"%s"' % (code, msg)

    def handle(self, command: str) -> Optional[str]:
        replies = []
        for part in _split_messages(command):
            self.log.append(part)
            head, _, rest = part.partition(" ")
            if "?" in head and not head.endswith("?"):        # e.g. "OUTP?1"
                head, _, more = head.partition("?")
                head += "?"
                rest = (more + " " + rest).strip()
            key = head if head.startswith(":") else ":" + head
            for rx, fn in self.handlers:
                m = rx.match(key)
                if m:
                    suffix = next((g for g in m.groups() if g), "")
                    try:
                        r = fn(split_args(rest), suffix)
                    except SCPIError as exc:
                        self.push_error(exc.code, exc.msg)
                        r = None
                    if r is not None:
                        if not isinstance(r, str):         # emulator bug, not a device reply
                            raise TypeError("handler for %r returned %r" % (head, r))
                        replies.append(r)
                    break
            else:
                self.unknown.append(part)
                self.push_error(-113, "Undefined header")
        return ";".join(replies) if replies else None

    def commands(self, prefix: str = "") -> List[str]:
        return [c for c in self.log if c.upper().startswith(prefix.upper())]


def _split_messages(line: str) -> List[str]:
    out, cur, quote = [], [], ""
    for ch in line:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "'\"":
            quote = ch
        if ch == ";":
            out.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    out.append("".join(cur).strip())
    return [p for p in out if p]
