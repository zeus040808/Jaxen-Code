"""
hh_agent 入口 — 交互式终端对话

用法: python -m hh_agent
"""

import threading
import time

import hh_agent.config as config
from hh_agent import skills_loader
from hh_agent.cron_scheduler import has_cron_queue, start_scheduler
from hh_agent.loop import (agent_loop, build_main_system, build_sub_system,
                           get_main_system)

skills_loader._scan_skills()                 # ① 扫技能
config.SYSTEM_PROMPT = build_main_system()   # ② 装配主提示词(此时技能目录已就绪)
config.SUB_SYSTEM    = build_sub_system()

def main():
    messages = [{"role": "system", "content": get_main_system()}]
    agent_lock = threading.Lock()

    def run_turn(user_input: str | None = None) -> None:
        nonlocal messages
        if user_input is not None:
            messages.append({"role": "user", "content": user_input})
        messages = agent_loop(messages)
        if messages[-1]["role"] == "assistant":
            print(f"\n{messages[-1]['content']}\n")

    def queue_processor_loop() -> None:
        while True:
            time.sleep(0.2)
            # blocking=False 表示：
            # Agent 忙碌 → 不等待，稍后再试
            # Agent 空闲 → 立即获得锁
            if not has_cron_queue() or not agent_lock.acquire(blocking=False):
                continue
            try:
                if has_cron_queue():
                    print("\n[cron] 正在交付定时任务")
                    # 获得锁后
                    run_turn()
            finally:
                agent_lock.release()

    start_scheduler()
    threading.Thread(target=queue_processor_loop, daemon=True).start()
    print(" hh_agent 已启动，输入对话（输入 /exit 退出）\n")

    while True:
        try:
            user_input = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n👋 退出")
            break

        if not user_input:
            continue
        if user_input == "/exit":
            print("👋 退出")
            break

        with agent_lock:
            run_turn(user_input)


if __name__ == "__main__":
    main()
