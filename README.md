# astrbot_plugin_member_notice

群成员入群/退群通知插件，为 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 设计。

- 新成员入群时发送**自定义欢迎语**（可 @ 新成员）
- 新成员入群时**自动禁言**（可开关、可设时长、可按群覆盖、可发独立提示）
- 成员退群时发送**自定义退群语**
- 支持**黑名单 / 白名单**，精准控制生效的群
- 欢迎语、退群语均支持 `{nickname}`、`{user_id}`、`{group_id}` 占位符
- 全量 WebUI 可视化配置，改完即生效，无需重启

## ✨ 核心特性：不会被其他欢迎插件屏蔽

市面上很多群欢迎插件在发送完欢迎消息后，会调用 `event.stop_event()` 来阻止事件继续向后传递，导致**后注册的欢迎插件收不到事件、永远发不出消息**。

本插件通过以下设计保证**即使识别到其他欢迎插件已经发送了欢迎/退群消息，本插件也依旧照常发送**：

1. **高优先级（`priority=999`）**：handler 按 priority 倒序执行，本插件几乎总是最先拿到事件，在别的插件调用 `stop_event()` 之前就已经完成了发送。
2. **主动发送，不依赖事件结果链路**：本插件直接调用 `context.send_message()` 主动把消息发出去，而不是靠 `yield` 事件结果返回，因此天然免疫其他插件的结果覆盖。
3. **不调用 `stop_event()`**：本插件处理完后**不会**中断事件，事件会继续传递给后面的所有插件。也就是说——**本插件不会影响其他插件的正常执行**，你的 LLM 对话、指令、其他功能插件该怎么跑还怎么跑。

> 简单说：**抢在别人屏蔽之前发出去，自己又不屏蔽别人。**

## 📦 安装

1. 将本仓库克隆到 AstrBot 插件目录（`data/plugins/` 下）：

```bash
git clone https://github.com/<你的用户名>/astrbot_plugin_member_notice.git
```

或者手动下载 zip 解压，把 `astrbot_plugin_member_notice` 文件夹放进 `data/plugins/`。

2. 在 AstrBot 管理面板 → 插件管理 → 刷新插件列表，找到「入群退群欢迎」并启用。

## ⚙️ 配置

在 AstrBot WebUI 的插件配置页即可可视化编辑（`_conf_schema.json` 自动生成）：

| 配置项 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `enable` | bool | `true` | 插件总开关 |
| `welcome_enable` | bool | `true` | 启用入群欢迎 |
| `leave_enable` | bool | `true` | 启用退群提示 |
| `welcome_msg` | text | `欢迎 {nickname} 加入本群！` | 入群欢迎语模板 |
| `leave_msg` | text | `{nickname} 离开了本群。` | 退群提示语模板 |
| `at_new_member` | bool | `true` | 欢迎时是否 @ 新成员 |
| `mute_enable` | bool | `false` | 新成员入群自动禁言总开关 |
| `mute_duration` | int | `600` | 默认禁言时长（秒），有效范围 1 ~ 2592000 |
| `mute_per_group` | template_list | `[]` | 分群禁言时长，每项两栏：群号 + 秒数；没写的群用默认值 |
| `mute_notice_msg` | text | 见下 | 禁言成功后的提示语，**留空则不发提示** |
| `mute_skip_users` | list | `[]` | 免禁言 QQ 名单 |
| `mute_skip_invite` | bool | `true` | 群主/管理员邀请入群时不禁言 |
| `blacklist_groups` | list | `[]` | 黑名单群号列表（黑名单内的群不生效，优先于白名单） |
| `whitelist_groups` | list | `[]` | 白名单群号列表（仅白名单内的群生效；留空则全部生效） |

### 占位符

| 占位符 | 含义 |
|---|---|
| `{nickname}` | 成员昵称（入群时尽力获取群名片，退群时尽力获取陌生人资料，失败则回退 QQ 号） |
| `{user_id}` | 成员 QQ 号 |
| `{group_id}` | 群号 |

## 🔇 入群自动禁言

打开「新成员入群自动禁言」后，新成员进群会被自动禁言，到点由协议端自动解禁（不需要插件常驻计时）。

- **时长**：默认用「默认禁言时长（秒）」，想给某个群单独设置，就在「分群禁言时长」里加一项填群号和秒数。
- **提示**：禁言成功后单独发一条「禁言提示语」，可用 `{nickname}`、`{user_id}`、`{group_id}`、`{duration}`；其中 `{duration}` 会自动变成 `10 分钟`、`1 小时`、`1 天` 这种可读文本。把这条文案清空就只禁言、不发提示。
- **不禁言的情况**：机器人自己被拉进群、QQ 号在「免禁言 QQ 名单」里、以及（默认开启的）由群主/管理员**邀请**进群的成员。
- **权限要求**：机器人在该群必须是**管理员或群主**，否则禁言会被协议端拒绝。失败时插件只往日志里写一条 warning，群里不发提示，也不影响欢迎语和其它插件。
- **超限处理**：秒数小于 1 或填了非数字时回退到默认时长；超过 2592000 秒（30 天）按 30 天算。

## 📝 黑白名单规则

- **黑名单优先**：群在黑名单内 → 直接不生效。
- 白名单非空时：仅白名单内的群生效。
- 白名单为空时：所有群生效（纯黑名单模式）。

## 🔧 原理（给开发者）

本插件通过 `@event_message_type(EventMessageType.GROUP_MESSAGE, priority=999)` 监听事件。OneBot V11 的群通知事件（`post_type=notice`）会被 aiocqhttp 适配器转换为 `GROUP_MESSAGE` 类型的消息事件，`raw_message` 中保留了原始的 `notice_type`（`group_increase` / `group_decrease`）、`group_id`、`user_id` 等字段，据此区分入群与退群。

发送消息走 `context.send_message(unified_msg_origin, MessageChain(...))` 主动发送链路，完全不依赖事件结果（`yield`）返回，因此**即使其他插件先执行并调用 `stop_event()`，本插件的消息也已经发出去了**；同时本插件自身不调用 `stop_event()`，**不影响任何其他插件的执行**。

内置 10 秒去重，防止多端/重复上报造成重复欢迎。

## 📄 License

MIT
