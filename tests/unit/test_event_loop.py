import asyncio
import sys

import evidencehub.event_loop as event_loop_module
import pytest
from uvicorn.config import Config

WINDOWS_LOOP_STRING = "evidencehub.event_loop:create_event_loop"


def test_create_event_loop_returns_selector_event_loop_on_windows() -> None:
    if sys.platform != "win32":
        pytest.skip("仅在 Windows 上验证真实事件循环类型")

    loop = event_loop_module.create_event_loop()
    try:
        assert isinstance(loop, asyncio.SelectorEventLoop)
    finally:
        loop.close()


def test_create_event_loop_uses_selector_loop_for_win32(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")

    loop = event_loop_module.create_event_loop()
    try:
        assert isinstance(loop, asyncio.SelectorEventLoop)
    finally:
        loop.close()


def test_create_event_loop_off_windows_uses_new_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    delegated: list[bool] = []

    def replacement() -> asyncio.AbstractEventLoop:
        delegated.append(True)
        return asyncio.SelectorEventLoop()

    monkeypatch.setattr(asyncio, "new_event_loop", replacement)

    loop = event_loop_module.create_event_loop()
    try:
        assert delegated == [True]
    finally:
        loop.close()


def test_uvicorn_resolves_the_documented_loop_string() -> None:
    """离线校验文档中的 --loop 字符串能被已安装的 uvicorn 解析为可用的循环工厂。"""

    config = Config(app="evidencehub.main:app", loop=WINDOWS_LOOP_STRING)

    loop_factory = config.get_loop_factory()
    assert loop_factory is not None

    loop = loop_factory()
    try:
        assert isinstance(loop, asyncio.AbstractEventLoop)
        if sys.platform == "win32":
            assert isinstance(loop, asyncio.SelectorEventLoop)
    finally:
        loop.close()
