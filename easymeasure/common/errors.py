"""Exception hierarchy used throughout easyMeasure.

Everything raised on purpose by the framework derives from :class:`EasyMeasureError`
so that callers can distinguish "expected" failures (bad hardware, bad input) from
programming errors.
"""


class EasyMeasureError(Exception):
    """Base class of all framework errors."""


# --- resources -----------------------------------------------------------------
class ResourceError(EasyMeasureError):
    """A low-level port could not be opened / used."""


class ResourceBusyError(ResourceError):
    """A resource is exclusively owned by somebody else, or the lock timed out."""


# --- instruments ---------------------------------------------------------------
class InstrumentError(EasyMeasureError):
    """An instrument call failed (communication error, bad reply, ...)."""


class InstrumentTimeout(InstrumentError):
    """An instrument call did not return within its deadline."""


class InstrumentNotConnected(InstrumentError):
    """The instrument is not (or no longer) connected."""


class ChannelError(EasyMeasureError):
    """Unknown channel, wrong direction (read-only/write-only) or bad data."""


class LimitError(ChannelError):
    """A requested value violates an alias' min/max protection."""


class OutOfRange(LimitError):
    """A multivariate composite would drive one of its targets outside its range.

    ``composite`` names the composite, ``mode`` is its out-of-range action
    (``"error"`` or ``"skip"``; ``"clamp"`` never raises this): a sweep skips the
    point when the mode is ``"skip"``."""

    def __init__(self, message: str, composite: str = "", mode: str = "error"):
        super().__init__(message)
        self.composite = composite
        self.mode = mode


class DriverError(EasyMeasureError):
    """A driver could not be found or imported."""


# --- execution -----------------------------------------------------------------
class SweepDefinitionError(EasyMeasureError):
    """A sweep definition is invalid."""


class AbortRequested(EasyMeasureError):
    """Raised inside running sweeps/scripts when the user aborts."""


class RampStopped(EasyMeasureError):
    """A ramp started by a direct write was stopped (``stop_ramps``); the channel
    stays where the ramp had got to."""


class SkipPoint(BaseException):
    """Raised by ``skip_point()`` in a sweep's per-point action: the rest of this
    point is skipped and its recorded data stay NaN.  A BaseException, so that a
    hook's own ``except Exception`` cannot swallow it by accident."""


class SkipLine(BaseException):
    """Raised by ``skip_line()`` in a per-point action: like ``break`` - the rest of
    this dimension's line is skipped (its data stay NaN) and the outer dimension
    continues."""


class EndSweep(BaseException):
    """Raised by ``end_sweep()`` in a per-point action: like ``exit`` - the sweep stops
    here with status ``terminated``; the post-sweep phase still runs."""


class ExpressionError(EasyMeasureError):
    """A user expression (mapping/composite) failed to compile or evaluate."""


class ProtocolError(EasyMeasureError):
    """Malformed message on the backend <-> frontend link."""


class RemoteError(EasyMeasureError):
    """An error reported by the backend in reply to a client request."""

    def __init__(self, message, kind="RemoteError", tb=None):
        super().__init__(message)
        self.kind = kind
        self.remote_traceback = tb
