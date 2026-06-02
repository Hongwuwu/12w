# -*- coding: utf-8 -*-
"""
DeepSeek Local MQTT Controller — 统一入口

用法：
    python main.py                 自动控制器 + GUI 图形界面（推荐）
    python main.py auto            仅启动自动控制器
    python main.py chat            仅启动命令行终端
    python main.py gui             仅启动 GUI 图形界面

所有组件通过 controller_state.json 共享全局运行模式：
- auto 模式：自动控制器运行，GUI/终端只读
- manual 模式：自动控制器休眠，GUI/终端可发送控制命令
"""

import sys
import threading

from common import log


def run_auto():
    """在独立线程中运行自动控制器。"""
    import auto_controller
    auto_controller.main()


def run_chat():
    """在主线程中运行命令行终端。"""
    import chat_terminal
    chat_terminal.main()


def run_gui():
    """在主线程中运行 PySide6 图形界面。"""
    try:
        import chat_gui
        chat_gui.main()
    except ImportError as e:
        log(f"GUI 启动失败：{e}")
        log("请先安装 PySide6：pip install PySide6==6.7.3")
        sys.exit(1)


def main():
    if len(sys.argv) > 1:
        cmd = sys.argv[1].lower()
        if cmd in ("auto", "controller"):
            run_auto()
        elif cmd in ("chat", "terminal"):
            run_chat()
        elif cmd in ("gui", "graphical"):
            run_gui()
        else:
            print(f"用法: python main.py [auto|chat|gui]")
            print(f"      python main.py          (自动控制器 + GUI)")
            sys.exit(1)
    else:
        # 默认：自动控制器（后台线程）+ GUI（前台窗口）
        log("DeepSeek Local MQTT Controller 启动中…")
        log(f"自动控制器：后台线程  |  GUI：前台窗口")
        log("关闭 GUI 窗口即可退出全部组件。")
        log("")

        auto_thread = threading.Thread(target=run_auto, daemon=True)
        auto_thread.start()

        run_gui()
        log("所有组件已退出。")


if __name__ == "__main__":
    main()
