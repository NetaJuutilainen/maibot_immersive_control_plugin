"""沉浸式遥控插件：配置模型（MaiBot PluginConfigBase，Pydantic 声明式）。

由 plugin.py 以受控 importlib 方式加载（模块名前缀 maibot_immersive_control_），
本文件不依赖任何 AstrBot API，只依赖 maibot_sdk。
"""

from __future__ import annotations

from typing import Any

from maibot_sdk import Field, PluginConfigBase

SUPPORTED_CONFIG_VERSION = "1.2.2"

# 提示词模板中的占位符（用 replace 而非 str.format，模板中出现裸花括号也不会炸）
PLACEHOLDER_ITEM = "{item_name}"
PLACEHOLDER_SENSITIVITY = "{sensitivity}"


def _ui_field(label: str, hint: str = "", **kwargs: Any) -> Any:
    """配置页字段工厂：插件配置页只读 `label` / `hint`，不读 `description`。

    只写 `description` 时，控制台会把字段名（`react_on_trigger` …）当标签显示；
    这里让同一段中文同时充当 `description` / `hint`，`label` 另给简短中文名。
    `json_schema_extra` 里已有的键（`hidden` / `rows` 等）保留。
    """
    extra = dict(kwargs.pop("json_schema_extra", None) or {})
    extra.setdefault("label", label)
    if hint:
        extra.setdefault("hint", hint)
    return Field(description=hint or label, json_schema_extra=extra, **kwargs)


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = _ui_field("启用插件", "是否启用插件", default=True)
    config_version: str = _ui_field(
        "配置版本",
        "配置版本（与插件版本同步）",
        default=SUPPORTED_CONFIG_VERSION,
        json_schema_extra={"hidden": True, "disabled": True},
    )


class ControlSectionConfig(PluginConfigBase):
    """触发与状态控制。"""

    __ui_label__ = "触发控制"
    __ui_icon__ = "settings_remote"
    __ui_order__ = 1

    require_at: bool = _ui_field(
        "群聊需 @ 机器人",
        "群聊中必须 @机器人 + 关键词才触发（私聊无需 @，始终有效）",
        default=True,
    )
    enter_keywords: list[str] = _ui_field(
        "进入关键词",
        "消息去掉首尾空白后与关键词完全一致才触发",
        default=["控制", "遥控", "我要控制你了", "td"],
    )
    exit_keywords: list[str] = _ui_field(
        "退出关键词",
        "激活期间命中才生效",
        default=["停止", "停止控制", "结束", "结束控制"],
    )
    state_duration: int = _ui_field(
        "状态持续秒数",
        "超时自动结束",
        default=180, ge=10, le=3600,
    )
    cooldown_seconds: int = _ui_field(
        "冷却秒数",
        "同一会话结束后的冷却秒数",
        default=30, ge=0, le=3600,
    )
    max_concurrent: int = _ui_field(
        "并发上限",
        "同时处于激活状态的最大会话数",
        default=50, ge=1, le=9999,
    )
    exit_pending_ttl: int = _ui_field(
        "退出提示有效期",
        "退出提示词的有效期（秒）：过期后下次回复不再注入退出模板",
        default=300, ge=5, le=3600,
    )
    admin_only_mode: bool = _ui_field(
        "仅管理员可触发",
        "仅管理员可触发进入/退出关键词",
        default=False,
    )
    admin_ids: list[str] = _ui_field(
        "管理员列表",
        "支持纯 ID（\"123456\"）或平台前缀（\"qq:123456\"）",
        default=[],
    )
    react_on_trigger: bool = _ui_field(
        "触发瞬间立刻反应",
        "触发瞬间是否让麦麦立刻做出反应（默认与原版一致：静默激活，靠后续回复体现状态）",
        default=False,
    )
    proactive_threshold: int = _ui_field(
        "主动表达阈值",
        "敏感度自动触发阈值（0=关闭）：会话敏感度达到该值时，麦麦自动主动表达一次",
        default=80, ge=0, le=120,
    )
    auto_speed_per_minute: int = _ui_field(
        "自动模式速度",
        "自动模式每分钟变化的敏感度点数（/强度 自动）",
        default=20, ge=1, le=120,
    )
    climax_threshold: int = _ui_field(
        "越限触发阈值",
        "持续越限触发阈值（0=关闭）：敏感度保持在该值以上达到时长后触发一次招架不住的顶点效果",
        default=100, ge=0, le=120,
    )
    climax_hold_seconds: int = _ui_field(
        "越限持续秒数",
        "需要持续保持在阈值以上的秒数（中途跌破则重新计时）",
        default=20, ge=1, le=600,
    )
    climax_max_per_activation: int = _ui_field(
        "每次激活触发上限",
        "每次激活最多触发顶点效果的次数（每次触发后重新计时，用完为止）",
        default=3, ge=1, le=99,
    )
    stealth_mode: bool = _ui_field(
        "隐蔽模式",
        "状态变更回执（设置强度/自动/全局、冷却、并发上限）不发送真实内容，改为随机发一句无察觉日常语句",
        default=False,
    )
    stealth_phrases: list[str] = _ui_field(
        "隐蔽语句池",
        "隐蔽模式下随机发送的日常语句池（群聊里别人看不出任何含义）",
        default=["好。", "嗯，知道了。", "哦哦，这样。", "行吧。", "……好吧。", "嗯？", "收到收到"],
    )


