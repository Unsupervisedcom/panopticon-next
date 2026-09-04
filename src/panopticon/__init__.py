"""panopticon — keep an eye on your agents."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("panopticon-next")
except PackageNotFoundError:  # pragma: no cover - only an unpackaged source tree
    __version__ = "0+unknown"
