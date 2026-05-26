# -*- coding: utf-8 -*-
"""
Dify Local MQTT Controller - 统一入口

用法：
    python main.py                 同时启动自动控制器 + 聊天终端（两个线程）
    python main.py auto            仅启动自动控制器
    python main.py chat            仅启动聊天终端

两个组件通过 controller_state.json 共享全局运行模式：
- auto 模式：自动控制器运行，聊天终端只读
- manual 模式：自动控制器休眠，聊天终端可发送控制命令
"""

import sys
import threading

from common import log


def run_auto():
    """在独立线程中运行自动控制器。"""
    import auto_controller

    auto_controller.main()


def run_chat():
    """在主线程中运行聊天终端（交互式命令行）。"""
    import chat_terminal

    chat_terminal.main()


def main():
    if len(sys.argv) > 1:
        cmd = sys.argv[1].lower()
        if cmd in ("auto", "controller"):
            run_auto()
        elif cmd in ("chat", "terminal"):
            run_chat()
        else:
            print(f"Usage: python main.py [auto|chat]")
            print(f"       python main.py          (run both)")
            sys.exit(1)
    else:
        log("Starting Dify Local MQTT Controller (both components)...")
        log("Auto controller: background thread")
        log("Chat terminal:   foreground (interactive)")
        log("Press Ctrl+C in the chat terminal to exit both.")
        log("")

        auto_thread = threading.Thread(target=run_auto, daemon=True)
        auto_thread.start()

        run_chat()

        log("Both components exited.")


if __name__ == "__main__":
    main()