class PromptSectionConfig(PluginConfigBase):
    """提示词模板。"""

    __ui_label__ = "提示词"
    __ui_icon__ = "edit_note"
    __ui_order__ = 2

    item_name: str = _ui_field(
        "道具名称",
        "模板占位符 {item_name} 会替换为该值，可以自定义为任何你想要的名称",
        default="特殊装置",
    )
    sensitivity: int = _ui_field(
        "全局敏感度",
        "全局敏感度 0-100（100-120 档只能通过 /强度 指令按会话设置）",
        default=50, ge=0, le=100,
    )
    tier_0_20: str = _ui_field(
        "0-20 档文案",
        "敏感度 0-20 档效果说明",
        default="几乎无感：只有极细微的破绽，偶尔一个不易察觉的停顿或走神，对话基本如常",
        json_schema_extra={"rows": 3},
    )
    tier_20_40: str = _ui_field(
        "20-40 档文案",
        "敏感度 20-40 档效果说明",
        default="隐约：偶尔忽然有点慌神，语气微微不稳，能自然掩饰，旁人几乎察觉不到",
        json_schema_extra={"rows": 3},
    )
    tier_40_60: str = _ui_field(
        "40-60 档文案",
        "敏感度 40-60 档效果说明",
        default="轻微：时不时紧张一下、偶尔结巴、容易分神，能维持对话但破绽渐多",
        json_schema_extra={"rows": 3},
    )
    tier_60_80: str = _ui_field(
        "60-80 档文案",
        "敏感度 60-80 档效果说明",
        default="中等偏强：慌乱一阵阵上来、语句断续、明显难以维持正常对话，会下意识掩饰但破绽明显",
        json_schema_extra={"rows": 3},
    )
    tier_80_100: str = _ui_field(
        "80-100 档文案",
        "敏感度 80-100 档效果说明",
        default="强烈：接近招架不住，语句破碎、慌乱和结巴怎么压都压不住，竭力维持镇定但随时露馅",
        json_schema_extra={"rows": 3},
    )
    tier_100_120: str = _ui_field(
        "100-120 档文案",
        "敏感度 100-120 档效果说明（该档只能通过 /强度 指令设置）",
        default="完全招架不住：害羞慌乱彻底占了上风，对话难以连贯，完全无法掩饰",
        json_schema_extra={"rows": 3},
    )
    enter_template: str = _ui_field(
        "进入提示词",
        "激活期间每次回复前注入的提示词（激活后的下一条回复起生效）",
        default=(
            "（你身上的{item_name}已被开启遥控模式，敏感度{sensitivity}/120，持续约{duration}秒。"
            "接下来回复时表现得像真的被某种神奇的装置影响了一样：产生自然的情绪和状态变化——"
            "突如其来的慌乱、害羞、紧张或困惑，语气变得结巴、停顿、词不达意，"
            "努力维持正常对话但总是忍不住分神，强度越高反应越明显；"
            "不要刻意声明设定，不要每句都提，交给表演。）"
        ),
        json_schema_extra={"rows": 6},
    )
    climax_emoji: str = _ui_field(
        "顶点标记 emoji",
        "持续越限触发时要求模型在回复末尾使用的 emoji 标记（留空则不附加）",
        default="😳",
    )
    climax_template: str = _ui_field(
        "顶点提示词",
        "持续越限触发后下一次回复注入一次的顶点效果模板",
        default=(
            "（{item_name}的强度瞬间冲上了顶——你彻底招架不住了：一阵最强烈的害羞慌乱涌上来，"
            "大脑一片空白，这回是真的连一个完整的字都说不出来，彻底乱了阵脚……"
            "此条回复直接描写这次彻底乱了阵脚的瞬间与随后的缓劲，不要克制，不要转移话题。）"
        ),
        json_schema_extra={"rows": 4},
    )
    exit_template: str = _ui_field(
        "退出提示词",
        "退出后下一次回复注入一次的提示词",
        default=(
            "（{item_name}的遥控已结束。接下来自然恢复正常，"
            "可以有一丝如释重负的余韵，但不必再提这个道具。）"
        ),
        json_schema_extra={"rows": 4},
    )


