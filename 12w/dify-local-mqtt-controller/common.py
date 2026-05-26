# -*- coding: utf-8 -*-
"""
Dify Local MQTT Controller - 共享工具模块。

提供两个组件共用的基础功能：
- 日志、配置加载与合并
- MQTT 协议版本选择
- PLC payload 解析
- 控制命令 payload 构建
- 全局模式状态文件读写（controller_state.json）
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import paho.mqtt.client as mqtt


def log(message):
    """打印带时间的日志。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {message}", flush=True)


def mqtt_protocol(version):
    """根据配置选择 MQTT 协议版本。"""
    if str(version) == "3.1":
        return mqtt.MQTTv31
    return mqtt.MQTTv311


def merge_dict(defaults, overrides):
    """把用户配置覆盖到默认配置上，避免用户漏填某些字段时报错。"""
    result = dict(defaults)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_dict(result[key], value)
        else:
            result[key] = value
    return result


def load_config(config_path, default_config):
    """
    读取 JSON 配置文件。
    如果文件不存在，生成默认文件并退出，提醒用户填写必要字段。
    """
    config_path = Path(config_path)
    if not config_path.exists():
        config_path.write_text(
            json.dumps(default_config, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        log(f"Created default config: {config_path.resolve()}")
        log("Please edit the config file and fill required fields, then restart.")
        sys.exit(2)

    with config_path.open("r", encoding="utf-8") as f:
        config = json.load(f)
    return merge_dict(default_config, config)


def parse_input_payload(payload_text):
    """
    解析 PLC 上报 payload，提取 MW0。

    期望格式：[{"DeviceSN":"bistu01","TagData":[{"Time":"...","MW0":3500}]}]

    返回 (parsed_data, mw0) 元组。
    """
    try:
        data = json.loads(payload_text)
        if not isinstance(data, list) or not data:
            return None, None
        tag_data = data[0].get("TagData") or []
        if not tag_data or not isinstance(tag_data[0], dict):
            return None, None
        return data, tag_data[0].get("MW0")
    except Exception:
        return None, None


def parse_command_payload(payload_text):
    """
    解析控制器发出的命令 payload，提取 MW20。

    期望格式：[{"DeviceSN":"bistu01","TagData":[{"MW20":1}]}]
    """
    try:
        data = json.loads(payload_text)
        if not isinstance(data, list) or not data:
            return None
        tag_data = data[0].get("TagData") or []
        if not tag_data or not isinstance(tag_data[0], dict):
            return None
        return tag_data[0].get("MW20")
    except Exception:
        return None


def build_command_payload(value, device_sn):
    """根据 MW20 值生成 PLC 控制命令 MQTT payload。"""
    payload = [{"DeviceSN": device_sn, "TagData": [{"MW20": int(value)}]}]
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def load_mode_state(state_file_path):
    """
    读取 controller_state.json，获取当前全局运行模式。

    返回 "auto" 或 "manual"。文件不存在或读取失败时默认返回 "auto"。
    """
    try:
        state_file = Path(state_file_path)
        if not state_file.exists():
            return "auto"
        with state_file.open("r", encoding="utf-8") as f:
            state = json.load(f)
        mode = state.get("mode", "auto")
        if mode in ("auto", "manual"):
            return mode
        return "auto"
    except Exception:
        return "auto"


def save_mode_state(state_file_path, mode, reason="", switched_by="chat_terminal"):
    """写入 controller_state.json，切换全局模式。"""
    try:
        state = {
            "mode": mode,
            "switched_at": datetime.now(timezone.utc).isoformat(),
            "switched_by": switched_by,
            "reason": reason,
        }
        with Path(state_file_path).open("w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        return True
    except Exception as exc:
        log(f"Save state file failed: {exc}")
        return False
