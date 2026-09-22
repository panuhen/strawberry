"""One version for the package, the module and (stamped at build time) the widget."""

import tomllib
from pathlib import Path

from strawberry_crab import __version__


def test_the_module_version_is_the_package_version():
    pyproject = Path(__file__).parents[1] / "pyproject.toml"
    assert tomllib.loads(pyproject.read_text())["project"]["version"] == __version__
