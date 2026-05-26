# -*- coding: utf-8 -*-
"""
Dify 本地 MQTT 自动控制器（Mode 感知版 + MW21/MW22）

作用：
1. 连接客户现场的内网 MQTT Broker。
2. 订阅 PLC 上报 topic，解析 MW0/MW21/MW22。
3. 调用 Dify Workflow API（工作流A），把 MW0 传给 Dify 做判断。
4. 读取 Dify 返回的 mw20，发布控制命令到 MQTT。
5. 自动模式：写入 MW20（散热）+ MW21=1（自动标志）+ MW22=0。
6. 通过 controller_state.json 感知全局模式，manual 模式下自动休眠。

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

VERSION = "local-api-controller-20260518-mode-patch"

running = True
local_connected = False
state_lock = threading.Lock()
latest_payload = None
latest_mw0 = None
latest_mw21 = None
latest_mw22 = None
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
    "dify": {
        "api_url": "https://api.dify.ai/v1/workflows/run",
        "api_key": "PASTE_DIFY_API_KEY_HERE",
        "user": "mqtt-local-controller",
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


def call_dify(config, payload_text, payload_json, mw0, seq):
    dify_config = config["dify"]
    api_key = dify_config.get("api_key", "")
    if not api_key or api_key == "PASTE_DIFY_API_KEY_HERE":
        raise RuntimeError("dify.api_key is not configured in config.json")

    bridge_iso = datetime.now(timezone.utc).isoformat()
    body = {
        "inputs": {
            "mqtt_payload_raw": payload_text,
            "mqtt_payload": payload_json,
            "mw0": mw0,
            "bridge_seq": seq,
            "bridge_time": bridge_iso,
        },
        "response_mode": "blocking",
        "user": dify_config.get("user", "mqtt-local-controller"),
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

    timeout = float(dify_config.get("timeout_sec", 30))
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


def normalize_command(outputs):
    """解析 Dify 输出，返回 (mw20, reason)。mw20 为 None 表示无动作。"""
    if not isinstance(outputs, dict):
        return None, "outputs is not an object"

    command_payload = outputs.get("command_payload") or outputs.get("mqtt_payload")
    if command_payload:
        if isinstance(command_payload, str):
            try:
                parsed = json.loads(command_payload)
                tag = parsed[0]["TagData"][0]
                return tag.get("MW20"), "command_payload"
            except Exception:
                return None, "invalid command_payload"
        tag = command_payload[0].get("TagData", [{}])[0]
        return tag.get("MW20"), "command_payload"

    mw20 = outputs.get("mw20")
    if mw20 is None:
        mw20 = outputs.get("MW20")
    if mw20 is not None:
        try:
            value = int(mw20)
        except Exception:
            return None, f"invalid mw20={mw20!r}"
        if value not in (0, 1):
            return None, f"mw20 must be 0 or 1, got {value}"
        return value, "mw20"

    action = outputs.get("action") or outputs.get("result")
    if isinstance(action, str):
        action_lower = action.strip().lower()
        if "open" in action_lower or action_lower in ("1", "on", "true"):
            return 1, f"action={action}"
        if "close" in action_lower or action_lower in ("0", "off", "false"):
            return 0, f"action={action}"
        if action_lower in ("none", "no_action", "noop", ""):
            return None, f"no action={action}"

    return None, "no command in Dify outputs"


def main():
    global running, local_connected, latest_payload, latest_mw0, latest_mw21, latest_mw22
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
    device_sn = control_config.get("device_sn", "bistu11")

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
        global latest_payload, latest_mw0, latest_mw21, latest_mw22, latest_payload_changed_at
        payload_text = msg.payload.decode("utf-8", errors="replace")
        _, mw0, mw21, mw22 = parse_input_payload(payload_text)
        with state_lock:
            latest_payload = payload_text
            latest_mw0 = mw0
            latest_mw21 = mw21
            latest_mw22 = mw22
            latest_payload_changed_at = time.time()
        log(f"LOCAL input received MW0={mw0} MW21={mw21} MW22={mw22}")

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message

    log(f"Dify Local MQTT Controller version={VERSION}")
    log(
        "Dify MQTT Trigger and Dify MQTT Publisher should be disabled in the workflow."
    )
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

        payload_json, parsed_mw0, _, _ = parse_input_payload(payload_text)
        if mw0 is None:
            mw0 = parsed_mw0
        bridge_seq += 1

        try:
            log(f"Calling Dify API seq={bridge_seq} MW0={mw0}")
            outputs, raw_result = call_dify(
                config, payload_text, payload_json, mw0, bridge_seq
            )
            mw20_value, reason = normalize_command(outputs)
            log(
                f"Dify outputs seq={bridge_seq} reason={reason} mw20={mw20_value} outputs={json.dumps(outputs, ensure_ascii=False)}"
            )
        except Exception as exc:
            log(f"Dify call failed seq={bridge_seq}: {exc}")
            last_processed_payload = payload_text
            continue

        last_processed_payload = payload_text

        if mw20_value is None:
            if control_config.get("publish_on_no_action", False):
                mw20_value = 0
            else:
                continue

        command_payload = build_command_payload(
            device_sn, mw20=mw20_value, mw21=1, mw22=0
        )

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
