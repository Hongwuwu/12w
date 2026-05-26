# -*- coding: utf-8 -*-
"""
DeepSeek 本地 MQTT 自动控制器（Mode 感知版）

作用：
1. 连接客户现场的内网 MQTT Broker。
2. 订阅 PLC 上报 topic，解析 MW0（温度）。
3. 调用 DeepSeek API，把温度传给 AI 做开关判断。
4. 解析 AI 返回的 mw20，发布控制命令到 MQTT。
5. 通过 controller_state.json 感知全局模式，manual 模式下自动休眠。

注意：
- 这个文件是源码版，方便阅读和二次开发。
- 客户电脑正式运行时用打包好的 exe，不需要安装 Python。
- 如果客户要直接运行源码，需要 Python 以及 paho-mqtt 依赖。
"""

import json
import os
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
import warnings
from datetime import datetime, timezone
from pathlib import Path

import paho.mqtt.client as mqtt

from common import (
    build_command_payload,
    load_config,
    load_mode_state,
    log,
    merge_dict,
    mqtt_protocol,
    parse_input_payload,
)

warnings.filterwarnings("ignore", category=DeprecationWarning)

CONFIG_PATH = Path(os.getenv("CONTROLLER_CONFIG", "config.json"))

VERSION = "deepseek-auto-controller-20260526"

running = True
local_connected = False
state_lock = threading.Lock()
latest_payload = None
latest_mw0 = None
latest_payload_changed_at = 0.0
last_processed_payload = None
last_command_payload = None
bridge_seq = 0

STATE_FILE_PATH = Path("controller_state.json")

DEFAULT_CONFIG = {
    "local_mqtt": {
        "host": "192.168.31.197",
        "port": 1883,
        "username": "admin",
        "password": "mmadmin@12345",
        "input_topic": "bistu11/TagValues",
        "command_topic": "bistu11/MQTTSetValueCommand",
        "mqtt_version": "3.1.1",
    },
    "deepseek": {
        "api_key": "PASTE_DEEPSEEK_API_KEY_HERE",
        "api_url": "https://api.deepseek.com/v1/chat/completions",
        "model": "deepseek-chat",
        "timeout_sec": 30,
    },
    "control": {
        "process_interval_sec": 2.0,
        "dedup_input": True,
        "dedup_command": True,
        "publish_on_no_action": False,
        "state_file": "controller_state.json",
    },
}


def stop_handler(signum, frame):
    global running
    running = False
    log("Stop signal received, exiting...")


AUTO_SYSTEM_PROMPT = """\
你是PLC控制助手，名叫"小P"。你的任务是判断当前温度是否需要开启设备（灯光/散热）。

规则：
- 温度 > 30度：建议开启设备（mw20=1），帮助散热
- 温度 <= 30度：建议关闭设备（mw20=0），节省能源
- 如果温度数据异常（如null或负数），保持当前状态不变（mw20=-1表示保持不变）

你必须只回复一个JSON对象，不要任何其他文字：
{"mw20": 1, "reason": "简短中文原因"}"""


def _call_deepseek_api(config, messages):
    """通用 DeepSeek API 调用。"""
    ds_config = config["deepseek"]
    api_key = ds_config.get("api_key", "")
    if not api_key or api_key == "PASTE_DEEPSEEK_API_KEY_HERE":
        raise RuntimeError("deepseek.api_key is not configured in config.json")

    body = {
        "model": ds_config.get("model", "deepseek-chat"),
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": 200,
        "response_format": {"type": "json_object"},
    }

    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        ds_config["api_url"],
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    timeout = float(ds_config.get("timeout_sec", 30))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        error_text = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"DeepSeek API HTTP {exc.code}: {error_text}") from exc


