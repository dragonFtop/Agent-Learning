import pytest

import config
from tools.file_ops import safe_path


def test_relative_path_inside_workdir():
    assert safe_path("README.md") == (config.WORKDIR / "README.md").resolve()


def test_parent_escape_rejected():
    with pytest.raises(ValueError):
        safe_path("../outside.txt")


def test_absolute_path_outside_rejected():
    outside = config.WORKDIR.parent / "outside.txt"
    with pytest.raises(ValueError):
        safe_path(str(outside))
