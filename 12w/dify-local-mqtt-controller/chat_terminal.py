# -*- coding: utf-8 -*-
"""
Dify PLC AI Chat Terminal（MW21/MW22 版）

作用：
1. 连接本地 MQTT Broker，实时缓存 PLC 数据（MW0、MW20、MW21、MW22）。
2. 提供交互式命令行，用户输入自然语言查询或控制指令。
3. 调用 Dify 工作流 B（deepseek），获取 AI 回答 + 结构化指令（mode/mw20/mw21/mw22）。
4. 切换全局运行模式（auto / manual），通过 controller_state.json + MQTT 寄存器与原控制器共享。
5. manual 模式：写入 MW22=1,MW21=0；auto 模式：写入 MW21=1,MW22=0；MW20 控制散热。

注意：
- 本程序需要 Python 3 和 paho-mqtt。
- 运行前必须填写 config.json 中的 dify_chat.api_key。
- 保持窗口打开，按 Ctrl+C 退出。
"""

import json
import os
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import paho.mqtt.client as mqtt

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

CONFIG_PATH = Path(os.getenv("CHAT_CONFIG", "config.json"))

VERSION = "chat-terminal-20260518"

running = True
local_connected = False
state_lock = threading.Lock()
latest_payload = None
latest_mw0 = None
latest_mw20 = None
latest_mw21 = None
latest_mw22 = None
latest_payload_at = 0.0
current_mode = "auto"

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
    "dify_chat": {
        "api_url": "http://192.168.31.197/v1/workflows/run",
        "api_key": "PASTE_DIFY_CHAT_API_KEY_HERE",
        "user": "mqtt-chat-terminal",
        "timeout_sec": 60,
    },
    "control": {
        "state_file": "controller_state.json",
        "device_sn": "bistu11",
        "poll_mqtt_sec": 2.0,
    },
}


def stop_handler(signum, frame):
    global running
    running = False
    log("Stop signal received, exiting...")


def call_dify_chat(config, mw0, user_query, current_mw20, current_mw21, current_mw22, current_mode):
    dify_config = config["dify_chat"]
    api_key = dify_config.get("api_key", "")
    if not api_key or api_key == "PASTE_DIFY_CHAT_API_KEY_HERE":
        raise RuntimeError("dify_chat.api_key is not configured in config.json")

    body = {
        "inputs": {
            "mw0": mw0,
            "user_query": user_query,
            "current_mw20": current_mw20,
            "current_mw21": current_mw21,
            "current_mw22": current_mw22,
            "current_mode": current_mode,
        },
        "response_mode": "blocking",
        "user": dify_config.get("user", "mqtt-chat-terminal"),
    }

    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        dify_config["api_url"],
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    timeout = float(dify_config.get("timeout_sec", 60))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_text = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        error_text = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Dify API HTTP {exc.code}: {error_text}") from exc

    result = json.loads(response_text)
    data_obj = result.get("data") or {}
    status = data_obj.get("status")
    if status and status != "succeeded":
        raise RuntimeError(
            f"Dify workflow status={status}, error={data_obj.get('error')}"
        )

    outputs = data_obj.get("outputs") or result.get("outputs") or {}
    return outputs, result


def publish_command(client, mqtt_config, device_sn, mw20_value, mw21=None, mw22=None):
    """发布控制命令到 MQTT，可同时写入 MW20/MW21/MW22。"""
    command_payload = build_command_payload(
        device_sn, mw20=mw20_value, mw21=mw21, mw22=mw22
    )
    try:
        info = client.publish(
            mqtt_config["command_topic"], command_payload, qos=0, retain=False
        )
        info.wait_for_publish(timeout=5)
        if info.is_published():
            log(
                f"Published command MW20={mw20_value} MW21={mw21} MW22={mw22} topic={mqtt_config['command_topic']}"
            )
            return True
        else:
            log(f"Publish command timeout MW20={mw20_value}")
            return False
    except Exception as exc:
        log(f"Publish command failed: {exc}")
        return False


