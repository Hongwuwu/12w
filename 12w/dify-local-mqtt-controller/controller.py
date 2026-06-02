# -*- coding: utf-8 -*-
"""
DeepSeek PLC AI Controller — 单文件整合版
===========================================

启动：
    python controller.py              自动控制器 + GUI（推荐，控制台显示日志）
    python controller.py auto         仅自动控制器
    python controller.py chat         仅命令行终端（交互式 REPL）
    python controller.py gui          仅 GUI 图形界面

架构：
    auto_controller（后台线程）── MQTT ──► PLC 数据 / 命令
    GUI（前台 Qt 事件循环）  ── DeepSeek API ──► AI 对话
    controller_state.json  ── 模式协调（auto/manual）

PLC 寄存器：
    MW0  = 温度 × 100    MW20 = 风扇 0=关 1=开
    MW21 = 自动标志       MW22 = 手动标志
"""

# ===========================================================================
# 1. 导入
# ===========================================================================
import json
import os
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import paho.mqtt.client as mqtt

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication, QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QLineEdit, QMainWindow, QPushButton, QScrollArea, QSizePolicy,
    QSplitter, QVBoxLayout, QWidget,
)

# ===========================================================================
# 2. 常量 & 默认配置
# ===========================================================================
VERSION = "deepseek-plc-controller-20260602"
CONFIG_PATH = Path(os.getenv("CONTROLLER_CONFIG", "config.json"))
STATE_FILE_PATH = Path("controller_state.json")

DEFAULT_CONFIG = {
    "local_mqtt": {
        "host": "192.168.31.197", "port": 1883,
        "username": "", "password": "",
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
        "dedup_input": True,
        "dedup_command": True,
        "publish_on_no_action": False,
    },
}

# ===========================================================================
# 3. 共享工具函数（原 common.py）
# ===========================================================================

def log(message):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {message}", flush=True)


def mqtt_protocol(version):
    return mqtt.MQTTv31 if str(version) == "3.1" else mqtt.MQTTv311


def merge_dict(defaults, overrides):
    result = dict(defaults)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_dict(result[key], value)
        else:
            result[key] = value
    return result


def load_config(config_path, default_config):
    config_path = Path(config_path)
    if not config_path.exists():
        config_path.write_text(
            json.dumps(default_config, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        log(f"Created default config: {config_path.resolve()}")
        log("Please edit config and restart.")
        sys.exit(2)
    with config_path.open("r", encoding="utf-8") as f:
        config = json.load(f)
    return merge_dict(default_config, config)


def parse_input_payload(payload_text):
    """解析 PLC 上报：提取 MW0 / MW21 / MW22。返回 (data, mw0, mw21, mw22)。"""
    try:
        data = json.loads(payload_text)
        if not isinstance(data, list) or not data:
            return None, None, None, None
        tag_data = data[0].get("TagData") or []
        if not tag_data or not isinstance(tag_data[0], dict):
            return None, None, None, None
        tag = tag_data[0]
        return data, tag.get("MW0"), tag.get("MW21"), tag.get("MW22")
    except Exception:
        return None, None, None, None


def parse_command_payload(payload_text):
    """解析命令 topic：提取 MW20 / MW21 / MW22。返回 (mw20, mw21, mw22)。"""
    try:
        data = json.loads(payload_text)
        if not isinstance(data, list) or not data:
            return None, None, None
        tag_data = data[0].get("TagData") or []
        if not tag_data or not isinstance(tag_data[0], dict):
            return None, None, None
        tag = tag_data[0]
        return tag.get("MW20"), tag.get("MW21"), tag.get("MW22")
    except Exception:
        return None, None, None


def build_command_payload(device_sn, mw20=None, mw21=None, mw22=None):
    """生成 PLC 控制命令 payload。只包含非 None 的寄存器。"""
    tag = {}
    if mw20 is not None: tag["MW20"] = int(mw20)
    if mw21 is not None: tag["MW21"] = int(mw21)
    if mw22 is not None: tag["MW22"] = int(mw22)
    return json.dumps([{"DeviceSN": device_sn, "TagData": [tag]}],
                      ensure_ascii=False, separators=(",", ":"))


def load_mode_state(state_file_path):
    try:
        sf = Path(state_file_path)
        if not sf.exists(): return "auto"
        with sf.open("r", encoding="utf-8") as f:
            state = json.load(f)
        mode = state.get("mode", "auto")
        return mode if mode in ("auto", "manual") else "auto"
    except Exception:
        return "auto"


def save_mode_state(state_file_path, mode, reason="", switched_by="controller"):
    try:
        state = {
            "mode": mode,
            "switched_at": datetime.now(timezone.utc).isoformat(),
            "switched_by": switched_by,
            "reason": reason,
        }
        Path(state_file_path).write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception as exc:
        log(f"Save state failed: {exc}")
        return False


# ===========================================================================
# 4. DeepSeek API 调用
# ===========================================================================

def _call_deepseek_api(config, messages, timeout=None):
    """通用 DeepSeek API 调用。"""
    ds = config["deepseek"]
    api_key = ds.get("api_key", "")
    if not api_key or api_key == "PASTE_DEEPSEEK_API_KEY_HERE":
        raise RuntimeError("deepseek.api_key not configured")

    body = {
        "model": ds.get("model", "deepseek-chat"),
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": 500,
        "response_format": {"type": "json_object"},
    }
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(ds["api_url"], data=data, headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }, method="POST")

    t = float(timeout or ds.get("timeout_sec", 60))
    try:
        with urllib.request.urlopen(req, timeout=t) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        err = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"DeepSeek HTTP {exc.code}: {err}") from exc


AUTO_SYSTEM_PROMPT = """\
你是PLC控制助手"小P"。判断当前温度是否需要开启设备（灯光/散热）。

规则：
- 温度 > 30度：建议开启设备（mw20=1）
- 温度 <= 30度：建议关闭设备（mw20=0）
- 温度数据异常（null/负数）：保持不变（mw20=-1）

只回复JSON：{"mw20": 1, "reason": "简短中文原因"}"""


def call_deepseek_auto(config, mw0):
    temp = mw0 / 100.0 if mw0 is not None else None
    if temp is None: return None, "no temperature data"
    result = _call_deepseek_api(config, [
        {"role": "system", "content": AUTO_SYSTEM_PROMPT},
        {"role": "user", "content": f"当前温度：{temp:.1f}度（MW0={mw0}）。请判断是否需要开启设备。"},
    ], timeout=30)
    content = result["choices"][0]["message"]["content"]
    parsed = json.loads(content)
    mw20 = parsed.get("mw20")
    reason = parsed.get("reason", "")
    if mw20 is None or mw20 == -1: return None, f"no action: {reason}"
    try:
        mw20 = int(mw20)
    except (ValueError, TypeError):
        return None, f"invalid mw20={mw20}"
    if mw20 not in (0, 1): return None, f"mw20 must be 0 or 1, got {mw20}"
    return mw20, reason


