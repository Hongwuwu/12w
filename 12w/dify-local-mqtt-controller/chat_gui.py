# -*- coding: utf-8 -*-
"""
DeepSeek PLC AI Chat GUI — PySide6 图形化界面

手机散热风扇控制器，包含：
- 左侧仪表盘：实时温度、风扇状态、连接状态、模式切换、PLC 寄存器
- 右侧对话窗口：AI 助手"小P"聊天、快捷命令按钮、消息输入

用法：
    python chat_gui.py
    python main.py gui
"""

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import paho.mqtt.client as mqtt
from PySide6.QtCore import (
    QObject,
    QThread,
    QTimer,
    Signal,
    Slot,
    Qt,
    QRect,
)
from PySide6.QtGui import QFont, QColor, QPalette, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
    QGridLayout,
)

from common import (
    build_command_payload,
    load_config,
    load_mode_state,
    log,
    mqtt_protocol,
    parse_command_payload,
    parse_input_payload,
    save_mode_state,
)
from chat_terminal import call_deepseek_chat

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

CONFIG_PATH = Path(os.getenv("CHAT_CONFIG", "config.json"))
VERSION = "deepseek-chat-gui-20260602"

DEFAULT_CONFIG = {
    "local_mqtt": {
        "host": "192.168.31.197",
        "port": 1883,
        "username": "",
        "password": "",
        "input_topic": "bistu11/TagValues",
        "command_topic": "bistu11/MQTTSetValueCommand",
        "mqtt_version": "3.1.1",
    },
    "deepseek": {
        "api_key": "PASTE_DEEPSEEK_API_KEY_HERE",
        "api_url": "https://api.deepseek.com/v1/chat/completions",
        "model": "deepseek-chat",
        "timeout_sec": 60,
    },
    "control": {
        "state_file": "controller_state.json",
        "device_sn": "bistu11",
        "process_interval_sec": 2.0,
    },
}

# ---------------------------------------------------------------------------
# QSS 暗色主题
# ---------------------------------------------------------------------------

