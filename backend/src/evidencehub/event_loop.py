"""uvicorn 自定义事件循环工厂入口。

Windows 上默认的 ProactorEventLoop 无法用于 psycopg 异步连接，因此显式使用
SelectorEventLoop；不修改全局事件循环 policy，避免影响同一进程中的其他组件。

uvicorn 0.53 对自定义 --loop 值的契约是：把 ``module:attr`` 解析出的对象直接当作
零参数事件循环工厂使用（不会再用 ``use_subprocess`` 调用它）。因此这里暴露的必须是
创建事件循环的函数本身。

用法：uvicorn evidencehub.main:app --loop evidencehub.event_loop:create_event_loop
"""

import asyncio
import sys


def create_event_loop() -> asyncio.AbstractEventLoop:
    """创建事件循环：Windows 用 SelectorEventLoop，其他平台沿用默认实现。"""

    if sys.platform == "win32":
        return asyncio.SelectorEventLoop()
    return asyncio.new_event_loop()
