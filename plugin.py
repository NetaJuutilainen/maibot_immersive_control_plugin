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

import asyncio
import importlib.util
import json
import random
import re
import sys
import time
from pathlib import Path
from typing import Any, ClassVar

from maibot_sdk import API, Command, HookHandler, MaiBotPlugin
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
_link_mod = _load_core_module("link")

SUPPORTED_CONFIG_VERSION = _config_mod.SUPPORTED_CONFIG_VERSION
ImmersiveControlConfig = _config_mod.ImmersiveControlConfig
render_template = _config_mod.render_template
SessionStore = _store_mod.SessionStore
auto_sensitivity_value = _store_mod.auto_sensitivity_value
ACTIVATED = _store_mod.ACTIVATED
COOLDOWN = _store_mod.COOLDOWN
LIMIT = _store_mod.LIMIT
BodyLink = _link_mod.BodyLink

INTENSITY_MIN, INTENSITY_MAX = 0, 120  # 指令可设置的敏感度范围（越界直接拒绝）

# A① 身体文案的触发阈值（读 get_state 字段；文案内容与开关在 [linkage] 配置）
BODY_TIRED_BELOW = 30.0     # energy 低于该值 → 精力见底
BODY_HUNGRY_ABOVE = 70.0    # hunger 高于该值 → 饿（body 的 hunger 高=饿）
BODY_SICK_BELOW = 40.0      # health 低于该值 → 不舒服
BODY_TENSE_ABOVE = 55.0     # stress 高于该值 → 紧绷档文案
BODY_BREAKDOWN_ABOVE = 70.0  # stress 高于该值 → 濒临崩溃档文案（叠加）
BODY_BLANK_ABOVE = 90.0     # stress 高于该值 → 大脑空白档文案（叠加）
BODY_EXCITED_ABOVE = 60.0    # euphoria 高于该值 → 亢奋档文案（body 1.3.0+）
BODY_IRRITABLE_ABOVE = 60.0  # dysphoria 高于该值 → 烦躁档文案（body 1.3.0+）
BODY_TEXT_CAP = 40          # 每条身体文案上限（字），防多条命中叠加超出 token 预算

# 顶点情绪脉冲：第 n 次顶点按 0.75^(n-1) 递减，避免连顶点把 valence 顶满
BODY_CLIMAX_VALENCE_DECAY = 0.75

# 顶点 emoji 标记的附加指令（{e} 为配置的 emoji）
_EMOJI_HINT = "（此条回复的结尾不经意地带上{e}，不要解释原因。）"

# 麦生活 sleep_phase → 睡眠深度（与 body 的映射一致，WebUI 展示用）
_SLEEP_DEPTH = {
    "awake": 0.0,
    "woken": 0.0,
    "falling_asleep": 0.35,
    "light_sleep": 0.65,
    "deep_sleep": 1.0,
}

# WebUI 快捷配置：联动段白名单（键名不含段前缀时默认落在 [linkage]）
_LINKAGE_WHITELIST: dict[str, tuple[str, Any, Any]] = {
    "enabled": ("bool", None, None),
    "write_stress": ("bool", None, None),
    "inject_body_text": ("bool", None, None),
    "sensitivity_boost": ("bool", None, None),
    "tick_interval_seconds": ("int", 10, 600),
    "state_ttl_seconds": ("int", 10, 3600),
    "call_timeout_seconds": ("int", 1, 30),
    "stress_target_base": ("float", 0.0, 100.0),
    "stress_target_range": ("float", 0.0, 100.0),
    "stress_approach_gain": ("float", 0.05, 1.0),
    "stress_max_step": ("float", 0.0, 20.0),
    "wake_meter_factor": ("float", 0.0, 1.0),
    "wake_threshold_light": ("float", 20.0, 100.0),
    "wake_threshold_deep": ("float", 40.0, 100.0),
    "sleep_wake_threshold": ("float", 0.0, 100.0),
    "release_on_climax_full": ("float", -20.0, 20.0),
    "release_on_stop": ("float", -20.0, 20.0),
    "climax_valence": ("float", 0.0, 20.0),
    "climax_stress_release": ("float", -10.0, 0.0),
    "afterglow_valence": ("float", 0.0, 15.0),
    "auto_cooldown": ("bool", None, None),
    "cooldown_rate_per_minute": ("float", 0.0, 10.0),
    "cooldown_floor": ("float", 0.0, 100.0),
    "body_wake_note_text": ("str", None, None),
    "body_wake_note_light_text": ("str", None, None),
}