class LinkageSectionConfig(PluginConfigBase):
    """与「麦麦身体」插件的联动（全部默认关：默认行为与未加联动时完全一致）。"""

    __ui_label__ = "身体联动"
    __ui_icon__ = "monitor_heart"
    __ui_order__ = 3

    enabled: bool = _ui_field(
        "启用身体联动",
        "联动总开关（关闭时一个调用都不发，行为与未加联动时完全一致）",
        default=False,
    )
    state_ttl_seconds: int = _ui_field(
        "体征缓存秒数",
        "get_state 体征快照的缓存秒数（下限 10，防止过频 RPC）",
        default=60, ge=10, le=3600,
    )
    call_timeout_seconds: int = _ui_field(
        "调用超时秒数",
        "单次跨插件调用的超时上界（秒）：防止 body 假死阻塞回复",
        default=3, ge=1, le=30,
    )
    body_plugin_id: str = _ui_field(
        "身体插件 ID",
        "留空 = 默认 maibot-body-emulator",
        default="",
    )
    inject_body_text: bool = _ui_field(
        "注入身体文案",
        "遥控激活期间，把身体状态文案追加进注入提示词",
        default=True,
    )
    body_tired_text: str = _ui_field(
        "精力低文案",
        "精力过低（<30）时的身体文案（留空禁用，上限 40 字）",
        default="你此刻精力见底，浑身发软",
        json_schema_extra={"rows": 2},
    )
    body_hungry_text: str = _ui_field(
        "饥饿文案",
        "饥饿（>70）时的身体文案（留空禁用，上限 40 字）",
        default="你的胃在咕咕抗议，饿得发慌",
        json_schema_extra={"rows": 2},
    )
    body_sick_text: str = _ui_field(
        "身体不适文案",
        "健康过低（<40）时的身体文案（留空禁用，上限 40 字）",
        default="你身体不舒服，脸颊发烫",
        json_schema_extra={"rows": 2},
    )
    body_tense_text: str = _ui_field(
        "压力 55 文案",
        "压力 ≥55 时的身体文案（留空禁用，上限 40 字）",
        default="心跳得飞快，一阵阵发热，慌乱有点压不住",
        json_schema_extra={"rows": 2},
    )
    body_breakdown_text: str = _ui_field(
        "压力 70 文案",
        "压力 ≥70 时的身体文案（替代上一档，上限 40 字）",
        default="慌乱一阵阵涌上来，说话都断断续续",
        json_schema_extra={"rows": 2},
    )
    body_blank_text: str = _ui_field(
        "压力 90 文案",
        "压力 ≥90 时的身体文案（替代上一档，上限 40 字）",
        default="大脑一片空白，彻底乱了阵脚，怎么都缓不过来",
        json_schema_extra={"rows": 2},
    )
    body_sleep_text: str = _ui_field(
        "睡眠文案",
        "麦麦正在仿真睡眠时的身体文案（留空禁用，上限 40 字）",
        default="你现在睡意沉沉，意识半浮半沉",
        json_schema_extra={"rows": 2},
    )
    body_excited_text: str = _ui_field(
        "欣快文案",
        "欣快（euphoria）≥60 时的身体文案（body 1.3.0+；留空禁用，上限 40 字）",
        default="心跳还没缓下来，脑子轻飘飘的",
        json_schema_extra={"rows": 2},
    )
    body_irritable_text: str = _ui_field(
        "烦躁文案",
        "烦躁（dysphoria）≥60 时的身体文案（body 1.3.0+；留空禁用，上限 40 字）",
        default="心里莫名发躁，耐心很薄",
        json_schema_extra={"rows": 2},
    )
    sensitivity_boost: bool = _ui_field(
        "敏感度显示加成",
        "身体脆弱时给敏感度表现加成（仅注入档位与 /强度 显示用，机制判定永不加成）",
        default=False,
    )
    boost_energy_below: int = _ui_field(
        "加成触发精力线",
        "精力低于该值触发显示加成",
        default=30, ge=0, le=100,
    )
    boost_delta: int = _ui_field(
        "加成幅度",
        "显示加成点数（0-20，整数防小数档位）",
        default=10, ge=0, le=20,
    )
    write_stress: bool = _ui_field(
        "写回身体压力",
        "激活期间压力向「强度目标」追踪（强度降则自然回落；睡着时不写）",
        default=False,
    )
    stress_target_base: float = _ui_field(
        "压力目标基线",
        "强度→0 时的目标值",
        default=15.0, ge=0.0, le=100.0,
    )
    stress_target_range: float = _ui_field(
        "强度 120 目标增量",
        "目标 = 基线 + 该值 × 强度/120（120 强度 ≈ 90 濒临崩溃）",
        default=75.0, ge=0.0, le=100.0,
    )
    stress_approach_gain: float = _ui_field(
        "压力追踪增益",
        "每次滴灌向目标靠近的比例（单次写入经 body ±20 夹取）",
        default=0.35, ge=0.05, le=1.0,
    )
    stress_max_step: float = _ui_field(
        "单 tick 最大步长",
        "压住早期跳变，让压力逐级攀升（30 秒/tick 时 6 点 ≈ 12 点/分钟，20→90 约 6 分钟；0=不限制）",
        default=6.0, ge=0.0, le=20.0,
    )
    wake_meter_factor: float = _ui_field(
        "觉醒值累积比例",
        "觉醒值随压力步长累积的比例（0.6 → 觉醒值涨得比压力慢，浅睡 60 约需 4-5 分钟）",
        default=0.6, ge=0.0, le=1.0,
    )
    release_on_climax_full: float = _ui_field(
        "顶点额度用尽泄压",
        "顶点额度用尽（count 达上限）后的泄压量（每次激活至多一次；0=不泄）",
        default=-15.0, ge=-20.0, le=20.0,
    )
    release_on_stop: float = _ui_field(
        "停下时泄压",
        "玩具停下（退出关键词）时的泄压量；自然超时结束不泄压，压力顶在原地自然回落",
        default=-15.0, ge=-20.0, le=20.0,
    )
    climax_valence: float = _ui_field(
        "顶点心情脉冲",
        "每次顶点给身体的瞬时心情（body 1.3.0+ valence；第 2/3 次按 0.75 递减；0=关闭）",
        default=12.0, ge=0.0, le=20.0,
    )
    climax_stress_release: float = _ui_field(
        "顶点小泄压",
        "每次顶点的小泄压（压力点，负值=下降）：与持续滴灌形成锯齿；0=不泄",
        default=-4.0, ge=-10.0, le=0.0,
    )
    afterglow_valence: float = _ui_field(
        "余韵心情",
        "到过顶点后正常停下时的余韵心情（body 1.3.0+；无顶点直接停不给；0=关闭）",
        default=5.0, ge=0.0, le=15.0,
    )
    auto_cooldown: bool = _ui_field(
        "无会话主动冷却",
        "压力高于回落底线时，定时器按下方速率写回，让回落肉眼可见（每 30 秒一次轻量写入，无 LLM、无额外查询）",
        default=True,
    )
    cooldown_rate_per_minute: float = _ui_field(
        "冷却速率",
        "主动冷却速率（压力点/分钟）：90→30 约 20 分钟；引擎自身慢衰减仍叠加",
        default=3.0, ge=0.0, le=10.0,
    )
    cooldown_floor: float = _ui_field(
        "冷却下限",
        "降到该值即停，交还给身体自身稳态；引擎自然稳态约 30，建议 25-30",
        default=25.0, ge=0.0, le=100.0,
    )
    sleep_wake_threshold: float = _ui_field(
        "觉醒唤醒阈值",
        "0=关闭睡眠场景；睡着时压力滴灌照常积累并同步攒觉醒值，自然醒来瞬间觉醒值 ≥ 阈值则触发余韵引爆 + 记账注入",
        default=60.0, ge=0.0, le=100.0,
    )
    tick_interval_seconds: int = _ui_field(
        "滴灌周期秒数",
        "后台滴灌周期：激活会话的压力按「强度×时间」累积，不依赖麦麦回话（睡着/夜间也照常累积），并顺带惰性结算超时",
        default=30, ge=10, le=600,
    )
    wake_threshold_deep: float = _ui_field(
        "深睡觉醒阈值",
        "深睡（deep_sleep）的觉醒引爆阈值：深睡要攒满整条觉醒值才醒（难醒）",
        default=100.0, ge=40.0, le=100.0,
    )
    wake_threshold_light: float = _ui_field(
        "浅睡觉醒阈值",
        "入睡期/浅睡（falling_asleep/light_sleep）的觉醒引爆阈值（易醒）",
        default=60.0, ge=20.0, le=100.0,
    )
    mai_life_plugin_id: str = _ui_field(
        "麦生活插件 ID",
        "留空 = 默认 maibot-community.mai-life，读睡眠阶段用",
        default="",
    )
    body_wake_note_text: str = _ui_field(
        "醒来记账文案",
        "觉醒值达到睡眠阈值时注入一次性记账文案（留空禁用）",
        default="（你睡着的时候，好像经历了些什么……记忆模糊，但心跳的余韵还隐约记得。）",
        json_schema_extra={"rows": 3},
    )
    body_wake_note_light_text: str = _ui_field(
        "轻记账文案",
        "觉醒值未达睡眠阈值时的轻记账文案（留空禁用）",
        default="（醒来时有些异样的感觉，像发生了什么，又想不起来。）",
        json_schema_extra={"rows": 2},
    )


class ImmersiveControlConfig(PluginConfigBase):
    """插件完整配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    control: ControlSectionConfig = Field(default_factory=ControlSectionConfig)
    prompt: PromptSectionConfig = Field(default_factory=PromptSectionConfig)
    linkage: LinkageSectionConfig = Field(default_factory=LinkageSectionConfig)


def render_template(template: str, item_name: str, sensitivity: int, duration: int = 0) -> str:
    """把模板占位符替换为实际值（replace 实现，容忍模板里的其他花括号）。"""
    text = template.replace(PLACEHOLDER_ITEM, item_name)
    text = text.replace(PLACEHOLDER_SENSITIVITY, str(sensitivity))
    text = text.replace("{duration}", str(duration))
    return text
