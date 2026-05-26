# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Dify Local MQTT Controller — an industrial IoT bridge between PLC devices, a local MQTT broker, and Dify AI workflows. It reads sensor data (temperature via MW0 register) from PLCs over MQTT, sends it to Dify for AI analysis, and publishes control commands (MW20 register) back to PLCs.

## Commands

```bash
# Run both auto controller + chat terminal (default)
python main.py

# Run only the auto controller
python main.py auto

# Run only the interactive chat terminal
python main.py chat

# Windows: double-click run.bat for a menu-driven launcher

# Package into a standalone exe (no Python required on target machine)
pyinstaller --onefile --name dify_local_controller auto_controller.py
pyinstaller --onefile --name dify_chat_terminal chat_terminal.py
```

**Dependency:** `paho-mqtt` (the only third-party package). Install with `pip install paho-mqtt`.

## Architecture

Two independently runnable components that coordinate through a shared JSON state file:

```
main.py  ──┬── auto_controller.py  ── MQTT (subscribe) ──► PLC data (MW0)
           │                           MQTT (publish)   ──► PLC commands (MW20)
           │                           Dify Workflow A  ──► AI decision → MW20
           │
           └── chat_terminal.py   ── MQTT (subscribe) ──► PLC data (MW0, MW20)
                                     stdin              ──► user natural language
                                     Dify Workflow B    ──► AI response + commands
```

**`common.py`** — Shared layer used by both components: config loading with defaults merging, MQTT protocol selection, PLC payload parsing (`parse_input_payload` for MW0, `parse_command_payload` for MW20), command payload building (`build_command_payload`), and the global mode state file read/write (`load_mode_state` / `save_mode_state`).

**`auto_controller.py`** — The autonomous control loop. Subscribes to the PLC input MQTT topic, extracts MW0 (temperature x 100), calls Dify Workflow A with the raw payload, and publishes the returned MW20 command (0=close, 1=open) to the command MQTT topic. Deduplicates consecutive identical inputs and commands. When the shared mode is `manual`, it suspends itself and skips all Dify calls and command publishing.

**`chat_terminal.py`** — Interactive REPL for operators. Subscribes to both input and command MQTT topics to maintain a live view of MW0/MW20 state. Accepts natural language queries (Chinese or English), sends them to Dify Workflow B (backed by deepseek), and displays the AI response. In `manual` mode, also publishes MW20 commands returned by the AI directly to MQTT. Can switch the global mode between `auto` and `manual` via natural language ("自动模式" / "自定义模式").

**Mode coordination** — Both components read `controller_state.json` (path configurable via `control.state_file` in config.json). The chat terminal writes mode changes; the auto controller polls the file each cycle. `auto` mode = controller drives PLC; `manual` mode = chat terminal drives PLC, controller sleeps.

**`config.json`** — Single config file with three sections:
- `local_mqtt` — broker host/port/credentials, input and command topics
- `dify` — Workflow A endpoint and API key (for auto controller)
- `dify_chat` — Workflow B endpoint and API key (for chat terminal, uses longer timeout)
- `control` — state file path, device SN, poll interval, dedup toggles

On first run, if `config.json` doesn't exist, a default is generated and the program exits so the user can fill in API keys.

## Important notes

- **PyInstaller packaging is the intended distribution method** for customer machines — they run `.exe` files, not Python. Source code is maintained for development and customization.
- The Dify workflows are expected to have their built-in MQTT Trigger and MQTT Publisher nodes **disabled** — this controller handles all MQTT I/O directly.
- MW0 values represent temperature x 100 (e.g., 3500 = 35.00°C). MW20 is binary: 0 = close/off, 1 = open/on.
- The project is Windows-first (the launcher is `run.bat`), but the Python source is cross-platform.