CHAT_SYSTEM_PROMPT = """\
你是PLC控制助手"小P"。负责帮助用户查询PLC状态和控制设备。

当前信息：温度 {temp} 度 | MW20={mw20} | MW21={mw21} | MW22={mw22} | 模式 {mode}
重要：PLC内部自动模式(MW21)优先级高于手动模式。切换手动时必须 MW21=0,MW22=1。

用户输入：{user_query}

只回复JSON：{{"text": "中文回复", "mode": null, "mw20": null}}

规则：
- 自动模式下要求开关 → 先切手动模式再执行
- "自动模式"/"自动" → mode="auto", 设MW21=1,MW22=0
- "手动模式"/"手动" → mode="manual", 设MW21=0,MW22=1
- manual模式开关 → mw20=1/0
- auto模式手动控制 → 提示先切手动
- 查询/状态 → 告知当前温度和状态, mode=null, mw20=null
- 回复简短友好，用中文"""


def call_deepseek_chat(config, mw0, user_query, current_mw20, current_mw21, current_mw22, current_mode):
    temp = mw0 / 100.0 if mw0 is not None else "未知"
    mode_label = "自动" if current_mode == "auto" else "手动"
    system = CHAT_SYSTEM_PROMPT.format(
        temp=temp, mw20=current_mw20,
        mw21=current_mw21 if current_mw21 is not None else "?",
        mw22=current_mw22 if current_mw22 is not None else "?",
        mode=mode_label, user_query=user_query,
    )
    result = _call_deepseek_api(config, [
        {"role": "system", "content": system},
        {"role": "user", "content": user_query},
    ])
    return json.loads(result["choices"][0]["message"]["content"])


# ===========================================================================
# 5. 自动控制器（原 auto_controller.py）
# ===========================================================================

class AutoController:
    """PLC 自动控制器。后台线程运行，通过 controller_state.json 感知模式。"""

    def __init__(self):
        self.running = True
        self.local_connected = False
        self.state_lock = threading.Lock()
        self.latest_payload = None
        self.latest_mw0 = None
        self.latest_mw21 = None
        self.latest_mw22 = None
        self.last_processed_payload = None
        self.last_command_payload = None

    def stop(self):
        self.running = False

    def run(self):
        config = load_config(CONFIG_PATH, DEFAULT_CONFIG)
        mqtt_cfg = config["local_mqtt"]
        ctrl_cfg = config["control"]
        device_sn = ctrl_cfg.get("device_sn", "bistu11")
        state_file = Path(ctrl_cfg.get("state_file", "controller_state.json"))

        client = mqtt.Client(
            client_id=f"auto-ctrl-{int(time.time())}",
            protocol=mqtt_protocol(mqtt_cfg.get("mqtt_version", "3.1.1")),
        )
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        if mqtt_cfg.get("username"):
            client.username_pw_set(mqtt_cfg.get("username"), mqtt_cfg.get("password", ""))

        def on_connect(c, u, flags, rc, props=None):
            self.local_connected = (rc == 0)
            if self.local_connected:
                log(f"Auto: MQTT connected {mqtt_cfg['host']}:{mqtt_cfg['port']}")
                c.subscribe(mqtt_cfg["input_topic"], qos=0)

        def on_disconnect(c, u, rc, props=None):
            self.local_connected = False

        def on_message(c, u, msg):
            text = msg.payload.decode("utf-8", errors="replace")
            _, mw0, mw21, mw22 = parse_input_payload(text)
            prev = self.latest_payload
            with self.state_lock:
                self.latest_payload = text
                self.latest_mw0 = mw0
                self.latest_mw21 = mw21
                self.latest_mw22 = mw22
            if text != prev:
                log(f"Auto input MW0={mw0} MW21={mw21} MW22={mw22}")

        client.on_connect = on_connect
        client.on_disconnect = on_disconnect
        client.on_message = on_message
        client.connect_async(mqtt_cfg["host"], int(mqtt_cfg["port"]), keepalive=60)
        client.loop_start()

        interval = float(ctrl_cfg.get("process_interval_sec", 2.0))
        next_tick = 0.0

        while self.running:
            now = time.time()
            if now < next_tick:
                time.sleep(0.1)
                continue
            next_tick = now + interval

            with self.state_lock:
                payload_text = self.latest_payload
                mw0 = self.latest_mw0
                mw21 = self.latest_mw21
                mw22 = self.latest_mw22

            if not payload_text:
                continue

            mode_state = load_mode_state(state_file)
            plc_manual = (mw22 == 1 and mw21 == 0)
            if mode_state == "manual" or plc_manual:
                if payload_text != self.last_processed_payload:
                    log(f"Auto: manual mode, suspended. MW0={mw0}")
                self.last_processed_payload = payload_text
                continue

            if ctrl_cfg.get("dedup_input", True) and payload_text == self.last_processed_payload:
                continue

            self.last_processed_payload = payload_text

            # 自动模式：PLC 内部逻辑接管（MW21=1 → PLC 按温度自动控制）
            # 仅发布 MW21=1,MW22=0 维持自动模式，不设 MW20
            command_payload = build_command_payload(device_sn, mw21=1, mw22=0)

            if ctrl_cfg.get("dedup_command", True) and command_payload == self.last_command_payload:
                continue

            if not self.local_connected:
                continue

            try:
                info = client.publish(mqtt_cfg["command_topic"], command_payload, qos=0, retain=False)
                info.wait_for_publish(timeout=5)
                if info.is_published():
                    self.last_command_payload = command_payload
                    log(f"Auto published MW21=1,MW22=0 → {mqtt_cfg['command_topic']}")
            except Exception as exc:
                log(f"Auto publish failed: {exc}")

        client.loop_stop()
        client.disconnect()
        log("Auto controller exited")


# ===========================================================================
# 6. 命令行终端（原 chat_terminal.py 核心，不含 GUI 依赖）
# ===========================================================================