QSS_THEME = """
/* === 全局 === */
QMainWindow {
    background-color: #0d1117;
}
QWidget {
    color: #c9d1d9;
    font-family: "Microsoft YaHei UI", "Segoe UI", "Helvetica Neue", sans-serif;
    font-size: 13px;
}

/* === 分割线 === */
QSplitter::handle {
    background-color: #21262d;
    width: 2px;
}

/* === 仪表盘面板 === */
#dashboardPanel {
    background-color: #161b22;
    border-right: 1px solid #21262d;
}
#dashboardTitle {
    font-size: 15px;
    font-weight: bold;
    color: #58a6ff;
    padding: 2px 0;
}
#sectionLabel {
    font-size: 11px;
    font-weight: bold;
    color: #8b949e;
    text-transform: uppercase;
    letter-spacing: 1px;
    padding-top: 6px;
}

/* === 温度显示 === */
#tempCard {
    background-color: #1c2333;
    border: 1px solid #30363d;
    border-radius: 14px;
    padding: 12px;
}
#tempValue {
    font-size: 52px;
    font-weight: bold;
}
#tempUnit {
    font-size: 20px;
    font-weight: normal;
    color: #8b949e;
}
#tempSub {
    font-size: 11px;
    color: #8b949e;
    margin-top: 2px;
}

/* === 风扇状态卡片 === */
#fanCard {
    background-color: #1c2333;
    border: 1px solid #30363d;
    border-radius: 14px;
    padding: 12px;
}
#fanStatusLabel {
    font-size: 15px;
    font-weight: bold;
}
#fanDetail {
    font-size: 11px;
    color: #8b949e;
}

/* === 连接状态 === */
#connectionCard {
    background-color: #1c2333;
    border: 1px solid #30363d;
    border-radius: 10px;
    padding: 8px 12px;
}
#connectionDot {
    font-size: 18px;
}
#connectionText {
    font-size: 12px;
    margin-left: 4px;
}

/* === 模式按钮 === */
#modeAutoButton, #modeManualButton {
    background-color: #21262d;
    color: #8b949e;
    border: 2px solid #30363d;
    border-radius: 10px;
    padding: 10px 18px;
    font-size: 13px;
    font-weight: bold;
}
#modeAutoButton:hover, #modeManualButton:hover {
    background-color: #30363d;
    color: #c9d1d9;
}
#modeAutoButton[active="true"] {
    background-color: #1a3a5c;
    border-color: #58a6ff;
    color: #58a6ff;
}
#modeManualButton[active="true"] {
    background-color: #3d1a2e;
    border-color: #f78166;
    color: #f78166;
}

/* === 寄存器表 === */
#registerCard {
    background-color: #1c2333;
    border: 1px solid #30363d;
    border-radius: 10px;
    padding: 10px;
}
#registerName {
    font-family: "Cascadia Code", "Consolas", monospace;
    font-size: 11px;
    color: #8b949e;
}
#registerValue {
    font-family: "Cascadia Code", "Consolas", monospace;
    font-size: 14px;
    font-weight: bold;
    color: #79c0ff;
}

/* === 聊天面板 === */
#chatPanel {
    background-color: #0d1117;
}
#chatTitle {
    font-size: 15px;
    font-weight: bold;
    color: #f78166;
    padding: 2px 0;
}

/* === 聊天记录区域 === */
#chatScrollArea {
    background-color: #0d1117;
    border: none;
}

/* === 聊天气泡 === */
#chatBubbleUser {
    background-color: #1a3a5c;
    border: 1px solid #1f4a78;
    border-radius: 14px;
    padding: 8px 14px;
}
#chatBubbleAI {
    background-color: #1c2333;
    border: 1px solid #30363d;
    border-radius: 14px;
    padding: 8px 14px;
}
#bubbleText {
    font-size: 13px;
    color: #c9d1d9;
}
#bubbleMeta {
    font-size: 10px;
    color: #8b949e;
    margin-top: 4px;
}

/* === 系统消息 === */
#systemBubble {
    background-color: transparent;
    border: none;
}
#systemBubble QLabel {
    font-size: 11px;
    color: #6e7681;
}

/* === 快捷命令按钮 === */
#quickCmdButton {
    background-color: #21262d;
    color: #8b949e;
    border: 1px solid #30363d;
    border-radius: 14px;
    padding: 5px 14px;
    font-size: 11px;
}
#quickCmdButton:hover {
    background-color: #30363d;
    color: #c9d1d9;
    border-color: #58a6ff;
}

/* === 输入区 === */
#chatInput {
    background-color: #1c2333;
    border: 2px solid #30363d;
    border-radius: 10px;
    padding: 10px 14px;
    font-size: 13px;
    color: #c9d1d9;
}
#chatInput:focus {
    border-color: #f78166;
}

/* === 发送按钮 === */
#sendButton {
    background-color: #f78166;
    color: #0d1117;
    border: none;
    border-radius: 10px;
    padding: 10px 24px;
    font-size: 13px;
    font-weight: bold;
}
#sendButton:hover {
    background-color: #f7997e;
}
#sendButton:pressed {
    background-color: #d96a52;
}

/* === 滚动条 === */
QScrollBar:vertical {
    background-color: #0d1117;
    width: 8px;
    border-radius: 4px;
}
QScrollBar::handle:vertical {
    background-color: #30363d;
    border-radius: 4px;
    min-height: 30px;
}
QScrollBar::handle:vertical:hover {
    background-color: #484f58;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}
"""

# ---------------------------------------------------------------------------
# SignalEmitter — paho MQTT 回调 → Qt 信号 的线程安全桥梁
# ---------------------------------------------------------------------------

class SignalEmitter(QObject):
    connected = Signal()
    disconnected = Signal()
    data_received = Signal(object, object, object, object)  # mw0, mw20, mw21, mw22


# ---------------------------------------------------------------------------
# DeepSeekWorker — 在独立线程中调用 DeepSeek API，不冻结 GUI
# ---------------------------------------------------------------------------

class DeepSeekWorker(QObject):
    finished = Signal(dict)
    error = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._config = None
        self._mw0 = None
        self._mw20 = None
        self._mw21 = None
        self._mw22 = None
        self._mode = "auto"
        self._query = ""

    @Slot()
    def do_request(self):
        try:
            result = call_deepseek_chat(
                self._config,
                self._mw0,
                self._query,
                self._mw20 or 0,
                self._mw21 or 0,
                self._mw22 or 0,
                self._mode,
            )
            self.finished.emit(result)
        except Exception as exc:
            self.error.emit(str(exc))

    def set_request(self, config, mw0, query, mw20, mw21, mw22, mode):
        self._config = config
        self._mw0 = mw0
        self._query = query
        self._mw20 = mw20
        self._mw21 = mw21
        self._mw22 = mw22
        self._mode = mode


