# joLink 内测与快速开始指南

joLink 是一套面向 coding agent 的 Runtime evidence 能力。当前内测重点是
Java Runtime：让模型通过真实 JVM 执行路径、异常、调用栈和变量状态理解代码、
定位问题并验证行为。

这份文档也写给第一次使用 Hermes/joLink 的内测成员。目标不是一次配完所有功能，
而是先完成模型配置、进入 Java 项目目录，并确认 Runtime 可以被模型调用。

当前为小范围 Alpha 内测版本。请不要把它用于无人值守的生产操作，也不要在反馈中
提交访问令牌、密码、完整生产日志或业务敏感数据。

## 1. 环境准备

- Windows 10/11、macOS 或 Linux
- 能访问 GitHub 和 Python 包源
- 需要 JDK 8 或更高版本
- 当前已实际验证 JDK 8 和 JDK 17；其他版本欢迎在内测中反馈
- 一个支持 tool calling 的模型及其可用账号或 API key
- 用于验证的本地 Java 项目、可执行 JAR，或已启用本地 JDWP 的 Java 进程

先确认 Java：

```bash
java -version
```

## 2. 安装

### Windows PowerShell

```powershell
irm https://raw.githubusercontent.com/L1ch404/hermes-agent/main/scripts/install.ps1 | iex
```

### macOS / Linux

```bash
curl -fsSL https://raw.githubusercontent.com/L1ch404/hermes-agent/main/scripts/install.sh | bash
```

安装器会完成 Python、虚拟环境和依赖安装。joLink 目前保留兼容命令 `hermes`
以及原有数据目录。

## 3. 首次配置怎么选

安装完成后运行：

```bash
hermes
```

首次启动会进入配置向导。根据自己的模型账号选择一条路径：

- 有 Nous Portal 账号，或希望用 OAuth 快速登录：选择
  `Quick Setup (Nous Portal)`。这是配置最少的方式(不推荐)。
- 已有 DeepSeek、OpenRouter、OpenAI、Anthropic 等服务的 API Key：选择
  `Full setup`，在 `Model & Provider` 中配置自己的模型服务。
- 第一次做 Runtime dogfood 时不要选择 `Blank Slate`，它会关闭大部分插件和工具。

如果选择 Full setup，建议只完成下面这些必要配置：

1. `Model & Provider`：选择一个支持 tool calling 的模型并填写凭证。
2. `Terminal Backend`：在自己的电脑上使用时选择本地终端。
3. Messaging/Gateway、语音和其他可选工具暂时跳过，需要时再配置。

不需要在第一次启动时把所有选项都弄明白。以后可以单独重跑某一段配置：

```bash
hermes setup model
hermes setup terminal
hermes setup tools
```

如果已有配置，只想补齐缺失项：

```bash
hermes setup --quick
```

## 4. 从 Java 项目目录启动

先进入准备测试的 Java 项目，再启动 joLink。这样模型能直接读取当前项目代码：

```bash
cd /path/to/your-java-project
hermes
```

Windows PowerShell 示例：

```powershell
cd C:\work\your-java-project
hermes
```

启动页应显示：

```text
joLink
Runtime evidence for coding agents
```

## 5. 第一次验证 Runtime

先发一个只读请求，不启动或停止任何 Java 进程：

```text
调用 Java Runtime 的 status，确认当前 Runtime 状态。不要启动或停止任何 Java 进程。
```

正常情况下，模型会调用 `java_runtime`，并返回 Runtime 当前是否运行、是否已连接 JVM
等状态。模型也可以按需加载 `java-runtime:observation` skill。

接下来可以让模型先阅读项目，再设计安全的测试步骤：

```text
先阅读这个 Java 项目的启动方式和主要接口，告诉我如何用 Java Runtime 做一次最小验证。
先不要启动进程，也不要修改代码。
```

需要实际定位问题时，请把现象、启动方式和可触发问题的请求告诉模型：

```text
这个项目通过 java -jar target/app.jar 启动。调用 GET /api/users/1 会返回 500。
请先读相关代码，再用 Java Runtime 观察真实执行路径和异常。写操作由我手动触发；
每次读取完暂停线程后都要 resume，最后告诉我证据和结论。
```

首次完整验证建议包含：

1. 一次 `run` 或 `attach`
2. 一个行断点命中、读取 `stack`/`variables` 并 `resume`
3. 一个具体异常事件，例如 `NullPointerException`
4. 最后 `stop` Runtime 启动的进程，或 `detach` 外部进程

Runtime 会暂停真实 JVM 线程。现有进程只有在本机 JDWP 端口已经开启时才能
attach。不要把 JDWP 端口暴露到公网或不可信网络，也不要在没有确认的情况下让
模型触发生产写操作或不可逆副作用。

## 6. 两类命令不要混淆