# 插件本体（非联动）的可快捷配置字段：control 段与 prompt 段
# 注：admin_only_mode / admin_ids 不放进来（避免把自己锁在门外，请在插件设置面板调整）
_SECTION_WHITELISTS: dict[str, dict[str, tuple[str, Any, Any]]] = {
    "control": {
        "require_at": ("bool", None, None),
        "enter_keywords": ("list", None, None),
        "exit_keywords": ("list", None, None),
        "state_duration": ("int", 10, 3600),
        "cooldown_seconds": ("int", 0, 3600),
        "max_concurrent": ("int", 1, 9999),
        "exit_pending_ttl": ("int", 5, 3600),
        "react_on_trigger": ("bool", None, None),
        "proactive_threshold": ("int", 0, 120),
        "auto_speed_per_minute": ("int", 1, 120),
        "climax_threshold": ("int", 0, 120),
        "climax_hold_seconds": ("int", 1, 600),
        "climax_max_per_activation": ("int", 1, 99),
        "stealth_mode": ("bool", None, None),
        "stealth_phrases": ("list", None, None),
    },
    "prompt": {
        "item_name": ("str", None, None),
        "sensitivity": ("int", 0, 100),
        "tier_0_20": ("multiline", None, None),
        "tier_20_40": ("multiline", None, None),
        "tier_40_60": ("multiline", None, None),
        "tier_60_80": ("multiline", None, None),
        "tier_80_100": ("multiline", None, None),
        "tier_100_120": ("multiline", None, None),
        "enter_template": ("multiline", None, None),
        "climax_emoji": ("str", None, None),
        "climax_template": ("multiline", None, None),
        "exit_template": ("multiline", None, None),
    },
}

_STATE_FILE = "state.json"


def _norm_text(raw: Any) -> str:
    """关键词匹配用的消息文本归一化：去首尾空白与单个 / 前缀，转小写。"""
    text = str(raw or "").strip().lower()
    if text.startswith("/"):
        text = text[1:].strip()
    return text


def _norm_text_strip_at(raw: Any, keywords: Any = ()) -> str:
    """在归一化基础上剥掉"@昵称"前缀（@ 与昵称间、昵称与关键词间可能有空格）。

    无空格形态（"@麦麦控制"）在文本上分不清昵称与关键词，只在 keywords 里有
    关键词正好收尾时才接受——部分适配器 @ 后不补空格，靠这条兜底。"""
    text = _norm_text(raw)
    if text.startswith("@"):
        if text[1:2].isspace():  # 无名 at（"@ 控制"）：@ 后直接是分隔空白
            return text[1:].strip()
        tail = text[1:].strip()
        if not tail:
            return ""
        if any(ch.isspace() for ch in tail):
            return tail.split(None, 1)[1].strip()
        for kw in keywords:
            kw = _norm_text(kw)
            if kw and len(tail) > len(kw) and tail.endswith(kw):
                return kw
        return ""  # "@麦麦控制"且结尾对不上任何关键词，不匹配
    return text


_CQ_AT = re.compile(r"\[CQ:at,[^\]]*\]", re.IGNORECASE)


def _text_from_raw(raw: Any) -> str:
    """processed_plain_text 缺失时从 raw_message 拼文本（仅用于关键词匹配）。

    兼容消息段数组、OneBot 的 {"message": [...]} 包装与 CQ 字符串
    （[CQ:at,...] 直接删掉，正文原样留下）；at 段后自动补空格，
    避免拼成"@麦麦控制"导致关键词剥不出来。"""
    if isinstance(raw, dict):
        raw = raw.get("message", raw.get("raw_message"))
    if isinstance(raw, str):
        return _CQ_AT.sub("", raw).strip()
    parts: list[str] = []
    for seg in raw or []:
        if not isinstance(seg, dict):
            continue
        stype = str(seg.get("type") or "")
        data = seg.get("data")
        if stype == "text":
            text = data if isinstance(data, str) else str((data or {}).get("text") or "")
            if parts and parts[-1].startswith("@") and text and not text[0].isspace():
                parts.append(" ")
            parts.append(text)
        elif stype in ("at", "mention"):
            name = ""
            if isinstance(data, dict):
                name = str(
                    data.get("target_user_cardname")
                    or data.get("target_user_nickname")
                    or data.get("target_user_id")
                    or ""
                ).strip()
            parts.append(f"@{name}" if name else "@")
    return "".join(parts).strip()


def _keyword_candidates(raw: Any, keywords: Any = ()) -> set[str]:
    """@消息既可能是"@麦麦 控制"也可能是裸"控制"（私聊），返回候选关键词。"""
    candidates = {_norm_text(raw), _norm_text_strip_at(raw, keywords)}
    candidates.discard("")
    return candidates