# ---------------------------------------------------------------------------
# ChatBubble — 单条聊天消息气泡
# ---------------------------------------------------------------------------

class ChatBubble(QFrame):
    def __init__(self, text, is_user=True, parent=None):
        super().__init__(parent)
        self.setObjectName("chatBubbleUser" if is_user else "chatBubbleAI")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)

        now = datetime.now().strftime("%H:%M:%S")

        inner = QVBoxLayout()
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(2)

        text_label = QLabel(text)
        text_label.setWordWrap(True)
        text_label.setMaximumWidth(420)
        text_label.setObjectName("bubbleText")
        inner.addWidget(text_label)

        meta = QLabel(f"{'你' if is_user else '小P'} · {now}")
        meta.setObjectName("bubbleMeta")
        inner.addWidget(meta)

        if is_user:
            layout.addStretch()
            layout.addLayout(inner)
        else:
            layout.addLayout(inner)
            layout.addStretch()


# ---------------------------------------------------------------------------
# SystemBubble — 系统通知（居中、灰色、无背景）
# ---------------------------------------------------------------------------

class SystemBubble(QFrame):
    def __init__(self, text, parent=None):
        super().__init__(parent)
        self.setObjectName("systemBubble")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        label = QLabel(text)
        label.setAlignment(Qt.AlignCenter)
        label.setWordWrap(True)
        layout.addStretch()
        layout.addWidget(label)
        layout.addStretch()


# ---------------------------------------------------------------------------
# DashboardPanel — 左侧仪表盘
# ---------------------------------------------------------------------------

