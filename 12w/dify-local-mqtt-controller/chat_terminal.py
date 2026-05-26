# -*- coding: utf-8 -*-
"""
DeepSeek PLC AI Chat Terminal

作用：
1. 连接本地 MQTT Broker，实时缓存 PLC 数据（MW0、MW20）。
2. 提供交互式命令行，用户输入自然语言查询或控制指令。
3. 调用 DeepSeek API，获取 AI 回答 + 结构化指令（mode/mw20）。
4. 切换全局运行模式（auto / manual），通过 controller_state.json 与原控制器共享。
5. 在 manual 模式下，根据 AI 返回的 mw20 直接发 MQTT 命令到 PLC。

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

VERSION = "deepseek-chat-terminal-20260526"

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
    "deepseek": {
        "api_key": "sk-ca2ba35803054b38a1d20176c0edefa9",
        "api_url": "https://api.deepseek.com/v1/chat/completions",
        "model": "deepseek-chat",
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


CHAT_SYSTEM_PROMPT = """\
你是PLC控制助手，名叫"小P"。你负责帮助用户查询PLC状态和控制设备。

当前信息：
- 温度：{temp} 度
- 控制状态：MW20={mw20}（0=关闭, 1=开启）
- 自动标志：MW21={mw21}（1=自动模式激活）
- 手动标志：MW22={mw22}（1=手动模式激活）
- 运行模式：{mode}（auto=自动, manual=手动）

重要：PLC内部自动模式(MW21)优先级高于手动模式。切换到手动模式时，
MW21必须设为0，MW22设为1，否则PLC不会接受手动指令。

用户输入：{user_query}

请根据以上信息，生成一段友好的中文回复，并判断用户意图。

你必须只回复一个JSON对象，不要任何其他文字：
{{"text": "你的中文回复", "mode": null, "mw20": null}}

