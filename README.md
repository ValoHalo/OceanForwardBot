# Telegram Forward Bot

将指定 Owner 私聊发送给 Bot 的消息，转发到 Telegram 群组、频道或论坛的特定话题的 Bot。自用，给多个群组发图片。

## 运行要求

- Windows 10/11；仓库自带的 `start.bat` 默认使用 Python 3.12。
- Python 3.10 或更高版本，并安装 Windows Python Launcher（`py.exe`）。
- 依赖使用 `python-telegram-bot` 22.x，具体兼容范围见 `requirements.txt`。
- 一个通过 [@BotFather](https://t.me/BotFather) 创建的 Bot，以及 Owner、目标 Chat 和可选 Topic 的 ID。

## 快速开始

1. 双击或在终端中运行：

   ```bat
   start.bat
   ```
2. 首次运行会把 `example.config.json` **复制**为本地的 `config.json`，随后停止。编辑新生成的 `config.json`，至少填写 Bot Token、Owner ID 和一个转发目标。请保留模板文件，后续才能正常通过 Git 更新仓库。
3. 再次运行 `start.bat`。启动器会创建 `.venv`、安装或检查依赖、执行本地配置校验，然后启动 Bot 并在进程崩溃时自动重启。
4. 按 `Ctrl+C` 停止程序。

启动器始终先切换到自身目录，因此可以从资源管理器或其他工作目录运行。任一准备步骤失败时，它都会返回非零退出码。

## Telegram 侧准备

- 将 Bot 加入每一个目标 Chat。普通群组通常只需允许 Bot 发送相应类型的消息；只有群组限制、频道发帖或话题管理确实要求时才需要授予管理员权限。
- 频道目标需要允许 Bot 发布消息；论坛话题必须存在、未关闭，并允许 Bot 在其中发言。
- `Chat not found` 通常表示 ID 错误、Bot 尚未加入目标，或 Bot 无权访问该 Chat，并不代表必须无条件授予管理员权限。
- 群组和超级群组 ID 通常为负数，超级群组经常以 `-100` 开头。Topic ID 对应 Telegram 的 `message_thread_id`。

## 配置

`config.json` 必须是标准 JSON：不能包含注释、尾随逗号或重复字段。建议直接基于仓库中的 `example.config.json` 修改。真实配置已被 `.gitignore` 排除；它包含 Token，仍应只保存在可信设备上。

### Bot

| 字段                  | 含义                                                                                                                                              |
| --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| `token`             | @BotFather 签发的 Bot Token。                                                                                                                     |
| `owner_id`          | 唯一允许触发转发的 Telegram 用户数字 ID。                                                                                                         |
| `proxy_url`         | 可选 `http`、`https`、`socks5` 或 `socks5h` 代理，例如 `http://127.0.0.1:7890` 或 `socks5://127.0.0.1:1080`；不用代理时填写空字符串。 |
| `silent_forwarding` | 全局静默发送开关。目标规则可以覆盖它。                                                                                                            |
| `log_level`         | `DEBUG`、`INFO`、`WARNING`、`ERROR` 或 `CRITICAL`。日常使用建议 `INFO`。                                                              |

代理会同时用于普通 Bot API 请求和 `getUpdates` 长轮询。若代理 URL 含用户名或密码，请对特殊字符进行 URL 编码，并妥善保护 `config.json`。

### 转发目标

`destinations` 是目标规则数组。每条规则包含：

| 字段                  | 含义                                                                                     |
| --------------------- | ---------------------------------------------------------------------------------------- |
| `chat_id`           | 目标群组、超级群组或频道的非零数字 ID；Telegram API 支持的非空字符串 Chat 标识也可使用。 |
| `topic_ids`         | 目标 Topic ID 数组。空数组表示不指定话题；多个 ID 会分别发送一份。                       |
| `silent_forwarding` | 可选；只覆盖当前目标的全局静默设置。                                                     |

### 网络与发送

| 字段                     |   默认值 | 含义                                                        |
| ------------------------ | -------: | ----------------------------------------------------------- |
| `connection_pool_size` |   `32` | 普通 Bot API 请求的连接池大小；轮询使用独立的单连接请求器。 |
| `connect_timeout`      | `10.0` | 建立连接和 TLS 握手的超时时间（秒）。                       |
| `read_timeout`         | `20.0` | 网络读取超时时间（秒）。                                    |
| `write_timeout`        | `20.0` | 普通请求写入超时时间（秒）。                                |
| `media_write_timeout`  | `60.0` | 上传媒体时的写入超时时间（秒）。                            |
| `pool_timeout`         | `10.0` | 等待连接池空闲连接的超时时间（秒）。                        |
| `max_concurrent_sends` |    `4` | 同时向不同目标发送的任务上限。                              |
| `max_retries`          |    `3` | 瞬时网络错误或限流时，在首次尝试之外的最大重试次数。        |
| `retry_base_delay_s`   |  `1.0` | 指数退避的基础延迟（秒）。                                  |
| `media_group_wait_s`   |  `2.0` | 等待同一相册其余项目到达的聚合窗口（秒）。                  |

不要仅为追求吞吐量而把连接池或并发数设得很大；Telegram 限流通常会先成为瓶颈。

### 消息处理

`message_processing.url_rewrites` 可以按顺序配置独立 URL 改写规则，第一条命中的规则生效。省略该配置或把规则设为 `enabled: false` 时不会改写消息。域名字段只填写主机名，不带 `http://`、`https://`、端口或路径；来源消息使用 HTTP 或 HTTPS 都按相同域名匹配。

模板内置的 `fxtwitter` 规则只处理整条消息仅包含一个 X/Twitter 帖子 URL 的纯文本消息，包括 `/用户/status/数字`、`/i/status/数字` 及 `/photo/1`、`/video/1` 等后续路径。它将输出统一改为 `https://fxtwitter.com/...`，并移除查询参数和 fragment。正文夹带其他文字、主页或搜索链接、媒体 caption 和相册均不会命中。

| 字段               | 含义                                                                  |
| ------------------ | --------------------------------------------------------------------- |
| `name`           | 规则名称；忽略大小写后不得重复。                                      |
| `enabled`        | 可选，默认为 `true`。                                               |
| `source_hosts`   | 非空来源域名数组；匹配时忽略域名大小写和输入 URL 的 HTTP/HTTPS 协议。 |
| `target_host`    | 改写后的目标域名。输出统一使用 HTTPS。                                |
| `path_pattern`   | 对 URL path 执行完整匹配的正则表达式。                                |
| `strip_query`    | 是否移除 `?` 查询参数，默认为 `false`。                           |
| `strip_fragment` | 是否移除 `#` fragment，默认为 `false`。                           |

### 崩溃后自动重启

`start.bat` 通过 `watchdog.py` 启动 Bot。它只检查进程是否退出，不写入或检查心跳文件，也不执行轮询假死探测。运行期间的临时断网由 Telegram 客户端自动重试；进程仍在运行但无法响应时，需要手动停止并重新启动。

`watchdog` 配置段可以省略，以下为默认值：

| 字段                         |  默认值 | 含义                                                         |
| ---------------------------- | ------: | ------------------------------------------------------------ |
| `restart_delay_s`          |   `5` | 异常退出后的重启等待时间（秒）。                             |
| `max_consecutive_restarts` |   `5` | 初始运行之外允许的连续自动重启次数；`0` 表示失败后不重启。 |
| `stable_reset_s`           | `600` | 单次进程持续运行达到该时长后，清零连续重启计数；必须大于 0。 |

正常退出、按 `Ctrl+C` 停止、配置错误或检测到重复实例时不会自动重启。连续崩溃达到上限后，启动器会停止并保留错误日志。直接运行 `main.py` 时不启用自动重启。

旧配置中的 `heartbeat_file`、`heartbeat_interval_s`、`heartbeat_timeout_s`、`startup_grace_s`，以及 `network` 下的 `polling_health_check_interval_s`、`polling_stale_timeout_s` 已不再使用，可以删除，也可以暂时保留。旧心跳文件不会再被读取、更新或清理。

需要手动验证 Token、代理和两条请求配置时，请使用下面的联网检查。

## 本地与联网检查

在依赖安装完成后，可以运行：

```powershell
# 只解析并校验 config.json，不联网
.\.venv\Scripts\python.exe main.py --check-config

# 分别使用普通请求配置和轮询请求配置调用 getMe，不读取或消费更新
.\.venv\Scripts\python.exe main.py --check
```

`start.bat` 会自动执行第一项检查；第二项验证两套网络配置的基本连通性，适合排查无效 Token 或代理不可达，但不等价于正在运行的 `getUpdates` 循环健康。不要同时运行多个使用同一 Token 的长轮询实例，否则 Telegram 会返回冲突错误。

## 转发规则

- 只处理 `owner_id` 对应用户发给 Bot 的私聊消息；其他用户和非私聊消息会被忽略。
- Owner 可以在与 Bot 的私聊中发送 `/ping`；Bot 回复“Bot 在线。”即表示消息处理循环正常工作，该指令不会进入草稿或转发流程。
- Bot 命令以及文本以 `/` 开头的消息不会转发。
- 普通消息和相册不会立即发布。Bot 回复“消息待发送。”或“相册（N 项）待发送。”，并提供“确认发送”“取消”两个按钮。
- 链接命中改写规则时，Bot 会发送改写后的 URL 作为预览，在预览下提供“确认发送”“取消”和“发送原文”。
- “确认发送”发送处理后的预览；没有命中消息处理器时发送原消息。“发送原文”绕过处理器并发送原消息。
- 发送结束后，操作消息更新为“已发送 成功数/目标总数 个目标”，清除待发送提示；只有实际发送了改写预览时才保留预览内容。按钮替换为单个“✅已发送”状态按钮，再次点击不会重复发送。有目标未完整发送时显示“⚠️发送未完成”。取消则不会访问任何发送目标。
- 普通消息使用 Telegram 的复制接口发送，因此目标消息不显示原始转发来源。
- 相册以最后一个项目到达为起点做短暂 debounce，随后对消息 ID 排序、去重并调用 Telegram 的批量复制接口，因此由 Telegram 原生保留相册分组。
- 一个目标配置多个 Topic 时，每个 Topic 都会收到一份副本。
- 重复配置的同一 `chat_id` + Topic 组合只发送一次；若这些重复规则的静默设置冲突，配置校验会拒绝启动。
- 多目标发送受并发上限控制。`RetryAfter`、`TimedOut` 和其他瞬时 `NetworkError` 会按配置重试；明确的 `BadRequest`、`Forbidden` 等永久错误不会重试。
- 重试提供的是 **at-least-once** 发送语义：网络超时可能发生在 Telegram 已接收请求之后，因此极少数情况下目标会收到重复消息。单个目标最终失败会记录日志，但不会撤回其他目标已经成功收到的消息。
- Telegram 批量复制若静默跳过不可复制的相册项，程序会把该目标标记为“部分成功”且不盲目重试，避免已复制项目再次重复。
- 待确认消息和待发送任务都只保存在内存中。进程重启后，旧按钮会失效；消息正在发送时被强制结束，待发内容也不会自动恢复。
- Bot 主进程和看门狗都使用 Windows 文件锁限制为单实例；重复启动会直接退出，不会创建第二个轮询进程。

## 手动安装

不使用启动脚本时，可在 PowerShell 中执行：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item example.config.json config.json
# 编辑 config.json 后：
.\.venv\Scripts\python.exe main.py --check-config
.\.venv\Scripts\python.exe watchdog.py
```

如果 `config.json` 已存在，不要再次执行 `Copy-Item` 覆盖真实配置。

## 常见问题

- **启动器提示找不到 Python 3.12**：安装 64 位 Python 3.12 并启用 Python Launcher，或在确认兼容后修改 `start.bat` 中的 `PYTHON_VERSION`。
- **Bot 能启动但收不到消息**：确认消息发在与 Bot 的私聊中且发送者 ID 等于 `owner_id`，再运行 `main.py --check` 检查轮询代理通道。
- **网络暂时中断**：客户端会自动重试，终端每分钟最多显示一次简短连接提示。需要异常详情时，将 `bot.log_level` 设为 `DEBUG` 后重新启动。
- **发送到某个目标失败**：检查负数 Chat ID、Topic ID、Bot 成员身份和发送媒体的权限，并查看终端中的具体 Telegram 错误。
- **出现 `Conflict`**：停止其他使用同一 Token 进行 polling 的进程或服务。
- **修改配置未生效**：配置只在进程启动时读取；停止并重新启动 Bot。

## 项目文件

- `main.py`：配置校验、Telegram 长轮询、消息过滤、相册聚合和广播发送。
- `watchdog.py`：启动 Bot 子进程，仅在异常退出时按次数上限重启。
- `start.bat`：Windows 环境准备、依赖检查、配置校验和启动入口。
- `example.config.json`：不含真实凭据的合法 JSON 配置模板。

## 待办

- [X] 配置文件独立于程序
- [X] 支持媒体组
- [X] 进程崩溃后自动重启
- [X] 全局及目标级静默发送
- [ ] 可选保留原消息来源
- [X] 可配置链接重写
- [ ] 管理命令

## 致谢

- Gemini
- ChatGPT