class DashboardPanel(QWidget):
    mode_switch_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("dashboardPanel")
        self.setMinimumWidth(240)
        self.setMaximumWidth(320)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        # --- 标题 ---
        title = QLabel("\U0001f4f1 手机散热风扇控制器")
        title.setObjectName("dashboardTitle")
        layout.addWidget(title)

        # --- 温度卡片 ---
        temp_section = QLabel("🌡 实时温度")
        temp_section.setObjectName("sectionLabel")
        layout.addWidget(temp_section)

        self.temp_card = QFrame()
        self.temp_card.setObjectName("tempCard")
        tcl = QVBoxLayout(self.temp_card)
        tcl.setContentsMargins(14, 10, 14, 10)
        tcl.setSpacing(2)

        temp_row = QHBoxLayout()
        temp_row.setSpacing(4)
        self.temp_value = QLabel("--.-")
        self.temp_value.setObjectName("tempValue")
        self.temp_value.setStyleSheet("color: #8b949e;")
        temp_row.addWidget(self.temp_value)
        self.temp_unit = QLabel("°C")
        self.temp_unit.setObjectName("tempUnit")
        temp_row.addWidget(self.temp_unit)
        temp_row.addStretch()
        tcl.addLayout(temp_row)

        self.temp_sub = QLabel("等待 PLC 数据...")
        self.temp_sub.setObjectName("tempSub")
        tcl.addWidget(self.temp_sub)

        layout.addWidget(self.temp_card)

        # --- 连接状态 ---
        conn_section = QLabel("🔌 连接状态")
        conn_section.setObjectName("sectionLabel")
        layout.addWidget(conn_section)

        self.conn_card = QFrame()
        self.conn_card.setObjectName("connectionCard")
        ccl = QHBoxLayout(self.conn_card)
        ccl.setContentsMargins(10, 6, 10, 6)
        ccl.setSpacing(6)
        self.conn_dot = QLabel("●")
        self.conn_dot.setObjectName("connectionDot")
        self.conn_dot.setStyleSheet("color: #f85149;")
        ccl.addWidget(self.conn_dot)
        self.conn_text = QLabel("MQTT 未连接")
        self.conn_text.setObjectName("connectionText")
        ccl.addWidget(self.conn_text)
        ccl.addStretch()
        layout.addWidget(self.conn_card)

        # --- 风扇状态 ---
        fan_section = QLabel("❄ 散热风扇")
        fan_section.setObjectName("sectionLabel")
        layout.addWidget(fan_section)

        self.fan_card = QFrame()
        self.fan_card.setObjectName("fanCard")
        fcl = QVBoxLayout(self.fan_card)
        fcl.setContentsMargins(14, 10, 14, 10)
        fcl.setSpacing(4)
        self.fan_status = QLabel("—")
        self.fan_status.setObjectName("fanStatusLabel")
        self.fan_status.setStyleSheet("color: #8b949e;")
        fcl.addWidget(self.fan_status)
        self.fan_detail = QLabel("MW20 = —")
        self.fan_detail.setObjectName("fanDetail")
        fcl.addWidget(self.fan_detail)
        layout.addWidget(self.fan_card)

        # --- 模式切换 ---
        mode_section = QLabel("🎮 控制模式")
        mode_section.setObjectName("sectionLabel")
        layout.addWidget(mode_section)

        mode_row = QHBoxLayout()
        mode_row.setSpacing(8)
        self.btn_auto = QPushButton("🔄 自动")
        self.btn_auto.setObjectName("modeAutoButton")
        self.btn_auto.setCheckable(True)
        self.btn_auto.clicked.connect(lambda: self.mode_switch_requested.emit("auto"))
        mode_row.addWidget(self.btn_auto)

        self.btn_manual = QPushButton("✋ 手动")
        self.btn_manual.setObjectName("modeManualButton")
        self.btn_manual.setCheckable(True)
        self.btn_manual.clicked.connect(lambda: self.mode_switch_requested.emit("manual"))
        mode_row.addWidget(self.btn_manual)
        layout.addLayout(mode_row)

        # --- PLC 寄存器 ---
        reg_section = QLabel("📊 PLC 寄存器")
        reg_section.setObjectName("sectionLabel")
        layout.addWidget(reg_section)

        self.reg_card = QFrame()
        self.reg_card.setObjectName("registerCard")
        rcl = QGridLayout(self.reg_card)
        rcl.setContentsMargins(10, 8, 10, 8)
        rcl.setSpacing(6)

        headers = ["寄存器", "值", "含义"]
        for ci, h in enumerate(headers):
            lbl = QLabel(h)
            lbl.setObjectName("registerName")
            rcl.addWidget(lbl, 0, ci)

        regs = [
            ("MW0", "—", "温度×100"),
            ("MW20", "—", "风扇 0=关 1=开"),
            ("MW21", "—", "自动 1=激活"),
            ("MW22", "—", "手动 1=激活"),
        ]
        self.reg_value_labels = {}
        for ri, (name, _, desc) in enumerate(regs):
            nl = QLabel(name)
            nl.setObjectName("registerName")
            rcl.addWidget(nl, ri + 1, 0)
            vl = QLabel("—")
            vl.setObjectName("registerValue")
            rcl.addWidget(vl, ri + 1, 1)
            self.reg_value_labels[name] = vl
            dl = QLabel(desc)
            dl.setObjectName("registerName")
            rcl.addWidget(dl, ri + 1, 2)

        layout.addWidget(self.reg_card)
        layout.addStretch()

    # ---- 公开更新接口 ----

    def set_connection(self, connected):
        if connected:
            self.conn_dot.setStyleSheet("color: #3fb950;")
            self.conn_text.setText("MQTT 已连接")
        else:
            self.conn_dot.setStyleSheet("color: #f85149;")
            self.conn_text.setText("MQTT 未连接")

    def update_data(self, mw0, mw20, mw21, mw22):
        # 温度
        if mw0 is not None:
            temp_f = mw0 / 100.0
            self.temp_value.setText(f"{temp_f:.1f}")
            self.temp_sub.setText(f"MW0 = {mw0}")

            # 颜色编码
            if temp_f <= 30:
                tc = "#3fb950"
            elif temp_f <= 35:
                tc = "#d29922"
            else:
                tc = "#f85149"
            self.temp_value.setStyleSheet(f"color: {tc};")
        else:
            self.temp_value.setText("--.-")
            self.temp_value.setStyleSheet("color: #8b949e;")
            self.temp_sub.setText("等待 PLC 数据...")

        # 风扇
        if mw20 is not None:
            if mw20 == 1:
                self.fan_status.setText("● 运行中")
                self.fan_status.setStyleSheet("color: #3fb950; font-size: 15px; font-weight: bold;")
            else:
                self.fan_status.setText("○ 已关闭")
                self.fan_status.setStyleSheet("color: #8b949e; font-size: 15px; font-weight: bold;")
            self.fan_detail.setText(f"MW20 = {mw20}")
        else:
            self.fan_status.setText("—")
            self.fan_status.setStyleSheet("color: #8b949e; font-size: 15px; font-weight: bold;")
            self.fan_detail.setText("MW20 = —")

        # 寄存器
        for name, val in [("MW0", mw0), ("MW20", mw20), ("MW21", mw21), ("MW22", mw22)]:
            lbl = self.reg_value_labels.get(name)
            if lbl:
                lbl.setText(str(val) if val is not None else "—")

    def set_mode(self, mode):
        self.btn_auto.setChecked(mode == "auto")
        self.btn_manual.setChecked(mode == "manual")
        self.btn_auto.setProperty("active", mode == "auto")
        self.btn_manual.setProperty("active", mode == "manual")
        # 强制刷新样式
        self.btn_auto.style().unpolish(self.btn_auto)
        self.btn_auto.style().polish(self.btn_auto)
        self.btn_manual.style().unpolish(self.btn_manual)
        self.btn_manual.style().polish(self.btn_manual)


