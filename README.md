# 石英未来提醒

独立的 AstrBot 提醒插件，复用原生 Cron 调度，自己维护提醒、发送回执、重试和取消状态。可以独立运行；有石英的 `message.compose` 能力时用其生成提醒文字，否则使用 AstrBot 当前对话模型。

## 安装

在 AstrBot 插件管理中安装本仓库，或放入 `data/plugins/astrbot_plugin_shirnk_ext_reminder` 后加载。需要 AstrBot 4.29.3 或以上版本；当前在 4.29.4 上验证。

需要可用的对话模型、已连接的消息平台及 AstrBot 原生未来任务功能。Windows 环境若缺少时区数据库，安装 `requirements.txt` 中的 `tzdata`。

## 行为

- “早上 10 点提醒我起床”默认创建下一次 10:00 的一次性提醒；明确要求每天才创建周期提醒。
- 当前时间解释使用 `Asia/Shanghai` 时区。明确给出的过去日期会报错，不擅自顺延。
- 记录原始会话和创建者，支持查看、取消以及相同请求去重。
- 发送失败按 1、5、15 分钟后按小时重试，最多持续 24 小时；延迟补发会说明情况。
- 发送结果不明时保留待核对状态，不把任务结束当成消息已送达。

公开能力 `reminders.manage` 提供 `list/create/delete`。现有工具名和旧 Cron 任务使用的 `shrink_deliver_reminder` 保持兼容。

## 数据与测试

数据在 AstrBot 插件数据目录中，包含 `reminders.db` 和可覆盖的提醒提示词；运行数据不进入源码仓库。迁移已有任务前应备份数据，保留原提醒标识和发送回执，不重新创建历史任务。

```bash
python -m pip install -r requirements.txt
python test_main.py
```

测试使用内存调度器和平台替身，不创建真实提醒、不发送 QQ 消息。

## 相关项目与许可

[石英核心](https://github.com/cneicy/astrbot_plugin_shrink) 通过插件能力协议发现本插件，不需要在核心里添加提醒专用适配。

AGPL-3.0，见 [LICENSE](LICENSE)。