class ChatTerminal:
    """交互式命令行 REPL。"""

    def __init__(self):
        self.running = True
        self.local_connected = False
        self.state_lock = threading.Lock()
        self.latest_mw0 = None
        self.latest_mw20 = None
        self.latest_mw21 = None
        self.latest_mw22 = None

    def stop(self):
        self.running = False

    def run(self):
        config = load_config(CONFIG_PATH, DEFAULT_CONFIG)
        mqtt_cfg = config["local_mqtt"]
        ctrl_cfg = config["control"]
        state_file = Path(ctrl_cfg.get("state_file", "controller_state.json"))
        device_sn = ctrl_cfg.get("device_sn", "bistu11")

        client = mqtt.Client(
            client_id=f"chat-term-{int(time.time())}",
            protocol=mqtt_protocol(mqtt_cfg.get("mqtt_version", "3.1.1")),
        )
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        if mqtt_cfg.get("username"):
            client.username_pw_set(mqtt_cfg.get("username"), mqtt_cfg.get("password", ""))

        def on_connect(c, u, flags, rc, props=None):
            self.local_connected = (rc == 0)
            if self.local_connected:
                log(f"Chat: MQTT connected {mqtt_cfg['host']}:{mqtt_cfg['port']}")
                c.subscribe(mqtt_cfg["input_topic"], qos=0)
                c.subscribe(mqtt_cfg["command_topic"], qos=0)

        def on_disconnect(c, u, rc, props=None):
            self.local_connected = False

        def on_message(c, u, msg):
            text = msg.payload.decode("utf-8", errors="replace")
            with self.state_lock:
                if msg.topic == mqtt_cfg["input_topic"]:
                    _, mw0, mw21, mw22 = parse_input_payload(text)
                    if mw0 is not None: self.latest_mw0 = mw0
                    if mw21 is not None: self.latest_mw21 = mw21
                    if mw22 is not None: self.latest_mw22 = mw22
                elif msg.topic == mqtt_cfg["command_topic"]:
                    mw20, mw21, mw22 = parse_command_payload(text)
                    if mw20 is not None: self.latest_mw20 = mw20
                    if mw21 is not None: self.latest_mw21 = mw21
                    if mw22 is not None: self.latest_mw22 = mw22

        client.on_connect = on_connect
        client.on_disconnect = on_disconnect
        client.on_message = on_message
        client.connect_async(mqtt_cfg["host"], int(mqtt_cfg["port"]), keepalive=60)
        client.loop_start()

        # 等 MQTT 连接
        wait_start = time.time()
        while not self.local_connected and time.time() - wait_start < 10:
            time.sleep(0.5)

        current_mode = load_mode_state(state_file)

        def publish_cmd(mw20=0, mw21=None, mw22=None):
            payload = build_command_payload(device_sn, mw20=mw20, mw21=mw21, mw22=mw22)
            try:
                info = client.publish(mqtt_cfg["command_topic"], payload, qos=0, retain=False)
                info.wait_for_publish(timeout=5)
                return info.is_published()
            except Exception as e:
                log(f"Publish failed: {e}")
                return False

        print(f"\nDeepSeek PLC Chat Terminal v{VERSION}")
        print(f"MQTT: {mqtt_cfg['host']}:{mqtt_cfg['port']}  Device: {device_sn}")
        print("Commands: 查询温度 | 自动模式 | 手动模式 | 开 | 关 | Ctrl+C 退出\n")

        while self.running:
            try:
                with self.state_lock:
                    mw0 = self.latest_mw0
                    mw20 = self.latest_mw20
                    mw21 = self.latest_mw21
                    mw22 = self.latest_mw22
                current_mode = load_mode_state(state_file)

                temp_str = f"{mw0/100.0:.1f}" if mw0 is not None else "?"
                print(f"[Temp:{temp_str}C MW20={mw20} MW21={mw21} MW22={mw22} Mode:{current_mode}]")

                try:
                    user_input = input("> ").strip()
                except EOFError:
                    break
                if not user_input:
                    continue

                # 刷新数据
                with self.state_lock:
                    mw0 = self.latest_mw0
                    mw20 = self.latest_mw20
                    mw21 = self.latest_mw21
                    mw22 = self.latest_mw22
                current_mode = load_mode_state(state_file)

                q = user_input.lower()

                if q in ("exit", "quit", "退出"):
                    break
                if q in ("自动模式", "自动", "auto"):
                    if current_mode != "auto":
                        save_mode_state(state_file, "auto", reason="local_cmd")
                        current_mode = "auto"
                        if self.local_connected: publish_cmd(mw20=0, mw21=1, mw22=0)
                        print("已切换到自动模式。MW21=1,MW22=0\n")
                    else:
                        print("当前已是自动模式。\n")
                    continue
                if q in ("手动模式", "手动", "manual", "自定义模式", "自定义"):
                    if current_mode != "manual":
                        save_mode_state(state_file, "manual", reason="local_cmd")
                        current_mode = "manual"
                        if self.local_connected: publish_cmd(mw20=0, mw21=0, mw22=1)
                        print("已切换到手动模式。MW21=0,MW22=1\n")
                    else:
                        print("当前已是手动模式。\n")
                    continue
                if q in ("开", "打开", "启动", "开启", "开灯"):
                    if current_mode != "manual":
                        print("请先切换到手动模式。\n")
                    elif self.local_connected:
                        publish_cmd(mw20=1, mw21=0, mw22=1)
                        print("已开灯。MW20=1\n")
                    continue
                if q in ("关", "关闭", "停止", "关灯"):
                    if current_mode != "manual":
                        print("请先切换到手动模式。\n")
                    elif self.local_connected:
                        publish_cmd(mw20=0, mw21=0, mw22=1)
                        print("已关灯。MW20=0\n")
                    continue

                if mw0 is None:
                    print("无 PLC 数据，请检查 MQTT 连接。\n")
                    continue

                print("调用 DeepSeek…")
                try:
                    outputs = call_deepseek_chat(
                        config, mw0, user_input, mw20 or 0, mw21 or 0, mw22 or 0, current_mode)
                except Exception as exc:
                    print(f"API 调用失败: {exc}\n")
                    continue

                text = outputs.get("text") or outputs.get("test") or "(无回复)"
                mode_cmd = outputs.get("mode")
                mw20_cmd = outputs.get("mw20")
                print(f"\n小P: {text}")

                if mode_cmd in ("auto", "manual") and mode_cmd != current_mode:
                    save_mode_state(state_file, mode_cmd, reason="ai_chat")
                    current_mode = mode_cmd
                    print(f"[模式已切换为 {mode_cmd}]")
                if mw20_cmd is not None and current_mode == "manual":
                    try:
                        v = int(mw20_cmd)
                        if v in (0, 1) and self.local_connected:
                            publish_cmd(mw20=v)
                            print(f"[已发送 MW20={v}]")
                    except (ValueError, TypeError):
                        pass
                print()
            except KeyboardInterrupt:
                break

        client.loop_stop()
        client.disconnect()
        log("Chat terminal exited")


