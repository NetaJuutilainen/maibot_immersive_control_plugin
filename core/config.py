"""沉浸式遥控插件：配置模型（MaiBot PluginConfigBase，Pydantic 声明式）。

由 plugin.py 以受控 importlib 方式加载（模块名前缀 maibot_immersive_control_），
本文件不依赖任何 AstrBot API，只依赖 maibot_sdk。
"""

from __future__ import annotations

from maibot_sdk import Field, PluginConfigBase

SUPPORTED_CONFIG_VERSION = "1.0.0"

# 提示词模板中的占位符（用 replace 而非 str.format，模板中出现裸花括号也不会炸）
PLACEHOLDER_ITEM = "{item_name}"
PLACEHOLDER_SENSITIVITY = "{sensitivity}"


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(
        default=SUPPORTED_CONFIG_VERSION,
        description="配置版本（与插件版本同步）",
        json_schema_extra={"hidden": True, "disabled": True},
    )


class ControlSectionConfig(PluginConfigBase):
    """触发与状态控制。"""

    __ui_label__ = "触发控制"
    __ui_icon__ = "settings_remote"
    __ui_order__ = 1

    require_at: bool = Field(
        default=True,
        description="群聊中必须 @机器人 + 关键词才触发（私聊无需 @，始终有效）",
    )
    enter_keywords: list[str] = Field(
        default=["控制", "遥控", "我要控制你了", "td"],
        description="进入关键词（消息去掉首尾空白后与关键词完全一致才触发）",
    )
    exit_keywords: list[str] = Field(
        default=["停止", "停止控制", "结束", "结束控制"],
        description="退出关键词（激活期间命中才生效）",
    )
    state_duration: int = Field(
        default=180, ge=10, le=3600,
        description="状态持续秒数，超时自动结束",
    )
    cooldown_seconds: int = Field(
        default=30, ge=0, le=3600,
        description="同一会话结束后的冷却秒数",
    )
    max_concurrent: int = Field(
        default=50, ge=1, le=9999,
        description="同时处于激活状态的最大会话数",
    )
    exit_pending_ttl: int = Field(
        default=300, ge=5, le=3600,
        description="退出提示词的有效期（秒）：过期后下次回复不再注入退出模板",
    )
    admin_only_mode: bool = Field(
        default=False,
        description="仅管理员可触发进入/退出关键词",
    )
    admin_ids: list[str] = Field(
        default=[],
        description="管理员列表，支持纯 ID（\"123456\"）或平台前缀（\"qq:123456\"）",
    )
    react_on_trigger: bool = Field(
        default=False,
        description="触发瞬间是否让麦麦立刻做出反应（默认与原版一致：静默激活，靠后续回复体现状态）",
    )
    proactive_threshold: int = Field(
        default=80, ge=0, le=120,
        description="敏感度自动触发阈值（0=关闭）：会话敏感度达到该值时，麦麦自动主动表达一次",
    )
    auto_speed_per_minute: int = Field(
        default=20, ge=1, le=120,
        description="自动模式每分钟变化的敏感度点数（/强度 自动）",
    )
    climax_threshold: int = Field(
        default=100, ge=0, le=120,
        description="持续越限触发阈值（0=关闭）：敏感度保持在该值以上达到时长后触发一次招架不住的顶点效果",
    )
    climax_hold_seconds: int = Field(
        default=20, ge=1, le=600,
        description="需要持续保持在阈值以上的秒数（中途跌破则重新计时）",
    )
    climax_max_per_activation: int = Field(
        default=3, ge=1, le=99,
        description="每次激活最多触发顶点效果的次数（每次触发后重新计时，用完为止）",
    )
    stealth_mode: bool = Field(
        default=False,
        description="隐蔽模式：状态变更回执（设置强度/自动/全局、冷却、并发上限）"
                    "不发送真实内容，改为随机发一句无察觉日常语句",
    )
    stealth_phrases: list[str] = Field(
        default=["好。", "嗯，知道了。", "哦哦，这样。", "行吧。", "……好吧。", "嗯？", "收到收到"],
        description="隐蔽模式下随机发送的日常语句池（群聊里别人看不出任何含义）",
    )


