# 💡 ideas.md

> 存放 Jolink 的所有灵感、猜想、未来方向。
>
> 原则：
>
> * 只记录，不立即实现。
> * 当前 Milestone 未完成前，不从这里取需求。
> * 当一个想法经过验证后，再移动到 ROADMAP.md。

---

# P0（当前正在做）

> 当前 Sprint 不记录在这里，而是在 ROADMAP 中维护。

---

# P1（下一阶段）

## [ ] Runtime Observation

描述：

目前 Runtime Tool 还不完整，需要逐步完善 Java Runtime 的观测能力。

想法：

* 更智能的启动状态检测
* 日志自动分析
* Debug Runtime
* Runtime 状态聚合

状态：

* 未开始

---

## [ ] Prompt 优化

目标：

研究 Prompt 是否应该：

* 鼓励 LLM 主动 Observation
* 或完全交由 LLM 自行判断

需要实验验证。

---

## [ ] Tool Lazy Loading

目标：

根据项目类型动态注册 Tool。

例如：

Java 项目：

* Java Runtime Tool

Python 项目：

* Python Runtime Tool

Node 项目：

* Node Runtime Tool

目的：

减少 Context。

---

# P2（未来研究）

## [ ] Runtime Protocol

思考：

是否能够抽象一套 Runtime Observation Protocol。

适用于：

* Java
* Python
* Go
* Node.js

最终成为 Jolink 的协议基础。

---

## [ ] Tool Output Schema

研究：

Tool 是否应该描述 Output。

目前：

OpenAI Function Calling 不支持。

未来观察 MCP 或其它协议的发展。

---

## [ ] Runtime Knowledge Cache

思考：

是否使用本地小模型或 RAG。

负责：

* 收集 Runtime 信息
* 整理上下文
* 减少主模型 Token

---

## [ ] IDEA 可视化

目标：

Agent Debug 时：

能够在 IDEA 中实时展示：

* 当前步骤
* 当前 Tool
* 当前思考
* 当前 Observation

用于提升可观测性和传播效果。

---

## [ ] Self Review

Agent 完成任务后：

自动 Review：

* 是否偏离需求
* 是否修改过多
* 是否遗漏
* 是否存在低级错误

目标：

降低 Requirement Drift。

---

# P3（产品）

## [ ] Java Runtime MVP

目标：

让 Java 开发者能够真正使用 Jolink 完成日常开发。

重点：

不是功能数量。

而是：

每天都愿意打开 Jolink。

---

## [ ] 社区建设

目标：

形成第一批真实用户。

包括：

* GitHub
* Bilibili
* 抖音

---

## [ ] Jolink Protocol

长期目标。

最终抽象 Runtime 与 Agent 之间的协议。

目前：

不要设计。

等待 Runtime 成熟后自然抽象。

---


## [ ] JDWP Runtime

背景：

目前 Python 社区几乎没有成熟的 JDWP Client。

后续研究：

- 是否自己实现部分协议
- 是否封装现有 Java Debug Adapter
- 是否直接使用 MCP

优先级：

P3

---

# 📌 Design Principles

## Everything exists to reduce uncertainty for the LLM.

一切能力都应服务于：

减少 LLM 在完成任务时的不确定性。

---

## Guess less. Observe more.

尽可能减少猜测。

优先获取 Runtime Evidence。

---

## Evidence before reasoning.

先获取证据。

再进行推理。

---

## Keep MVP Small.

先解决一个真实问题。

再扩大能力范围。

---

# Parking Lot

记录突然想到，但目前不会去做的想法。

（想到就写，不展开。）

* [ ]
* [ ]
* [ ]
* [ ]
* [ ]
