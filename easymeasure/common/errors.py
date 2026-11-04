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


class DriverError(EasyMeasureError):
    """A driver could not be found or imported."""


# --- execution -----------------------------------------------------------------
class SweepDefinitionError(EasyMeasureError):
    """A sweep definition is invalid."""


class AbortRequested(EasyMeasureError):
    """Raised inside running sweeps/scripts when the user aborts."""


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