# ===========================================================================
# 7. PySide6 GUI（原 chat_gui.py）
# ===========================================================================

# ---- QSS 暗色主题 ----

QSS_THEME = """
QMainWindow { background-color: #0d1117; }
QWidget { color: #c9d1d9; font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif; font-size: 13px; }
QSplitter::handle { background-color: #21262d; width: 2px; }

#dashboardPanel { background-color: #161b22; border-right: 1px solid #21262d; }
#dashboardScroll { background-color: #161b22; border: none; }
#dashboardTitle { font-size: 15px; font-weight: bold; color: #58a6ff; padding: 2px 0; }
#sectionLabel { font-size: 11px; font-weight: bold; color: #8b949e; text-transform: uppercase; letter-spacing: 1px; padding-top: 6px; }

#tempCard { background-color: #1c2333; border: 1px solid #30363d; border-radius: 14px; padding: 12px; }
#tempValue { font-size: 52px; font-weight: bold; }
#tempUnit { font-size: 20px; font-weight: normal; color: #8b949e; }
#tempSub { font-size: 11px; color: #8b949e; margin-top: 2px; }

#fanCard { background-color: #1c2333; border: 1px solid #30363d; border-radius: 14px; padding: 12px; }
#fanStatusLabel { font-size: 15px; font-weight: bold; }
#fanDetail { font-size: 11px; color: #8b949e; }

#connectionCard { background-color: #1c2333; border: 1px solid #30363d; border-radius: 10px; padding: 8px 12px; }
#connectionDot { font-size: 18px; }
#connectionText { font-size: 12px; margin-left: 4px; }

#modeAutoButton, #modeManualButton { background-color: #21262d; color: #8b949e; border: 2px solid #30363d; border-radius: 10px; padding: 10px 18px; font-size: 13px; font-weight: bold; }
#modeAutoButton:hover, #modeManualButton:hover { background-color: #30363d; color: #c9d1d9; }
#modeAutoButton[active="true"] { background-color: #1a3a5c; border-color: #58a6ff; color: #58a6ff; }
#modeManualButton[active="true"] { background-color: #3d1a2e; border-color: #f78166; color: #f78166; }

#registerCard { background-color: #1c2333; border: 1px solid #30363d; border-radius: 10px; padding: 10px; }
#registerName { font-family: "Cascadia Code", "Consolas", monospace; font-size: 11px; color: #8b949e; }
#registerValue { font-family: "Cascadia Code", "Consolas", monospace; font-size: 14px; font-weight: bold; color: #79c0ff; }

#chatPanel { background-color: #0d1117; }
#chatTitle { font-size: 15px; font-weight: bold; color: #f78166; padding: 2px 0; }
#chatScrollArea { background-color: #0d1117; border: none; }

#chatBubbleUser { background-color: #1a3a5c; border: 1px solid #1f4a78; border-radius: 10px; }
#chatBubbleAI { background-color: #1c2333; border: 1px solid #30363d; border-radius: 10px; }
#bubbleText { font-size: 13px; color: #c9d1d9; }
#bubbleMeta { font-size: 9px; color: #5a6370; }

#systemBubble QLabel { font-size: 11px; color: #6e7681; }

#quickCmdButton { background-color: #21262d; color: #8b949e; border: 1px solid #30363d; border-radius: 14px; padding: 5px 14px; font-size: 11px; }
#quickCmdButton:hover { background-color: #30363d; color: #c9d1d9; border-color: #58a6ff; }

#chatInput { background-color: #1c2333; border: 2px solid #30363d; border-radius: 10px; padding: 10px 14px; font-size: 13px; color: #c9d1d9; }
#chatInput:focus { border-color: #f78166; }

#sendButton { background-color: #f78166; color: #0d1117; border: none; border-radius: 10px; padding: 10px 24px; font-size: 13px; font-weight: bold; }
#sendButton:hover { background-color: #f7997e; }
#sendButton:pressed { background-color: #d96a52; }

QScrollBar:vertical { background-color: #0d1117; width: 8px; border-radius: 4px; }
QScrollBar::handle:vertical { background-color: #30363d; border-radius: 4px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background-color: #484f58; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
"""

# ---- SignalEmitter ----

class SignalEmitter(QObject):
    connected = Signal()
    disconnected = Signal()
    data_updated = Signal()

# ---- DeepSeekWorker ----

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
                self._config, self._mw0, self._query,
                self._mw20 or 0, self._mw21 or 0, self._mw22 or 0, self._mode)
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

# ---- ChatBubble ----

class ChatBubble(QFrame):
    def __init__(self, text, is_user=True, parent=None):
        super().__init__(parent)
        self.setObjectName("chatBubbleUser" if is_user else "chatBubbleAI")
        self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)

        inner = QVBoxLayout()
        inner.setContentsMargins(12, 8, 12, 8)
        inner.setSpacing(2)
        self.setLayout(inner)

        now = datetime.now().strftime("%H:%M")
        text_label = QLabel(text)
        text_label.setWordWrap(True)
        text_label.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        text_label.setMaximumWidth(480)
        text_label.setObjectName("bubbleText")
        inner.addWidget(text_label)

        meta = QLabel(f"{'你' if is_user else '小P'} {now}")
        meta.setObjectName("bubbleMeta")
        inner.addWidget(meta)

# ---- SystemBubble ----

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

# ---- DashboardPanel ----

