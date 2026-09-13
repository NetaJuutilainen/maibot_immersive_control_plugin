"""沉浸式遥控插件：会话状态机（纯标准库，不依赖任何框架）。

移植自 astrbot_plugin_immersive_control 的 SessionStore，语义保持一致：
 - 会话按聊天流（session_id）隔离；
 - 进入关键词激活，冷却期内不能再次进入；
 - 激活期间命中退出关键词 → 转为"待退出"（exit-pending），下一次 LLM 回复
   注入一次退出模板后彻底结束；
 - 激活超过 state_duration 自动结束（惰性判定，无后台定时任务）；
 - 状态可持久化为 JSON，重启不丢。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

ACTIVATED = "activated"
COOLDOWN = "cooldown"
LIMIT = "limit"


def auto_sensitivity_value(elapsed_sec: float, speed_per_minute: float) -> int:
    """自动模式的敏感度曲线（整数输出）：
    30 → 100 爬升（70 点），之后在 100 → 50 → 100 之间往复（每程 50 点）。
    speed_per_minute 为每分钟变化的点数。"""
    p = max(0.0, speed_per_minute) * max(0.0, float(elapsed_sec)) / 60.0
    if p <= 70.0:
        value = 30.0 + p
    else:
        r = (p - 70.0) % 100.0
        value = 100.0 - r if r < 50.0 else 50.0 + (r - 50.0)
    return int(round(value))

# 配置鸭子类型：只需要 store 用到的这几个属性（来自 core.config.ControlSectionConfig）。


@dataclass(slots=True)
class Session:
    active: bool = False
    started_ts: float = 0.0        # 本次激活开始时间
    exit_ts: float = 0.0           # 非 0 表示待退出（退出模板尚未注入）
    cooldown_end: float = 0.0      # 冷却截止时间
    started_by: str = ""           # 触发者 user_id（管理命令展示用）
    sensitivity: int = -1          # 会话级敏感度覆盖（-1 = 跟随全局；0-120 为合法档位）
    auto_mode: bool = False        # 自动模式：敏感度按三角波随时间变化
    auto_ts: float = 0.0           # 自动模式的时间锚点
    proactive_fired: bool = False  # 本次激活期间阈值自动触发是否已用过
    climax_ts: float = 0.0         # 持续越限开始时间（0 = 未在计时）
    climax_count: int = 0          # 本次激活期间顶点效果已触发次数

    def to_dict(self) -> dict:
        return {
            "active": self.active,
            "started_ts": self.started_ts,
            "exit_ts": self.exit_ts,
            "cooldown_end": self.cooldown_end,
            "started_by": self.started_by,
            "sensitivity": self.sensitivity,
            "auto_mode": self.auto_mode,
            "auto_ts": self.auto_ts,
            "proactive_fired": self.proactive_fired,
            "climax_ts": self.climax_ts,
            "climax_count": self.climax_count,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Session":
        return cls(
            active=bool(data.get("active", False)),
            started_ts=float(data.get("started_ts", 0.0) or 0.0),
            exit_ts=float(data.get("exit_ts", 0.0) or 0.0),
            cooldown_end=float(data.get("cooldown_end", 0.0) or 0.0),
            started_by=str(data.get("started_by", "") or ""),
            sensitivity=int(data.get("sensitivity", -1)),
            auto_mode=bool(data.get("auto_mode", False)),
            auto_ts=float(data.get("auto_ts", 0.0) or 0.0),
            proactive_fired=bool(data.get("proactive_fired", False)),
            climax_ts=float(data.get("climax_ts", 0.0) or 0.0),
            climax_count=int(data.get("climax_count", 0) or 0),
        )


@dataclass
class SessionStore:
    """会话状态表。config 只需暴露 state_duration / cooldown_seconds /
    max_concurrent / exit_pending_ttl 四个属性；on_changed 在每次状态变更后
    被调用（用于插件层落盘），失败不影响状态机本身。"""

    sessions: dict[str, Session] = field(default_factory=dict)
    on_changed: object = None  # Callable[[], None] | None

    # ---- 基础 ----

    def get(self, key: str) -> Session:
        return self.sessions.setdefault(key, Session())

    def _changed(self) -> None:
        if self.on_changed is not None:
            try:
                self.on_changed()
            except Exception:  # noqa: BLE001  持久化失败只丢最近一次状态
                pass

    def active_count(self) -> int:
        return sum(1 for s in self.sessions.values() if s.active)

    # ---- 状态机 ----

    def try_activate(self, key: str, cfg, now: float | None = None) -> tuple[str, int]:
        """尝试激活会话。返回 (结果, 提示秒数)：COOLDOWN 时为剩余冷却秒数，其余为 0。"""
        now = time.time() if now is None else now
        s = self.get(key)

        # 冷却判定（含未过期冷却）
        if now < s.cooldown_end:
            return COOLDOWN, max(1, int(s.cooldown_end - now + 0.999))

        if s.active:
            if s.exit_ts > 0:
                s.exit_ts = 0.0  # 待退出期间再次进入 → 撤销退出，续上原状态
                s.proactive_fired = False
            else:
                s.started_ts = now  # 刷新时长（重置阈值触发标记，允许再次自动触发）
                s.proactive_fired = False
            s.climax_ts = 0.0
            s.climax_count = 0
            self._changed()
            return ACTIVATED, 0

        if self.active_count() >= max(1, int(cfg.max_concurrent)):
            return LIMIT, 0

        s.active = True
        s.started_ts = now
        s.exit_ts = 0.0
        s.proactive_fired = False
        s.climax_ts = 0.0
        s.climax_count = 0
        self._changed()
        return ACTIVATED, 0

    def set_sensitivity(self, key: str, value: int) -> None:
        """设置会话级敏感度覆盖（0-120），同时关闭自动模式。"""
        s = self.get(key)
        s.sensitivity = max(0, min(120, int(value)))
        s.auto_mode = False
        s.auto_ts = 0.0
        self._changed()

    def set_auto(self, key: str, now: float | None = None) -> None:
        """开启自动模式：从 30 开始按三角波变化。"""
        now = time.time() if now is None else now
        s = self.get(key)
        s.auto_mode = True
        s.auto_ts = now
        self._changed()

    def clear_override(self, key: str) -> None:
        """清除会话覆盖（含自动模式），恢复跟随全局配置。"""
        s = self.get(key)
        s.sensitivity = -1
        s.auto_mode = False
        s.auto_ts = 0.0
        self._changed()

    def ensure_auto_anchor(self, key: str, now: float | None = None) -> None:
        """自动模式但锚点缺失（如旧数据）时补上锚点。"""
        now = time.time() if now is None else now
        s = self.sessions.get(key)
        if s is not None and s.auto_mode and s.auto_ts <= 0:
            s.auto_ts = now
            self._changed()

    def try_exit(self, key: str, now: float | None = None) -> bool:
        """命中退出关键词：仅在激活中/待退出时生效，返回是否处理了该消息。"""
        now = time.time() if now is None else now
        s = self.sessions.get(key)
        if s is None or not s.active:
            return False
        s.exit_ts = now
        self._changed()
        return True

    def prompt_state(self, key: str, cfg, now: float | None = None) -> str:
        """LLM 回复前查询：返回 "enter" / "exit" / "none"，并在此惰性结算超时与退出。

        - "enter"：激活中（持续注入进入模板）；
        - "exit"：待退出（本词仅返回一次，随后彻底结束并进入冷却）；
        - "none"：无状态。激活超过 state_duration 时在此结算为结束+冷却。
        """
        now = time.time() if now is None else now
        s = self.sessions.get(key)
        if s is None:
            return "none"

        if s.active and not s.exit_ts and now - s.started_ts >= cfg.state_duration:
            # 自然到期：结束并进入冷却
            s.active = False
            s.started_ts = 0.0
            s.cooldown_end = now + max(0, int(cfg.cooldown_seconds))
            self._changed()

        if s.active:
            if s.exit_ts > 0:
                if now - s.exit_ts > max(5, int(cfg.exit_pending_ttl)):
                    # 待退出过期：静默结束
                    s.active = False
                    s.exit_ts = 0.0
                    s.cooldown_end = now + max(0, int(cfg.cooldown_seconds))
                    self._changed()
                    return "none"
                # 注入一次退出模板后彻底结束
                s.active = False
                s.exit_ts = 0.0
                s.cooldown_end = now + max(0, int(cfg.cooldown_seconds))
                self._changed()
                return "exit"
            return "enter"
        return "none"

    def status_of(self, key: str, cfg, now: float | None = None) -> dict:
        """管理命令用：某会话的当前状态摘要。"""
        now = time.time() if now is None else now
        s = self.sessions.get(key)
        if s is None:
            return {"state": "无记录"}
        if s.active and not s.exit_ts and now - s.started_ts >= cfg.state_duration:
            self.prompt_state(key, cfg, now=now)
            s = self.sessions.get(key, Session())
        if s.active:
            if s.exit_ts > 0:
                return {"state": "待退出（下一次回复注入退出提示词）"}
            remaining = max(0, int(cfg.state_duration - (now - s.started_ts)))
            return {"state": "激活中", "剩余秒数": remaining, "触发者": s.started_by or "未知"}
        if now < s.cooldown_end:
            return {"state": "冷却中", "剩余秒数": int(s.cooldown_end - now + 0.999)}
        if s.started_by:
            return {"state": "已结束（上次触发者 " + s.started_by + "）"}
        return {"state": "已结束"}

    def clear(self) -> int:
        """清空全部状态，返回清除的会话数。"""
        n = len(self.sessions)
        self.sessions.clear()
        if n:
            self._changed()
        return n

    # ---- 持久化 ----

    def to_dict(self) -> dict:
        return {k: s.to_dict() for k, s in self.sessions.items()}

    def load_dict(self, data: dict) -> None:
        self.sessions = {
            str(k): Session.from_dict(v) for k, v in (data or {}).items() if isinstance(v, dict)
        }

    def save(self, path: Path) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self.to_dict(), ensure_ascii=False), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    def load(self, path: Path) -> None:
        try:
            self.load_dict(json.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            pass
        except Exception:  # noqa: BLE001  坏文件当无状态处理
            self.sessions = {}