def _has_at(message: dict) -> bool:
    """是否 @ 了机器人：优先信适配器置位的 is_at；否则兜底扫 raw_message。

    兜底兼容消息段数组、{"message": [...]} 包装、CQ 字符串与 mention 段。"""
    if message.get("is_at"):
        return True
    raw = message.get("raw_message")
    if isinstance(raw, dict):
        raw = raw.get("message", raw.get("raw_message"))
    if isinstance(raw, str):
        return "[cq:at," in raw.lower()
    for seg in raw or []:
        stype = str(seg.get("type") or "") if isinstance(seg, dict) else str(getattr(seg, "type", "") or "")
        if stype in ("at", "mention"):
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
        # 身体联动簿记（B 方向防重复写入的内存集合；插件重载丢失可接受，见设计稿 §八）
        self._active_sessions: set[str] = set()
        self._body_tick: dict[str, float] = {}   # 每会话上次压力追踪时刻
        self._body_released: set[str] = set()    # 每会话已泄压标记（顶点额度用尽后至多一次）
        self._body_wake_meter = 0.0              # 觉醒值（睡着时积累，醒来瞬间结算）
        self._body_last_sleeping: bool | None = None  # 自然醒翻转检测
        self._body_last_phase: str | None = None      # 最近一次已知的睡眠阶段
        self._body_deep_reached = False          # 本次睡眠是否进入过深睡（决定引爆阈值档）
        self._body_wake_note_pending = False     # 醒后记账待注入
        self._body_wake_note_light = False       # 醒后记账档位：False=完整（过阈值）/True=轻度（未达）
        self._ticker_task = None                 # 后台滴灌定时器任务
        self._body_ref = None

    # ---- 生命周期 ----

    async def on_load(self) -> None:
        state_path = self.ctx.paths.data_dir / _STATE_FILE
        self._store.on_changed = lambda: self._store.save(state_path)
        self._store.load(state_path)
        # 重载恢复：把持久化里仍激活的会话补回内存簿记（否则会误判"无会话"而启动冷却）
        self._active_sessions = {sid for sid, s in self._store.sessions.items() if s.active}
        for sid in self._active_sessions:
            self._body_tick.setdefault(sid, time.time())
        # 后台滴灌定时器：激活会话的压力按「强度×时间」累积，不依赖麦麦回话
        self._ticker_task = asyncio.get_running_loop().create_task(self._body_ticker())
        self.ctx.logger.info(
            "沉浸式遥控已加载：持久化状态 %d 条（%s）",
            len(self._store.sessions), state_path,
        )

    async def on_unload(self) -> None:
        task = getattr(self, "_ticker_task", None)
        if task is not None:
            task.cancel()
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

    # ---- 身体联动（[linkage] 全部默认关：关闭时以下方法零 RPC、零路径） ----

    def _body(self):
        """联动客户端（懒建；配置节每次经 getter 现取，热更新安全）。"""
        if self._body_ref is None:
            self._body_ref = BodyLink(lambda: self.config.linkage, self.ctx)
        return self._body_ref

    async def _body_state(self) -> dict[str, Any] | None:
        """读身体状态（缓存内）。任何失败静默返回 None，绝不抛（守卫 2/3/4/5/7）。"""
        try:
            return await self._body().get_state()
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.debug("身体联动读取异常（已降级）：%r", exc)
            return None

    async def _body_text_for(self) -> str:
        """A①：按缓存身体状态拼一段文案（关闭/失败/无命中 → 空串，注入与原版一致）。"""
        link_cfg = self.config.linkage
        if not link_cfg.enabled or not link_cfg.inject_body_text:
            return ""
        state = await self._body_state()
        if not isinstance(state, dict):
            return ""

        def add(condition: bool, text: str) -> None:
            text = str(text or "").strip()[:BODY_TEXT_CAP]
            if condition and text:
                parts.append(text)

        parts: list[str] = []
        add(bool(state.get("sleeping")), link_cfg.body_sleep_text)
        add(float(state.get("energy", 0.0)) < BODY_TIRED_BELOW, link_cfg.body_tired_text)
        add(float(state.get("hunger", 0.0)) > BODY_HUNGRY_ABOVE, link_cfg.body_hungry_text)
        add(float(state.get("health", 0.0)) < BODY_SICK_BELOW, link_cfg.body_sick_text)
        # 压力分档：随滴灌爬坡换词（55 紧绷 → 70 濒临崩溃 → 90 大脑空白，高档替换低档）
        stress_v = float(state.get("stress", 0.0))
        if stress_v >= BODY_BLANK_ABOVE:
            add(True, link_cfg.body_blank_text)
        elif stress_v >= BODY_BREAKDOWN_ABOVE:
            add(True, link_cfg.body_breakdown_text)
        elif stress_v >= BODY_TENSE_ABOVE:
            add(True, link_cfg.body_tense_text)
        add(float(state.get("euphoria", 0.0)) >= BODY_EXCITED_ABOVE,
            link_cfg.body_excited_text)
        add(float(state.get("dysphoria", 0.0)) >= BODY_IRRITABLE_ABOVE,
            link_cfg.body_irritable_text)
        return "；".join(parts)

    async def _body_write(self, delta: float, label: str, depression_delta: float = 0.0,
                          valence: float = 0.0) -> None:
        """B：写一次压力/情绪变化。睡眠场景关闭（阈值=0）时睡着不写；失败静默不重试。"""
        link_cfg = self.config.linkage
        if not link_cfg.enabled or not link_cfg.write_stress:
            return
        state = await self._body_state()
        if state is None:
            return
        if bool(state.get("sleeping")) and float(link_cfg.sleep_wake_threshold) <= 0:
            return  # 睡眠场景关闭：睡着不写（守卫回退到原语义）
        if valence:
            # 情绪写入只对 body 1.3.0+ 生效；睡着时不写（避免睡梦中被刷开心）
            if bool(state.get("sleeping")) or not bool(state.get("mood_capable")):
                valence = 0.0
        try:
            ok = await self._body().adjust_mood(float(delta), label=label,
                                                depression_delta=float(depression_delta),
                                                valence=float(valence))
            if ok:
                self.ctx.logger.debug("身体写入 %+.1f/%+.2f/valence%+.1f（%s）",
                                      delta, depression_delta, valence, label)
            else:
                self.ctx.logger.debug("身体写入未生效（%s）：%s", label, self._body()._last_error)
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.debug("身体联动写入异常（已降级）：%r", exc)

    async def _body_on_activated(self, session_id: str) -> None:
        """B：激活事件。首次激活登记追踪锚点并重置泄压标记；重入刷新不重复登记。
        集合先改后 await（asyncio 在 await 处让出，先改封死并发双写窗口）。"""
        link_cfg = self.config.linkage
        if not link_cfg.enabled or not link_cfg.write_stress:
            return
        if session_id in self._active_sessions:
            return
        self._active_sessions.add(session_id)
        self._body_released.discard(session_id)  # 新一轮激活重新获得泄压额度
        self._body_tick[session_id] = time.time()

    async def _body_tick_stress(self, session_id: str, effective: int) -> None:
        """B 核心：压力向「强度目标」追踪（强度降 → 自然回落；强度顶满 → 顶在高位）。

        目标 = stress_target_base + stress_target_range × (当前强度/120)。
        每次回复滴灌一次：delta = (目标 − 当前压力) × approach_gain，单次经 body ±20 夹取。
        会话自然超时结束时不泄压——压力顶在原地，由 body 引擎的自我平衡慢慢回落。
        """
        link_cfg = self.config.linkage
        if not link_cfg.enabled or not link_cfg.write_stress:
            return
        now = time.time()
        last = self._body_tick.get(session_id)
        if last is None:
            self._body_tick[session_id] = now  # 无锚点（如重载恢复的会话）：先登记不写
            return
        self._body_tick[session_id] = now  # 先改簿记后 await（防并发双写）
        state = await self._body_state()
        if not isinstance(state, dict):
            return
        sleeping = bool(state.get("sleeping"))
        threshold = float(link_cfg.sleep_wake_threshold)
        target = float(link_cfg.stress_target_base) + float(link_cfg.stress_target_range) * (
            max(0, effective) / 120.0)
        current = float(state.get("stress", 0.0))
        delta = float(link_cfg.stress_approach_gain) * (target - current)
        delta = max(-20.0, min(20.0, delta))
        step_cap = float(link_cfg.stress_max_step)
        if step_cap > 0:
            delta = max(-step_cap, min(step_cap, delta))  # 单 tick 步长上限，避免早期跳变
        # 睡眠场景：睡着时滴灌照常（身体在睡梦中积累），觉醒值按比例积累；
        # 引爆阈值由睡眠深度决定——深睡要攒满整条（100）才醒，入睡/浅睡半条（60）即醒
        if sleeping and threshold > 0 and delta > 0:
            self._body_wake_meter = min(100.0, self._body_wake_meter + delta * float(link_cfg.wake_meter_factor))
            phase = None
            try:
                phase = await self._body().get_sleep_phase()
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.debug("睡眠阶段读取异常（已降级）：%r", exc)
            if phase:
                self._body_last_phase = phase
                if phase == "deep_sleep":
                    self._body_deep_reached = True
        # 自然醒翻转检测：醒来瞬间按本次睡眠最深阶段结算觉醒值
        prev = self._body_last_sleeping
        self._body_last_sleeping = sleeping
        if prev is True and not sleeping:
            # 睡眠场景关闭（sleep_wake_threshold=0）→ 直接清零不记账
            if float(link_cfg.sleep_wake_threshold) <= 0:
                self._body_wake_meter = 0.0
                self._body_deep_reached = False
            else:
                thr = (float(link_cfg.wake_threshold_deep) if self._body_deep_reached
                       else float(link_cfg.wake_threshold_light))
                if self._body_wake_meter >= thr:
                    # 达标 → 余韵引爆 + 完整模糊记账
                    self._body_wake_note_pending = bool(
                        str(link_cfg.body_wake_note_text or "").strip())
                    self._body_wake_note_light = False
                    await self._body_write(20.0, label="睡梦余韵引爆")
                elif self._body_wake_meter > 0:
                    # 未达阈值 → 轻记账（醒来身体有异样感，不点破细节）
                    self._body_wake_note_light = bool(
                        str(link_cfg.body_wake_note_light_text or "").strip())
                    self._body_wake_note_pending = self._body_wake_note_light
                self._body_wake_meter = 0.0
                self._body_deep_reached = False
        if abs(delta) < 0.5:
            return  # 已贴住目标，不值得一次 RPC
        await self._body_write(delta, label="遥控强度追踪")

    async def _body_on_climax_full(self, session_id: str) -> None:
        """B：顶点额度用尽（count 达上限）→ 泄压一次（每次激活至多一次）。"""
        link_cfg = self.config.linkage
        if not link_cfg.enabled or not link_cfg.write_stress:
            return
        if session_id in self._body_released:
            return
        s = self._store.sessions.get(session_id)
        if s is None or not s.active:
            return
        cap = max(1, int(self._control().climax_max_per_activation))
        if int(getattr(s, "climax_count", 0)) < cap:
            return
        if float(link_cfg.release_on_climax_full):
            self._body_released.add(session_id)  # 先改簿记后 await
            await self._body_write(float(link_cfg.release_on_climax_full), label="顶点额度用尽泄压")

    async def _body_on_climax(self, session_id: str) -> None:
        """B：每次顶点给一次正向情绪脉冲（按 0.75 递减），并小泄一口压力形成锯齿。"""
        link_cfg = self.config.linkage
        if not link_cfg.enabled or not link_cfg.write_stress:
            return
        s = self._store.sessions.get(session_id)
        if s is None:
            return
        n = max(1, int(getattr(s, "climax_count", 0)))
        base = float(link_cfg.climax_valence)
        val = base * (BODY_CLIMAX_VALENCE_DECAY ** (n - 1)) if base > 0 else 0.0
        drop = float(link_cfg.climax_stress_release)
        if not val and not drop:
            return
        await self._body_write(drop, label=f"顶点第{n}次", valence=val)

    async def _body_ticker(self) -> None:
        """后台滴灌定时器（修复：睡着/夜间麦麦不回话时压力不再累积的结构性问题）。

        每 tick_interval_seconds 对各激活会话：
        - prompt_state 惰性结算超时（修掉「无回复时超时永不结算」的挂起问题）；
        - enter → 压力向强度目标追踪一步 + 睡眠场景觉醒值积累（深度感知）；
        - exit → 玩具停下泄压；none → 自然结束，清理簿记不泄压。
        循环内所有异常降级（守卫 7），永不炸任务。"""
        while True:
            try:
                link_cfg = self.config.linkage
                await asyncio.sleep(max(10, int(link_cfg.tick_interval_seconds)))
                if not self.config.plugin.enabled or not link_cfg.enabled:
                    continue
                await self._body_guard(self._body_tick_all)
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.debug("身体滴灌定时器异常（已降级）：%r", exc)

    async def _body_tick_all(self) -> None:
        """一个 tick 内推进全部激活会话（**以 store 为准**，插件重载后也能续上）。

        冷却条件也看 store 的真实激活数，避免"内存集合为空但其实有会话"时误冷却。
        """
        active_ids = [sid for sid, s in self._store.sessions.items()
                      if s.active or s.exit_ts > 0]
        self._active_sessions &= set(active_ids)   # 清理陈旧簿记（会话已被清除/结束）
        for sid in active_ids:
            state = self._store.prompt_state(sid, self._control())
            await self._body_guard(lambda: self._sync_body_session(sid, state))
            if state == "enter":
                await self._body_guard(lambda: self._body_tick_stress(
                    sid, self._effective_sensitivity(sid)))
        if not active_ids:
            await self._body_guard(self._body_cooldown_tick)
            # 无会话时觉醒值缓慢消退（避免会话结束后停在某个值上不动）
            if self._body_wake_meter > 0:
                self._body_wake_meter = max(0.0, self._body_wake_meter - 2.0)

    async def _body_cooldown_tick(self) -> None:
        """无激活会话时的主动冷却：压力高于下限就按速率写回一小步。

        开销：每个 tick（默认 30 秒）最多一次轻量 adjust_mood，无 LLM、无额外查询
        （get_state 走缓存）；降到 cooldown_floor 即停，交还身体自身稳态。
        """
        link_cfg = self.config.linkage
        if not link_cfg.enabled or not link_cfg.write_stress or not link_cfg.auto_cooldown:
            return
        rate = float(link_cfg.cooldown_rate_per_minute)
        if rate <= 0:
            return
        state = await self._body_state()
        if not isinstance(state, dict):
            return
        floor = float(link_cfg.cooldown_floor)
        gap = float(state.get("stress", 0.0)) - floor
        if gap < 0.5:
            return  # 已到下限
        dt_min = max(10, int(link_cfg.tick_interval_seconds)) / 60.0
        step = min(gap, rate * dt_min)
        if step < 0.3:
            return
        await self._body_write(-step, label="冷却回落")

    async def _sync_body_session(self, session_id: str, state: str) -> None:
        """B：过渡检测。enter 入集；「exit」（玩具停下）→ 泄压一次；
        「none」（自然超时/待退出过期）→ 不泄压，压力顶在原地由引擎自然回落。"""
        link_cfg = self.config.linkage
        if not link_cfg.enabled or not link_cfg.write_stress:
            return
        if state == "enter":
            self._active_sessions.add(session_id)
            return
        if session_id in self._active_sessions:
            self._active_sessions.discard(session_id)  # 先改集合后 await
            self._body_tick.pop(session_id, None)      # 追踪锚点一并清掉
            if state == "exit" and float(link_cfg.release_on_stop):
                s = self._store.sessions.get(session_id)
                climaxes = int(getattr(s, "climax_count", 0)) if s is not None else 0
                # 只有真的到过顶点才给「余韵」；无顶点直接停只泄压，不凭空发糖
                afterglow = float(link_cfg.afterglow_valence) if climaxes > 0 else 0.0
                await self._body_write(float(link_cfg.release_on_stop), label="玩具停下泄压",
                                       valence=afterglow)

    async def _body_display_boost(self) -> int:
        """A②（默认关）：身体脆弱 → 显示用加成值；仅注入档位与 /强度 显示，机制判定永不加成。"""
        link_cfg = self.config.linkage
        if not link_cfg.enabled or not link_cfg.sensitivity_boost:
            return 0
        state = await self._body_state()
        if not isinstance(state, dict) or state.get("sleeping"):
            return 0
        fragile = (float(state.get("energy", 100.0)) < float(link_cfg.boost_energy_below)
                   or float(state.get("health", 100.0)) < BODY_SICK_BELOW)
        return int(link_cfg.boost_delta) if fragile else 0

    async def _body_guard(self, action) -> None:
        """守卫 7 兜底：联动代码整体不得向钩子抛异常（error_policy=SKIP 会连坐原版注入）。"""
        try:
            await action()
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.debug("身体联动异常（已降级）：%r", exc)

    def _capture_display_name(self, session_id: str, message: dict) -> None:
        """激活时记录会话显示名：群聊取群名、私聊取名片/昵称（WebUI 展示用真名）。"""
        s = self._store.sessions.get(session_id)
        if s is None:
            return
        try:
            info = message.get("message_info") or {}
            user = info.get("user_info") or {}
            group = info.get("group_info") or {}
            if group.get("group_id"):
                s.is_group = True
                s.display_name = str(group.get("group_name") or "").strip()[:40]
            else:
                s.is_group = False
                s.display_name = str(
                    user.get("user_cardname") or user.get("user_nickname") or ""
                ).strip()[:40]
            self._store._changed()
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.debug("显示名捕获异常（已降级）：%r", exc)

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
        """持续越限检查：敏感度保持在阈值以上达到时长 → 返回一次顶点注入文本。
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

        raw_text = message.get("processed_plain_text")
        if not isinstance(raw_text, str) or not raw_text.strip():
            raw_text = _text_from_raw(message.get("raw_message"))
        text = _norm_text(raw_text)
        if not text:
            return None

        enter_words = {_norm_text(w) for w in (cfg.enter_keywords or []) if str(w).strip()}
        exit_words = {_norm_text(w) for w in (cfg.exit_keywords or []) if str(w).strip()}
        words = enter_words | exit_words

        candidates = _keyword_candidates(raw_text, words)
        hit_words = candidates & words

        # @ 门槛：群聊要求 @bot（私聊始终放行）
        if cfg.require_at and _is_group_message(message) and not _has_at(message):
            # 疑似关键词却读不到 @：留一行日志，避免再次「完全静默」无从排查
            if hit_words:
                self.ctx.logger.warning(
                    "群聊疑似关键词但未识别到 @（适配器未提供 at 信息？）text=%r", raw_text,
                )
            return None

        if not hit_words:
            return None  # 非关键词消息完全旁路，零开销
        hit_enter = bool(hit_words & enter_words)
        hit_exit = bool(hit_words & exit_words)

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
                # WebUI 展示用：记录会话真名（群名 / 用户昵称·名片）
                self._capture_display_name(session_id, message)
                # B：首次激活写压力（重入刷新不写；联动关闭时零路径）
                await self._body_guard(lambda: self._body_on_activated(session_id))
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
        # B 簿记：过渡检测（enter 入集；exit/none 且原在集合 → 一次结束缓释）
        await self._body_guard(lambda: self._sync_body_session(session_id, state))
        if state == "none":
            return None

        prompt_cfg = self.config.prompt
        effective = self._effective_sensitivity(session_id)
        # A②（默认关）：显示用加成值仅作用于注入档位与 {sensitivity}；climax/proactive 判定仍用原值
        try:
            boost = await self._body_display_boost()
        except Exception:  # noqa: BLE001 —— 守卫 7
            boost = 0
        effective_display = min(120, effective + boost) if boost > 0 else effective
        if state == "enter":
            inject = render_template(
                prompt_cfg.enter_template,
                prompt_cfg.item_name,
                effective_display,
                self._control().state_duration,
            )
            inject = f"{inject}\n（当前强度参考：{self._tier_text(effective_display)}）"
            # A①：身体状态文案（固定拼在档位行后、climax 注入前；关闭/失败/无命中 → 不追加）
            body_text = await self._body_text_for()
            if body_text:
                inject = f"{inject}\n{body_text}"
            # 自动模式爬升越过阈值时在对话流中触发（每次激活最多一次）
            await self._maybe_proactive(session_id)
            climax_inject = self._check_climax(session_id, effective)
            if climax_inject:
                inject = f"{inject}\n{climax_inject}"
                await self._body_guard(lambda: self._body_on_climax(session_id))
            # B：顶点额度用尽 → 泄压一次（判定用未加成的 effective，机制不受 A② 影响）
            await self._body_guard(lambda: self._body_on_climax_full(session_id))
            # B 核心：压力向「强度目标」追踪（强度降自然回落；自然超时不泄压）
            await self._body_guard(lambda: self._body_tick_stress(session_id, effective))
            # 睡梦余韵记账（自然醒来后的一次性注入；档位由觉醒值是否过阈值决定）
            if self._body_wake_note_pending:
                src = (self.config.linkage.body_wake_note_text if not self._body_wake_note_light
                       else self.config.linkage.body_wake_note_light_text)
                note = str(src or "").strip()
                if note:
                    inject = f"{inject}\n{note[:120]}"
                self._body_wake_note_pending = False
                self._body_wake_note_light = False
                self.ctx.logger.debug("会话 %s 注入睡梦余韵记账", session_id)
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
        self._active_sessions.clear()  # B 簿记同步：清除后重激活应重新登记追踪锚点
        self._body_tick.clear()
        self._body_released.clear()
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
            # A②（默认关）：身体脆弱时显示加成值；加成 0 → 原格式（守卫 8）
            try:
                boost = await self._body_display_boost()
            except Exception:  # noqa: BLE001 —— 守卫 7
                boost = 0
            if boost > 0:
                shown = min(120, current + boost)
                head = (f"当前敏感度：{shown}/120（基础 {current} + 身体加成 {boost}；"
                        f"模式：{self._intensity_mode_text(stream_id)}）")
            else:
                head = f"当前敏感度：{current}/120（模式：{self._intensity_mode_text(stream_id)}）"
                shown = current
            await self._send(
                stream_id,
                f"{head}\n"
                f"档位：{self._tier_text(shown)}\n"
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
                f"玩具：{p.item_name}（全局敏感度 {p.sensitivity}/120，"
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

    # ---- WebUI（P1 状态页数据；页面在 webui/linkage.html，经 window.maibot 桥调用） ----

    @API(name="webui_linkage", description="联动管理页数据：激活会话/觉醒值/压力/睡眠/熔断（WebUI 专用）",
         version="1", public=False)
    async def webui_linkage(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        cfg = self.config.linkage
        now = time.time()
        sessions: list[dict[str, Any]] = []
        for sid, s in self._store.sessions.items():
            if not s.active and s.exit_ts <= 0:
                continue
            remain = 0
            if s.active and s.exit_ts <= 0:
                remain = max(0, int(self._control().state_duration - (now - s.started_ts)))
            sessions.append({
                "display_name": s.display_name or sid[-8:],
                "is_group": bool(s.is_group),
                "sensitivity": int(self._effective_sensitivity(sid)),
                "remaining_seconds": remain,
                "climax_count": int(s.climax_count),
                "released": sid in self._body_released,
            })
        state = await self._body_state()
        sleeping = bool(state.get("sleeping")) if isinstance(state, dict) else False
        phase = ""
        if sleeping:
            try:
                phase = str(await self._body().get_sleep_phase() or "")
            except Exception:  # noqa: BLE001
                phase = ""
        eff = max([x["sensitivity"] for x in sessions],
                  default=int(self.config.prompt.sensitivity))
        target = float(cfg.stress_target_base) + float(cfg.stress_target_range) * (eff / 120.0)
        body_ref = self._body()
        return {
            "ok": True,
            "sessions": sessions,
            "wake_meter": round(float(self._body_wake_meter), 1),
            "wake_threshold_light": float(cfg.wake_threshold_light),
            "wake_threshold_deep": float(cfg.wake_threshold_deep),
            "sleeping": sleeping,
            "sleep_phase": phase,
            "sleep_depth": round(_SLEEP_DEPTH.get(phase, 0.0), 2),
            "stress": (round(float(state.get("stress", 0.0)), 1)
                       if isinstance(state, dict) else None),
            "stress_target": round(target, 1),
            "drip_interval": int(cfg.tick_interval_seconds),
            "breaker": {
                "fail_count": int(getattr(body_ref, "_fail_count", 0)),
                "open_until": float(getattr(body_ref, "_open_until", 0.0)),
            },
            "enabled": bool(cfg.enabled),
            "write_stress": bool(cfg.write_stress),
            "config": self._config_snapshot(),
        }

    def _config_snapshot(self) -> dict[str, dict[str, Any]]:
        """WebUI 配置画像：linkage / control / prompt 三段的白名单字段当前值。"""
        snap: dict[str, dict[str, Any]] = {
            "linkage": {k: getattr(self.config.linkage, k, None) for k in _LINKAGE_WHITELIST},
        }
        for section, table in _SECTION_WHITELISTS.items():
            obj = self._section_cfg(section)
            snap[section] = {k: getattr(obj, k, None) for k in table}
        return snap

    # ---- WebUI P2：快捷配置（白名单补丁）与快捷动作 ----

    def _config_path(self):
        return getattr(self, "_cfg_path", _PLUGIN_DIR / "config.toml")

    def _section_cfg(self, section: str):
        """按段名取配置对象（linkage / control / prompt）。"""
        if section == "control":
            return self.config.control
        if section == "prompt":
            return self.config.prompt
        return self.config.linkage

    @staticmethod
    def _split_list(raw: Any, cap: int = 64) -> list[str]:
        """规范成 list[str]：**只按换行拆**（关键词/隐蔽语句本身可能含中英文逗号），去空白、滤空项、保序去重。"""
        if isinstance(raw, (list, tuple)):
            items = [str(x or "").strip() for x in raw]
        else:
            text = str(raw or "").replace("\r\n", "\n").replace("\r", "\n")
            items = [x.strip() for x in text.split("\n")]
        out: list[str] = []
        for x in items:
            if not x or x in out:
                continue
            out.append(x[:120])
            if len(out) >= cap:
                break
        return out

    def _apply_linkage_patch(self, patch: dict) -> tuple[dict, list[str]]:
        """校验并应用配置补丁（支持 "段.键" 前缀；无前缀默认 linkage）：内存生效 + 写回 config.toml。"""
        applied: dict[str, Any] = {}
        rejected: list[str] = []
        by_section: dict[str, dict[str, Any]] = {}
        for raw_key, raw in (patch or {}).items():
            key = str(raw_key).strip()
            if "." in key:
                section, name = key.split(".", 1)
            else:
                section, name = "linkage", key
            table = _LINKAGE_WHITELIST if section == "linkage" else _SECTION_WHITELISTS.get(
                section, {})
            spec = table.get(name)
            if spec is None:
                rejected.append(f"{key}: 不在白名单")
                continue
            kind, lo, hi = spec
            try:
                if kind == "bool":
                    val: Any = bool(raw) if isinstance(raw, bool) else str(raw).lower() in (
                        "1", "true", "on", "yes")
                elif kind == "int":
                    val = int(float(raw))
                    if lo is not None:
                        val = max(int(lo), min(int(hi), val))
                elif kind == "float":
                    val = round(float(raw), 3)
                    if lo is not None:
                        val = max(float(lo), min(float(hi), val))
                elif kind == "list":
                    val = self._split_list(raw)
                    if not val and section == "control" and name in (
                            "enter_keywords", "exit_keywords"):
                        rejected.append(f"{key}: 不能为空（空列表会让关键词永不触发）")
                        continue
                elif kind == "multiline":
                    val = str(raw or "")[:4000]
                else:
                    val = str(raw or "")[:200]
            except (TypeError, ValueError):
                rejected.append(f"{key}: 值非法")
                continue
            try:
                setattr(self._section_cfg(section), name, val)
            except Exception as exc:  # noqa: BLE001
                rejected.append(f"{key}: {exc}"[:80])
                continue
            applied[key] = val
            by_section.setdefault(section, {})[name] = val
        for section, values in by_section.items():
            self._write_config_section(section, values)
        return applied, rejected

    def _write_config_section(self, section: str, applied: dict) -> None:
        """把已应用的键写回 config.toml 的指定段（只改该段，其余原样保留）。"""
        path = self._config_path()
        try:
            text = path.read_text(encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("读取 config.toml 失败（配置仅内存生效）：%r", exc)
            return
        lines = text.splitlines()
        header = f"[{section}]"
        start = next((i for i, ln in enumerate(lines) if ln.strip() == header), None)
        if start is None:
            lines.extend(["", header])
            start = len(lines) - 1
        end = next((i for i in range(start + 1, len(lines))
                    if lines[i].lstrip().startswith("[")), len(lines))

        def fmt(v: Any) -> str:
            if isinstance(v, bool):
                return "true" if v else "false"
            if isinstance(v, (list, tuple)):
                inner = ", ".join('"' + str(x).replace('"', '\\"') + '"' for x in v)
                return f"[{inner}]"
            if isinstance(v, str):
                return '"' + v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
            return str(v)

        for key, val in applied.items():
            new_line = f"{key} = {fmt(val)}"
            for i in range(start + 1, end):
                if lines[i].split("=", 1)[0].strip() == key:
                    lines[i] = new_line
                    break
            else:
                lines.insert(end, new_line)
                end += 1
        try:
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("写回 config.toml 失败（配置仅内存生效）：%r", exc)

    @API(name="webui_linkage_config",
         description="联动配置补丁（白名单键）：内存立即生效并写回 config.toml，供 WebUI 快捷配置区使用",
         version="1", public=False)
    async def webui_linkage_config(self, patch: str = "", **kwargs: Any) -> dict[str, Any]:
        del kwargs
        try:
            data = json.loads(patch) if patch else {}
        except Exception:  # noqa: BLE001
            return {"ok": False, "error": "patch 不是合法 JSON"}
        if not isinstance(data, dict):
            return {"ok": False, "error": "patch 必须是对象"}
        applied, rejected = self._apply_linkage_patch(data)
        if applied:
            self.ctx.logger.info("WebUI 快捷配置已应用：%s", ", ".join(applied))
        return {"ok": True, "applied": applied, "rejected": rejected}

    @API(name="webui_linkage_action",
         description="联动快捷动作：release 立即泄压 / clear 清空会话 / pause 暂停滴灌 / resume 恢复滴灌",
         version="1", public=False)
    async def webui_linkage_action(self, op: str = "", **kwargs: Any) -> dict[str, Any]:
        del kwargs
        name = str(op or "").strip().lower()
        if name == "release":
            cfg = self.config.linkage
            delta = float(cfg.release_on_stop or -15.0)
            await self._body_guard(lambda: self._body_write(delta, label="手动泄压"))
            return {"ok": True, "op": name, "delta": delta}
        if name == "clear":
            cleared = self._store.clear()
            self._active_sessions.clear()
            self._body_tick.clear()
            self._body_released.clear()
            return {"ok": True, "op": name, "cleared": int(cleared)}
        if name in ("pause", "resume"):
            applied, rejected = self._apply_linkage_patch({"write_stress": name == "resume"})
            return {"ok": True, "op": name, "applied": applied, "rejected": rejected}
        return {"ok": False, "error": f"未知操作: {name}"}


def create_plugin() -> ImmersiveControlPlugin:
    """Runner 加载入口。"""
    return ImmersiveControlPlugin()
