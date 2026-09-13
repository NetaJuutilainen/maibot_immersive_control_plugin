"""maibot_sdk 离线桩：让 plugin.py 在未安装 SDK 的环境可导入并自检。

仅覆盖本插件用到的 API 面；行为与真实 SDK 不保证一致，只用于离线验证。
"""

from __future__ import annotations

from typing import Any, ClassVar

import pydantic

CONFIG_RELOAD_SCOPE_SELF = "self"


class PluginConfigBase(pydantic.BaseModel):
    """真实 SDK 中为带 __ui_* 元数据处理与归一化逻辑的 BaseModel 子类。"""


Field = pydantic.Field


def _component_decorator(kind: str, name: str, **meta: Any):
    def decorator(fn):
        fn.__maibot_component_info__ = {"kind": kind, "name": name, **meta}
        return fn

    return decorator


def Command(name: str, description: str = "", pattern: str = "", aliases=None, **meta):
    return _component_decorator(
        "command", name, description=description, pattern=pattern, aliases=aliases, **meta
    )


def HookHandler(hook: str, *, name: str = "", description: str = "", mode=None,
                order=None, timeout_ms: int = 0, error_policy=None, **meta):
    return _component_decorator(
        "hook_handler", name or hook, hook=hook, description=description,
        mode=mode, order=order, timeout_ms=timeout_ms, error_policy=error_policy, **meta
    )


class HookMode:
    BLOCKING = "blocking"
    OBSERVE = "observe"


class HookOrder:
    EARLY = "early"
    NORMAL = "normal"
    LATE = "late"


class ErrorPolicy:
    ABORT = "abort"
    SKIP = "skip"
    LOG = "log"


class MaiBotPlugin:
    """真实 SDK 中由 Runner 注入 ctx 与 config；此处按 config_model 自动实例化。"""

    config_model: ClassVar = None
    ctx: Any = None
    _cfg: Any = None

    @property
    def config(self):
        if self._cfg is None and type(self).config_model is not None:
            self._cfg = type(self).config_model()
        return self._cfg

    async def on_load(self) -> None:
        raise NotImplementedError

    async def on_unload(self) -> None:
        raise NotImplementedError

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        raise NotImplementedError

    def get_components(self) -> list[dict]:
        comps = []
        for klass in type(self).__mro__:
            for value in vars(klass).values():
                info = getattr(value, "__maibot_component_info__", None)
                if info:
                    comps.append(dict(info))
        return comps
