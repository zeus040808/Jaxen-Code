# HH Agent

## 项目是什么
从零实现的本地 coding agent harness(Claude Code 的简化版),
跑在终端、作用于本地代码仓库。学习 + 求职作品集项目。
参考架构:learn-claude-code s01–s20 / HH Agent Pro。

## 技术栈
- Python 3.x,OpenAI SDK(走 DeepSeek/通义 OpenAI 兼容接口)

## 运行 / 测试
- 运行:python -m hh_agent
- 测试:pytest
（命令随项目补充）

## 协作约定（重要）
- 这是我的学习项目,目标是我能独立讲清每一行代码。
- 写代码前先简要说思路和设计取舍,别直接甩一大段实现。
- 保持简单,不要过度设计;能 50 行解决的别写成 200 行。
- 涉及核心机制(loop、工具调用、上下文压缩)时多讲原理,当教学。
- 每次改动控制在小范围,方便我逐步 review 和理解。