规则：
- 如果是在"自动模式"下要求实现"打开"或者"关闭"，那么由你先实现切换到"手动模式"然后实现要求的功能
- "自动模式"/"自动" → mode="auto", text简短确认切换结果（系统会设MW21=1,MW22=0）
- "手动模式"/"手动"/"自定义模式"/"自定义" → mode="manual", text简短确认切换结果（系统会设MW21=0,MW22=1让PLC接受手动指令）
- manual模式下："开"/"打开"/"启动"/"开启" → mw20=1, text告知已开启
- manual模式下："关"/"关闭"/"停止" → mw20=0, text告知已关闭
- auto模式下如果用户试图手动控制 → 提示先切到手动模式, mode=null, mw20=null
- 查询温度/状态 → text告知当前温度和状态（含MW21/MW22）, mode=null, mw20=null
- "查询"/"状态"/"温度" → 同查询
- 其他闲聊 → 只回复text, mode=null, mw20=null
- 回复要简短友好，用中文
"""


def call_deepseek_chat(config, mw0, user_query, current_mw20, current_mw21, current_mw22, current_mode):
    ds_config = config["deepseek"]
    api_key = ds_config.get("api_key", "")
    if not api_key or api_key == "PASTE_DEEPSEEK_API_KEY_HERE":
        raise RuntimeError("deepseek.api_key is not configured in config.json")

    temp = mw0 / 100.0 if mw0 is not None else "未知"
    mode_label = "自动" if current_mode == "auto" else "手动"

    system_prompt = CHAT_SYSTEM_PROMPT.format(
        temp=temp,
        mw20=current_mw20,
        mw21=current_mw21 if current_mw21 is not None else "?",
        mw22=current_mw22 if current_mw22 is not None else "?",
        mode=mode_label,
        user_query=user_query,
    )

    body = {
        "model": ds_config.get("model", "deepseek-chat"),
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_query},
        ],
        "temperature": 0.7,
        "max_tokens": 500,
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

    timeout = float(ds_config.get("timeout_sec", 60))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        error_text = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"DeepSeek API HTTP {exc.code}: {error_text}") from exc

    content = result["choices"][0]["message"]["content"]
    try:
        outputs = json.loads(content)
    except json.JSONDecodeError:
        raise RuntimeError(f"DeepSeek returned invalid JSON: {content}")

    return outputs


def publish_command(client, mqtt_config, device_sn, mw20_value, mw21=None, mw22=None):
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
                f"Published MW20={mw20_value} MW21={mw21} MW22={mw22} topic={mqtt_config['command_topic']}"
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
    print("  DeepSeek PLC AI Chat Terminal (MW21/MW22)")
    print(f"  Version: {VERSION}")
    print(f"  Device:  {control_config['device_sn']}")
    print(f"  MQTT:    {mqtt_config['host']}:{mqtt_config['port']}")
    print("-" * 50)
    print("  Commands:")
    print('    "查询温度"     - Ask about current temperature')
    print('    "自动模式"     - Switch to auto (MW21=1,MW22=0)')
    print('    "手动模式"     - Switch to manual (MW21=0,MW22=1)')
    print('    "开" / "关"    - Control PLC (manual mode only)')
    print("    Ctrl+C        - Exit")
    print("=" * 50)


def print_status(mw0, mw20, mw21, mw22, mode):
    temp = mw0 / 100.0 if mw0 is not None else None
    print(
        f"\n  [Status] Temp: {temp} C | Light: MW20={mw20} | Auto: MW21={mw21} | Manual: MW22={mw22} | Mode: {mode}"
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

    log(f"DeepSeek Chat Terminal version={VERSION}")
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

            # === 本地硬命令（不经过DeepSeek，100%可靠）===
            local_handled = True
            q = user_input.strip().lower()

            if q in ("exit", "quit", "退出"):
                break
            if q in ("help", "帮助", "?"):
                print_banner(config)
                continue

            # 模式切换
            if q in ("自动模式", "自动", "auto"):
                if current_mode != "auto":
                    save_mode_state(state_file, "auto", reason="local_cmd")
                    current_mode = "auto"
                    if local_connected:
                        publish_command(client, mqtt_config, device_sn, 0, mw21=1, mw22=0)
                    print("\n  [AI] 已切换到自动模式。MW21=1, MW22=0")
                    print(f"  [Action] Published MW21=1,MW22=0 to PLC")
                else:
                    print("\n  [AI] 当前已经是自动模式。")
            elif q in ("手动模式", "手动", "manual", "自定义模式", "自定义"):
                if current_mode != "manual":
                    save_mode_state(state_file, "manual", reason="local_cmd")
                    current_mode = "manual"
                    if local_connected:
                        publish_command(client, mqtt_config, device_sn, 0, mw21=0, mw22=1)
                    print("\n  [AI] 已切换到手动模式。MW21=0, MW22=1（已关闭PLC自动）")
                    print(f"  [Action] Published MW21=0,MW22=1 to PLC")
                else:
                    print("\n  [AI] 当前已经是手动模式。")

            # 手动控制（仅手动模式）
            elif q in ("开", "打开", "启动", "开启", "开灯"):
                if current_mode != "manual":
                    print("\n  [AI] 请先切换到手动模式再操作。")
                elif local_connected:
                    publish_command(client, mqtt_config, device_sn, 1, mw21=0, mw22=1)
                    print("\n  [AI] 已开灯。MW20=1, MW21=0, MW22=1")
                    print(f"  [Action] Published MW20=1,MW21=0,MW22=1 to PLC")
                else:
                    print("\n  [Error] MQTT disconnected")
            elif q in ("关", "关闭", "停止", "关灯"):
                if current_mode != "manual":
                    print("\n  [AI] 请先切换到手动模式再操作。")
                elif local_connected:
                    publish_command(client, mqtt_config, device_sn, 0, mw21=0, mw22=1)
                    print("\n  [AI] 已关灯。MW20=0, MW21=0, MW22=1")
                    print(f"  [Action] Published MW20=0,MW21=0,MW22=1 to PLC")
                else:
                    print("\n  [Error] MQTT disconnected")

            else:
                local_handled = False

            if local_handled:
                continue

            if mw0 is None:
                print(
                    "  [Error] No PLC data received yet. Please check MQTT connection."
                )
                continue

            seq += 1
            print(f"  [Thinking] Calling DeepSeek... (seq={seq})")

            try:
                outputs = call_deepseek_chat(
                    config, mw0, user_input, mw20 or 0, mw21 or 0, mw22 or 0, current_mode
                )
            except Exception as exc:
                print(f"  [Error] DeepSeek API call failed: {exc}")
                continue

            text = outputs.get("test") or outputs.get("text") or "(no response)"
            mode_cmd = outputs.get("mode")
            mw20_cmd = outputs.get("mw20")

            print(f"\n  [AI] {text}")

            if mode_cmd in ("auto", "manual") and mode_cmd != current_mode:
                if save_mode_state(
                    state_file, mode_cmd, reason=f"user_chat_seq_{seq}"
                ):
                    current_mode = mode_cmd
                    print(f"  [Mode] Switched to {mode_cmd}")
                else:
                    print(f"  [Error] Failed to save mode state")

            if mw20_cmd is not None and current_mode == "manual":
                try:
                    mw20_value = int(mw20_cmd)
                    if mw20_value in (0, 1):
                        if local_connected:
                            if publish_command(
                                client, mqtt_config, device_sn, mw20_value
                            ):
                                print(f"  [Action] Sent MW20={mw20_value} to PLC")
                            else:
                                print(
                                    f"  [Error] Failed to send MW20={mw20_value}"
                                )
                        else:
                            print(
                                f"  [Error] MQTT disconnected, cannot send command"
                            )
                    else:
                        print(
                            f"  [Warning] Invalid mw20={mw20_value}, must be 0 or 1"
                        )
                except (ValueError, TypeError):
                    print(f"  [Warning] Invalid mw20 value: {mw20_cmd}")
            elif mw20_cmd is not None and current_mode == "auto":
                print(
                    f"  [Info] Ignored mw20={mw20_cmd} because current mode is auto"
                )

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
