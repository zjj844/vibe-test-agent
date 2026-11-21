"""pytest 共享夹具 + 关键环境约束。"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

# 本工具的所有测试都应该**离线可跑**：
# - 不调真模型（critic / generator 的测试用 stub 替代 llm_client.chat_for）
# - 不依赖被测服务（基线测试在服务不可达时 skip）
# 唯一的例外是 tests/test_manual_baseline.py，它需要一个真实运行的服务。


@pytest.fixture()
def ws_tmp():
    """工作区内的临时目录（替代 pytest 的 tmp_path）。

    为什么不用 tmp_path：它写在系统临时目录（C:\\Users\\...\\Temp），
    在受限环境（沙箱、权限异常的 Windows）会直接 PermissionError。
    """
    path = Path(__file__).resolve().parent / "_tmp_ws"
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    yield path
    shutil.rmtree(path, ignore_errors=True)