class DashboardPanel(QWidget):
    mode_switch_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("dashboardPanel")
        self.setMinimumWidth(240)
        self.setMaximumWidth(320)

        # 外层用 QScrollArea 包裹，窗口高度不足时自动出现滚动条
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setObjectName("dashboardScroll")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.NoFrame)
        outer.addWidget(scroll)

        content = QWidget()
        content.setObjectName("dashboardContent")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        title = QLabel("\U0001f4f1 手机散热风扇控制器")
        title.setObjectName("dashboardTitle")
        layout.addWidget(title)

        # 温度
        layout.addWidget(self._section("🌡 实时温度"))
        self.temp_card = self._card()
        tcl = QVBoxLayout(self.temp_card)
        tcl.setContentsMargins(14, 10, 14, 10); tcl.setSpacing(2)
        tr = QHBoxLayout(); tr.setSpacing(4)
        self.temp_value = QLabel("--.-")
        self.temp_value.setObjectName("tempValue")
        self.temp_value.setStyleSheet("color: #8b949e;")
        tr.addWidget(self.temp_value)
        tu = QLabel("°C"); tu.setObjectName("tempUnit"); tr.addWidget(tu)
        tr.addStretch(); tcl.addLayout(tr)
        self.temp_sub = QLabel("等待 PLC 数据…")
        self.temp_sub.setObjectName("tempSub"); tcl.addWidget(self.temp_sub)
        layout.addWidget(self.temp_card)

        # 连接
        layout.addWidget(self._section("🔌 连接状态"))
        self.conn_card = self._card_small()
        ccl = QHBoxLayout(self.conn_card)
        ccl.setContentsMargins(10, 6, 10, 6); ccl.setSpacing(6)
        self.conn_dot = QLabel("●")
        self.conn_dot.setObjectName("connectionDot")
        self.conn_dot.setStyleSheet("color: #f85149;"); ccl.addWidget(self.conn_dot)
        self.conn_text = QLabel("MQTT 未连接")
        self.conn_text.setObjectName("connectionText"); ccl.addWidget(self.conn_text)
        ccl.addStretch(); layout.addWidget(self.conn_card)

        # 风扇
        layout.addWidget(self._section("❄ 散热风扇"))
        self.fan_card = self._card()
        fcl = QVBoxLayout(self.fan_card)
        fcl.setContentsMargins(14, 10, 14, 10); fcl.setSpacing(4)
        self.fan_status = QLabel("—")
        self.fan_status.setObjectName("fanStatusLabel")
        self.fan_status.setStyleSheet("color: #8b949e;"); fcl.addWidget(self.fan_status)
        self.fan_detail = QLabel("等待数据…")
        self.fan_detail.setObjectName("fanDetail"); fcl.addWidget(self.fan_detail)
        layout.addWidget(self.fan_card)

        # 模式
        layout.addWidget(self._section("🎮 控制模式"))
        mr = QHBoxLayout(); mr.setSpacing(8)
        self.btn_auto = QPushButton("🔄 自动")
        self.btn_auto.setObjectName("modeAutoButton")
        self.btn_auto.setCheckable(True)
        self.btn_auto.clicked.connect(lambda: self.mode_switch_requested.emit("auto"))
        mr.addWidget(self.btn_auto)
        self.btn_manual = QPushButton("✋ 手动")
        self.btn_manual.setObjectName("modeManualButton")
        self.btn_manual.setCheckable(True)
        self.btn_manual.clicked.connect(lambda: self.mode_switch_requested.emit("manual"))
        mr.addWidget(self.btn_manual); layout.addLayout(mr)

        # 寄存器
        layout.addWidget(self._section("📊 PLC 寄存器"))
        self.reg_card = self._card_small()
        rcl = QGridLayout(self.reg_card)
        rcl.setContentsMargins(10, 8, 10, 8); rcl.setSpacing(6)
        for ci, h in enumerate(["寄存器", "值", "含义"]):
            lbl = QLabel(h); lbl.setObjectName("registerName"); rcl.addWidget(lbl, 0, ci)
        self.reg_value_labels = {}
        regs = [("MW0","—","温度×100"),("MW20","—","风扇"),("MW21","—","自动"),("MW22","—","手动")]
        for ri, (n, _, d) in enumerate(regs):
            QLabel(n).setObjectName("registerName"); rcl.addWidget(QLabel(n), ri+1, 0)
            vl = QLabel("—"); vl.setObjectName("registerValue"); rcl.addWidget(vl, ri+1, 1)
            self.reg_value_labels[n] = vl
            dl = QLabel(d); dl.setObjectName("registerName"); rcl.addWidget(dl, ri+1, 2)
        layout.addWidget(self.reg_card)

        # 温度趋势图按钮
        self.btn_trend = QPushButton("📈 温度趋势")
        self.btn_trend.setObjectName("quickCmdButton")
        self.btn_trend.setMinimumHeight(34)
        layout.addWidget(self.btn_trend)

        layout.addStretch()
        scroll.setWidget(content)

    def _section(self, text):
        l = QLabel(text); l.setObjectName("sectionLabel"); return l

    def _card(self):
        f = QFrame(); f.setObjectName("tempCard"); return f

    def _card_small(self):
        f = QFrame(); f.setObjectName("connectionCard"); return f

    def set_connection(self, ok):
        if ok:
            self.conn_dot.setStyleSheet("color: #3fb950;")
            self.conn_text.setText("MQTT 已连接")
        else:
            self.conn_dot.setStyleSheet("color: #f85149;")
            self.conn_text.setText("MQTT 未连接")

    def set_mode(self, mode):
        self.btn_auto.setChecked(mode == "auto")
        self.btn_manual.setChecked(mode == "manual")
        self.btn_auto.setProperty("active", mode == "auto")
        self.btn_manual.setProperty("active", mode == "manual")
        self.btn_auto.style().unpolish(self.btn_auto)
        self.btn_auto.style().polish(self.btn_auto)
        self.btn_manual.style().unpolish(self.btn_manual)
        self.btn_manual.style().polish(self.btn_manual)

    def update_data(self, mw0, mw20, mw21, mw22):
        # 温度
        if mw0 is not None:
            tf = mw0 / 100.0
            self.temp_value.setText(f"{tf:.1f}")
            self.temp_sub.setText(f"MW0 = {mw0}")
            tc = "#3fb950" if tf <= 30 else ("#d29922" if tf <= 35 else "#f85149")
            self.temp_value.setStyleSheet(f"color: {tc};")
        else:
            tf = None
            self.temp_value.setText("--.-")
            self.temp_value.setStyleSheet("color: #8b949e;")
            self.temp_sub.setText("等待 PLC 数据…")

        # 风扇状态
        is_auto = (mw21 == 1)
        if is_auto and tf is not None:
            fan_on = tf > 30.0
        else:
            fan_on = (mw20 == 1) if mw20 is not None else None

        if fan_on is True:
            self.fan_status.setText("● 运行中")
            self.fan_status.setStyleSheet("color: #3fb950; font-size: 15px; font-weight: bold;")
            self.fan_detail.setText("PLC 自动控制（>30°C）" if is_auto else f"手动 MW20 = {mw20}")
        elif fan_on is False:
            self.fan_status.setText("○ 已关闭")
            self.fan_status.setStyleSheet("color: #8b949e; font-size: 15px; font-weight: bold;")
            self.fan_detail.setText("PLC 自动控制（≤30°C）" if is_auto else f"手动 MW20 = {mw20}")
        else:
            self.fan_status.setText("—")
            self.fan_status.setStyleSheet("color: #8b949e; font-size: 15px; font-weight: bold;")
            self.fan_detail.setText("等待数据…")

        for name, val in [("MW0",mw0),("MW20",mw20),("MW21",mw21),("MW22",mw22)]:
            lbl = self.reg_value_labels.get(name)
            if lbl: lbl.setText(str(val) if val is not None else "—")

# ---- ChatPanel ----