def call_deepseek_auto(config, mw0):
    """调用 DeepSeek 判断是否需要开启设备。返回 (mw20, reason)。"""
    temp = mw0 / 100.0 if mw0 is not None else None
    if temp is None:
        return None, "no temperature data"

    user_msg = f"当前温度：{temp:.1f}度（MW0={mw0}）。请判断是否需要开启设备。"

    result = _call_deepseek_api(config, [
        {"role": "system", "content": AUTO_SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ])

    content = result["choices"][0]["message"]["content"]
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        raise RuntimeError(f"DeepSeek returned invalid JSON: {content}")

    mw20 = parsed.get("mw20")
    reason = parsed.get("reason", "")

    if mw20 is None or mw20 == -1:
        return None, f"no action: {reason}"
    try:
        mw20 = int(mw20)
    except (ValueError, TypeError):
        return None, f"invalid mw20={mw20}"
    if mw20 not in (0, 1):
        return None, f"mw20 must be 0 or 1, got {mw20}"

    return mw20, reason


def main():
    global running, local_connected, latest_payload, latest_mw0
    global latest_payload_changed_at, last_processed_payload, last_command_payload
    global bridge_seq, STATE_FILE_PATH

    try:
        signal.signal(signal.SIGINT, stop_handler)
        signal.signal(signal.SIGTERM, stop_handler)
    except ValueError:
        pass

    config = load_config(CONFIG_PATH, DEFAULT_CONFIG)
    mqtt_config = config["local_mqtt"]
    control_config = config["control"]

    state_file_config = control_config.get("state_file", "controller_state.json")
    STATE_FILE_PATH = Path(state_file_config)

    client = mqtt.Client(
        client_id=f"dify-local-controller-{int(time.time())}",
        protocol=mqtt_protocol(mqtt_config.get("mqtt_version", "3.1.1")),
    )
    client.reconnect_delay_set(min_delay=1, max_delay=30)

    if mqtt_config.get("username"):
        client.username_pw_set(
            mqtt_config.get("username"), mqtt_config.get("password", "")
        )

    def on_connect(mqtt_client, userdata, flags, rc, properties=None):
        global local_connected
        local_connected = rc == 0
        if local_connected:
            log(f"Connected LOCAL MQTT {mqtt_config['host']}:{mqtt_config['port']}")
            mqtt_client.subscribe(mqtt_config["input_topic"], qos=0)
            log(f"Subscribed LOCAL input topic: {mqtt_config['input_topic']}")
        else:
            log(f"Connect LOCAL MQTT failed rc={rc}")

    def on_disconnect(mqtt_client, userdata, rc, properties=None):
        global local_connected
        local_connected = False
        log(f"LOCAL MQTT disconnected rc={rc}")

    def on_message(mqtt_client, userdata, msg):
        global latest_payload, latest_mw0, latest_payload_changed_at
        payload_text = msg.payload.decode("utf-8", errors="replace")
        _, mw0 = parse_input_payload(payload_text)
        previous_payload = latest_payload
        with state_lock:
            latest_payload = payload_text
            latest_mw0 = mw0
            latest_payload_changed_at = time.time()
        if payload_text != previous_payload:
            log(f"LOCAL input received MW0={mw0}")

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message

    log(f"DeepSeek Auto Controller version={VERSION}")
    log(f"State file: {STATE_FILE_PATH.resolve()} (mode-aware patch enabled)")
    log(f"Connecting LOCAL MQTT {mqtt_config['host']}:{mqtt_config['port']}")

    client.connect_async(
        mqtt_config["host"], int(mqtt_config["port"]), keepalive=60
    )
    client.loop_start()

    interval = float(control_config.get("process_interval_sec", 2.0))
    next_process_time = 0.0

    while running:
        now = time.time()
        if now < next_process_time:
            time.sleep(0.1)
            continue
        next_process_time = now + interval

        with state_lock:
            payload_text = latest_payload
            mw0 = latest_mw0

        if not payload_text:
            continue

        mode_state = load_mode_state(STATE_FILE_PATH)
        if mode_state == "manual":
            if payload_text != last_processed_payload:
                log(
                    f"Manual mode active (by chat terminal), auto workflow suspended. MW0={mw0}"
                )
            last_processed_payload = payload_text
            continue

        if (
            control_config.get("dedup_input", True)
            and payload_text == last_processed_payload
        ):
            continue

        if mw0 is None:
            _, parsed_mw0 = parse_input_payload(payload_text)
            mw0 = parsed_mw0
        bridge_seq += 1

        try:
            log(f"Calling DeepSeek seq={bridge_seq} MW0={mw0}")
            mw20_value, reason = call_deepseek_auto(config, mw0)
            log(f"DeepSeek response seq={bridge_seq} mw20={mw20_value} reason={reason}")
        except Exception as exc:
            log(f"DeepSeek call failed seq={bridge_seq}: {exc}")
            last_processed_payload = payload_text
            continue

        last_processed_payload = payload_text

        if mw20_value is None:
            if control_config.get("publish_on_no_action", False):
                mw20_value = 0
            else:
                continue

        command_payload = build_command_payload(mw20_value, "bistu11")

        if (
            control_config.get("dedup_command", True)
            and command_payload == last_command_payload
        ):
            log(f"Duplicate command skipped payload={command_payload}")
            continue

        if not local_connected:
            log(
                f"LOCAL MQTT not connected, command skipped payload={command_payload}"
            )
            continue

        try:
            info = client.publish(
                mqtt_config["command_topic"], command_payload, qos=0, retain=False
            )
            info.wait_for_publish(timeout=5)
            if info.is_published():
                last_command_payload = command_payload
                log(
                    f"Published LOCAL command topic={mqtt_config['command_topic']} payload={command_payload}"
                )
            else:
                log(f"Publish LOCAL command timeout payload={command_payload}")
        except Exception as exc:
            log(f"Publish LOCAL command failed: {exc}")

    client.loop_stop()
    client.disconnect()
    log("Controller exited")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log(f"Fatal error: {exc}")
        sys.exit(1)
