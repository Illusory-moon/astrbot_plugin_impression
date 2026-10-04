# 个人印象

独立的 AstrBot 插件。默认关闭；开启后也只处理 `enabled_self_ids` 中明确列出的 bot。每个 bot 对每位发言者的印象单独保存在插件数据目录的 `states.json`，不会写进人格仓库或 mindscape 的记忆文件。

安装到 AstrBot 的 `data/plugins/`，重启后在插件配置中开启 `enabled`，并将目标 bot 的 `self_id` 填入 `enabled_self_ids`。留空时全部关闭。

插件只在正常 LLM 回复轮读取当前发言者的短印象。bot 在回复末尾附带私有判断标签；没有值得记的变化时标签写空值，不更新文件。标签在发送前从回复中移除。插件不额外调用 LLM，但每轮会多消耗少量输出 token；未进入回复链路的群消息和自主冒泡轮不处理。

没有某人的记录时，bot 仍会正常回复，并能在后续有意义的互动中自行写入第一条记录。可见回复应自然体现态度，不主动提及“印象”“好感”或内部记录；这是提示词约束，具体措辞仍由模型决定。

可与 [bot-mindscape](https://github.com/Illusory-moon/bot-mindscape) 并用，两者没有代码或数据依赖。需要在同一容器服务多个 bot 时，先开启 `enabled`，再只把目标 bot 的 `self_id` 填入 `enabled_self_ids`；空白名单不处理任何 bot。

状态用 `{"version": 1, "bots": {bot_id: {user_id: {impression, reason, updated_at}}}}` 组织。手动修改前请先备份 `states.json`；格式错误时插件会保留原文件并记录警告。