class PromptSectionConfig(PluginConfigBase):
    """提示词模板。"""

    __ui_label__ = "提示词"
    __ui_icon__ = "edit_note"
    __ui_order__ = 2

    item_name: str = Field(
        default="特殊装置",
        description="道具名称（模板占位符 {item_name} 会替换为该值，可以自定义为任何你想要的名称）",
    )
    sensitivity: int = Field(
        default=50, ge=0, le=100,
        description="全局敏感度 0-100（100-120 档只能通过 /强度 指令按会话设置）",
    )
    tier_0_20: str = Field(
        default="几乎无感：只有极细微的破绽，偶尔一个不易察觉的停顿或走神，对话基本如常",
        description="敏感度 0-20 档效果说明",
        json_schema_extra={"rows": 3},
    )
    tier_20_40: str = Field(
        default="隐约：偶尔忽然有点慌神，语气微微不稳，能自然掩饰，旁人几乎察觉不到",
        description="敏感度 20-40 档效果说明",
        json_schema_extra={"rows": 3},
    )
    tier_40_60: str = Field(
        default="轻微：时不时紧张一下、偶尔结巴、容易分神，能维持对话但破绽渐多",
        description="敏感度 40-60 档效果说明",
        json_schema_extra={"rows": 3},
    )
    tier_60_80: str = Field(
        default="中等偏强：慌乱一阵阵上来、语句断续、明显难以维持正常对话，会下意识掩饰但破绽明显",
        description="敏感度 60-80 档效果说明",
        json_schema_extra={"rows": 3},
    )
    tier_80_100: str = Field(
        default="强烈：接近招架不住，语句破碎、慌乱和结巴怎么压都压不住，竭力维持镇定但随时露馅",
        description="敏感度 80-100 档效果说明",
        json_schema_extra={"rows": 3},
    )
    tier_100_120: str = Field(
        default="完全招架不住：害羞慌乱彻底占了上风，对话难以连贯，完全无法掩饰",
        description="敏感度 100-120 档效果说明（该档只能通过 /强度 指令设置）",
        json_schema_extra={"rows": 3},
    )
    enter_template: str = Field(
        default=(
            "（你身上的{item_name}已被开启遥控模式，敏感度{sensitivity}/120，持续约{duration}秒。"
            "接下来回复时表现得像真的被某种神奇的装置影响了一样：产生自然的情绪和状态变化——"
            "突如其来的慌乱、害羞、紧张或困惑，语气变得结巴、停顿、词不达意，"
            "努力维持正常对话但总是忍不住分神，强度越高反应越明显；"
            "不要刻意声明设定，不要每句都提，交给表演。）"
        ),
        description="激活期间每次回复前注入的提示词（激活后的下一条回复起生效）",
        json_schema_extra={"rows": 6},
    )
    climax_emoji: str = Field(
        default="😳",
        description="持续越限触发时要求模型在回复末尾使用的 emoji 标记（留空则不附加）",
    )
    climax_template: str = Field(
        default=(
            "（{item_name}的强度瞬间冲上了顶——你彻底招架不住了：一阵最强烈的害羞慌乱涌上来，"
            "大脑一片空白，这回是真的连一个完整的字都说不出来，彻底乱了阵脚……"
            "此条回复直接描写这次彻底乱了阵脚的瞬间与随后的缓劲，不要克制，不要转移话题。）"
        ),
        description="持续越限触发后下一次回复注入一次的顶点效果模板",
        json_schema_extra={"rows": 4},
    )
    exit_template: str = Field(
        default=(
            "（{item_name}的遥控已结束。接下来自然恢复正常，"
            "可以有一丝如释重负的余韵，但不必再提这个道具。）"
        ),
        description="退出后下一次回复注入一次的提示词",
        json_schema_extra={"rows": 4},
    )


class ImmersiveControlConfig(PluginConfigBase):
    """插件完整配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    control: ControlSectionConfig = Field(default_factory=ControlSectionConfig)
    prompt: PromptSectionConfig = Field(default_factory=PromptSectionConfig)


def render_template(template: str, item_name: str, sensitivity: int, duration: int = 0) -> str:
    """把模板占位符替换为实际值（replace 实现，容忍模板里的其他花括号）。"""
    text = template.replace(PLACEHOLDER_ITEM, item_name)
    text = text.replace(PLACEHOLDER_SENSITIVITY, str(sensitivity))
    text = text.replace("{duration}", str(duration))
    return text