def print_banner(config):
    mqtt_config = config["local_mqtt"]
    control_config = config["control"]
    print("=" * 50)
    print("  Dify PLC AI Chat Terminal")
    print(f"  Version: {VERSION}")
    print(f"  Device:  {control_config['device_sn']}")
    print(f"  MQTT:    {mqtt_config['host']}:{mqtt_config['port']}")
    print("-" * 50)
    print("  Commands:")
    print('    "查询温度"     - Ask about current temperature')
    print('    "自动模式"     - Switch to auto control mode')
    print('    "自定义模式"   - Switch to manual control mode')
    print('    "开" / "关"    - Control PLC (manual mode only)')
    print("    Ctrl+C        - Exit")
    print("=" * 50)


def print_status(mw0, mw20, mw21, mw22, mode):
    temp = mw0 / 100.0 if mw0 is not None else None
    print(
        f"\n  [Status] Temp: {temp} C (MW0={mw0}) | Cooling: MW20={mw20} | Auto: MW21={mw21} | Manual: MW22={mw22} | Mode: {mode}"
    )


def main():
    global running, local_connected, latest_payload, latest_mw0, latest_mw20, latest_mw21, latest_mw22
    global latest_payload_at, current_mode

    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)

    config = load_config(CONFIG_PATH, DEFAULT_CONFIG)
    mqtt_config = config["local_mqtt"]
    control_config = config["control"]
    state_file = Path(control_config["state_file"])
    device_sn = control_config["device_sn"]

    client = mqtt.Client(
        client_id=f"dify-chat-terminal-{int(time.time())}",
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
            log(f"Subscribed input topic: {mqtt_config['input_topic']}")
            mqtt_client.subscribe(mqtt_config["command_topic"], qos=0)
            log(f"Subscribed command topic: {mqtt_config['command_topic']}")
        else:
            log(f"Connect LOCAL MQTT failed rc={rc}")

    def on_disconnect(mqtt_client, userdata, rc, properties=None):
        global local_connected
        local_connected = False
        log(f"LOCAL MQTT disconnected rc={rc}")

    def on_message(mqtt_client, userdata, msg):
        global latest_payload, latest_mw0, latest_mw20, latest_mw21, latest_mw22, latest_payload_at
        payload_text = msg.payload.decode("utf-8", errors="replace")
        topic = msg.topic

        with state_lock:
            if topic == mqtt_config["input_topic"]:
                _, mw0, mw21, mw22 = parse_input_payload(payload_text)
                latest_payload = payload_text
                latest_mw0 = mw0
                if mw21 is not None:
                    latest_mw21 = mw21
                if mw22 is not None:
                    latest_mw22 = mw22
                latest_payload_at = time.time()
            elif topic == mqtt_config["command_topic"]:
                mw20, mw21, mw22 = parse_command_payload(payload_text)
                if mw20 is not None:
                    latest_mw20 = mw20
                if mw21 is not None:
                    latest_mw21 = mw21
                if mw22 is not None:
                    latest_mw22 = mw22

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message

    log(f"Dify Chat Terminal version={VERSION}")
    log(f"Connecting LOCAL MQTT {mqtt_config['host']}:{mqtt_config['port']}")

    client.connect_async(
        mqtt_config["host"], int(mqtt_config["port"]), keepalive=60
    )
    client.loop_start()

    log("Waiting for MQTT connection...")
    wait_start = time.time()
    while not local_connected and time.time() - wait_start < 10:
        time.sleep(0.5)

    current_mode = load_mode_state(state_file)

    print_banner(config)
    print(f"\n  Current mode: {current_mode}")
    if not local_connected:
        print("  [Warning] MQTT not connected, PLC data unavailable\n")
    else:
        print("")

    seq = 0

    while running:
        try:
            with state_lock:
                mw0 = latest_mw0
                mw20 = latest_mw20
                mw21 = latest_mw21
                mw22 = latest_mw22

            current_mode = load_mode_state(state_file)

            print_status(mw0, mw20, mw21, mw22, current_mode)
            try:
                user_input = input("\n> ").strip()
            except EOFError:
                break

            if not user_input:
                continue

            if user_input.lower() in ("exit", "quit", "退出"):
                break
            if user_input.lower() in ("help", "帮助", "?"):
                print_banner(config)
                continue

            if mw0 is None:
                print(
                    "  [Error] No PLC data received yet. Please check MQTT connection."
                )
                continue

            seq += 1
            print(f"  [Thinking] Calling Dify API... (seq={seq})")

            try:
                outputs, raw_result = call_dify_chat(
                    config, mw0, user_input, mw20 or 0, mw21 or 0, mw22 or 0, current_mode
                )
            except Exception as exc:
                print(f"  [Error] Dify API call failed: {exc}")
                continue

            text = outputs.get("test") or outputs.get("text") or "(no response)"
            mode_cmd = outputs.get("mode")
            mw20_cmd = outputs.get("mw20")
            mw21_cmd = outputs.get("mw21")
            mw22_cmd = outputs.get("mw22")

            print(f"\n  [AI] {text}")

            if mode_cmd in ("auto", "manual") and mode_cmd != current_mode:
                if save_mode_state(
                    state_file, mode_cmd, reason=f"user_chat_seq_{seq}"
                ):
                    current_mode = mode_cmd
                    print(f"  [Mode] Switched to {mode_cmd}")
                    # 发布模式寄存器到 MQTT，让 PLC 内部程序感知模式变化
                    if local_connected:
                        if mode_cmd == "auto":
                            publish_command(
                                client, mqtt_config, device_sn, 0, mw21=1, mw22=0
                            )
                            print(f"  [Action] Published MW21=1,MW22=0 (auto mode) to PLC")
                        else:
                            publish_command(
                                client, mqtt_config, device_sn, 0, mw21=0, mw22=1
                            )
                            print(f"  [Action] Published MW22=1,MW21=0 (manual mode) to PLC")
                else:
                    print(f"  [Error] Failed to save mode state")

            if mw20_cmd is not None:
                try:
                    mw20_value = int(mw20_cmd)
                    if mw20_value not in (0, 1):
                        print(f"  [Warning] Invalid mw20={mw20_value}, must be 0 or 1")
                    elif current_mode == "manual":
                        if local_connected:
                            if publish_command(
                                client, mqtt_config, device_sn, mw20_value, mw21=0, mw22=1
                            ):
                                print(f"  [Action] Sent MW20={mw20_value},MW22=1 (manual) to PLC")
                            else:
                                print(f"  [Error] Failed to send command")
                        else:
                            print(f"  [Error] MQTT disconnected, cannot send command")
                    else:
                        # auto 模式下也允许聊天终端发命令（MW21=1,MW22=0）
                        if local_connected:
                            if publish_command(
                                client, mqtt_config, device_sn, mw20_value, mw21=1, mw22=0
                            ):
                                print(f"  [Action] Sent MW20={mw20_value},MW21=1 (auto) to PLC")
                            else:
                                print(f"  [Error] Failed to send command")
                        else:
                            print(f"  [Error] MQTT disconnected, cannot send command")
                except (ValueError, TypeError):
                    print(f"  [Warning] Invalid mw20 value: {mw20_cmd}")

            # 处理 Dify 直接返回的 mw21/mw22 指令（不通过模式切换）
            if mw20_cmd is None and (mw21_cmd is not None or mw22_cmd is not None):
                if local_connected:
                    m21 = int(mw21_cmd) if mw21_cmd is not None else None
                    m22 = int(mw22_cmd) if mw22_cmd is not None else None
                    publish_command(
                        client, mqtt_config, device_sn, 0, mw21=m21, mw22=m22
                    )
                    print(f"  [Action] Published MW21={m21},MW22={m22} to PLC")

        except KeyboardInterrupt:
            break
        except Exception as exc:
            log(f"Error in main loop: {exc}")

    client.loop_stop()
    client.disconnect()
    log("Chat terminal exited")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log(f"Fatal error: {exc}")
        sys.exit(1)
