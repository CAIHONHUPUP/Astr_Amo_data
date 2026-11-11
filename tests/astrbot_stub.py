# -*- coding: utf-8 -*-
"""最小 astrbot 桩：让 segment_engine / heartflow_engine 能在纯单测里 import。

两个引擎在模块顶层 `from astrbot.api...`，CI 上没有 AstrBot，之前这两个模块
（以及分段死循环、0 当默认值这类 bug）完全没有测试。这里只桩住 import 需要的东西，
不模拟平台行为。
"""

import sys
import types

_INSTALLED = False


class _Logger:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    astrbot = types.ModuleType("astrbot")
    astrbot.__path__ = []
    astrbot.__dsh_stub__ = True

    api = types.ModuleType("astrbot.api")
    api.logger = _Logger()

    class AstrBotConfig(dict):
        def save_config(self):
            return True

    api.AstrBotConfig = AstrBotConfig

    star = types.ModuleType("astrbot.api.star")

    class Star:
        def __init__(self, context=None, config=None):
            self.context = context
            self.config = config or {}

    class Context:
        pass

    star.Star = Star
    star.Context = Context

    event = types.ModuleType("astrbot.api.event")

    class AstrMessageEvent:
        pass

    event.AstrMessageEvent = AstrMessageEvent

    class _Filter:
        class EventMessageType:
            ALL = "all"
            PRIVATE_MESSAGE = "private"
            GROUP_MESSAGE = "group"

        class PermissionType:
            ADMIN = "admin"
            MEMBER = "member"

        def __getattr__(self, name):
            def decorator(*args, **kwargs):
                def wrap(func):
                    return func
                return wrap
            return decorator

        def command(self, *args, **kwargs):
            return self.__getattr__("command")(*args, **kwargs)

    event.filter = _Filter()
    event.AstrBotEvent = AstrMessageEvent

    components = types.ModuleType("astrbot.api.message_components")

    class Plain:
        def __init__(self, text=""):
            self.text = text

    class Reply:
        def __init__(self, id=None):
            self.id = id

    class At:
        def __init__(self, qq=""):
            self.qq = qq

    components.Plain = Plain
    components.Reply = Reply
    components.At = At

    provider = types.ModuleType("astrbot.api.provider")

    class LLMResponse:
        pass

    provider.LLMResponse = LLMResponse

    core = types.ModuleType("astrbot.core")
    core.__path__ = []

    message_pkg = types.ModuleType("astrbot.core.message")
    message_pkg.__path__ = []

    result_mod = types.ModuleType("astrbot.core.message.message_event_result")

    class MessageChain:
        def __init__(self):
            self.chain = []

        def clear(self):
            self.chain.clear()

        def append(self, item):
            self.chain.append(item)

        def message(self, text):
            self.chain.append(Plain(text))

    result_mod.MessageChain = MessageChain

    platform_pkg = types.ModuleType("astrbot.core.platform")
    platform_pkg.__path__ = []

    ame = types.ModuleType("astrbot.core.platform.astr_message_event")

    class MessageSession:
        pass

    ame.MessageSession = MessageSession

    session_mod = types.ModuleType("astrbot.core.platform.message_session")
    session_mod.MessageSession = MessageSession

    sys.modules.update({
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.star": star,
        "astrbot.api.event": event,
        "astrbot.api.message_components": components,
        "astrbot.api.provider": provider,
        "astrbot.core": core,
        "astrbot.core.message": message_pkg,
        "astrbot.core.message.message_event_result": result_mod,
        "astrbot.core.platform": platform_pkg,
        "astrbot.core.platform.astr_message_event": ame,
        "astrbot.core.platform.message_session": session_mod,
    })
