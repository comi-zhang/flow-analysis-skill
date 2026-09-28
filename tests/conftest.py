"""把 flow-analysis/scripts/ 下的脚本按路径加载成模块（它们不是安装包）。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "flow-analysis" / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def qa_flow():
    return _load("qa_flow")


@pytest.fixture(scope="session")
def pdf_probe():
    return _load("pdf_probe")