class ChatPanel(QWidget):
    send_message = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("chatPanel")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 12); layout.setSpacing(8)

        title = QLabel("💬 AI 助手 · 小P"); title.setObjectName("chatTitle")
        layout.addWidget(title)

        self.scroll = QScrollArea()
        self.scroll.setObjectName("chatScrollArea")
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setFrameShape(QFrame.NoFrame)

        self.chat_container = QWidget()
        self.chat_layout = QVBoxLayout(self.chat_container)
        self.chat_layout.setContentsMargins(4, 4, 4, 4)
        self.chat_layout.setSpacing(4)
        self.chat_layout.addStretch()
        self.scroll.setWidget(self.chat_container)
        layout.addWidget(self.scroll, 1)

        # 快捷命令
        for cmds in [(["📊 查询温度","查询温度"],["🔄 自动模式","自动模式"],["✋ 手动模式","手动模式"]),
                      (["❄ 开风扇","开"],["⏻ 关风扇","关"])]:
            row = QHBoxLayout(); row.setSpacing(6)
            for label, cmd in cmds:
                btn = QPushButton(label); btn.setObjectName("quickCmdButton")
                btn.clicked.connect(lambda checked=False, c=cmd: self.send_message.emit(c))
                row.addWidget(btn)
            row.addStretch(); layout.addLayout(row)

        # 输入行
        ir = QHBoxLayout(); ir.setSpacing(8)
        self.input_field = QLineEdit()
        self.input_field.setObjectName("chatInput")
        self.input_field.setPlaceholderText("输入消息，Enter 发送…")
        self.input_field.returnPressed.connect(self._on_send)
        ir.addWidget(self.input_field, 1)
        self.send_btn = QPushButton("发送")
        self.send_btn.setObjectName("sendButton")
        self.send_btn.clicked.connect(self._on_send)
        ir.addWidget(self.send_btn); layout.addLayout(ir)

        self._thinking_bubble = None

    def _on_send(self):
        text = self.input_field.text().strip()
        if text:
            self.send_message.emit(text)
            self.input_field.clear()

    def add_bubble(self, text, is_user=True):
        bubble = ChatBubble(text, is_user)
        row = QWidget()
        rl = QHBoxLayout(row); rl.setContentsMargins(0, 1, 0, 1); rl.setSpacing(0)
        if is_user: rl.addStretch(); rl.addWidget(bubble)
        else: rl.addWidget(bubble); rl.addStretch()
        self.chat_layout.insertWidget(self.chat_layout.count() - 1, row)
        self._scroll_bottom()

    def add_system(self, text):
        bubble = SystemBubble(text)
        self.chat_layout.insertWidget(self.chat_layout.count() - 1, bubble)
        self._scroll_bottom()

    def _scroll_bottom(self):
        QTimer.singleShot(50, lambda: self.scroll.verticalScrollBar().setValue(
            self.scroll.verticalScrollBar().maximum()))

    def show_thinking(self):
        self._thinking_bubble = SystemBubble("🤔 思考中…")
        self.chat_layout.insertWidget(self.chat_layout.count() - 1, self._thinking_bubble)
        self._scroll_bottom()

    def hide_thinking(self):
        if self._thinking_bubble:
            self._thinking_bubble.deleteLater()
            self._thinking_bubble = None

# ---- TempChartDialog ----

class TempChartDialog(QDialog):
    """温度趋势图弹窗。"""

    def __init__(self, temp_history, parent=None):
        super().__init__(parent)
        self.setWindowTitle("📈 手机温度趋势")
        self.resize(820, 460)
        self.setMinimumSize(600, 360)

        from PySide6.QtCharts import (
            QChart, QChartView, QLineSeries, QValueAxis, QDateTimeAxis,
        )

        chart = QChart()
        chart.setTitle("手机温度变化趋势")
        chart.setAnimationOptions(QChart.AnimationOption.SeriesAnimations)
        chart.setTheme(QChart.ChartTheme.ChartThemeDark)
        chart.setBackgroundBrush(QColor("#0d1117"))
        chart.setTitleBrush(QColor("#c9d1d9"))
        chart.legend().setLabelColor(QColor("#8b949e"))

        # 温度曲线
        series = QLineSeries()
        series.setName("温度")
        series.setColor(QColor("#58a6ff"))
        pen = series.pen()
        pen.setWidth(2)
        series.setPen(pen)

        if temp_history:
            for ts, temp in temp_history:
                series.append(ts * 1000, temp)  # QDateTimeAxis 用毫秒

        chart.addSeries(series)

        # X 轴 — 时间
        axis_x = QDateTimeAxis()
        axis_x.setFormat("HH:mm:ss")
        axis_x.setTitleText("时间")
        axis_x.setLabelsColor(QColor("#8b949e"))
        axis_x.setTitleBrush(QColor("#8b949e"))
        axis_x.setGridLineColor(QColor("#21262d"))
        chart.addAxis(axis_x, Qt.AlignmentFlag.AlignBottom)
        series.attachAxis(axis_x)

        # Y 轴 — 温度
        axis_y = QValueAxis()
        axis_y.setTitleText("温度 (°C)")
        axis_y.setLabelsColor(QColor("#8b949e"))
        axis_y.setTitleBrush(QColor("#8b949e"))
        axis_y.setGridLineColor(QColor("#21262d"))
        if temp_history:
            temps = [t for _, t in temp_history]
            min_t = min(temps) - 2
            max_t = max(temps) + 2
            axis_y.setRange(max(0, min_t), min(60, max_t if max_t > 30 else 35))
        else:
            axis_y.setRange(0, 40)
        chart.addAxis(axis_y, Qt.AlignmentFlag.AlignLeft)
        series.attachAxis(axis_y)

        # 30°C 阈值线
        if temp_history:
            threshold_series = QLineSeries()
            threshold_series.setName("阈值 30°C")
            threshold_series.setColor(QColor("#f85149"))
            tpen = threshold_series.pen()
            tpen.setWidth(1)
            tpen.setStyle(Qt.PenStyle.DashLine)
            threshold_series.setPen(tpen)
            first_ts = temp_history[0][0] * 1000
            last_ts = temp_history[-1][0] * 1000
            threshold_series.append(first_ts, 30)
            threshold_series.append(last_ts, 30)
            chart.addSeries(threshold_series)
            threshold_series.attachAxis(axis_x)
            threshold_series.attachAxis(axis_y)

        chart_view = QChartView(chart)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(chart_view)

        # 关闭按钮
        btn_close = QPushButton("关闭")
        btn_close.setObjectName("quickCmdButton")
        btn_close.clicked.connect(self.close)
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_row.addWidget(btn_close)
        btn_row.setContentsMargins(12, 6, 12, 10)
        layout.addLayout(btn_row)


