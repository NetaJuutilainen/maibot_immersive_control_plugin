# 更新日志

## v1.0.2（2026-10-26）

- **修复：部分适配器下关键词完全不识别的问题。**
  宿主在 `chat.receive.before_process` 钩子触发之后才生成 `processed_plain_text`，
  此前关键词匹配完全依赖适配器是否预填该字段——预填型适配器
  （NapCat / SnowLuma / 官方 QQ 适配器等）一切正常，其余适配器会表现为
  「关键词怎么发都没反应」。现在适配器未预填时自动从 `raw_message`
  消息段拼出文本再匹配，不再依赖适配器行为；预填型适配器行为不变。
- manifest 新增 `changelog` 字段，WebUI 插件页可直接看到本更新日志。
- 版本区间按市场新规收紧：`host_application` **1.3.0 ~ 1.3.99**（对齐官方适配器口径）、
  `sdk` **2.5.0 ~ 2.16.99**（市场校验禁止版本前两段填 99/999，旧值 2.99.99 不再合规）。

## v1.0.1

- `host_application.max_version` 上界拉满（`114.514.810`），插件不因宿主大版本升级被挡；
- `SUPPORTED_CONFIG_VERSION` 与 manifest 版本同步。

## v1.0.0

- 首个版本：自 AstrBot [astrbot_plugin_immersive_control](https://github.com/muyouzhi6/astrbot_plugin_immersive_control)（MIT）移植，
  针对 MaiBot 1.3.x + maibot-plugin-sdk 2.x 重写。