`hermes ...` 是在 PowerShell、Terminal 或命令提示符中执行的终端命令；
`/...` 是启动 joLink 后，在聊天输入框中执行的会话命令。

### 常用终端命令

| 命令 | 用途 |
| --- | --- |
| `hermes` | 从当前目录启动 joLink |
| `hermes setup` | 重新运行完整配置向导 |
| `hermes setup model` | 重新配置模型和 Provider |
| `hermes model` | 在终端中选择默认模型 |
| `hermes tools --summary` | 查看各平台已启用的工具摘要 |
| `hermes tools list --platform cli` | 查看 CLI 工具的启用状态 |
| `hermes config path` | 查看当前配置文件位置 |
| `hermes logs` | 查看最近的 joLink/Hermes 日志 |
| `hermes logs -f` | 持续观察日志 |
| `hermes version` | 查看当前版本和 commit |
| `hermes update --backup` | 备份后更新到最新内测版本 |

### 常用会话命令

| 命令 | 用途 |
| --- | --- |
| `/help` | 查看当前环境支持的命令 |
| `/new` | 开始一个全新的会话 |
| `/status` | 查看当前会话、模型和上下文状态 |
| `/model` | 切换当前模型 |
| `/retry` | 重试上一条消息 |
| `/undo` | 撤回一个用户回合并重新输入 |
| `/compress` | 压缩较长的对话上下文 |
| `/usage` | 查看当前会话的 token 使用情况 |
| `/sessions` | 浏览之前的会话 |
| `/resume` | 恢复一个已命名的会话 |
| `/tools` | 在 CLI 中查看或管理工具 |
| `/plugins` | 查看已安装插件及其状态 |
| `/stop` | 停止 joLink 启动的后台任务 |
| `/quit` | 退出 joLink CLI |

以 `/help` 的实际输出为准；不同界面或平台可用的命令可能略有不同。

## 7. 更新

内测更新发布到 joLink 仓库的 `main` 分支：

```bash
hermes update --backup
```

更新前先退出正在运行的 joLink CLI、TUI 或桌面应用，Windows 上尤其不要让其他
Hermes/joLink 进程占用虚拟环境中的可执行文件。

也可以重新执行安装命令；安装器检测到已有 joLink 源码后会拉取更新并重新安装
依赖，不会主动删除配置和会话。如果安装目录原本来自官方 Hermes，joLink 安装器
会把该代码仓库的 `origin` 切换到 joLink fork，但仍保留原有配置和数据目录。

更新后请重新启动 joLink，并开启新会话，让工具和 skill 重新发现。

## 8. 常见问题

### 找不到 `hermes` 命令

安装完成后关闭并重新打开 PowerShell/Terminal，再运行 `hermes`。如果仍然找不到，
重新执行安装命令，并保留安装器最后的报错信息。

### 模型无法调用工具

先确认使用的模型支持 tool calling，然后退出 joLink，在终端检查：

```bash
hermes tools --summary
hermes tools list --platform cli
```

如果 Runtime 被关闭，可以运行交互式工具配置：

```bash
hermes setup tools
```

修改工具或插件配置后，重新启动 joLink 并使用 `/new` 开启新会话，让工具和 skill
重新发现。

### 模型或 API Key 报错

重新配置模型：

```bash
hermes setup model
```

仍有问题时查看：

```bash
hermes logs errors
hermes logs --level WARNING
```

### Java Runtime 连接失败

先告诉模型只调用 Runtime `status`，再根据返回的 `error_code`、`warnings` 和
`suggested_next_step` 处理。`attach` 只能连接已经开启 JDWP 的本机 JVM；Runtime
无法从 attach 的外部进程中读取该进程原有的 stdout/stderr 日志。

## 9. 问题反馈

请提供：

```text
joLink 版本 / commit：
操作系统：
java -version：
Java 启动方式：run / attach
项目类型：Spring Boot JAR / classpath / 其他
执行的 Runtime action：
返回的 error_code、warnings、suggested_next_step：
是否存在未 resume 的 suspension：
最小复现步骤：
```

辅助信息：

```bash
hermes version
hermes logs --level INFO
```

日志提交前请删除令牌、密码、业务数据、用户信息和其他敏感内容。对于 attach 的
外部 JVM，`java_runtime(action="logs")` 不包含该 JVM 的 stdout/stderr，不能把
空日志当作应用没有输出的证据。

## 10. 已知边界

- 当前仅连接本机可达的 IPv4 JDWP endpoint
- Runtime 观察只证明当前输入、当前线程和当前运行状态发生的行为
- `run` 产生的应用日志可由 Runtime 读取；`attach` 的外部应用日志不会被捕获
- 断点依赖运行中 class 的可执行行号和调试信息，源码必须与实际 bytecode 匹配
- 异常 class 尚未加载时，需要先触发相关代码路径，再重试异常事件注册
- 生产写操作、不可逆操作或外部副作用应由测试者明确确认或手动触发