# ---- MainWindow ----

class MainWindow(QMainWindow):
    _trigger_ai = Signal()

    def __init__(self):
        super().__init__()
        self.setWindowTitle("DeepSeek PLC AI Controller — 手机散热风扇")
        self.resize(1100, 680)
        self.setMinimumSize(900, 560)

        self._latest_mw0 = None
        self._latest_mw20 = 0
        self._latest_mw21 = 0
        self._latest_mw22 = 0
        self._data_lock = threading.Lock()
        self._temp_history = deque(maxlen=600)  # 温度历史（10 分钟，1 点/秒）
        self._mqtt_connected = False
        self._current_mode = "auto"
        self._pending_ai = False

        self._config = load_config(CONFIG_PATH, DEFAULT_CONFIG)
        self._mqtt_cfg = self._config["local_mqtt"]
        self._ctrl_cfg = self._config["control"]
        self._state_file = Path(self._ctrl_cfg.get("state_file", "controller_state.json"))
        self._device_sn = self._ctrl_cfg.get("device_sn", "bistu11")
        self._current_mode = load_mode_state(self._state_file)

        self._emitter = SignalEmitter()
        self._setup_ui()
        self._emitter.connected.connect(self._on_mqtt_connected)
        self._emitter.disconnected.connect(self._on_mqtt_disconnected)
        self._emitter.data_updated.connect(self._refresh_dashboard)
        self._dashboard.mode_switch_requested.connect(self._on_mode_switch)
        self._chat.send_message.connect(self._on_user_message)
        self._setup_mqtt()
        self._setup_ai_worker()
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._refresh_dashboard)
        self._refresh_timer.start(1000)

        self._chat.add_bubble(
            "你好！我是 PLC 控制助手小P。\n可以帮你查询温度、控制风扇、切换模式。",
            is_user=False)
        self._chat.add_system(f"版本 {VERSION}  |  设备 {self._device_sn}")

    def _setup_ui(self):
        central = QWidget(); self.setCentralWidget(central)
        root = QHBoxLayout(central); root.setContentsMargins(0,0,0,0); root.setSpacing(0)
        self._dashboard = DashboardPanel()
        self._chat = ChatPanel()
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._dashboard); splitter.addWidget(self._chat)
        splitter.setStretchFactor(0, 3); splitter.setStretchFactor(1, 7)
        splitter.setHandleWidth(2); root.addWidget(splitter)
        self._dashboard.set_mode(self._current_mode)
        self._dashboard.btn_trend.clicked.connect(self._show_trend_chart)

    def _show_trend_chart(self):
        """打开温度趋势图弹窗。"""
        dlg = TempChartDialog(self._temp_history, self)
        dlg.setStyleSheet(QSS_THEME)
        dlg.exec()

    def _setup_mqtt(self):
        self._mqtt_client = mqtt.Client(
            client_id=f"gui-{int(time.time())}",
            protocol=mqtt_protocol(self._mqtt_cfg.get("mqtt_version", "3.1.1")),
        )
        self._mqtt_client.reconnect_delay_set(min_delay=1, max_delay=30)
        if self._mqtt_cfg.get("username"):
            self._mqtt_client.username_pw_set(self._mqtt_cfg.get("username"),
                                               self._mqtt_cfg.get("password", ""))

        it = self._mqtt_cfg["input_topic"]
        ct = self._mqtt_cfg["command_topic"]

        def on_connect(c, u, flags, rc, props=None):
            if rc == 0:
                c.subscribe(it, qos=0); c.subscribe(ct, qos=0)
                self._emitter.connected.emit()
            else:
                self._emitter.disconnected.emit()

        def on_disconnect(c, u, rc, props=None):
            self._emitter.disconnected.emit()

        def on_message(c, u, msg):
            text = msg.payload.decode("utf-8", errors="replace")
            with self._data_lock:
                if msg.topic == it:
                    _, mw0, mw21, mw22 = parse_input_payload(text)
                    if mw0 is not None: self._latest_mw0 = mw0
                    if mw21 is not None: self._latest_mw21 = mw21
                    if mw22 is not None: self._latest_mw22 = mw22
                    log(f"GUI input  MW0={mw0} MW21={mw21} MW22={mw22}")
                elif msg.topic == ct:
                    mw20, mw21, mw22 = parse_command_payload(text)
                    if mw20 is not None: self._latest_mw20 = mw20
                    if mw21 is not None: self._latest_mw21 = mw21
                    if mw22 is not None: self._latest_mw22 = mw22
                    log(f"GUI cmd    MW20={mw20} MW21={mw21} MW22={mw22}")
            self._emitter.data_updated.emit()

        self._mqtt_client.on_connect = on_connect
        self._mqtt_client.on_disconnect = on_disconnect
        self._mqtt_client.on_message = on_message
        self._mqtt_client.connect_async(self._mqtt_cfg["host"],
                                         int(self._mqtt_cfg["port"]), keepalive=60)
        self._mqtt_client.loop_start()

    def _setup_ai_worker(self):
        self._ai_thread = QThread()
        self._ai_worker = DeepSeekWorker()
        self._ai_worker.moveToThread(self._ai_thread)
        self._ai_worker.finished.connect(self._on_ai_result)
        self._ai_worker.error.connect(self._on_ai_error)
        self._trigger_ai.connect(self._ai_worker.do_request)
        self._ai_thread.start()

    @Slot()
    def _on_mqtt_connected(self):
        self._mqtt_connected = True
        self._dashboard.set_connection(True)

    @Slot()
    def _on_mqtt_disconnected(self):
        self._mqtt_connected = False
        self._dashboard.set_connection(False)

    def _refresh_dashboard(self):
        with self._data_lock:
            mw0, mw20, mw21, mw22 = (self._latest_mw0, self._latest_mw20,
                                       self._latest_mw21, self._latest_mw22)
        self._dashboard.update_data(mw0, mw20, mw21, mw22)
        # 记录温度历史（用于趋势图）
        if mw0 is not None:
            self._temp_history.append((time.time(), mw0 / 100.0))
        mode = load_mode_state(self._state_file)
        if mode != self._current_mode:
            self._current_mode = mode
            self._dashboard.set_mode(mode)

    @Slot(str)
    def _on_mode_switch(self, target_mode):
        if target_mode == self._current_mode: return
        save_mode_state(self._state_file, target_mode, reason="gui_button")
        self._current_mode = target_mode
        self._dashboard.set_mode(target_mode)
        if self._mqtt_connected:
            if target_mode == "auto":
                self._publish_cmd(mw20=0, mw21=1, mw22=0)
            else:
                self._publish_cmd(mw20=0, mw21=0, mw22=1)

    @Slot(str)
    def _on_user_message(self, text):
        self._chat.add_bubble(text, is_user=True)
        q = text.strip().lower()

        mode = load_mode_state(self._state_file)
        if mode != self._current_mode:
            self._current_mode = mode
            self._dashboard.set_mode(mode)

        if q in ("exit", "quit", "退出"):
            self._chat.add_system("再见～"); self.close(); return
        if q in ("自动模式", "自动", "auto"):
            self._on_mode_switch("auto"); return
        if q in ("手动模式", "手动", "manual", "自定义模式", "自定义"):
            self._on_mode_switch("manual"); return
        if q in ("开", "打开", "启动", "开启", "开灯", "开风扇"):
            if self._current_mode != "manual":
                self._chat.add_bubble("请先切换到 ✋ 手动模式。", is_user=False)
            elif self._mqtt_connected:
                self._publish_cmd(mw20=1, mw21=0, mw22=1)
                self._chat.add_bubble("已开风扇 ❄ MW20=1", is_user=False)
            return
        if q in ("关", "关闭", "停止", "关灯", "关风扇"):
            if self._current_mode != "manual":
                self._chat.add_bubble("请先切换到 ✋ 手动模式。", is_user=False)
            elif self._mqtt_connected:
                self._publish_cmd(mw20=0, mw21=0, mw22=1)
                self._chat.add_bubble("已关风扇 ⏻ MW20=0", is_user=False)
            return

        with self._data_lock:
            mw0, mw20, mw21, mw22 = (self._latest_mw0, self._latest_mw20,
                                       self._latest_mw21, self._latest_mw22)
        if mw0 is None:
            self._chat.add_bubble("还没有收到 PLC 数据，请检查 MQTT 连接。", is_user=False)
            return
        if self._pending_ai:
            self._chat.add_system("AI 思考中，请稍候…"); return

        self._pending_ai = True
        self._chat.show_thinking()
        self._ai_worker.set_request(self._config, mw0, text, mw20, mw21, mw22, self._current_mode)
        self._trigger_ai.emit()

    @Slot(dict)
    def _on_ai_result(self, result):
        self._pending_ai = False; self._chat.hide_thinking()
        text = result.get("text") or result.get("test") or "(无回复)"
        mode_cmd = result.get("mode"); mw20_cmd = result.get("mw20")
        extras = []

        if mode_cmd in ("auto", "manual") and mode_cmd != self._current_mode:
            save_mode_state(self._state_file, mode_cmd, reason="ai_chat")
            self._current_mode = mode_cmd; self._dashboard.set_mode(mode_cmd)
            if self._mqtt_connected:
                if mode_cmd == "auto": self._publish_cmd(mw20=0, mw21=1, mw22=0)
                else: self._publish_cmd(mw20=0, mw21=0, mw22=1)
            extras.append(f"已切换到 {'🔄 自动' if mode_cmd == 'auto' else '✋ 手动'} 模式")

        if mw20_cmd is not None and self._current_mode == "manual":
            try:
                v = int(mw20_cmd)
                if v in (0, 1) and self._mqtt_connected:
                    self._publish_cmd(mw20=v)
                    extras.append(f"{'已开风扇 ❄' if v == 1 else '已关风扇 ⏻'} (MW20={v})")
            except (ValueError, TypeError): pass

        if extras: text += "\n\n" + "\n".join(f"• {e}" for e in extras)
        self._chat.add_bubble(text, is_user=False)

    @Slot(str)
    def _on_ai_error(self, err):
        self._pending_ai = False; self._chat.hide_thinking()
        self._chat.add_bubble(f"⚠ AI 调用失败：{err}", is_user=False)

    def _publish_cmd(self, mw20=0, mw21=None, mw22=None):
        payload = build_command_payload(self._device_sn, mw20=mw20, mw21=mw21, mw22=mw22)
        try:
            info = self._mqtt_client.publish(self._mqtt_cfg["command_topic"],
                                              payload, qos=0, retain=False)
            info.wait_for_publish(timeout=5)
        except Exception as exc:
            log(f"GUI publish failed: {exc}")

    def closeEvent(self, event):
        self._refresh_timer.stop()
        if self._mqtt_client:
            self._mqtt_client.loop_stop()
            self._mqtt_client.disconnect()
        if self._ai_thread:
            self._ai_thread.quit()
            self._ai_thread.wait(3000)
        event.accept()


