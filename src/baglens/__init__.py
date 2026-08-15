"""baglens — audit whether a robot recording can be trusted, then investigate a fleet of them."""

from importlib.metadata import PackageNotFoundError, version

try:
    #: Read from the installed distribution rather than written here, because a version
    #: in two places is a version that drifts. It already had: the wheel built from
    #: pyproject 0.3.0 introduced itself as 0.2.0, and the release workflow's own
    #: "install the wheel and import it" check is what surfaced it.
    __version__ = version("baglens")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
