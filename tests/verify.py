"""离线验证脚本：不启动 MaiBot，对本插件做结构自检 + 状态机 + 钩子行为验证。

检查项：
 1. stub SDK 下 plugin.py 可导入，组件声明完整（3 命令 + 2 钩子）且名称唯一；
 2. manifest 结构校验（必填字段、ID/版本正则、能力名格式、SDK/Host 区间）；
 3. 配置模型默认值（含 1.2.3 硬性要求的 plugin.config_version）；
 4. 命令正则按 MaiBot re.search 语义的行为（命中/不命中样例）；
 5. 状态机：激活/冷却/并发上限/自动到期/待退出注入一次/撤销退出/持久化往返；
 6. 钩子端到端：关键词驱动（含静默 abort、冷却提示、非关键词旁路、无权限忽略）
    与 replyer 模板注入（占位符替换、extra_prompt 拼接、退出仅一次）。

用法：python tests/verify.py
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import tempfile
import time
import traceback
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR / "tests"))
sys.path.insert(0, str(PLUGIN_DIR))

import stub_maibot_sdk as stub  # noqa: E402

# 注入 stub SDK（必须在导入 plugin 之前）
sys.modules["maibot_sdk"] = stub
types_mod = type(sys)("maibot_sdk.types")
for attr in ("CONFIG_RELOAD_SCOPE_SELF", "HookMode", "HookOrder", "ErrorPolicy"):
    setattr(types_mod, attr, getattr(stub, attr))
types_mod.__path__ = []
sys.modules["maibot_sdk.types"] = types_mod

import plugin  # noqa: E402  真实插件入口（以文件 spec 同样方式可加载）

PASS, FAIL = 0, 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    mark = "PASS" if ok else "FAIL"
    if ok:
        PASS += 1
    else:
        FAIL += 1
    print(f"[{mark}] {name}" + (f"  -- {detail}" if detail and not ok else ""))


class FakePaths:
    def __init__(self, root: Path):
        self.data_dir = root


class FakeLogger:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def debug(self, *a, **k):
        pass


class FakeSend:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    async def text(self, text, stream_id):
        self.sent.append((text, stream_id))
        return True


class _ProactiveNS:
    def __init__(self, outer):
        self._outer = outer

    async def trigger(self, stream_id, intent, **kwargs):
        self._outer.proactive_calls.append((stream_id, intent))
        return True


class _ContextNS:
    def __init__(self, outer):
        self._outer = outer

    async def append(self, stream_id, segments, **kwargs):
        self._outer.append_calls.append((stream_id, segments))
        return True


class FakeMaisaka:
    """模拟 ctx.maisaka：proactive.trigger / context.append 两个能力调用记录到 *_calls。"""

    def __init__(self):
        self.proactive_calls: list[tuple[str, str]] = []
        self.append_calls: list[tuple[str, list]] = []
        self.proactive = _ProactiveNS(self)
        self.context = _ContextNS(self)


class FakeCtx:
    def __init__(self, root: Path):
        self.paths = FakePaths(root)
        self.logger = FakeLogger()
        self.send = FakeSend()
        self.maisaka = FakeMaisaka()


def make_plugin(tmp: Path, **control_overrides):
    p = plugin.create_plugin()
    p.ctx = FakeCtx(tmp)
    for key, value in control_overrides.items():
        setattr(p.config.control, key, value)
    return p


def msg(text, session_id="s1", user_id="10001", notify=False, is_at=False,
        group_id="", raw_message=None):
    m = {
        "processed_plain_text": text,
        "session_id": session_id,
        "is_notify": notify,
        "is_at": is_at,
        "message_info": {"user_info": {"user_id": user_id}},
    }
    if group_id:
        m["message_info"]["group_info"] = {"group_id": group_id}
    if raw_message is not None:
        m["raw_message"] = raw_message
    return m


async def main() -> None:
    print("== 1. 组件声明 ==")
    p = plugin.create_plugin()
    comps = p.get_components()
    commands = [c for c in comps if c["kind"] == "command"]
    hooks = [c for c in comps if c["kind"] == "hook_handler"]
    check("组件总数 = 6", len(comps) == 6, str(comps))
    check("命令 = 4", len(commands) == 4, str([c["name"] for c in commands]))
    check("钩子 = 2", len(hooks) == 2, str([c["name"] for c in hooks]))
    names = [c["name"] for c in comps]
    check("组件名唯一", len(names) == len(set(names)), str(names))
    hook_names = {c["hook"] for c in hooks}
    check("钩子挂载点正确",
          hook_names == {"chat.receive.before_process", "maisaka.replyer.before_request"},
          str(hook_names))

    print("== 2. manifest 校验 ==")
    manifest = json.loads((PLUGIN_DIR / "_manifest.json").read_text(encoding="utf-8"))
    required = ["manifest_version", "id", "version", "name", "description", "author",
                "license", "urls", "host_application", "sdk", "capabilities", "i18n"]
    check("manifest 必填字段齐全", all(k in manifest for k in required),
          str([k for k in required if k not in manifest]))
    check("manifest 无多余字段",
          set(manifest) <= set(required + ["dependencies", "plugin_type", "llm_providers",
                                           "display", "changelog"]),
          str(set(manifest) - set(required)))
    check("id 含分隔符且合法",
          bool(re.fullmatch(r"[A-Za-z0-9_]+(?:[.-][a-zA-Z0-9_]+)+", manifest["id"])),
          manifest["id"])
    check("version 三段式", bool(re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"])))
    caps = manifest["capabilities"]
    check("能力名均为 <代理>.<方法> 格式",
          all(re.fullmatch(r"[a-z_]+(?:\.[a-z_]+)+", c) for c in caps), str(caps))
    check("send.text 已声明", "send.text" in caps, str(caps))
    check("maisaka 能力已声明",
          "maisaka.proactive.trigger" in caps and "maisaka.context.append" in caps, str(caps))
    check("host 区间覆盖 1.x",
          manifest["host_application"]["min_version"] == "1.0.0"
          and manifest["host_application"]["max_version"] == "1.99.99")

    print("== 3. 配置模型 ==")
    cfg = plugin.ImmersiveControlConfig()
    check("plugin.config_version 存在且三段式",
          bool(re.fullmatch(r"\d+\.\d+\.\d+", cfg.plugin.config_version)),
          cfg.plugin.config_version)
    check("config_version 与 manifest 版本同步",
          cfg.plugin.config_version == manifest["version"],
          f"{cfg.plugin.config_version} vs {manifest['version']}")
    check("进入关键词默认含 控制/td", "控制" in cfg.control.enter_keywords
          and "td" in cfg.control.enter_keywords)
    check("持续/冷却默认 180/30",
          cfg.control.state_duration == 180 and cfg.control.cooldown_seconds == 30)
    check("敏感度默认 50 且模板含占位符",
          cfg.prompt.sensitivity == 50
          and "{item_name}" in cfg.prompt.enter_template
          and "{sensitivity}" in cfg.prompt.enter_template)
    rendered = plugin.render_template(cfg.prompt.enter_template, "测试道具", 77, 180)
    check("占位符替换正确", "测试道具" in rendered and "77" in rendered
          and "{item_name}" not in rendered and "{" not in rendered, rendered)

    print("== 4. 命令正则（re.search 语义） ==")
    patterns = {c["name"]: c["pattern"] for c in commands}
    check("/imm_status 命中", re.search(patterns["imm_status"], "/imm_status"))
    check("imm_status（无斜杠）命中", re.search(patterns["imm_status"], "imm_status"))
    check("回复引用后缀仍命中", re.search(patterns["imm_status"], "别人说的话 imm_status"))
    check("imm_statusX 不命中", not re.search(patterns["imm_status"], "imm_statusX"))
    check("imm_reload 别名命中 imm_config",
          re.search(patterns["imm_config"], "/imm_reload"))
    check("普通聊天不命中", not re.search(patterns["imm_status"], "今天天气不错"))

    print("== 5. 状态机 ==")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        store = plugin.SessionStore()
        changes = {"n": 0}
        store.on_changed = lambda: changes.__setitem__("n", changes["n"] + 1)

        class Cfg:
            state_duration = 180
            cooldown_seconds = 30
            max_concurrent = 2
            exit_pending_ttl = 300

        c = Cfg()
        now = 1000.0
        r, rem = store.try_activate("a", c, now=now)
        check("激活成功", (r, rem) == (plugin.ACTIVATED, 0), f"{r},{rem}")
        r, rem = store.try_activate("b", c, now=now)
        check("第二会话激活", r == plugin.ACTIVATED)
        r, rem = store.try_activate("d", c, now=now)
        check("并发上限生效", r == plugin.LIMIT and rem == 0, f"{r},{rem}")
        r, rem = store.try_activate("a", c, now=now + 10)
        check("激活中重复进入 → 刷新时长", r == plugin.ACTIVATED and rem == 0)
        # 到期结算
        state = store.prompt_state("a", c, now=now + 10 + 180)
        check("超时后 prompt_state = none", state == "none", state)
        r, rem = store.try_activate("a", c, now=now + 10 + 185)
        check("到期后进入冷却", r == plugin.COOLDOWN and 20 <= rem <= 30, f"{r},{rem}")
        r, _ = store.try_activate("a", c, now=now + 10 + 185 + 31)
        check("冷却过后可再激活", r == plugin.ACTIVATED)
        # 退出流程
        store.try_exit("a", now=now + 2000)
        state = store.prompt_state("a", c, now=now + 2001)
        check("退出后下次回复注入 exit 且仅一次", state == "exit", state)
        state = store.prompt_state("a", c, now=now + 2002)
        check("exit 注入后回到 none", state == "none")
        r, rem = store.try_activate("a", c, now=now + 2003)
        check("退出后也进冷却", r == plugin.COOLDOWN and rem > 0)
        # 待退出期间再次进入 → 撤销
        store.try_activate("b", c, now=now + 3000)
        store.try_exit("b", now=now + 3001)
        r, _ = store.try_activate("b", c, now=now + 3002)
        state = store.prompt_state("b", c, now=now + 3003)
        check("待退出期间再进入 → 撤销退出回到 enter", r == plugin.ACTIVATED and state == "enter",
              f"{r},{state}")
        # 待退出过期 → 静默结束
        store.try_exit("b", now=now + 4000)
        state = store.prompt_state("b", c, now=now + 4000 + 301)
        check("待退出过期静默结束", state == "none", state)
        # 持久化往返
        store2 = plugin.SessionStore()
        store2.load_dict(json.loads(json.dumps(store.to_dict())))
        check("持久化往返一致", store2.to_dict() == store.to_dict())
        # save/load 文件
        f = tmp / "sub" / "state.json"
        store.save(f)
        store3 = plugin.SessionStore()
        store3.load(f)
        check("文件保存/加载一致", store3.to_dict() == store.to_dict())
        store3.load(tmp / "not-exist.json")
        check("缺失文件不影响现有状态", store3.to_dict() == store.to_dict())
        check("on_changed 变更回调已触发", changes["n"] > 10, str(changes["n"]))

    print("== 6. 钩子端到端 ==")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        p = make_plugin(tmp)  # 默认 react_on_trigger=False（原版静默语义）
        await p.on_load()

        # 非关键词旁路
        r = await p.hook_keyword_driver(message=msg("今天天气不错"))
        check("非关键词旁路返回 None", r is None, str(r))
        # 进入关键词 → 静默 abort
        r = await p.hook_keyword_driver(message=msg("控制", session_id="s1"))
        check("进入关键词 → abort", isinstance(r, dict) and r.get("action") == "abort", str(r))
        check("激活瞬间未发消息（静默）", p.ctx.send.sent == [], str(p.ctx.send.sent))
        # 重复进入不再提示
        r = await p.hook_keyword_driver(message=msg("td", session_id="s1"))
        check("激活中再触发仍 abort 且静默", isinstance(r, dict) and r.get("action") == "abort")
        # 冷却提示
        p._store.clear()
        p._store.get("s2").cooldown_end = time.time() + 20
        r = await p.hook_keyword_driver(message=msg("控制", session_id="s2"))
        check("冷却中 → abort + 提示消息",
              isinstance(r, dict) and r.get("action") == "abort"
              and len(p.ctx.send.sent) == 1 and "休息" in p.ctx.send.sent[0][0],
              str(p.ctx.send.sent))
        # 退出
        r = await p.hook_keyword_driver(message=msg("控制", session_id="s3"))
        check("新会话激活（为退出用例准备）",
              isinstance(r, dict) and r.get("action") == "abort")
        r = await p.hook_keyword_driver(message=msg("停止控制", session_id="s3"))
        check("退出关键词 → abort（静默结束）",
              isinstance(r, dict) and r.get("action") == "abort")
        r = await p.hook_keyword_driver(message=msg("结束", session_id="s4"))
        check("未激活时退出词放行", r is None, str(r))
        # 通知消息旁路
        p._store.clear()
        r = await p.hook_keyword_driver(message=msg("控制", notify=True))
        check("通知消息旁路", r is None, str(r))

        # replyer 注入
        r = await p.hook_prompt_injector(session_id="sx", extra_prompt="")
        check("无状态会话不注入", r is None, str(r))
        await p.hook_keyword_driver(message=msg("控制", session_id="s5"))
        r = await p.hook_prompt_injector(session_id="s5", extra_prompt="原有提示")
        check("enter 注入 continue + modified_kwargs",
              isinstance(r, dict) and r["action"] == "continue"
              and "modified_kwargs" in r, str(r))
        injected = r["modified_kwargs"]["extra_prompt"]
        check("extra_prompt 拼接保留原提示", injected.startswith("原有提示"), injected[:50])
        check("进入模板占位符已渲染",
              "特殊装置" in injected and "50" in injected and "{" not in injected,
              injected)
        # 退出注入一次
        await p.hook_keyword_driver(message=msg("结束控制", session_id="s5"))
        r1 = await p.hook_prompt_injector(session_id="s5", extra_prompt="")
        r2 = await p.hook_prompt_injector(session_id="s5", extra_prompt="")
        check("退出模板注入一次",
              r1 is not None and "遥控已结束" in r1["modified_kwargs"]["extra_prompt"]
              and r2 is None, f"{bool(r1)},{r2}")

        # react_on_trigger=True：不拦截，交给麦麦立刻回复
        p2 = make_plugin(tmp, react_on_trigger=True)
        p2.ctx = FakeCtx(tmp)
        r = await p2.hook_keyword_driver(message=msg("控制", session_id="s9"))
        check("react_on_trigger=True 时放行不拦截", r is None, str(r))

        # admin_only_mode
        p3 = make_plugin(tmp, admin_only_mode=True, admin_ids=["88888", "qq:99999"])
        p3.ctx = FakeCtx(tmp)
        r = await p3.hook_keyword_driver(message=msg("控制", session_id="s6", user_id="10001"))
        check("非管理员触发被静默忽略", r is None, str(r))
        r = await p3.hook_keyword_driver(message=msg("控制", session_id="s6", user_id="88888"))
        check("纯 ID 管理员可触发", isinstance(r, dict) and r.get("action") == "abort", str(r))
        p3._store.clear()
        r = await p3.hook_keyword_driver(message=msg("控制", session_id="s7", user_id="99999"))
        check("平台前缀管理员可触发", isinstance(r, dict) and r.get("action") == "abort", str(r))

        # 管理命令
        p4 = make_plugin(tmp, admin_ids=["88888"])
        p4.ctx = FakeCtx(tmp)
        ok, resp, intercept = await p4.cmd_status(
            stream_id="sa", user_id="88888", matched_groups={})
        check("imm_status 管理员可用", ok and intercept and "无记录" in p4.ctx.send.sent[0][0],
              str(p4.ctx.send.sent))
        await p4.hook_keyword_driver(message=msg("控制", session_id="sa"))
        p4.ctx.send.sent.clear()
        await p4.cmd_status(stream_id="sa", user_id="88888", matched_groups={})
        check("imm_status 显示激活中", "激活中" in p4.ctx.send.sent[0][0], str(p4.ctx.send.sent))
        p4.ctx.send.sent.clear()
        ok, resp, intercept = await p4.cmd_status(
            stream_id="sa", user_id="10001", matched_groups={})
        check("imm_status 非管理员拒绝", not ok and "权限不足" in p4.ctx.send.sent[0][0])
        await p4.cmd_clear(stream_id="sa", user_id="88888", matched_groups={})
        check("imm_clear 清空并汇报", "1 个会话" in p4.ctx.send.sent[-1][0],
              str(p4.ctx.send.sent))
        await p4.cmd_config(stream_id="sa", user_id="88888", matched_groups={})
        check("imm_config 展示关键词与道具",
              "控制" in p4.ctx.send.sent[-1][0] and "特殊装置" in p4.ctx.send.sent[-1][0])

        # @ 门槛（require_at 默认开）
        p_at = make_plugin(tmp)
        p_at.ctx = FakeCtx(tmp)
        # 群聊未 @：忽略
        r = await p_at.hook_keyword_driver(message=msg("控制", session_id="g1", group_id="111"))
        check("群聊未 @bot 不触发", r is None, str(r))
        # 群聊 @ 了（is_at=True，文本带 @麦麦 前缀）：触发
        r = await p_at.hook_keyword_driver(
            message=msg("@麦麦 控制", session_id="g1", group_id="111", is_at=True))
        check("群聊 @bot + 关键词触发（@前缀已剥）",
              isinstance(r, dict) and r.get("action") == "abort", str(r))
        check("@ 前缀剥离后关键词命中（is_at 为 False 时兜底扫 at 段）",
              plugin._has_at({"is_at": False,
                              "raw_message": [{"type": "at", "data": {"target_user_id": "1"}}]}))
        # 私聊不需要 @
        r = await p_at.hook_keyword_driver(message=msg("控制", session_id="g2"))
        check("私聊无需 @ 即可触发", isinstance(r, dict) and r.get("action") == "abort", str(r))
        # require_at=False：群聊裸关键词也触发
        p_noat = make_plugin(tmp, require_at=False)
        p_noat.ctx = FakeCtx(tmp)
        r = await p_noat.hook_keyword_driver(
            message=msg("控制", session_id="g3", group_id="222"))
        check("require_at=False 恢复裸关键词触发",
              isinstance(r, dict) and r.get("action") == "abort", str(r))
        # 剥 @ 前缀的归一化单测
        check("_norm_text_strip_at 剥 @昵称",
              plugin._norm_text_strip_at("@麦麦 控制") == "控制"
              and plugin._norm_text_strip_at("控制") == "控制"
              and plugin._norm_text_strip_at("@麦麦控制") == "",
              plugin._norm_text_strip_at("@麦麦 控制"))

    print("== 4b. 敏感度指令 / 六档 / 自动模式 / 阈值触发 ==")
    patterns = {c["name"]: c["pattern"] for c in commands}
    m = re.search(patterns["imm_intensity"], "/强度 80")
    check("/强度 80 命中且捕获 80", m and m.group("value") == "80")
    m = re.search(patterns["imm_intensity"], "sensitivity 65")
    check("sensitivity 65 命中", m and m.group("value") == "65")
    check("/强度（查询）命中且无值",
          (m2 := re.search(patterns["imm_intensity"], "/强度")) and m2.group("value") is None)
    check("/强度 max 命中",
          (m2 := re.search(patterns["imm_intensity"], "/强度 max"))
          and str(m2.group("value")).lower() == "max")
    check("/强度 MAX 命中",
          (m2 := re.search(patterns["imm_intensity"], "/强度 MAX"))
          and str(m2.group("value")).lower() == "max")
    check("/强度 自动 命中",
          (m2 := re.search(patterns["imm_intensity"], "/强度 自动"))
          and m2.group("value") == "自动")
    check("/强度 全局 命中",
          (m2 := re.search(patterns["imm_intensity"], "/强度 全局"))
          and m2.group("value") == "全局")
    check("/强度 150 捕获 150（由代码拒绝）",
          (m3 := re.search(patterns["imm_intensity"], "/强度 150"))
          and m3.group("value") == "150")
    check("普通聊天不命中", not re.search(patterns["imm_intensity"], "这个强度好大"))
    p0 = plugin.create_plugin()
    check("六档文案各不相同且非空",
          len({p0._tier_text(v) for v in (10, 30, 50, 70, 90, 110)}) == 6)
    # 自动曲线（速度 20 点/分钟）
    check("自动曲线：起点 30", plugin.auto_sensitivity_value(0, 20) == 30)
    check("自动曲线：60 秒后 50", plugin.auto_sensitivity_value(60, 20) == 50)
    check("自动曲线：210 秒首达 100", plugin.auto_sensitivity_value(210, 20) == 100)
    check("自动曲线：360 秒回落 50", plugin.auto_sensitivity_value(360, 20) == 50)
    check("自动曲线：510 秒再回 100", plugin.auto_sensitivity_value(510, 20) == 100)
    check("自动曲线：始终在 30~100",
          all(30 <= plugin.auto_sensitivity_value(t, 20) <= 100 for t in range(0, 3000, 7)))
    check("自动曲线：始终为整数",
          all(isinstance(plugin.auto_sensitivity_value(t, 13), int) for t in range(0, 2000, 11)))

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        # 设置覆盖 + 注入使用覆盖值 + 阈值触发一次
        p_i = make_plugin(tmp, admin_ids=["88888"], proactive_threshold=80)
        p_i.ctx = FakeCtx(tmp)
        ok, _, _ = await p_i.cmd_intensity(stream_id="si", user_id="88888",
                                           matched_groups={"value": "90"})
        check("/强度 90 设置成功", ok and "90/120" in p_i.ctx.send.sent[-1][0],
              str(p_i.ctx.send.sent[-1]))
        check("覆盖设置后 _effective_sensitivity 生效",
              p_i._effective_sensitivity("si") == 90)
        await p_i.hook_keyword_driver(message=msg("控制", session_id="si"))
        r = await p_i.hook_prompt_injector(session_id="si", extra_prompt="")
        injected = r["modified_kwargs"]["extra_prompt"]
        check("注入使用覆盖敏感度 90 且带档位文案",
              "90/120" in injected and "强度参考" in injected and "接近招架不住" in injected,
              injected[:120])
        check("敏感度越过阈值 → 主动触发一次",
              len(p_i.ctx.maisaka.proactive_calls) == 1
              and p_i.ctx.maisaka.proactive_calls[0][0] == "si",
              str(p_i.ctx.maisaka.proactive_calls))
        await p_i.hook_prompt_injector(session_id="si", extra_prompt="")
        check("同一次激活内不重复触发", len(p_i.ctx.maisaka.proactive_calls) == 1)
        # 重新激活 → 允许再次触发
        await p_i.hook_keyword_driver(message=msg("控制", session_id="si"))
        await p_i._maybe_proactive("si")
        check("重新激活后可再次触发", len(p_i.ctx.maisaka.proactive_calls) == 2)
        # 低于阈值不触发
        await p_i.cmd_intensity(stream_id="si", user_id="88888",
                                matched_groups={"value": "50"})
        n = len(p_i.ctx.maisaka.proactive_calls)
        await p_i._maybe_proactive("si")
        check("低于阈值不触发", len(p_i.ctx.maisaka.proactive_calls) == n)
        # /强度 max → 120
        await p_i.cmd_intensity(stream_id="si", user_id="88888",
                                matched_groups={"value": "max"})
        check("/强度 max = 120", p_i._effective_sensitivity("si") == 120
              and "120/120" in p_i.ctx.send.sent[-1][0], str(p_i.ctx.send.sent[-1]))
        # 越界直接拒绝且不修改
        before = p_i._effective_sensitivity("si")
        await p_i.cmd_intensity(stream_id="si", user_id="88888",
                                matched_groups={"value": "150"})
        check("/强度 150 被忽略",
              "超出范围" in p_i.ctx.send.sent[-1][0]
              and p_i._effective_sensitivity("si") == before)
        # /强度 0 → 有效档位（几乎无感），不再是"清除覆盖"
        await p_i.cmd_intensity(stream_id="si", user_id="88888",
                                matched_groups={"value": "0"})
        check("/强度 0 设置为 0 档", p_i._effective_sensitivity("si") == 0
              and "几乎无感" in p_i.ctx.send.sent[-1][0], str(p_i.ctx.send.sent[-1]))
        # /强度 全局 → 清除覆盖
        await p_i.cmd_intensity(stream_id="si", user_id="88888",
                                matched_groups={"value": "全局"})
        check("/强度 全局 恢复跟随全局", p_i._effective_sensitivity("si") == 50
              and "恢复跟随全局" in p_i.ctx.send.sent[-1][0], str(p_i.ctx.send.sent[-1]))
        # 查询形态
        await p_i.cmd_intensity(stream_id="si", user_id="88888", matched_groups={})
        check("/强度（无值）显示模式与档位",
              "模式：全局配置" in p_i.ctx.send.sent[-1][0]
              and "档位：" in p_i.ctx.send.sent[-1][0], str(p_i.ctx.send.sent[-1]))
        # 非管理员拒绝
        await p_i.cmd_intensity(stream_id="si", user_id="10001",
                                matched_groups={"value": "99"})
        check("/强度 非管理员拒绝", "权限不足" in p_i.ctx.send.sent[-1][0])

        # 自动模式
        p_a = make_plugin(tmp, admin_ids=["88888"], auto_speed_per_minute=600)  # 10 点/秒加速验证
        p_a.ctx = FakeCtx(tmp)
        await p_a.cmd_intensity(stream_id="su", user_id="88888",
                                matched_groups={"value": "自动"})
        check("/强度 自动 开启", "自动模式" in p_a.ctx.send.sent[-1][0], str(p_a.ctx.send.sent[-1]))
        s_a = p_a._store.sessions.get("su")
        check("自动模式会话字段落位", s_a is not None and s_a.auto_mode and s_a.auto_ts > 0)
        v0 = p_a._effective_sensitivity("su")
        check("自动模式起点约 30", 30 <= v0 <= 35, str(v0))
        await asyncio.sleep(2.2)
        v1 = p_a._effective_sensitivity("su")
        check("自动模式随时间爬升", v1 - v0 >= 15 and v1 <= 100, f"{v0}->{v1}")
        await p_a.cmd_intensity(stream_id="su", user_id="88888",
                                matched_groups={"value": "40"})
        check("手动设置后退出自动模式",
              p_a._store.sessions["su"].auto_mode is False
              and p_a._effective_sensitivity("su") == 40)
        # 自动模式爬升越过阈值 → 注入钩子中触发
        p_b = make_plugin(tmp, admin_ids=["88888"], proactive_threshold=60,
                          auto_speed_per_minute=600)  # 10 点/秒
        p_b.ctx = FakeCtx(tmp)
        await p_b.cmd_intensity(stream_id="sv", user_id="88888",
                                matched_groups={"value": "自动"})
        await p_b.hook_keyword_driver(message=msg("控制", session_id="sv"))
        await asyncio.sleep(3.5)  # 30 + 10*3.5 = 65，仍在爬升段且已越过 60
        await p_b.hook_prompt_injector(session_id="sv", extra_prompt="")
        check("自动模式越过阈值触发一次",
              len(p_b.ctx.maisaka.proactive_calls) == 1, str(p_b.ctx.maisaka.proactive_calls))
        # threshold=0 关闭
        p_t = make_plugin(tmp, admin_ids=["88888"], proactive_threshold=0)
        p_t.ctx = FakeCtx(tmp)
        await p_t.cmd_intensity(stream_id="st", user_id="88888",
                                matched_groups={"value": "100"})
        check("阈值=0 时即使 100 也不触发", len(p_t.ctx.maisaka.proactive_calls) == 0)
        # 未经覆盖的会话：全局 50 不触发（阈值 80）
        await p_t.hook_keyword_driver(message=msg("控制", session_id="st2"))
        check("全局敏感度未达阈值不触发", len(p_t.ctx.maisaka.proactive_calls) == 0)

        # /强度 查询显示持续越限状态
        p_q = make_plugin(tmp, admin_ids=["88888"], climax_threshold=100,
                          climax_hold_seconds=20)
        p_q.ctx = FakeCtx(tmp)
        await p_q.cmd_intensity(stream_id="sqq", user_id="88888", matched_groups={})
        check("查询：未激活时显示未激活与阈值",
              "未激活" in p_q.ctx.send.sent[-1][0] and "100/120" in p_q.ctx.send.sent[-1][0],
              str(p_q.ctx.send.sent[-1]))
        await p_q.cmd_intensity(stream_id="sqq", user_id="88888",
                                matched_groups={"value": "110"})
        p_q.ctx.send.sent.clear()
        await p_q.hook_keyword_driver(message=msg("控制", session_id="sqq"))
        await p_q.hook_prompt_injector(session_id="sqq", extra_prompt="")  # 开始计时
        await p_q.cmd_intensity(stream_id="sqq", user_id="88888", matched_groups={})
        check("查询：计时中显示剩余秒数",
              "计时中" in p_q.ctx.send.sent[-1][0] and "0/3" in p_q.ctx.send.sent[-1][0],
              str(p_q.ctx.send.sent[-1]))
        p_q._store.sessions["sqq"].climax_count = 3
        await p_q.cmd_intensity(stream_id="sqq", user_id="88888", matched_groups={})
        check("查询：次数用尽可见 3/3", "3/3" in p_q.ctx.send.sent[-1][0])

        # 隐蔽模式：状态变更回执换成无察觉语句，查询不受影响
        p_h = make_plugin(tmp, admin_ids=["88888"], stealth_mode=True)
        p_h.ctx = FakeCtx(tmp)
        pool = p_h.config.control.stealth_phrases
        await p_h.cmd_intensity(stream_id="sh", user_id="88888",
                                matched_groups={"value": "90"})
        check("隐蔽模式：设置强度只发无察觉语句",
              p_h.ctx.send.sent[-1][0] in pool, str(p_h.ctx.send.sent[-1]))
        await p_h.cmd_intensity(stream_id="sh", user_id="88888",
                                matched_groups={"value": "自动"})
        check("隐蔽模式：开自动也只发无察觉语句",
              p_h.ctx.send.sent[-1][0] in pool, str(p_h.ctx.send.sent[-1]))
        s_h = p_h._store.sessions.get("sh")
        check("隐蔽模式：设置本身照常生效", s_h is not None and s_h.auto_mode)
        await p_h.cmd_intensity(stream_id="sh", user_id="88888", matched_groups={})
        check("隐蔽模式：主动查询仍返回真实信息",
              "当前敏感度" in p_h.ctx.send.sent[-1][0], str(p_h.ctx.send.sent[-1]))
        # 冷却提示同样隐蔽
        p_h.ctx.send.sent.clear()
        p_h._store.clear()
        p_h._store.get("sh2").cooldown_end = time.time() + 20
        await p_h.hook_keyword_driver(message=msg("控制", session_id="sh2"))
        check("隐蔽模式：冷却提示也是无察觉语句",
              len(p_h.ctx.send.sent) == 1 and p_h.ctx.send.sent[0][0] in pool,
              str(p_h.ctx.send.sent))
        # 隐蔽关闭时保持原行为（回归）
        p_n = make_plugin(tmp, admin_ids=["88888"])
        p_n.ctx = FakeCtx(tmp)
        await p_n.cmd_intensity(stream_id="sn", user_id="88888",
                                matched_groups={"value": "90"})
        check("隐蔽关闭：回执为真实内容",
              "90/120" in p_n.ctx.send.sent[-1][0], str(p_n.ctx.send.sent[-1]))

        # 持续越限顶点效果触发
        p_c = make_plugin(tmp, admin_ids=["88888"], climax_threshold=100,
                          climax_hold_seconds=1)
        p_c.ctx = FakeCtx(tmp)
        await p_c.cmd_intensity(stream_id="sc", user_id="88888",
                                matched_groups={"value": "110"})
        await p_c.hook_keyword_driver(message=msg("控制", session_id="sc"))
        r = await p_c.hook_prompt_injector(session_id="sc", extra_prompt="")
        inj1 = r["modified_kwargs"]["extra_prompt"]
        check("越限但未满时长：不注入顶点效果且开始计时",
              "乱了阵脚" not in inj1 and "强度参考" in inj1, inj1[-80:])
        await asyncio.sleep(1.3)
        r = await p_c.hook_prompt_injector(session_id="sc", extra_prompt="")
        inj2 = r["modified_kwargs"]["extra_prompt"]
        check("持续越限达到时长 → 注入一次顶点效果模板（含 emoji 标记）",
              "乱了阵脚" in inj2 and "110/120" in inj2 and "😳" in inj2, inj2[-120:])
        r = await p_c.hook_prompt_injector(session_id="sc", extra_prompt="")
        check("顶点效果只触发一次",
              "乱了阵脚" not in r["modified_kwargs"]["extra_prompt"])
        # 跌破阈值 → 计时清零，重新爬升后需重新计时
        p_c2 = make_plugin(tmp, admin_ids=["88888"], climax_threshold=100,
                           climax_hold_seconds=1)
        p_c2.ctx = FakeCtx(tmp)
        await p_c2.cmd_intensity(stream_id="sd", user_id="88888",
                                 matched_groups={"value": "110"})
        await p_c2.hook_keyword_driver(message=msg("控制", session_id="sd"))
        await p_c2.hook_prompt_injector(session_id="sd", extra_prompt="")  # 开始计时
        await p_c2.cmd_intensity(stream_id="sd", user_id="88888",
                                 matched_groups={"value": "30"})  # 跌落
        await p_c2.hook_prompt_injector(session_id="sd", extra_prompt="")  # 计时应清零
        await p_c2.cmd_intensity(stream_id="sd", user_id="88888",
                                 matched_groups={"value": "110"})  # 回到高位
        await p_c2.hook_prompt_injector(session_id="sd", extra_prompt="")  # 重新计时（不触发）
        r = await p_c2.hook_prompt_injector(session_id="sd", extra_prompt="")
        check("跌破阈值后重新计时（未满 1 秒不触发）",
              "乱了阵脚" not in r["modified_kwargs"]["extra_prompt"])
        await asyncio.sleep(1.3)
        r = await p_c2.hook_prompt_injector(session_id="sd", extra_prompt="")
        check("重新计满后触发", "乱了阵脚" in r["modified_kwargs"]["extra_prompt"])
        # threshold=0 关闭
        p_c3 = make_plugin(tmp, admin_ids=["88888"], climax_threshold=0)
        p_c3.ctx = FakeCtx(tmp)
        await p_c3.cmd_intensity(stream_id="se", user_id="88888",
                                 matched_groups={"value": "120"})
        await p_c3.hook_keyword_driver(message=msg("控制", session_id="se"))
        await asyncio.sleep(1.2)
        r = await p_c3.hook_prompt_injector(session_id="se", extra_prompt="")
        check("climax_threshold=0 关闭持续越限触发",
              "乱了阵脚" not in r["modified_kwargs"]["extra_prompt"])

        # climax_emoji 留空则不附加标记
        p_e = make_plugin(tmp, admin_ids=["88888"], climax_threshold=100,
                          climax_hold_seconds=1)
        p_e.config.prompt.climax_emoji = ""
        p_e.ctx = FakeCtx(tmp)
        await p_e.cmd_intensity(stream_id="sq", user_id="88888",
                                matched_groups={"value": "120"})
        await p_e.hook_keyword_driver(message=msg("控制", session_id="sq"))
        await p_e.hook_prompt_injector(session_id="sq", extra_prompt="")  # 开始计时
        await asyncio.sleep(1.3)
        r = await p_e.hook_prompt_injector(session_id="sq", extra_prompt="")
        check("climax_emoji 留空 → 不附加 emoji 指令",
              "乱了阵脚" in r["modified_kwargs"]["extra_prompt"]
              and "带上" not in r["modified_kwargs"]["extra_prompt"])

        # 每次激活触发次数上限（默认 3）
        p_m = make_plugin(tmp, admin_ids=["88888"], climax_threshold=100,
                          climax_hold_seconds=1)
        p_m.ctx = FakeCtx(tmp)
        await p_m.cmd_intensity(stream_id="sm", user_id="88888",
                                matched_groups={"value": "120"})
        await p_m.hook_keyword_driver(message=msg("控制", session_id="sm"))
        fired = 0
        for _ in range(8):
            await asyncio.sleep(1.15)
            r = await p_m.hook_prompt_injector(session_id="sm", extra_prompt="")
            if "乱了阵脚" in r["modified_kwargs"]["extra_prompt"]:
                fired += 1
        check("默认上限 3 次：钉在 120 只触发 3 次", fired == 3, str(fired))
        # 自定义上限 1 次
        p_m2 = make_plugin(tmp, admin_ids=["88888"], climax_threshold=100,
                           climax_hold_seconds=1, climax_max_per_activation=1)
        p_m2.ctx = FakeCtx(tmp)
        await p_m2.cmd_intensity(stream_id="sn2", user_id="88888",
                                 matched_groups={"value": "120"})
        await p_m2.hook_keyword_driver(message=msg("控制", session_id="sn2"))
        fired = 0
        for _ in range(4):
            await asyncio.sleep(1.15)
            r = await p_m2.hook_prompt_injector(session_id="sn2", extra_prompt="")
            if "乱了阵脚" in r["modified_kwargs"]["extra_prompt"]:
                fired += 1
        check("上限设为 1：只触发 1 次", fired == 1, str(fired))
        # 重新激活后次数重置
        await p_m2.hook_keyword_driver(message=msg("控制", session_id="sn2"))
        await p_m2.hook_prompt_injector(session_id="sn2", extra_prompt="")  # 开始计时
        await asyncio.sleep(1.3)
        r = await p_m2.hook_prompt_injector(session_id="sn2", extra_prompt="")
        check("重新激活后次数重置（再触发一次）",
              "乱了阵脚" in r["modified_kwargs"]["extra_prompt"])

        # on_load / on_unload 持久化
        with tempfile.TemporaryDirectory() as td2:
            tmp2 = Path(td2)
            p5 = plugin.create_plugin()
            p5.ctx = FakeCtx(tmp2)
            await p5.on_load()
            await p5.hook_keyword_driver(message=msg("控制", session_id="sp"))
            await p5.on_unload()
            p6 = plugin.create_plugin()
            p6.ctx = FakeCtx(tmp2)
            await p6.on_load()
            state = p6._store.prompt_state("sp", p6._control())
            check("重启后状态恢复（enter 仍生效）", state == "enter", state)

        # 配置热更新回调不炸
        await p.on_config_update("self", {}, "0")

    print(f"\n结果：PASS={PASS} FAIL={FAIL}")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
