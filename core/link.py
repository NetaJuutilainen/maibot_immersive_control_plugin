# -*- coding: utf-8 -*-
"""沉浸式遥控插件：与「麦麦身体」（maibot-body-emulator）的联动客户端。

设计原则（前三条照抄 body 自身 link.py，第四条为本插件审查后新增）：
1. 只用对方 public API（@API public=True）与 ctx.api.call，不 import 它的模块、不读它的存档
   —— 它的内部结构随时会变；
2. 所有调用都可能失败：body 不在跑、版本不兼容、超时，都只能降级，绝不把异常抛回钩子主流程；
3. 跨插件 RPC 有往返开销，状态拉取用固定间隔缓存，不在每条回复里调；
4. 每次调用带 asyncio.wait_for 超时上界：body 自身的 _call 是裸 await 无超时（勿照抄），
   「慢」是异常与错误字典之外的第三种失败形态。api.call 的失败形态有两种——
   抛异常（如 E_CAPABILITY_DENIED）与返回 {"success": False, "error": ...} 错误字典（不抛），
   两者都必须按失败计数，否则熔断永不触发。

总开关 enabled=False 时一个调用都不发；熔断开启期间 get_state 与 adjust_mood 一并短路。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

BODY_PLUGIN_ID = "maibot-body-emulator"
MAI_LIFE_PLUGIN_ID = "maibot-community.mai-life"

BREAKER_FAIL_THRESHOLD = 3    # 连续失败次数达到该值 → 熔断
BREAKER_OPEN_SECONDS = 300.0  # 熔断时长（秒）

# get_state 字段级守卫的取值集合：缺字段/类型异常一律回退默认，防 body 未来改字段炸进钩子
_NUM_FIELDS = ("hunger", "thirst", "energy", "health", "stress", "depression")


class BodyLink:
    """麦麦身体联动客户端（enabled=False 时零调用、零路径）。"""

    def __init__(self, cfg_getter: Callable[[], Any], ctx: Any) -> None:
        self._cfg_getter = cfg_getter  # 每次现取配置节引用（不缓存，热更新安全）
        self._ctx = ctx
        self._cache: dict[str, Any] | None = None
        self._last_pull = 0.0
        self._fail_count = 0
        self._last_error = ""
        self._open_until = 0.0         # 熔断截止时刻（0 = 未熔断）
        self._phase_cache: str | None = None   # 麦生活睡眠阶段缓存
        self._phase_pull = 0.0
        self._last_ml_error = ""

    # ---- 内部 ----

    def _cfg(self) -> Any:
        return self._cfg_getter()

    def _plugin_id(self) -> str:
        cfg = self._cfg()
        return str(getattr(cfg, "body_plugin_id", "") or "").strip() or BODY_PLUGIN_ID

    def _mai_life_id(self) -> str:
        cfg = self._cfg()
        return str(getattr(cfg, "mai_life_plugin_id", "") or "").strip() or MAI_LIFE_PLUGIN_ID

    async def get_sleep_phase(self) -> str | None:
        """读麦生活的睡眠阶段（sleep_phase，公开 API，只读适配）。

        取值：awake / woken / falling_asleep / light_sleep / deep_sleep。
        麦生活不在/失败 → None（调用方按未知阶段处理，不猜）。缓存同 state_ttl。"""
        cfg = self._cfg()
        if not cfg.enabled:
            return None
        now = time.time()
        if self._phase_cache is not None and (now - self._phase_pull) < max(
                10.0, float(cfg.state_ttl_seconds)):
            return self._phase_cache
        self._phase_pull = now
        try:
            result = await asyncio.wait_for(
                self._ctx.api.call(f"{self._mai_life_id()}.get_life_state"),
                timeout=max(1.0, float(cfg.call_timeout_seconds)),
            )
        except Exception as exc:  # noqa: BLE001
            self._last_ml_error = f"{type(exc).__name__}: {exc}"[:200]
            return None
        if not isinstance(result, dict) or result.get("success") is False:
            self._last_ml_error = "get_life_state 失败"
            return None
        phase = str(result.get("sleep_phase") or "") or None
        self._phase_cache = phase
        return phase

    def _breaker_open(self, now: float) -> bool:
        return self._open_until > 0.0 and now < self._open_until

    def _record_fail(self, message: str) -> None:
        self._fail_count += 1
        self._last_error = message[:200]
        if self._fail_count >= BREAKER_FAIL_THRESHOLD:
            self._open_until = time.time() + BREAKER_OPEN_SECONDS

    async def _call(self, api_name: str, **args: Any) -> Any:
        """调 body 公开 API；异常 / 错误字典 / 超时三种失败都计数并返回 None，绝不抛。"""
        cfg = self._cfg()
        try:
            result = await asyncio.wait_for(
                self._ctx.api.call(f"{self._plugin_id()}.{api_name}", **args),
                timeout=max(1.0, float(cfg.call_timeout_seconds)),
            )
        except Exception as exc:  # noqa: BLE001 —— 联动失败必须降级（含 TimeoutError）
            self._record_fail(f"{type(exc).__name__}: {exc}")
            return None
        if isinstance(result, dict) and result.get("success") is False:
            # 能力执行失败的错误字典形态（body 未装/未就绪的主失败形态）也是失败
            self._record_fail(str(result.get("error") or "capability failed"))
            return None
        self._fail_count = 0
        self._last_error = ""
        self._open_until = 0.0
        return result

    # ---- 对外 ----

    async def get_state(self) -> dict[str, Any] | None:
        """只读体征快照（固定间隔缓存 + 字段级守卫）。关闭/失败/熔断期 → None。"""
        cfg = self._cfg()
        if not cfg.enabled:
            return None
        now = time.time()
        if self._breaker_open(now):
            return None  # 熔断期零 RPC
        ttl = max(10, int(cfg.state_ttl_seconds))
        if self._cache is not None and (now - self._last_pull) < ttl:
            return self._cache
        self._last_pull = now  # 先盖时间戳防并发重复拉（asyncio 在 await 处让出）
        raw = await self._call("get_state")
        state = self._normalize(raw)
        if state is None:
            self._cache = None  # 失败不缓存成功值；下次回复再试（受熔断保护）
            return None
        self._cache = state
        return state

    async def adjust_mood(self, stress_delta: float, label: str = "",
                          depression_delta: float = 0.0) -> bool:
        """写入压力/心境变化。关闭/失败/熔断期 → False；调用方负责 sleeping 守卫与幂等。"""
        cfg = self._cfg()
        if not cfg.enabled:
            return False
        if self._breaker_open(time.time()):
            return False  # 熔断期写入一并短路，不给必败 RPC
        args: dict[str, Any] = {
            "stress_delta": float(stress_delta), "label": str(label or "")[:80],
        }
        if depression_delta:
            args["depression_delta"] = float(depression_delta)
        result = await self._call("adjust_mood", **args)
        return bool(isinstance(result, dict) and result.get("ok"))

    @staticmethod
    def _normalize(raw: Any) -> dict[str, Any] | None:
        """字段级守卫：body 返回 ok=True 但缺字段/类型异常时也不炸。

        缺省值取中性（精力/健康=满、饥饿/口渴/压力=无）——缺数据不应凭空捏造
        饥饿/生病/高压状态。"""
        if not isinstance(raw, dict) or not raw.get("ok"):
            return None
        neutral = {"energy": 100.0, "health": 100.0, "hunger": 0.0,
                   "thirst": 0.0, "stress": 0.0, "depression": 0.0}
        state: dict[str, Any] = {"sleeping": bool(raw.get("sleeping", False))}
        for key in _NUM_FIELDS:
            try:
                state[key] = float(raw.get(key, neutral[key]))
            except (TypeError, ValueError):
                state[key] = neutral[key]
        return state
