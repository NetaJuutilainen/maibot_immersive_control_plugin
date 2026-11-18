"""离线验证脚本：不启动 MaiBot，对本插件做结构自检 + 状态机 + 钩子行为验证。

检查项：
 1. stub SDK 下 plugin.py 可导入，组件声明完整（3 命令 + 2 钩子）且名称唯一；
 2. manifest 结构校验（必填字段、ID/版本正则、能力名格式、SDK/Host 区间）；
 3. 配置模型默认值（含 1.2.3 硬性要求的 plugin.config_version）；
 4. 命令正则按 MaiBot re.search 语义的行为（命中/不命中样例）；
 5. 状态机：激活/冷却/并发上限/自动到期/待退出注入一次/撤销退出/持久化往返；
 6. 钩子端到端：关键词驱动（含静默 abort、冷却提示、非关键词旁路、无权限忽略）
    与 replyer 模板注入（占位符替换、extra_prompt 拼接、退出仅一次）；
    含适配器未预填 processed_plain_text 时的 raw_message 兜底识别。

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


class FakeApi:
    """模拟 ctx.api.call：脚本化返回 / 抛错 / 挂起，并记录调用（身体联动测试用）。"""

    HANG = object()  # 哨兵：该 API 挂起不返回，触发客户端 wait_for 超时

    def __init__(self, outer):
        self._outer = outer
        self.calls: list[tuple[str, dict]] = []

    async def call(self, name, **kwargs):
        self.calls.append((name, kwargs))
        behavior = self._outer.api_behavior.get(name.rsplit(".", 1)[-1])
        if behavior is None:
            return None
        if behavior is FakeApi.HANG:
            await asyncio.sleep(30)
            return None
        if isinstance(behavior, Exception):
            raise behavior
        return behavior


class FakeCtx:
    def __init__(self, root: Path):
        self.paths = FakePaths(root)
        self.logger = FakeLogger()
        self.send = FakeSend()
        self.maisaka = FakeMaisaka()
        self.api_behavior: dict = {}
        self.api = FakeApi(self)


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
    apis = [c for c in comps if c["kind"] == "api"]
    check("组件总数 = 9", len(comps) == 9, str(comps))
    check("命令 = 4", len(commands) == 4, str([c["name"] for c in commands]))
    check("钩子 = 2", len(hooks) == 2, str([c["name"] for c in hooks]))
    check("API = 3（webui_linkage / _config / _action）",
          sorted(c["name"] for c in apis) == ["webui_linkage", "webui_linkage_action",
                                              "webui_linkage_config"],
          str([c["name"] for c in apis]))
    check("WebUI API 均非 public", all(c.get("public") is False for c in apis),
          str([(c["name"], c.get("public")) for c in apis]))
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
    # 宿主有两类能力名：运行时能力 <代理>.<方法>（send.text / api.call…），
    # 以及 webui.json 作用域的声明式能力（custom_pages / image_upload…，见宿主 webui_schema.WEBUI_CAPABILITIES）
    WEBUI_SCOPE_CAPS = {"declarative_basic", "custom_pages", "image_upload", "structured_arguments"}
    check("能力名均为 <代理>.<方法> 格式或 webui 声明式能力",
          all(re.fullmatch(r"[a-z_]+(?:\.[a-z_]+)+", c) or c in WEBUI_SCOPE_CAPS for c in caps),
          str(caps))
    check("send.text 已声明", "send.text" in caps, str(caps))
    check("maisaka 能力已声明",
          "maisaka.proactive.trigger" in caps and "maisaka.context.append" in caps, str(caps))
    check("host 区间 1.3.0~1.4.99 / sdk 区间 2.5.0~2.23.99（dev 兼容当前宿主 1.4.5/SDK 2.23.0）",
          manifest["host_application"]["min_version"] == "1.3.0"
          and manifest["host_application"]["max_version"] == "1.4.99"
          and manifest["sdk"]["min_version"] == "2.5.0"
          and manifest["sdk"]["max_version"] == "2.23.99")

    def _seg99(v):
        parts = str(v).split(".")
        return "99" in parts[:2] or "999" in parts[:2]

    check("版本前两段无 99/999（市场校验禁令）",
          not any(_seg99(v) for v in [manifest["version"],
                                      manifest["host_application"]["min_version"],
                                      manifest["host_application"]["max_version"],
                                      manifest["sdk"]["min_version"],
                                      manifest["sdk"]["max_version"]]))

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
    rendered = plugin.render_template(cfg.prompt.enter_template, "测试玩具", 77, 180)
    check("占位符替换正确", "测试玩具" in rendered and "77" in rendered
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

        # 适配器未预填 processed_plain_text：从 raw_message 消息段兜底拼文本
        check("_text_from_raw 拼接 at+text",
              plugin._text_from_raw([
                  {"type": "at", "data": {"target_user_nickname": "麦麦"}},
                  {"type": "text", "data": " 控制"},
              ]) == "@麦麦 控制")
        check("_text_from_raw 忽略畸形段与无名 at",
              plugin._text_from_raw([
                  "junk", {"type": "at", "data": {}}, {"type": "text", "data": "td"},
              ]) == "@td")
        m = msg("", session_id="s7", raw_message=[{"type": "text", "data": "控制"}])
        del m["processed_plain_text"]
        r = await p.hook_keyword_driver(message=m)
        check("无 processed_plain_text 时兜底识别进入关键词",
              isinstance(r, dict) and r.get("action") == "abort", str(r))
        m = msg("", session_id="s8", raw_message=[{"type": "text", "data": "今天天气不错"}])
        del m["processed_plain_text"]
        r = await p.hook_keyword_driver(message=m)
        check("兜底路径非关键词仍旁路", r is None, str(r))
        m = msg("", session_id="s10", group_id="g1", is_at=True,
                raw_message=[{"type": "at", "data": {"target_user_cardname": "麦麦"}},
                             {"type": "text", "data": " 控制"}])
        del m["processed_plain_text"]
        r = await p.hook_keyword_driver(message=m)
        check("群聊 @+关键词（兜底文本）仍激活",
              isinstance(r, dict) and r.get("action") == "abort", str(r))

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
        check("imm_config 展示关键词与玩具",
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

        # 持续越限顶点触发
        p_c = make_plugin(tmp, admin_ids=["88888"], climax_threshold=100,
                          climax_hold_seconds=1)
        p_c.ctx = FakeCtx(tmp)
        await p_c.cmd_intensity(stream_id="sc", user_id="88888",
                                matched_groups={"value": "110"})
        await p_c.hook_keyword_driver(message=msg("控制", session_id="sc"))
        r = await p_c.hook_prompt_injector(session_id="sc", extra_prompt="")
        inj1 = r["modified_kwargs"]["extra_prompt"]
        check("越限但未满时长：不注入顶点且开始计时",
              "乱了阵脚" not in inj1 and "强度参考" in inj1, inj1[-80:])
        await asyncio.sleep(1.3)
        r = await p_c.hook_prompt_injector(session_id="sc", extra_prompt="")
        inj2 = r["modified_kwargs"]["extra_prompt"]
        check("持续越限达到时长 → 注入一次顶点模板（含 emoji 标记）",
              "乱了阵脚" in inj2 and "110/120" in inj2 and "😳" in inj2, inj2[-120:])
        r = await p_c.hook_prompt_injector(session_id="sc", extra_prompt="")
        check("顶点只触发一次",
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

    print("== 9. 身体联动（linkage，B=强度×时间滴灌累积） ==")
    with tempfile.TemporaryDirectory() as td_link:
        tmp = Path(td_link)

        # 9.1 默认全关：零调用、注入与原版一致
        p_link = make_plugin(tmp)
        await p_link.hook_keyword_driver(message=msg("控制", session_id="lx1"))
        r = await p_link.hook_prompt_injector(session_id="lx1", extra_prompt="")
        base_inject = r["modified_kwargs"]["extra_prompt"]
        check("联动关：注入含原版档位行", "当前强度参考" in base_inject, base_inject[:80])
        await p_link.hook_prompt_injector(session_id="lx1", extra_prompt="")
        check("联动关：零 api.call（守卫 1）", p_link.ctx.api.calls == [], str(p_link.ctx.api.calls[:2]))

        # 9.2 目标追踪模型：激活零写入；强度 50 → 首次滴灌向目标 46.25 靠近；拉满 120 → 夹取 +20
        p_link = make_plugin(tmp)
        link_cfg = p_link.config.linkage
        link_cfg.enabled = True
        link_cfg.write_stress = True
        link_cfg.state_ttl_seconds = 600
        p_link.ctx.api_behavior["get_state"] = {
            "ok": True, "hunger": 80.0, "energy": 10.0, "health": 90.0,
            "stress": 20.0, "sleeping": False,
        }
        link_cfg.stress_max_step = 0.0      # 本组测"目标追踪数学"，显式不限步长
        link_cfg.wake_meter_factor = 1.0
        r = await p_link.hook_keyword_driver(message=msg("控制", session_id="lx2"))
        check("联动开：激活 abort", isinstance(r, dict) and r.get("action") == "abort")
        check("目标追踪：激活只登记锚点零写入", p_link.ctx.api.calls == [], str(p_link.ctx.api.calls))
        r = await p_link.hook_prompt_injector(session_id="lx2", extra_prompt="")
        check("联动开：A① 文案追加（精力见底+饿）",
              "精力见底" in r["modified_kwargs"]["extra_prompt"], r["modified_kwargs"]["extra_prompt"][:120])
        adj = [(n, kw) for n, kw in p_link.ctx.api.calls if n.endswith("adjust_mood")]
        check("目标追踪：强度 50 目标 46.25 → 首次滴灌 +9.19",
              len(adj) == 1 and abs(adj[0][1].get("stress_delta", 0) - 9.1875) < 0.01
              and adj[0][1].get("label") == "遥控强度追踪", str(adj))
        await p_link._apply_intensity("lx2", 120)
        r = await p_link.hook_prompt_injector(session_id="lx2", extra_prompt="")
        adj = [(n, kw) for n, kw in p_link.ctx.api.calls if n.endswith("adjust_mood")]
        check("目标追踪：强度拉满 → 目标 90，单次夹取 +20",
              len(adj) == 2 and adj[1][1].get("stress_delta") == 20.0, str(adj))
        await p_link.hook_keyword_driver(message=msg("停止控制", session_id="lx2"))
        r = await p_link.hook_prompt_injector(session_id="lx2", extra_prompt="")
        adj = [(n, kw) for n, kw in p_link.ctx.api.calls if n.endswith("adjust_mood")]
        check("联动开：exit 注入不带身体文案", "精力见底" not in r["modified_kwargs"]["extra_prompt"])
        check("泄压语义：玩具停下 → −15 恰一次",
              len(adj) == 3 and adj[2][1].get("stress_delta") == -15.0
              and adj[2][1].get("label") == "玩具停下泄压", str(adj))
        await p_link.hook_prompt_injector(session_id="lx2", extra_prompt="")
        adj = [(n, kw) for n, kw in p_link.ctx.api.calls if n.endswith("adjust_mood")]
        check("泄压语义：不重复（簿记已清）", len(adj) == 3, str(len(adj)))

        # 9.2b 自然超时结束：不泄压，压力顶在原地（body 自我平衡慢慢回落）
        p_n = make_plugin(tmp)
        n_cfg = p_n.config.linkage
        n_cfg.enabled = True
        n_cfg.write_stress = True
        n_cfg.state_ttl_seconds = 600
        n_cfg.release_on_stop = -15.0
        p_n.config.control.state_duration = 60
        p_n.ctx.api_behavior["get_state"] = {"ok": True, "sleeping": False, "stress": 55.0}
        await p_n.hook_keyword_driver(message=msg("控制", session_id="lx2b"))
        await p_n.hook_prompt_injector(session_id="lx2b", extra_prompt="")
        adj0 = sum(1 for n, _ in p_n.ctx.api.calls if n.endswith("adjust_mood"))
        s = p_n._store.get("lx2b")
        s.started_ts = time.time() - 200  # 直接把会话拨到超时（避免真等 60 秒）
        r = await p_n.hook_prompt_injector(session_id="lx2b", extra_prompt="")
        adj1 = sum(1 for n, _ in p_n.ctx.api.calls if n.endswith("adjust_mood"))
        check("自然超时：到期结算不泄压（压力顶在原地）", adj0 == 1 and adj1 == adj0,
              f"adj0={adj0} adj1={adj1}")
        check("自然超时：注入回到 none", p_n._store.prompt_state("lx2b", p_n._control()) == "none")

        # 9.3 睡眠场景：睡着 → A① 睡意文案；滴灌照常（身体在睡梦中积累）+ 觉醒值积累
        p_sl = make_plugin(tmp)
        sl_cfg = p_sl.config.linkage
        sl_cfg.enabled = True
        sl_cfg.write_stress = True
        sl_cfg.state_ttl_seconds = 600
        p_sl.ctx.api_behavior["get_state"] = {"ok": True, "sleeping": True, "energy": 90.0}
        await p_sl.hook_keyword_driver(message=msg("控制", session_id="lx3"))
        await p_sl._apply_intensity("lx3", 120)
        r = await p_sl.hook_prompt_injector(session_id="lx3", extra_prompt="")
        check("睡着：A① 睡意文案追加", "睡意沉沉" in r["modified_kwargs"]["extra_prompt"])
        adj = [(n, kw) for n, kw in p_sl.ctx.api.calls if n.endswith("adjust_mood")]
        check("睡着：滴灌照常（睡眠场景）",
              len(adj) >= 1 and all(kw.get("label") == "遥控强度追踪" for _, kw in adj), str(adj))
        check("睡着：觉醒值在积累", 0 < p_sl._body_wake_meter <= 100, str(p_sl._body_wake_meter))
        # 睡眠场景关闭（threshold=0）→ 睡着不写（守卫回退）
        p_sl.config.linkage.sleep_wake_threshold = 0.0
        p_sl._body()._cache = None
        adj0 = sum(1 for n, _ in p_sl.ctx.api.calls if n.endswith("adjust_mood"))
        await p_sl._body_write(10.0, label="x")
        adj1 = sum(1 for n, _ in p_sl.ctx.api.calls if n.endswith("adjust_mood"))
        check("睡眠场景关闭：睡着不写（守卫回退）", adj1 == adj0, str(adj1))

        # 9.2c 步长上限与觉醒值比例（默认 6 点/tick、0.6 倍）
        p_cap = make_plugin(tmp)
        c_cap = p_cap.config.linkage
        c_cap.enabled = True
        c_cap.write_stress = True
        c_cap.state_ttl_seconds = 600
        p_cap.ctx.api_behavior["get_state"] = {"ok": True, "sleeping": True, "stress": 20.0}
        p_cap.ctx.api_behavior["get_life_state"] = {"sleep_phase": "light_sleep"}
        await p_cap.hook_keyword_driver(message=msg("控制", session_id="lc0"))
        await p_cap._apply_intensity("lc0", 120)
        await p_cap.hook_prompt_injector(session_id="lc0", extra_prompt="")
        adj = [(n, kw) for n, kw in p_cap.ctx.api.calls if n.endswith("adjust_mood")]
        check("步长上限：目标 90 但单 tick 只写 +6",
              len(adj) == 1 and adj[0][1].get("stress_delta") == 6.0, str(adj))
        check("觉醒值按 0.6 倍累积（6×0.6=3.6）",
              abs(p_cap._body_wake_meter - 3.6) < 0.01, str(p_cap._body_wake_meter))

        # 9.3c 深睡：阈值 100——攒满整条，醒来瞬间余韵引爆 + 完整记账
        p_d = make_plugin(tmp)
        d_cfg = p_d.config.linkage
        d_cfg.enabled = True
        d_cfg.write_stress = True
        d_cfg.state_ttl_seconds = 600
        d_cfg.stress_max_step = 0.0
        d_cfg.wake_meter_factor = 1.0
        p_d.ctx.api_behavior["get_state"] = {"ok": True, "sleeping": True, "stress": 0.0}
        p_d.ctx.api_behavior["get_life_state"] = {"sleep_phase": "deep_sleep"}
        await p_d.hook_keyword_driver(message=msg("控制", session_id="ld"))
        await p_d._apply_intensity("ld", 120)
        for _ in range(5):  # 每次 +20，攒满 100
            await p_d.hook_prompt_injector(session_id="ld", extra_prompt="")
        check("深睡：觉醒值攒满 100（睡梦中不引爆）",
              p_d._body_wake_meter == 100.0
              and not any(kw.get("label") == "睡梦余韵引爆"
                          for _, kw in p_d.ctx.api.calls), str(p_d._body_wake_meter))
        p_d.ctx.api_behavior["get_state"] = {"ok": True, "sleeping": False, "stress": 0.0}
        p_d._body()._cache = None
        r = await p_d.hook_prompt_injector(session_id="ld", extra_prompt="")
        adj = [(n, kw) for n, kw in p_d.ctx.api.calls if n.endswith("adjust_mood")]
        check("深睡醒来：余韵引爆 +20 + 完整模糊记账",
              any(kw.get("label") == "睡梦余韵引爆" and kw.get("stress_delta") == 20.0 for _, kw in adj)
              and "好像经历了" in r["modified_kwargs"]["extra_prompt"]
              and p_d._body_wake_meter == 0.0, str(adj))
        check("深睡标记重置", p_d._body_deep_reached is False)

        # 9.3d 浅睡：阈值 60——半条即引爆
        p_l = make_plugin(tmp)
        l_cfg = p_l.config.linkage
        l_cfg.enabled = True
        l_cfg.write_stress = True
        l_cfg.state_ttl_seconds = 600
        l_cfg.stress_max_step = 0.0
        l_cfg.wake_meter_factor = 1.0
        p_l.ctx.api_behavior["get_state"] = {"ok": True, "sleeping": True, "stress": 0.0}
        p_l.ctx.api_behavior["get_life_state"] = {"sleep_phase": "light_sleep"}
        await p_l.hook_keyword_driver(message=msg("控制", session_id="ll"))
        await p_l._apply_intensity("ll", 120)
        for _ in range(3):  # 每次 +20 → 60
            await p_l.hook_prompt_injector(session_id="ll", extra_prompt="")
        p_l.ctx.api_behavior["get_state"] = {"ok": True, "sleeping": False, "stress": 0.0}
        p_l._body()._cache = None
        r = await p_l.hook_prompt_injector(session_id="ll", extra_prompt="")
        adj = [(n, kw) for n, kw in p_l.ctx.api.calls if n.endswith("adjust_mood")]
        check("浅睡：攒到 60 即引爆 + 完整记账",
              any(kw.get("label") == "睡梦余韵引爆" and kw.get("stress_delta") == 20.0 for _, kw in adj)
              and "好像经历了" in r["modified_kwargs"]["extra_prompt"], str(adj))

        # 9.3e 未达阈值：轻记账（身体异样感），无引爆
        p_n2 = make_plugin(tmp)
        n2_cfg = p_n2.config.linkage
        n2_cfg.enabled = True
        n2_cfg.write_stress = True
        n2_cfg.state_ttl_seconds = 600
        n2_cfg.wake_threshold_light = 60.0
        n2_cfg.stress_max_step = 0.0
        n2_cfg.wake_meter_factor = 1.0
        p_n2.ctx.api_behavior["get_state"] = {"ok": True, "sleeping": True, "stress": 0.0}
        p_n2.ctx.api_behavior["get_life_state"] = {"sleep_phase": "light_sleep"}
        await p_n2.hook_keyword_driver(message=msg("控制", session_id="ln"))
        await p_n2._apply_intensity("ln", 120)
        await p_n2.hook_prompt_injector(session_id="ln", extra_prompt="")
        await p_n2.hook_prompt_injector(session_id="ln", extra_prompt="")
        p_n2.ctx.api_behavior["get_state"] = {"ok": True, "sleeping": False, "stress": 0.0}
        p_n2._body()._cache = None
        r = await p_n2.hook_prompt_injector(session_id="ln", extra_prompt="")
        adj = [(n, kw) for n, kw in p_n2.ctx.api.calls if n.endswith("adjust_mood")]
        check("未达阈值：轻记账注入、无引爆",
              "异样" in r["modified_kwargs"]["extra_prompt"]
              and not any(kw.get("label") == "睡梦余韵引爆" for _, kw in adj), str(adj))

        # 9.3b 自然醒来 + 觉醒值攒满 → 余韵引爆 + 模糊记账注入一次
        p_w = make_plugin(tmp)
        w_cfg = p_w.config.linkage
        w_cfg.enabled = True
        w_cfg.write_stress = True
        w_cfg.state_ttl_seconds = 600
        w_cfg.stress_max_step = 0.0
        w_cfg.wake_meter_factor = 1.0
        p_w.ctx.api_behavior["get_state"] = {"ok": True, "sleeping": True, "stress": 70.0}
        await p_w.hook_keyword_driver(message=msg("控制", session_id="lw"))
        await p_w._apply_intensity("lw", 120)
        for _ in range(10):  # 每次注入滴灌 +7 左右，攒到觉醒值 ≥ 阈值 60
            r = await p_w.hook_prompt_injector(session_id="lw", extra_prompt="")
        check("睡着：滴灌积累觉醒值至阈值", p_w._body_wake_meter >= 60.0, str(p_w._body_wake_meter))
        p_w.ctx.api_behavior["get_state"] = {"ok": True, "sleeping": False, "stress": 70.0}
        p_w._body()._cache = None
        r = await p_w.hook_prompt_injector(session_id="lw", extra_prompt="")
        adj = [(n, kw) for n, kw in p_w.ctx.api.calls if n.endswith("adjust_mood")]
        check("醒来瞬间：余韵引爆 +20（觉醒值清零）",
              any(kw.get("label") == "睡梦余韵引爆" and kw.get("stress_delta") == 20.0 for _, kw in adj)
              and p_w._body_wake_meter == 0.0, str(adj))
        check("醒来瞬间：模糊记账注入一次",
              "好像经历了" in r["modified_kwargs"]["extra_prompt"]
              and r["modified_kwargs"]["extra_prompt"].count("好像经历了") == 1)

        # 9.4 错误字典失败形态 + 熔断（3 连败 → 读写一并短路零 RPC）
        p_err = make_plugin(tmp)
        e_cfg = p_err.config.linkage
        e_cfg.enabled = True
        e_cfg.write_stress = True
        e_cfg.state_ttl_seconds = 10
        p_err.ctx.api_behavior["get_state"] = {"success": False, "error": "目标未就绪"}
        for _ in range(3):
            st = await p_err._body_state()
        link_obj = p_err._body()
        check("错误字典形态按失败计数（3 连败后熔断开启）",
              link_obj._fail_count == 3 and link_obj._open_until > 0,
              f"{link_obj._fail_count},{link_obj._open_until}")
        calls_before = len(p_err.ctx.api.calls)
        st = await p_err._body_state()
        st2 = await link_obj.adjust_mood(6.0, label="x")
        check("熔断期读写一并短路（零 RPC）",
              st is None and st2 is False and len(p_err.ctx.api.calls) == calls_before)

        # 9.5 超时形态（挂起 → wait_for 掐断并计数）
        p_to = make_plugin(tmp)
        p_to.config.linkage.enabled = True
        p_to.config.linkage.call_timeout_seconds = 1
        p_to.ctx.api_behavior["get_state"] = FakeApi.HANG
        st = await p_to._body_state()
        check("挂起调用被 wait_for 掐断并按失败计数",
              st is None and p_to._body()._fail_count == 1
              and "TimeoutError" in p_to._body()._last_error, p_to._body()._last_error)

        # 9.6 字段级守卫：ok=True 但缺字段 → 归一化为中性值不炸（缺数据不捏造饥饿/生病）
        p_g = make_plugin(tmp)
        p_g.config.linkage.enabled = True
        p_g.ctx.api_behavior["get_state"] = {"ok": True, "junk": 1}
        st = await p_g._body_state()
        check("ok=True 缺字段 → 中性默认值（守卫 5）",
              isinstance(st, dict) and st["energy"] == 100.0 and st["health"] == 100.0
              and st["stress"] == 0.0 and st["sleeping"] is False, str(st))

        # 9.6b 压力分档文案（55/70/90 三档随滴灌爬坡换词）
        p_t = make_plugin(tmp)
        p_t.config.linkage.enabled = True
        p_t.config.linkage.state_ttl_seconds = 600
        await p_t.hook_keyword_driver(message=msg("控制", session_id="lt"))
        for stress_v, expect in ((40.0, None), (60.0, "心跳得飞快"),
                                 (80.0, "断断续续"), (95.0, "怎么都缓不过来")):
            p_t.ctx.api_behavior["get_state"] = {"ok": True, "sleeping": False, "stress": stress_v}
            p_t._body()._cache = None
            await p_t._body_state()
            r = await p_t.hook_prompt_injector(session_id="lt", extra_prompt="")
            inject = r["modified_kwargs"]["extra_prompt"]
            if expect is None:
                check(f"压力 {stress_v:.0f}：无压力文案（低于 55 不触发）",
                      "心跳得飞快" not in inject and "喘息" not in inject and "亢奋" not in inject)
            else:
                check(f"压力 {stress_v:.0f}：「{expect}」档文案", expect in inject, inject[:140])

        # 9.7 /imm_clear 同步簿记（集合+滴灌锚点）
        p_c = make_plugin(tmp, admin_ids=["88888"])
        c_cfg = p_c.config.linkage
        c_cfg.enabled = True
        c_cfg.write_stress = True
        c_cfg.state_ttl_seconds = 600
        p_c.ctx.api_behavior["get_state"] = {"ok": True, "sleeping": False}
        await p_c.hook_keyword_driver(message=msg("控制", session_id="lx4"))
        await p_c._apply_intensity("lx4", 120)
        await p_c.hook_prompt_injector(session_id="lx4", extra_prompt="")
        adj1 = sum(1 for n, _ in p_c.ctx.api.calls if n.endswith("adjust_mood"))
        check("lx4 追踪写入一次", adj1 == 1, str(adj1))
        await p_c.cmd_clear(stream_id="lx4", user_id="88888", matched_groups={})
        await p_c.hook_keyword_driver(message=msg("控制", session_id="lx4"))
        await p_c._apply_intensity("lx4", 120)
        await p_c.hook_prompt_injector(session_id="lx4", extra_prompt="")
        adj2 = sum(1 for n, _ in p_c.ctx.api.calls if n.endswith("adjust_mood"))
        check("/imm_clear 后重激活重新追踪（簿记同步）", adj2 == 2, str(adj2))

        # 9.8 A② 显示加成与 Y=0 回退（机制判定永不加成 → 白名单结构性保证）
        p_b = make_plugin(tmp, admin_ids=["88888"])
        b_cfg = p_b.config.linkage
        b_cfg.enabled = True
        b_cfg.sensitivity_boost = True
        b_cfg.state_ttl_seconds = 600
        p_b.ctx.api_behavior["get_state"] = {"ok": True, "sleeping": False,
                                             "energy": 10.0, "health": 90.0}
        await p_b.hook_keyword_driver(message=msg("控制", session_id="lb"))  # 先激活，注入器才有 enter 态
        await p_b.cmd_intensity(stream_id="lb", user_id="88888", matched_groups={})
        out = p_b.ctx.send.sent[-1][0]
        check("A② 开：/强度 显示基础+身体加成",
              "基础 50 + 身体加成 10" in out and "60/120" in out, out[:120])
        r = await p_b.hook_prompt_injector(session_id="lb", extra_prompt="")
        check("A② 开：enter {sensitivity} 与档位用加成值",
              "敏感度60/120" in r["modified_kwargs"]["extra_prompt"]
              and "中等偏强" in r["modified_kwargs"]["extra_prompt"])
        p_b.ctx.api_behavior["get_state"] = {"ok": True, "sleeping": False,
                                             "energy": 90.0, "health": 90.0}
        p_b._body()._cache = None
        p_b._body()._last_pull = 0.0
        await p_b.cmd_intensity(stream_id="lb", user_id="88888", matched_groups={})
        out = p_b.ctx.send.sent[-1][0]
        check("A② 加成 0：/强度 回退原格式（守卫 8）",
              "当前敏感度：50/120" in out and "身体加成" not in out, out[:120])

        # 9.9 守卫 7：api.call 抛异常（如 E_CAPABILITY_DENIED）→ 钩子照常返回不连坐
        p_x = make_plugin(tmp)
        x_cfg = p_x.config.linkage
        x_cfg.enabled = True
        x_cfg.write_stress = True
        x_cfg.state_ttl_seconds = 600
        p_x.ctx.api_behavior["get_state"] = RuntimeError("E_CAPABILITY_DENIED")
        await p_x.hook_keyword_driver(message=msg("控制", session_id="lx5"))
        r = await p_x.hook_prompt_injector(session_id="lx5", extra_prompt="")
        check("异常形态：注入照常返回（守卫 7 不连坐）",
              isinstance(r, dict) and "当前强度参考" in r["modified_kwargs"]["extra_prompt"])
        check("异常形态：按失败计数", p_x._body()._fail_count >= 1)

        # 9.10 climax 注入与滴灌共存（climax 事件本身不再一次性写压力）
        p_cl = make_plugin(tmp)
        cl_link = p_cl.config.linkage
        cl_link.enabled = True
        cl_link.write_stress = True
        cl_link.state_ttl_seconds = 600
        p_cl.config.control.climax_threshold = 100
        p_cl.config.control.climax_hold_seconds = 1
        p_cl.config.control.climax_max_per_activation = 1
        p_cl.ctx.api_behavior["get_state"] = {"ok": True, "sleeping": False}
        await p_cl.hook_keyword_driver(message=msg("控制", session_id="lc1"))
        await p_cl._apply_intensity("lc1", 120)
        await asyncio.sleep(1.2)
        r1 = await p_cl.hook_prompt_injector(session_id="lc1", extra_prompt="")
        check("首次注入：开始越限计时不出顶点", "乱了阵脚" not in r1["modified_kwargs"]["extra_prompt"])
        await asyncio.sleep(1.2)
        r2 = await p_cl.hook_prompt_injector(session_id="lc1", extra_prompt="")
        check("climax 注入出现（第二次注入）", "乱了阵脚" in r2["modified_kwargs"]["extra_prompt"])
        adj = [(n, kw) for n, kw in p_cl.ctx.api.calls if n.endswith("adjust_mood")]
        check("climax 额度用尽 → 泄压 −15 恰一次（追踪滴灌继续）",
              sum(1 for _, kw in adj if kw.get("label") == "顶点额度用尽泄压") == 1
              and all(kw.get("label") in ("遥控强度追踪", "顶点额度用尽泄压") for _, kw in adj), str(adj))

        # 9.11 WebUI P1：真名捕获 + webui_linkage 数据形状 + webui.json 声明
        p_ui = make_plugin(tmp)
        m = msg("控制", session_id="lu", group_id="g1", is_at=True)
        m["message_info"]["group_info"] = {"group_id": "g1", "group_name": "麦麦日常摸鱼群"}
        await p_ui.hook_keyword_driver(message=m)
        s_g = p_ui._store.get("lu")
        check("群聊激活记录群名",
              s_g.is_group is True and s_g.display_name == "麦麦日常摸鱼群",
              str(s_g.display_name))
        m2 = msg("控制", session_id="lu2")
        m2["message_info"]["user_info"]["user_nickname"] = "阿澈"
        await p_ui.hook_keyword_driver(message=m2)
        s_p = p_ui._store.get("lu2")
        check("私聊激活记录昵称（无名片时）",
              s_p.is_group is False and s_p.display_name == "阿澈", str(s_p.display_name))
        p_ui.config.linkage.enabled = True
        p_ui.config.linkage.write_stress = True
        d = await p_ui.webui_linkage()
        names = [x["display_name"] for x in d["sessions"]]
        check("webui_linkage：会话带真名", "麦麦日常摸鱼群" in names and "阿澈" in names, str(names))
        check("webui_linkage：字段齐全（阈值/压力/觉醒值）",
              d.get("ok") is True and "wake_meter" in d and "stress_target" in d
              and d.get("wake_threshold_light") == 60 and d.get("wake_threshold_deep") == 100,
              str({k: d.get(k) for k in ("ok", "wake_threshold_light", "wake_threshold_deep")}))
        wj = json.loads((PLUGIN_DIR / "webui.json").read_text(encoding="utf-8"))
        page = (wj.get("pages") or [{}])[0]
        check("webui.json：custom 页 entry 文件存在",
              page.get("mode") == "custom" and (PLUGIN_DIR / page.get("entry", "")).is_file(),
              str(page.get("entry")))
        check("webui.json：query 指向 webui_linkage",
              (page.get("queries") or {}).get("linkage", {}).get("api") == "webui_linkage")
        acts = page.get("actions") or {}
        check("webui.json：config / op 两个 action 声明齐全",
              acts.get("config", {}).get("api") == "webui_linkage_config"
              and acts.get("op", {}).get("api") == "webui_linkage_action",
              str(list(acts)))

        # 9.12 WebUI P2：快捷配置补丁（白名单+夹取+写回 config.toml）与快捷动作
        p_cfg = make_plugin(tmp)
        cfg_file = tmp / "config.toml"
        cfg_file.write_text(
            "[plugin]\nenabled = true\n\n[linkage]\nenabled = false\nwrite_stress = false\n\n"
            "[control]\nstate_duration = 180\n", encoding="utf-8")
        p_cfg._cfg_path = cfg_file
        r = await p_cfg.webui_linkage_config(patch=json.dumps({
            "enabled": True, "wake_threshold_deep": 85, "tick_interval_seconds": 45,
            "stress_target_range": 999, "bogus_key": 1,
        }))
        check("配置补丁：白名单应用 + 越界夹取 + 非法键拒绝",
              r.get("ok") and r["applied"].get("enabled") is True
              and r["applied"].get("wake_threshold_deep") == 85.0
              and r["applied"].get("stress_target_range") == 100.0
              and any("bogus_key" in x for x in (r.get("rejected") or [])), str(r))
        saved = cfg_file.read_text(encoding="utf-8")
        check("配置补丁：写回 [linkage] 段且不动其他段",
              "wake_threshold_deep = 85.0" in saved and "tick_interval_seconds = 45" in saved
              and "enabled = true" in saved and "[control]" in saved
              and "state_duration = 180" in saved, saved[:200])
        p_cfg.config.linkage.write_stress = True
        r2 = await p_cfg.webui_linkage_action(op="pause")
        check("快捷动作：pause 关闭 write_stress",
              r2.get("ok") and p_cfg.config.linkage.write_stress is False, str(r2))
        r3 = await p_cfg.webui_linkage_action(op="resume")
        check("快捷动作：resume 恢复 write_stress",
              r3.get("ok") and p_cfg.config.linkage.write_stress is True, str(r3))
        n0 = len(p_cfg._store.sessions)
        await p_cfg.hook_keyword_driver(message=msg("控制", session_id="lx9"))
        r4 = await p_cfg.webui_linkage_action(op="clear")
        check("快捷动作：clear 清空会话并同步簿记",
              r4.get("ok") and p_cfg._store.active_count() == 0
              and not p_cfg._active_sessions and n0 >= 0, str(r4))
        r5 = await p_cfg.webui_linkage_action(op="release")
        check("快捷动作：release 返回已执行", r5.get("ok") is True, str(r5))
        r6 = await p_cfg.webui_linkage_action(op="nope")
        check("快捷动作：未知操作拒绝", r6.get("ok") is False, str(r6))

        # 9.13 本体配置（control / prompt 段）：列表归一 + 写回对应段 + 段自动补建
        r7 = await p_cfg.webui_linkage_config(patch=json.dumps({
            "control.state_duration": 600,
            "control.enter_keywords": "控制\n遥控\ntd",
            "control.stealth_phrases": ["好。", "", "嗯，知道了。"],
            "prompt.item_name": "小项链",
            "admin_only_mode": True,
        }))
        check("本体配置：control/prompt 应用 + 列表按行归一（句内逗号保留）",
              r7.get("ok") and p_cfg.config.control.state_duration == 600
              and p_cfg.config.control.enter_keywords == ["控制", "遥控", "td"]
              and p_cfg.config.control.stealth_phrases == ["好。", "嗯，知道了。"]
              and p_cfg.config.prompt.item_name == "小项链", str(r7))
        r7b = await p_cfg.webui_linkage_config(patch=json.dumps({"control.enter_keywords": "  \n "}))
        check("本体配置：空关键词列表被拒绝（防空触发）",
              any("enter_keywords" in x for x in (r7b.get("rejected") or []))
              and p_cfg.config.control.enter_keywords == ["控制", "遥控", "td"], str(r7b))
        r7c = await p_cfg.webui_linkage_config(patch=json.dumps(
            {"prompt.enter_template": "长模板" * 1000}))
        check("本体配置：长模板不被截断（multiline 上限 4000，非 str 的 120）",
              len(p_cfg.config.prompt.enter_template) > 2000,
              str(len(p_cfg.config.prompt.enter_template)))
        check("本体配置：危险字段（admin_only_mode）被白名单拒绝",
              any("admin_only_mode" in x for x in (r7.get("rejected") or [])), str(r7.get("rejected")))
        saved2 = cfg_file.read_text(encoding="utf-8")
        check("本体配置：写回 [control] 段（含列表格式）",
              "state_duration = 600" in saved2
              and 'enter_keywords = ["控制", "遥控", "td"]' in saved2, saved2[:400])
        check("本体配置：[prompt] 段缺失时自动补建",
              "[prompt]" in saved2 and 'item_name = "小项链"' in saved2, saved2[-260:])
        snap = p_cfg._config_snapshot()
        check("配置画像：三段齐全",
              set(snap) == {"linkage", "control", "prompt"}
              and snap["control"].get("state_duration") == 600
              and snap["prompt"].get("item_name") == "小项链", str(list(snap)))

        # 9.13b 重载恢复 + tick 以 store 为准 + 冷却守卫
        p_rs = make_plugin(tmp)
        r_cfg = p_rs.config.linkage
        r_cfg.enabled = True
        r_cfg.write_stress = True
        r_cfg.state_ttl_seconds = 600
        p_rs.ctx.api_behavior["get_state"] = {"ok": True, "sleeping": True, "stress": 20.0}
        p_rs.ctx.api_behavior["get_life_state"] = {"sleep_phase": "light_sleep"}
        p_rs._store.try_activate("lrs", p_rs._control())   # 只在 store 里（内存簿记为空，模拟重载后）
        await p_rs._body_tick_all()   # 首次 tick 只登记锚点
        await p_rs._body_tick_all()   # 第二次才滴灌
        adj = [(n, kw) for n, kw in p_rs.ctx.api.calls if n.endswith("adjust_mood")]
        check("tick 以 store 为准：内存簿记为空也会滴灌",
              len(adj) == 1 and adj[0][1].get("label") == "遥控强度追踪", str(adj))
        check("有激活会话时绝不冷却",
              all(kw.get("label") != "冷却回落" for _, kw in adj), str(adj))
        p_rs._store.clear()
        p_rs._body_wake_meter = 10.0
        await p_rs._body_tick_all()
        check("无会话时觉醒值缓慢消退（每 tick −2）",
              abs(p_rs._body_wake_meter - 8.0) < 0.01, str(p_rs._body_wake_meter))

        with tempfile.TemporaryDirectory() as td3:
            p_l = plugin.create_plugin()
            p_l.ctx = FakeCtx(Path(td3))
            p_l._store.try_activate("lrx", p_l.config.control)
            p_l._store.save(Path(td3) / "state.json")
            p_l2 = plugin.create_plugin()
            p_l2.ctx = FakeCtx(Path(td3))
            await p_l2.on_load()
            check("on_load 从持久化恢复激活会话簿记",
                  "lrx" in p_l2._active_sessions, str(p_l2._active_sessions))
            await p_l2.on_unload()

        # 9.14 无会话主动冷却（可见的"自然回落"）
        p_cd = make_plugin(tmp)
        d_cfg2 = p_cd.config.linkage
        d_cfg2.enabled = True
        d_cfg2.write_stress = True
        d_cfg2.state_ttl_seconds = 600
        d_cfg2.auto_cooldown = True
        d_cfg2.cooldown_rate_per_minute = 3.0
        d_cfg2.cooldown_floor = 20.0
        p_cd.ctx.api_behavior["get_state"] = {"ok": True, "sleeping": False, "stress": 80.0}
        await p_cd._body_cooldown_tick()
        adj = [(n, kw) for n, kw in p_cd.ctx.api.calls if n.endswith("adjust_mood")]
        check("主动冷却：无会话时每 tick 写回一步（3/分 × 0.5 分 ≈ −1.5）",
              len(adj) == 1 and abs(adj[0][1].get("stress_delta", 0) + 1.5) < 0.01
              and adj[0][1].get("label") == "冷却回落", str(adj))
        p_cd.ctx.api_behavior["get_state"] = {"ok": True, "sleeping": False, "stress": 20.2}
        p_cd._body()._cache = None
        await p_cd._body_cooldown_tick()
        adj = [(n, kw) for n, kw in p_cd.ctx.api.calls if n.endswith("adjust_mood")]
        check("主动冷却：到下限即停", len(adj) == 1, str(len(adj)))
        d_cfg2.auto_cooldown = False
        p_cd.ctx.api_behavior["get_state"] = {"ok": True, "sleeping": False, "stress": 80.0}
        p_cd._body()._cache = None
        await p_cd._body_cooldown_tick()
        adj = [(n, kw) for n, kw in p_cd.ctx.api.calls if n.endswith("adjust_mood")]
        check("主动冷却：开关关闭不写", len(adj) == 1, str(len(adj)))

    print(f"\n结果：PASS={PASS} FAIL={FAIL}")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