# ===========================================================================
# 8. 命令行终端（独立运行，不依赖 GUI）
# ===========================================================================

def run_chat_terminal():
    """阻塞式命令行 REPL。"""
    try:
        signal.signal(signal.SIGINT, lambda s, f: sys.exit(0))
        signal.signal(signal.SIGTERM, lambda s, f: sys.exit(0))
    except ValueError:
        pass
    ChatTerminal().run()


# ===========================================================================
# 9. 主入口
# ===========================================================================

def main():
    if len(sys.argv) > 1:
        cmd = sys.argv[1].lower()
        if cmd in ("auto", "controller"):
            # 仅自动控制器
            try:
                signal.signal(signal.SIGINT, lambda s, f: sys.exit(0))
                signal.signal(signal.SIGTERM, lambda s, f: sys.exit(0))
            except ValueError: pass
            AutoController().run()
        elif cmd in ("chat", "terminal"):
            # 仅命令行终端
            run_chat_terminal()
        elif cmd in ("gui", "graphical"):
            # 仅 GUI
            app = QApplication(sys.argv)
            app.setStyle("Fusion")
            app.setStyleSheet(QSS_THEME)
            w = MainWindow()
            w.show()
            sys.exit(app.exec())
        else:
            print(f"用法: python controller.py [auto|chat|gui]")
            print(f"      python controller.py          (自动控制器 + GUI)")
            sys.exit(1)
    else:
        # 默认：自动控制器（后台线程）+ GUI（前台窗口）+ 控制台日志
        log(f"DeepSeek PLC AI Controller v{VERSION}")
        log("启动：自动控制器（后台线程） + GUI（前台窗口）")
        log("控制台显示实时日志，关闭 GUI 窗口退出。")
        log("")

        auto_ctrl = AutoController()
        auto_thread = threading.Thread(target=auto_ctrl.run, daemon=True)
        auto_thread.start()
        log("自动控制器已启动（后台线程）")

        app = QApplication(sys.argv)
        app.setStyle("Fusion")
        app.setStyleSheet(QSS_THEME)
        w = MainWindow()
        w.show()
        log("GUI 已启动（前台窗口）")

        exit_code = app.exec()
        auto_ctrl.stop()
        log("所有组件已退出。")
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