# ---------------------------------------------------------------------------
# ChatPanel — 右侧聊天面板
# ---------------------------------------------------------------------------

class ChatPanel(QWidget):
    send_message = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("chatPanel")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 12)
        layout.setSpacing(8)

        # --- 标题 ---
        title = QLabel("💬 AI 助手 · 小P")
        title.setObjectName("chatTitle")
        layout.addWidget(title)

        # --- 聊天记录滚动区 ---
        self.scroll = QScrollArea()
        self.scroll.setObjectName("chatScrollArea")
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setFrameShape(QFrame.NoFrame)

        self.chat_container = QWidget()
        self.chat_container.setObjectName("chatContainer")
        self.chat_layout = QVBoxLayout(self.chat_container)
        self.chat_layout.setContentsMargins(4, 4, 4, 4)
        self.chat_layout.setSpacing(6)
        self.chat_layout.addStretch()
        self.scroll.setWidget(self.chat_container)
        layout.addWidget(self.scroll, 1)

        # --- 快捷命令按钮 ---
        quick_row1 = QHBoxLayout()
        quick_row1.setSpacing(6)
        cmds1 = [("📊 查询温度", "查询温度"), ("🔄 自动模式", "自动模式"), ("✋ 手动模式", "手动模式")]
        for label, cmd in cmds1:
            btn = QPushButton(label)
            btn.setObjectName("quickCmdButton")
            btn.clicked.connect(lambda checked=False, c=cmd: self.send_message.emit(c))
            quick_row1.addWidget(btn)
        quick_row1.addStretch()
        layout.addLayout(quick_row1)

        quick_row2 = QHBoxLayout()
        quick_row2.setSpacing(6)
        cmds2 = [("❄ 开风扇", "开"), ("⏻ 关风扇", "关")]
        for label, cmd in cmds2:
            btn = QPushButton(label)
            btn.setObjectName("quickCmdButton")
            btn.clicked.connect(lambda checked=False, c=cmd: self.send_message.emit(c))
            quick_row2.addWidget(btn)
        quick_row2.addStretch()
        layout.addLayout(quick_row2)

        # --- 输入行 ---
        input_row = QHBoxLayout()
        input_row.setSpacing(8)
        self.input_field = QLineEdit()
        self.input_field.setObjectName("chatInput")
        self.input_field.setPlaceholderText("输入消息或命令，按 Enter 发送…")
        self.input_field.returnPressed.connect(self._on_send)
        input_row.addWidget(self.input_field, 1)

        self.send_btn = QPushButton("发送")
        self.send_btn.setObjectName("sendButton")
        self.send_btn.clicked.connect(self._on_send)
        input_row.addWidget(self.send_btn)
        layout.addLayout(input_row)

    def _on_send(self):
        text = self.input_field.text().strip()
        if text:
            self.send_message.emit(text)
            self.input_field.clear()

    def add_bubble(self, text, is_user=True):
        bubble = ChatBubble(text, is_user)
        self.chat_layout.insertWidget(self.chat_layout.count() - 1, bubble)
        self._scroll_bottom()

    def add_system(self, text):
        bubble = SystemBubble(text)
        self.chat_layout.insertWidget(self.chat_layout.count() - 1, bubble)
        self._scroll_bottom()

    def _scroll_bottom(self):
        QTimer.singleShot(50, lambda: self.scroll.verticalScrollBar().setValue(
            self.scroll.verticalScrollBar().maximum()
        ))

    def show_thinking(self):
        self._thinking_bubble = SystemBubble("🤔 思考中，请稍候…")
        self.chat_layout.insertWidget(self.chat_layout.count() - 1, self._thinking_bubble)
        self._scroll_bottom()

    def hide_thinking(self):
        if hasattr(self, '_thinking_bubble') and self._thinking_bubble:
            self._thinking_bubble.deleteLater()
            self._thinking_bubble = None


