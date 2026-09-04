"""The runtime version must stay aligned with the installable distribution."""

from importlib.metadata import version

import panopticon


def test_runtime_version_matches_distribution_metadata() -> None:
    assert panopticon.__version__ == version("panopticon-next")
