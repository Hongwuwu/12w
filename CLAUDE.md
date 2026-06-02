# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

DeepSeek Local MQTT Controller — an industrial IoT bridge between PLC devices, a local MQTT broker, and the DeepSeek AI API. It reads sensor data (temperature via MW0 register) from PLCs over MQTT, sends it to DeepSeek for AI-based control decisions, and publishes control commands (MW20/MW21/MW22 registers) back to PLCs.

Originally built on Dify workflows (see git history `00e9326`), now uses direct DeepSeek API calls. The Dify workflow names persist in some variable/comment naming as legacy artifacts.

## Commands

```bash
# Install dependencies
pip install paho-mqtt
pip install PySide6==6.7.3    # GUI 必需

# 默认：自动控制器（后台线程）+ GUI（前台窗口）—— 推荐
python main.py

# 仅自动控制器
python main.py auto

# 仅命令行终端
python main.py chat

# 仅 GUI
python main.py gui

# Windows: 双击 run.bat 菜单选择

# Package into standalone exe
pyinstaller --onefile --name dify_local_controller auto_controller.py
pyinstaller --onefile --name dify_chat_terminal chat_terminal.py
pyinstaller --onefile --windowed --name dify_gui chat_gui.py
```

## Architecture

Three independently runnable frontends share `common.py` and coordinate through `controller_state.json`:

```
main.py  ──┬── auto_controller.py  ── MQTT subscribe ──► PLC data (MW0/MW21/MW22)
           │                           DeepSeek API    ──► AI decision → MW20 command
           │                           MQTT publish    ──► PLC commands
           │
           ├── chat_terminal.py    ── MQTT subscribe ──► PLC data (MW0/MW20/MW21/MW22)
           │                           stdin            ──► user natural language input
           │                           DeepSeek API     ──► AI response + commands
           │
           └── chat_gui.py         ── MQTT subscribe ──► PLC data (MW0/MW20/MW21/MW22)
                                      PySide6 GUI       ──► dashboard + chat bubbles
                                      DeepSeek API      ──► AI response + commands
```

**`common.py`** — Shared layer: config loading with defaults merging (`merge_dict`), MQTT protocol version selection, PLC payload parsing (`parse_input_payload` extracts MW0/MW21/MW22; `parse_command_payload` extracts MW20/MW21/MW22), command payload builder (`build_command_payload`), and global mode state file read/write (`load_mode_state` / `save_mode_state`).

**`auto_controller.py`** — Autonomous control loop. Subscribes to the PLC input MQTT topic, extracts MW0 (temperature × 100), calls DeepSeek with a system prompt that decides whether to turn equipment on/off based on temperature thresholds (>30°C = on, ≤30°C = off), and publishes the returned MW20 command. Deduplicates consecutive identical inputs and commands. When the shared mode is `manual` OR when PLC registers show MW22=1 and MW21=0, it suspends itself.

**`chat_terminal.py`** — Console REPL. Subscribes to both input and command MQTT topics for live MW0/MW20/MW21/MW22 state. Contains **local hard commands** that bypass DeepSeek for mode switching (`自动模式`/`手动模式`) and manual control (`开`/`关`). In `manual` mode, publishes MW20 commands to MQTT. Also has a background daemon thread that silently refreshes data every 0.5s.

**`chat_gui.py`** — PySide6 GUI (Fusion style + dark QSS theme). Split-panel layout: left dashboard (temperature with color coding, fan status, connection indicator, mode buttons, PLC register grid), right chat panel (ChatBubble history, quick command buttons, input field). Uses `SignalEmitter` QObject to bridge paho MQTT callbacks to the GUI thread, and `DeepSeekWorker` on a QThread for non-blocking API calls. Local hard commands are handled client-side without API calls.

## Mode coordination (auto/manual)

Both components read `controller_state.json` (path from `control.state_file` in `config.json`). The chat terminal writes mode changes; the auto controller polls the file each cycle.

**Dual-check mechanism in auto controller**: checks BOTH `controller_state.json` AND PLC registers (MW22=1 and MW21=0 indicates PLC-level manual mode). This prevents the auto controller from fighting PLC-internal manual mode.

**PLC register semantics**:
- MW0: temperature × 100 (e.g., 3500 = 35.00°C)
- MW20: binary control (0 = close/off, 1 = open/on)
- MW21: auto mode flag (1 = PLC internal auto active, takes priority over manual)
- MW22: manual mode flag (1 = manual mode, must set MW21=0 simultaneously)

**Critical constraint**: PLC internal auto mode (MW21) has priority over manual mode. When switching to manual, the controller MUST set MW21=0 and MW22=1, otherwise the PLC will not accept manual commands. When switching back to auto, set MW21=1, MW22=0.

## Configuration

Single `config.json` with three sections:
- `local_mqtt` — broker host/port/credentials, input and command topics, MQTT protocol version
- `deepseek` — API key, API URL, model name, timeout
- `control` — state file path, device SN, poll interval, dedup toggles, publish-on-no-action

On first run, if `config.json` doesn't exist, a default is generated and the program exits so the user can fill in API keys. Each component (`auto_controller.py` and `chat_terminal.py`) has its own independent defaults dict — there is no single shared default config.

## Key implementation details

- **PyInstaller packaging is the intended distribution method** — customer machines run `.exe` files, not Python. Source code is maintained for development and customization.
- **Local hard commands** in `chat_terminal.py` (lines 337–393) bypass DeepSeek for mode switching and manual control. These are the authoritative code path for those operations — do not remove or weaken them. The git history shows they were added specifically because AI-based command parsing was unreliable.
- **`config.json` contains API keys** — never commit real keys. The current file in the working tree has keys that were already rotated after a GitGuardian alert (see commit `96fd856`).
- The project is Windows-first (`run.bat` launcher), but the Python source is cross-platform.
- Both components use `paho-mqtt` with async connect (`connect_async`) and the threaded loop (`loop_start`), not the blocking `loop_forever`.
- The auto controller uses `urllib.request` directly (stdlib, no `requests` dependency) for DeepSeek API calls.