# ---------------------------------------------------------------------------
# MainWindow — 主窗口，总控所有模块
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):

    _trigger_ai = Signal()  # 跨线程触发 AI 请求

    def __init__(self):
        super().__init__()

        self.setWindowTitle("DeepSeek PLC AI Controller — 手机散热风扇")
        self.resize(1100, 680)
        self.setMinimumSize(900, 560)

        # 内部状态
        self._latest_mw0 = None
        self._latest_mw20 = None
        self._latest_mw21 = None
        self._latest_mw22 = None
        self._mqtt_connected = False
        self._current_mode = "auto"
        self._pending_ai = False

        # 加载配置
        self._config = load_config(CONFIG_PATH, DEFAULT_CONFIG)
        self._mqtt_config = self._config["local_mqtt"]
        self._ctrl_config = self._config["control"]
        self._state_file = Path(self._ctrl_config.get("state_file", "controller_state.json"))
        self._device_sn = self._ctrl_config.get("device_sn", "bistu11")
        self._current_mode = load_mode_state(self._state_file)

        # 信号桥
        self._emitter = SignalEmitter()

        # 构建 UI
        self._setup_ui()

        # 连接信号
        self._emitter.connected.connect(self._on_mqtt_connected)
        self._emitter.disconnected.connect(self._on_mqtt_disconnected)
        self._emitter.data_received.connect(self._on_data_received)
        self._dashboard.mode_switch_requested.connect(self._on_mode_switch)
        self._chat.send_message.connect(self._on_user_message)

        # 启动 MQTT
        self._setup_mqtt()

        # 启动 AI 工作线程
        self._setup_ai_worker()

        # 刷新定时器（1 秒）
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._refresh_dashboard)
        self._refresh_timer.start(1000)

        # 初始欢迎消息
        self._chat.add_bubble(
            "你好！我是 PLC 控制助手小P。\n"
            "可以帮你查询温度、控制风扇、切换模式。\n"
            "试试下面的快捷命令吧～", is_user=False
        )
        self._chat.add_system(f"版本 {VERSION}  |  设备 {self._device_sn}")

    # ---- UI 构建 ----

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._dashboard = DashboardPanel()
        self._chat = ChatPanel()

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._dashboard)
        splitter.addWidget(self._chat)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 7)
        splitter.setHandleWidth(2)
        root.addWidget(splitter)

        self._dashboard.set_mode(self._current_mode)

    # ---- MQTT 设置 ----

    def _setup_mqtt(self):
        self._mqtt_client = mqtt.Client(
            client_id=f"dify-gui-{int(time.time())}",
            protocol=mqtt_protocol(self._mqtt_config.get("mqtt_version", "3.1.1")),
        )
        self._mqtt_client.reconnect_delay_set(min_delay=1, max_delay=30)

        if self._mqtt_config.get("username"):
            self._mqtt_client.username_pw_set(
                self._mqtt_config.get("username"),
                self._mqtt_config.get("password", ""),
            )

        input_topic = self._mqtt_config["input_topic"]
        cmd_topic = self._mqtt_config["command_topic"]

        def on_connect(client, userdata, flags, rc, properties=None):
            if rc == 0:
                client.subscribe(input_topic, qos=0)
                client.subscribe(cmd_topic, qos=0)
                self._emitter.connected.emit()
            else:
                self._emitter.disconnected.emit()

        def on_disconnect(client, userdata, rc, properties=None):
            self._emitter.disconnected.emit()

        def on_message(client, userdata, msg):
            payload_text = msg.payload.decode("utf-8", errors="replace")
            topic = msg.topic

            if topic == input_topic:
                _, mw0, mw21, mw22 = parse_input_payload(payload_text)
                self._emitter.data_received.emit(
                    mw0, self._latest_mw20, mw21, mw22
                )
            elif topic == cmd_topic:
                mw20, mw21, mw22 = parse_command_payload(payload_text)
                self._emitter.data_received.emit(
                    self._latest_mw0, mw20, mw21, mw22
                )

        self._mqtt_client.on_connect = on_connect
        self._mqtt_client.on_disconnect = on_disconnect
        self._mqtt_client.on_message = on_message

        self._mqtt_client.connect_async(
            self._mqtt_config["host"],
            int(self._mqtt_config["port"]),
            keepalive=60,
        )
        self._mqtt_client.loop_start()

    # ---- AI 工作线程 ----

    def _setup_ai_worker(self):
        self._ai_thread = QThread()
        self._ai_worker = DeepSeekWorker()
        self._ai_worker.moveToThread(self._ai_thread)
        self._ai_worker.finished.connect(self._on_ai_result)
        self._ai_worker.error.connect(self._on_ai_error)
        self._trigger_ai.connect(self._ai_worker.do_request)
        self._ai_thread.start()

    # ---- MQTT 信号槽 ----

    @Slot()
    def _on_mqtt_connected(self):
        self._mqtt_connected = True
        self._dashboard.set_connection(True)
        self._chat.add_system("MQTT 已连接，开始接收 PLC 数据")

    @Slot()
    def _on_mqtt_disconnected(self):
        self._mqtt_connected = False
        self._dashboard.set_connection(False)
        self._chat.add_system("MQTT 连接断开")

    @Slot(object, object, object, object)
    def _on_data_received(self, mw0, mw20, mw21, mw22):
        if mw0 is not None:
            self._latest_mw0 = mw0
        if mw20 is not None:
            self._latest_mw20 = mw20
        if mw21 is not None:
            self._latest_mw21 = mw21
        if mw22 is not None:
            self._latest_mw22 = mw22

    # ---- 定时刷新仪表盘 ----

    def _refresh_dashboard(self):
        self._dashboard.update_data(
            self._latest_mw0,
            self._latest_mw20,
            self._latest_mw21,
            self._latest_mw22,
        )
        # 同时检查 state file 变化（auto_controller.py 可能改了模式）
        mode = load_mode_state(self._state_file)
        if mode != self._current_mode:
            self._current_mode = mode
            self._dashboard.set_mode(mode)

    # ---- 模式切换 ----

    @Slot(str)
    def _on_mode_switch(self, target_mode):
        if target_mode == self._current_mode:
            return

        ok = save_mode_state(self._state_file, target_mode, reason="gui_button")
        if not ok:
            self._chat.add_system("⚠ 保存模式状态失败")
            return

        self._current_mode = target_mode
        self._dashboard.set_mode(target_mode)

        if self._mqtt_connected:
            if target_mode == "auto":
                self._publish_cmd(mw20=0, mw21=1, mw22=0)
                self._chat.add_system("已切换到 🔄 自动模式（MW21=1, MW22=0）")
            else:
                self._publish_cmd(mw20=0, mw21=0, mw22=1)
                self._chat.add_system("已切换到 ✋ 手动模式（MW21=0, MW22=1）")
        else:
            self._chat.add_system(f"已切换到 {'自动' if target_mode == 'auto' else '手动'} 模式（MQTT 未连接）")

    # ---- 用户消息处理 ----

    @Slot(str)
    def _on_user_message(self, text):
        self._chat.add_bubble(text, is_user=True)
        q = text.strip().lower()

        # 刷新最新数据
        mode = load_mode_state(self._state_file)
        if mode != self._current_mode:
            self._current_mode = mode
            self._dashboard.set_mode(mode)

        # ---- 本地硬命令 ----
        if q in ("exit", "quit", "退出"):
            self._chat.add_system("再见～")
            self.close()
            return

        if q in ("help", "帮助", "?"):
            self._chat.add_bubble(
                "支持的命令：\n"
                "• 查询温度 / 状态 — 查看当前温度和设备状态\n"
                "• 自动模式 — 切换到自动控制（AI 自动判断开关）\n"
                "• 手动模式 — 切换到手动控制\n"
                "• 开 / 关 — 手动开关风扇（需在手动模式下）\n"
                "• 其他自然语言 — 由 AI 智能回复",
                is_user=False,
            )
            return

        # 模式切换
        if q in ("自动模式", "自动", "auto"):
            self._on_mode_switch("auto")
            return
        if q in ("手动模式", "手动", "manual", "自定义模式", "自定义"):
            self._on_mode_switch("manual")
            return

        # 手动控制
        if q in ("开", "打开", "启动", "开启", "开灯", "开风扇"):
            if self._current_mode != "manual":
                self._chat.add_bubble("请先切换到 ✋ 手动模式再操作。", is_user=False)
            elif self._mqtt_connected:
                self._publish_cmd(mw20=1, mw21=0, mw22=1)
                self._chat.add_bubble("已开风扇 ❄ MW20=1", is_user=False)
            else:
                self._chat.add_system("⚠ MQTT 未连接，无法发送命令")
            return

        if q in ("关", "关闭", "停止", "关灯", "关风扇"):
            if self._current_mode != "manual":
                self._chat.add_bubble("请先切换到 ✋ 手动模式再操作。", is_user=False)
            elif self._mqtt_connected:
                self._publish_cmd(mw20=0, mw21=0, mw22=1)
                self._chat.add_bubble("已关风扇 ⏻ MW20=0", is_user=False)
            else:
                self._chat.add_system("⚠ MQTT 未连接，无法发送命令")
            return

        # ---- AI 请求 ----
        if self._latest_mw0 is None:
            self._chat.add_bubble("还没有收到 PLC 数据，请检查 MQTT 连接。", is_user=False)
            return

        if self._pending_ai:
            self._chat.add_system("AI 正在思考中，请稍候…")
            return

        self._pending_ai = True
        self._chat.show_thinking()
        self._ai_worker.set_request(
            self._config,
            self._latest_mw0,
            text,
            self._latest_mw20,
            self._latest_mw21,
            self._latest_mw22,
            self._current_mode,
        )
        self._trigger_ai.emit()

    # ---- AI 结果 ----

    @Slot(dict)
    def _on_ai_result(self, result):
        self._pending_ai = False
        self._chat.hide_thinking()

        text = result.get("test") or result.get("text") or "(无回复)"
        mode_cmd = result.get("mode")
        mw20_cmd = result.get("mw20")

        extras = []

        # AI 要求切换模式
        if mode_cmd in ("auto", "manual") and mode_cmd != self._current_mode:
            ok = save_mode_state(self._state_file, mode_cmd, reason="ai_chat")
            if ok:
                self._current_mode = mode_cmd
                self._dashboard.set_mode(mode_cmd)
                if self._mqtt_connected:
                    if mode_cmd == "auto":
                        self._publish_cmd(mw20=0, mw21=1, mw22=0)
                    else:
                        self._publish_cmd(mw20=0, mw21=0, mw22=1)
                extras.append(f"已切换到 {'🔄 自动' if mode_cmd == 'auto' else '✋ 手动'} 模式")

        # AI 返回 mw20 命令（仅手动模式生效）
        if mw20_cmd is not None and self._current_mode == "manual":
            try:
                mw20_val = int(mw20_cmd)
                if mw20_val in (0, 1) and self._mqtt_connected:
                    self._publish_cmd(mw20=mw20_val)
                    extras.append(f"{'已开风扇 ❄' if mw20_val == 1 else '已关风扇 ⏻'} (MW20={mw20_val})")
            except (ValueError, TypeError):
                pass
        elif mw20_cmd is not None and self._current_mode == "auto":
            extras.append("⚠ 自动模式下忽略开关指令，请先切换到手动模式")

        if extras:
            text += "\n\n" + "\n".join(f"• {e}" for e in extras)

        self._chat.add_bubble(text, is_user=False)

    @Slot(str)
    def _on_ai_error(self, error_msg):
        self._pending_ai = False
        self._chat.hide_thinking()
        self._chat.add_bubble(f"⚠ AI 调用失败：{error_msg}", is_user=False)

    # ---- MQTT 发布 ----

    def _publish_cmd(self, mw20=0, mw21=None, mw22=None):
        payload = build_command_payload(self._device_sn, mw20=mw20, mw21=mw21, mw22=mw22)
        try:
            info = self._mqtt_client.publish(
                self._mqtt_config["command_topic"], payload, qos=0, retain=False
            )
            info.wait_for_publish(timeout=5)
        except Exception as exc:
            log(f"Publish failed: {exc}")

    # ---- 关闭 ----

    def closeEvent(self, event):
        self._refresh_timer.stop()
        if self._mqtt_client:
            self._mqtt_client.loop_stop()
            self._mqtt_client.disconnect()
        if self._ai_thread:
            self._ai_thread.quit()
            self._ai_thread.wait(3000)
        event.accept()


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(QSS_THEME)
    app.setApplicationName("DeepSeek PLC AI Controller")

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
