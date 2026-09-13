"""沉浸式遥控（移植自 AstrBot astrbot_plugin_immersive_control）。

玩法：消息内容命中进入关键词（如「控制」「td」）→ 该会话进入"被遥控"状态，
麦麦在后续回复中自然融入沉浸式反应（默认静默激活，靠提示词注入体现状态）；
命中退出关键词或到达持续时长后结束，退出后下一次回复注入一次收尾提示词。
支持冷却、并发上限、管理员限制、状态持久化（重启不丢）。

框架挂载点：
 - chat.receive.before_process（blocking）：关键词匹配 + 状态机驱动 + 拦截；
 - maisaka.replyer.before_request（blocking）：向 extra_prompt 注入进入/退出模板；
 - @Command：/imm_status、/imm_clear、/imm_config（管理命令，插件自管管理员鉴权）。
"""

from __future__ import annotations

import importlib.util
import random
import sys
import time
from pathlib import Path
from typing import Any, ClassVar

from maibot_sdk import Command, HookHandler, MaiBotPlugin
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

_PLUGIN_DIR = Path(__file__).resolve().parent


def _load_core_module(stem: str) -> Any:
    """以受控方式加载 core/ 下模块：不改 Runner 的全局 sys.path，
    模块以 maibot_immersive_control_ 前缀注册进 sys.modules 防撞名。
    （Runner 以文件 spec 导入 plugin.py，core 包未必可按包名导入，故不写顶层 from core...）"""
    module_name = f"maibot_immersive_control_{stem}"
    spec = importlib.util.spec_from_file_location(module_name, _PLUGIN_DIR / "core" / f"{stem}.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"无法定位核心模块: core/{stem}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


_config_mod = _load_core_module("config")
_store_mod = _load_core_module("store")

SUPPORTED_CONFIG_VERSION = _config_mod.SUPPORTED_CONFIG_VERSION
ImmersiveControlConfig = _config_mod.ImmersiveControlConfig
render_template = _config_mod.render_template
SessionStore = _store_mod.SessionStore
auto_sensitivity_value = _store_mod.auto_sensitivity_value
ACTIVATED = _store_mod.ACTIVATED
COOLDOWN = _store_mod.COOLDOWN
LIMIT = _store_mod.LIMIT

INTENSITY_MIN, INTENSITY_MAX = 0, 120  # 指令可设置的敏感度范围（越界直接拒绝）

# 顶点效果 emoji 标记的附加指令（{e} 为配置的 emoji）
_EMOJI_HINT = "（此条回复的结尾不经意地带上{e}，不要解释原因。）"

_STATE_FILE = "state.json"


def _norm_text(raw: Any) -> str:
    """关键词匹配用的消息文本归一化：去首尾空白与单个 / 前缀，转小写。"""
    text = str(raw or "").strip().lower()
    if text.startswith("/"):
        text = text[1:].strip()
    return text


def _norm_text_strip_at(raw: Any) -> str:
    """在归一化基础上再剥掉"@昵称"前缀（@ 与昵称间、昵称与关键词间可能有空格）。"""
    text = _norm_text(raw)
    if text.startswith("@"):
        tail = text[1:].strip()
        if not tail:
            return ""
        if " " in tail or "\u3000" in tail:
            return tail.split(None, 1)[1].strip()
        return ""  # 形如"@麦麦控制"无法区分昵称与关键词，不匹配（需在 @ 后加空格）
    return text


def _keyword_candidates(raw: Any) -> set[str]:
    """@消息既可能是"@麦麦 控制"也可能是裸"控制"（私聊），返回两个候选。"""
    candidates = {_norm_text(raw), _norm_text_strip_at(raw)}
    candidates.discard("")
    return candidates


def _has_at(message: dict) -> bool:
    """是否 @ 了机器人：优先信适配器置位的 is_at；否则兜底扫 raw_message 的 at 段。"""
    if message.get("is_at"):
        return True
    for seg in message.get("raw_message") or []:
        if isinstance(seg, dict) and seg.get("type") == "at":
            return True
    return False


def _is_group_message(message: dict) -> bool:
    info = message.get("message_info") or {}
    return bool((info.get("group_info") or {}).get("group_id"))


def _extract(message: dict, *path: str) -> str:
    node: Any = message
    for key in path:
        if not isinstance(node, dict):
            return ""
        node = node.get(key)
    return str(node or "")


class ImmersiveControlPlugin(MaiBotPlugin):
    """沉浸式遥控插件入口。"""

    config_model: ClassVar[type] = ImmersiveControlConfig

    def __init__(self) -> None:
        super().__init__()
        self._store = SessionStore()

    # ---- 生命周期 ----

    async def on_load(self) -> None:
        state_path = self.ctx.paths.data_dir / _STATE_FILE
        self._store.on_changed = lambda: self._store.save(state_path)
        self._store.load(state_path)
        self.ctx.logger.info(
            "沉浸式遥控已加载：持久化状态 %d 条（%s）",
            len(self._store.sessions), state_path,
        )

    async def on_unload(self) -> None:
        state_path = self.ctx.paths.data_dir / _STATE_FILE
        self._store.save(state_path)
        self.ctx.logger.info("沉浸式遥控已卸载，状态已保存")

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        self.ctx.logger.info("配置已热更新 scope=%s version=%s", scope, version)

    # ---- 内部工具 ----

    def _is_admin(self, user_id: str) -> bool:
        """管理员判定：兼容 ["123456"] 与 ["qq:123456"] 两种配置写法。"""
        admins = self.config.control.admin_ids or []
        uid = str(user_id or "").strip()
        if not uid:
            return False
        for entry in admins:
            entry = str(entry or "").strip()
            if entry and entry.split(":")[-1] == uid:
                return True
        return False

    async def _send(self, stream_id: str, text: str) -> None:
        try:
            await self.ctx.send.text(text, stream_id)
        except Exception as e:  # noqa: BLE001  发送失败不影响状态机
            self.ctx.logger.warning("发送提示失败: %s", e)

    async def _feedback(self, stream_id: str, real_text: str) -> None:
        """状态变更回执：隐蔽模式下随机发一句无察觉日常语句代替真实内容。"""
        if self.config.control.stealth_mode:
            phrases = self.config.control.stealth_phrases or ["好。"]
            await self._send(stream_id, random.choice(phrases))
        else:
            await self._send(stream_id, real_text)

    def _control(self):
        return self.config.control

    def _effective_sensitivity(self, session_id: str, now: float | None = None) -> int:
        """会话实际生效的敏感度（整数）：自动模式按三角波实时计算，
        否则会话覆盖优先，再否则全局配置值。"""
        cfg = self._control()
        s = self._store.sessions.get(session_id)
        if s is not None and s.auto_mode:
            now = time.time() if now is None else now
            self._store.ensure_auto_anchor(session_id, now)
            return auto_sensitivity_value(now - s.auto_ts, cfg.auto_speed_per_minute)
        if s is not None and s.sensitivity >= 0:
            return s.sensitivity
        return int(self.config.prompt.sensitivity)

    def _tier_text(self, value: int) -> str:
        """按敏感度取对应档位的效果说明（模板可在 WebUI 修改）。"""
        p = self.config.prompt
        if value >= 100:
            return p.tier_100_120
        if value >= 80:
            return p.tier_80_100
        if value >= 60:
            return p.tier_60_80
        if value >= 40:
            return p.tier_40_60
        if value >= 20:
            return p.tier_20_40
        return p.tier_0_20

    def _intensity_mode_text(self, session_id: str) -> str:
        s = self._store.sessions.get(session_id)
        if s is not None and s.auto_mode:
            return f"自动（{self._control().auto_speed_per_minute} 点/分钟）"
        if s is not None and s.sensitivity >= 0:
            return "会话覆盖"
        return "全局配置"

    def _climax_status_text(self, session_id: str) -> str:
        """/强度 查询用：持续越限计时的当前状态。"""
        cfg = self._control()
        if int(cfg.climax_threshold) <= 0:
            return "关闭"
        s = self._store.sessions.get(session_id)
        if s is None or not s.active:
            return f"未激活（阈值 {cfg.climax_threshold}/120，需持续 {cfg.climax_hold_seconds} 秒）"
        if s.climax_ts > 0:
            remaining = max(0, int(cfg.climax_hold_seconds - (time.time() - s.climax_ts) + 0.999))
            return (f"计时中，还差约 {remaining} 秒（阈值 {cfg.climax_threshold}/120，"
                    f"已触发 {s.climax_count}/{cfg.climax_max_per_activation} 次）")
        return (f"未在计时（当前值未达阈值 {cfg.climax_threshold} 或刚重置，"
                f"已触发 {s.climax_count}/{cfg.climax_max_per_activation} 次）")

    def _check_climax(self, session_id: str, effective: int, now: float | None = None) -> str:
        """持续越限检查：敏感度保持在阈值以上达到时长 → 返回一次顶点效果注入文本。
        中途跌破阈值则重新计时；每次激活最多触发一次。返回空串表示本次不注入。"""
        cfg = self._control()
        threshold = int(cfg.climax_threshold)
        if threshold <= 0:
            return ""
        s = self._store.sessions.get(session_id)
        if s is None or s.climax_count >= max(1, int(cfg.climax_max_per_activation)):
            return ""
        now = time.time() if now is None else now
        if effective < threshold:
            if s.climax_ts != 0.0:
                s.climax_ts = 0.0
                self._store._changed()
            return ""
        if s.climax_ts == 0.0:
            s.climax_ts = now
            self._store._changed()
            return ""
        if now - s.climax_ts >= max(1, int(cfg.climax_hold_seconds)):
            s.climax_count += 1
            s.climax_ts = 0.0  # 重新计时，允许（在次数上限内）再次触发
            self._store._changed()
            self.ctx.logger.info("会话 %s 持续越限 %d 秒，触发顶点效果（第 %d 次）",
                                 session_id, int(cfg.climax_hold_seconds), s.climax_count)
            text = render_template(
                self.config.prompt.climax_template,
                self.config.prompt.item_name,
                effective,
            )
            emoji = str(self.config.prompt.climax_emoji or "").strip()
            if emoji:
                text += (_EMOJI_HINT.format(e=emoji))
            return text
        return ""

    async def _maybe_proactive(self, session_id: str) -> bool:
        """敏感度达到阈值时自动触发一次主动表达（每次激活最多一次）。"""
        threshold = int(self._control().proactive_threshold)
        if threshold <= 0:
            return False
        s = self._store.sessions.get(session_id)
        if s is None or s.proactive_fired:
            return False
        if self._effective_sensitivity(session_id) < threshold:
            return False
        s.proactive_fired = True
        self._store._changed()
        p = self.config.prompt
        intent = render_template(
            "（你身上的{item_name}的强度已经到了{sensitivity}/100，害羞慌乱快藏不住了，"
            "忍不住要主动吐槽求饶……）",
            p.item_name,
            self._effective_sensitivity(session_id),
        )
        try:
            await self.ctx.maisaka.proactive.trigger(
                session_id, intent, reason="immersive-control 敏感度阈值",
            )
            self.ctx.logger.info("会话 %s 敏感度达阈值，已触发主动表达", session_id)
        except Exception as e:  # noqa: BLE001  能力未授权或触发失败：降级为注入上下文
            self.ctx.logger.warning("主动表达触发失败（%s），降级为上下文注入", e)
            try:
                await self.ctx.maisaka.context.append(
                    session_id, [{"type": "text", "content": intent}],
                    visible_text="", source_kind="plugin_immersive_threshold",
                )
            except Exception as e2:  # noqa: BLE001
                self.ctx.logger.warning("上下文注入也失败: %s", e2)
        return True

    # ---- 入站消息：关键词驱动状态机 ----

    @HookHandler(
        "chat.receive.before_process",
        name="immersive_keyword_driver",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        error_policy=ErrorPolicy.SKIP,
    )
    async def hook_keyword_driver(self, **kwargs: Any):
        cfg = self._control()
        if not self.config.plugin.enabled:
            return None

        message = kwargs.get("message") or {}
        if message.get("is_notify"):
            return None

        # @ 门槛：群聊要求 @bot（私聊始终放行）
        if cfg.require_at and _is_group_message(message) and not _has_at(message):
            return None

        text = _norm_text(message.get("processed_plain_text"))
        if not text:
            return None

        enter_words = {_norm_text(w) for w in (cfg.enter_keywords or []) if str(w).strip()}
        exit_words = {_norm_text(w) for w in (cfg.exit_keywords or []) if str(w).strip()}

        candidates = _keyword_candidates(message.get("processed_plain_text"))
        if not candidates & (enter_words | exit_words):
            return None  # 非关键词消息完全旁路，零开销
        hit_enter = bool(candidates & enter_words)
        hit_exit = bool(candidates & exit_words)

        user_id = _extract(message, "message_info", "user_info", "user_id")
        session_id = str(message.get("session_id") or "")
        if not session_id:
            return None

        # 命中关键词但没权限：按原版语义静默忽略（当作普通聊天）
        if cfg.admin_only_mode and not self._is_admin(user_id):
            return None

        if hit_enter:
            result, remaining = self._store.try_activate(session_id, cfg)
            if result == ACTIVATED:
                await self._maybe_proactive(session_id)
                if cfg.react_on_trigger:
                    # 不拦截：麦麦立刻回复这条消息，replyer 钩子会注入进入模板
                    return None
                # 原版语义：激活瞬间静默（should_call_llm(False)），状态靠后续回复体现
                return {"action": "abort", "abort_message": "沉浸式遥控已激活（静默）"}
            if result == COOLDOWN:
                await self._feedback(
                    session_id,
                    f"「{self.config.prompt.item_name}」还在休息中，约 {remaining} 秒后再试吧～",
                )
                return {"action": "abort", "abort_message": "冷却中"}
            if result == LIMIT:
                await self._feedback(session_id, "当前使用的小沙发已经满了，稍后再来吧～")
                return {"action": "abort", "abort_message": "并发上限"}
            return None

        # 退出关键词
        if hit_exit and self._store.try_exit(session_id):
            # 静默结束；退出模板由 replyer 钩子在下次回复时注入一次
            return {"action": "abort", "abort_message": "沉浸式遥控已退出"}
        return None  # 没有激活会话时，「停止」等词按普通聊天放行

    # ---- LLM 回复前：注入状态提示词 ----

    @HookHandler(
        "maisaka.replyer.before_request",
        name="immersive_prompt_injector",
        mode=HookMode.BLOCKING,
        error_policy=ErrorPolicy.SKIP,
    )
    async def hook_prompt_injector(self, **kwargs: Any):
        if not self.config.plugin.enabled:
            return None

        session_id = str(kwargs.get("session_id") or "")
        if not session_id:
            return None

        state = self._store.prompt_state(session_id, self._control())
        if state == "none":
            return None

        prompt_cfg = self.config.prompt
        effective = self._effective_sensitivity(session_id)
        if state == "enter":
            inject = render_template(
                prompt_cfg.enter_template,
                prompt_cfg.item_name,
                effective,
                self._control().state_duration,
            )
            inject = f"{inject}\n（当前强度参考：{self._tier_text(effective)}）"
            # 自动模式爬升越过阈值时在对话流中触发（每次激活最多一次）
            await self._maybe_proactive(session_id)
            climax_inject = self._check_climax(session_id, effective)
            if climax_inject:
                inject = f"{inject}\n{climax_inject}"
        else:
            inject = render_template(
                prompt_cfg.exit_template,
                prompt_cfg.item_name,
                effective,
            )

        extra = str(kwargs.get("extra_prompt") or "")
        kwargs["extra_prompt"] = f"{extra}\n{inject}" if extra else inject
        self.ctx.logger.debug("会话 %s 注入%s模板", session_id, state)
        return {"action": "continue", "modified_kwargs": kwargs}

    # ---- 管理命令 ----

    @Command("imm_status", description="查看本会话的遥控状态（管理员）",
             pattern=r"(?<!\S)/?imm_status\s*$", aliases=["控制状态"])
    async def cmd_status(self, **kwargs: Any) -> tuple[bool, str | None, bool]:
        stream_id = str(kwargs.get("stream_id") or "")
        user_id = str(kwargs.get("user_id") or "")
        if not self._is_admin(user_id):
            await self._send(stream_id, "权限不足：仅管理员可用。")
            return False, "权限不足", True
        info = self._store.status_of(stream_id, self._control())
        lines = [f"{k}：{v}" for k, v in info.items()]
        lines.append(f"当前激活会话数：{self._store.active_count()}")
        await self._send(stream_id, "\n".join(lines))
        return True, "imm_status", True

    @Command("imm_clear", description="清除所有会话的遥控状态（管理员）",
             pattern=r"(?<!\S)/?imm_clear\s*$")
    async def cmd_clear(self, **kwargs: Any) -> tuple[bool, str | None, bool]:
        stream_id = str(kwargs.get("stream_id") or "")
        user_id = str(kwargs.get("user_id") or "")
        if not self._is_admin(user_id):
            await self._send(stream_id, "权限不足：仅管理员可用。")
            return False, "权限不足", True
        n = self._store.clear()
        await self._send(stream_id, f"已清除 {n} 个会话的遥控状态。")
        return True, "imm_clear", True

    @Command("imm_intensity", description="设置本会话敏感度（管理员）：/强度 80、/强度 max、/强度 自动、/强度 全局",
             pattern=r"(?<!\S)/?(?:强度|sensitivity)(?:\s+(?P<value>\d{1,3}|[Mm][Aa][Xx]|自动|全局))?\s*$",
             aliases=["sensitivity"])
    async def cmd_intensity(self, **kwargs: Any) -> tuple[bool, str | None, bool]:
        stream_id = str(kwargs.get("stream_id") or "")
        user_id = str(kwargs.get("user_id") or "")
        if not self._is_admin(user_id):
            await self._send(stream_id, "权限不足：仅管理员可用。")
            return False, "权限不足", True

        cfg = self._control()
        raw_value = (kwargs.get("matched_groups") or {}).get("value")

        if raw_value is None:  # 查询
            current = self._effective_sensitivity(stream_id)
            await self._send(
                stream_id,
                f"当前敏感度：{current}/120（模式：{self._intensity_mode_text(stream_id)}）\n"
                f"档位：{self._tier_text(current)}\n"
                f"持续越限：{self._climax_status_text(stream_id)}\n"
                f"阈值自动触发：{'关闭' if cfg.proactive_threshold <= 0 else f'{cfg.proactive_threshold}/120'}\n"
                f"用法：/强度 0~120、/强度 max(=120)、/强度 自动、/强度 全局",
            )
            return True, "imm_intensity", True

        raw_value = str(raw_value).strip()

        if raw_value.lower() == "max":
            return await self._apply_intensity(stream_id, 120)
        if raw_value == "自动":
            self._store.set_auto(stream_id)
            speed = cfg.auto_speed_per_minute
            await self._feedback(
                stream_id,
                f"已开启自动模式：敏感度将从 30 爬升到 100（约 {70 / speed:.1f} 分钟），"
                f"之后在 50↔100 之间往复（每程约 {50 / speed:.1f} 分钟，{speed} 点/分钟）。\n"
                f"当前档位：{self._tier_text(self._effective_sensitivity(stream_id))}",
            )
            s = self._store.sessions.get(stream_id)
            if s is not None and s.active:
                await self._maybe_proactive(stream_id)
            return True, "imm_intensity", True
        if raw_value == "全局":
            self._store.clear_override(stream_id)
            await self._feedback(
                stream_id,
                f"已清除本会话设置（含自动模式），恢复跟随全局敏感度"
                f"（{self.config.prompt.sensitivity}/120）。",
            )
            return True, "imm_intensity", True

        value = int(raw_value)  # 正则保证是纯数字
        if value < INTENSITY_MIN or value > INTENSITY_MAX:
            await self._send(
                stream_id,
                f"敏感度 {value} 超出范围（{INTENSITY_MIN}~{INTENSITY_MAX}），已忽略。"
                f"如需最高强度请用 /强度 max。",
            )
            return True, "imm_intensity", True
        return await self._apply_intensity(stream_id, value)

    async def _apply_intensity(self, stream_id: str, value: int) -> tuple[bool, str | None, bool]:
        """应用会话级敏感度覆盖并回执（含越过阈值时的即时触发）。"""
        self._store.set_sensitivity(stream_id, value)
        await self._feedback(
            stream_id,
            f"本会话敏感度已设为 {value}/120。\n档位：{self._tier_text(value)}",
        )
        s = self._store.sessions.get(stream_id)
        if s is not None and s.active:
            await self._maybe_proactive(stream_id)
        return True, "imm_intensity", True


    @Command("imm_config", description="查看当前生效配置（管理员；配置本身走 WebUI 热更新）",
             pattern=r"(?<!\S)/?imm_(config|reload)\s*$", aliases=["控制配置"])
    async def cmd_config(self, **kwargs: Any) -> tuple[bool, str | None, bool]:
        stream_id = str(kwargs.get("stream_id") or "")
        user_id = str(kwargs.get("user_id") or "")
        if not self._is_admin(user_id):
            await self._send(stream_id, "权限不足：仅管理员可用。")
            return False, "权限不足", True
        cfg = self._control()
        p = self.config.prompt
        await self._send(
            stream_id,
            "\n".join([
                f"进入关键词：{'、'.join(cfg.enter_keywords)}",
                f"退出关键词：{'、'.join(cfg.exit_keywords)}",
                f"道具：{p.item_name}（全局敏感度 {p.sensitivity}/120，"
                f"本会话 {self._effective_sensitivity(stream_id)}/100，可用 /强度 修改）",
                f"持续 {cfg.state_duration} 秒，冷却 {cfg.cooldown_seconds} 秒，"
                f"并发上限 {cfg.max_concurrent}，退出提示词有效期 {cfg.exit_pending_ttl} 秒",
                f"阈值自动触发：{'关闭' if cfg.proactive_threshold <= 0 else f'{cfg.proactive_threshold}/100'}",
                f"仅管理员可触发：{'是' if cfg.admin_only_mode else '否'}"
                f"（管理员 {len(cfg.admin_ids or [])} 人）",
                f"触发瞬间立刻反应：{'是' if cfg.react_on_trigger else '否（原版静默语义）'}",
                f"配置版本：{SUPPORTED_CONFIG_VERSION}（在 WebUI 修改即热更新，无需重载）",
            ]),
        )
        return True, "imm_config", True


def create_plugin() -> ImmersiveControlPlugin:
    """Runner 加载入口。"""
    return ImmersiveControlPlugin()
