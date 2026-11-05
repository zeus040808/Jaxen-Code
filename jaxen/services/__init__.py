"""工具和 CLI 背后的长期服务，三者互相独立：

    background_jobs  后台命令：run_shell(background=true) 的慢命令，跑完通知 agent
    backlog          长任务待办：跨会话保留，只在 full 工具集里给模型用
    scheduler        定时提示：用户在 CLI 里 /schedule，到点由 CLI 交给 agent.ask()，模型不可见

当前请求内的进度和计划（短任务）不在这里，在 progress.py。
"""
