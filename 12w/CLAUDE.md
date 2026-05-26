# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Dify Local MQTT Controller — an industrial IoT bridge between PLC devices, a local MQTT broker, and Dify AI workflows. It reads sensor data (temperature via MW0 register) from PLCs over MQTT, sends it to Dify for AI analysis, and publishes control commands (MW20/21/22 registers) back to PLCs.

### PLC Register Map

| Register | Purpose | Values |
|---|---|---|
| MW0 | Temperature sensor (input) | temp × 100 (e.g. 3500 = 35.00°C) |
| MW20 | Light/power control | 0 = off, 1 = on |
| MW21 | Auto mode flag | 1 = auto mode active |
| MW22 | Manual mode flag | 1 = manual mode active |

The PLC's internal program (云组态) reads MW21/MW22 to determine the active mode. It "favors manual" — once MW22=1 is set, auto mode is latched off. Writing all three registers (MW20+MW21+MW22) together allows the Dify controller to fully interact with the PLC's mode logic, matching what the cloud SCADA does.

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
main.py  ──┬── auto_controller.py  ── MQTT (subscribe) ──► PLC data (MW0, MW21, MW22)
           │                           MQTT (publish)   ──► MW20 + MW21=1 + MW22=0
           │                           Dify Workflow A  ──► AI decision → MW20
           │
           └── chat_terminal.py   ── MQTT (subscribe) ──► PLC data (MW0, MW20, MW21, MW22)
                                     stdin              ──► user natural language
                                     Dify Workflow B    ──► AI response + mode + commands
```

**`common.py`** — Shared layer: config loading with defaults merging, MQTT protocol selection, PLC payload parsing (`parse_input_payload` returns MW0/MW21/MW22, `parse_command_payload` returns MW20/MW21/MW22), command payload building (`build_command_payload(device_sn, mw20, mw21, mw22)` — only non-None registers are included), and mode state file read/write.

**`auto_controller.py`** — Autonomous control loop. Subscribes to PLC input topic, parses MW0/MW21/MW22, calls Dify Workflow A, and publishes commands. In auto mode, every published command includes **MW21=1, MW22=0** (tells PLC "auto mode active"). Deduplicates consecutive identical payloads. When `controller_state.json` mode is `manual`, suspends all Dify calls and publishing.

**`chat_terminal.py`** — Interactive REPL. Subscribes to both input and command MQTT topics, maintaining a live view of all four MW registers. Sends user queries to Dify Workflow B (deepseek LLM). On mode switch: publishes MW21=1,MW22=0 for auto, or MW22=1,MW21=0 for manual. In manual mode, commands include **MW22=1, MW21=0**; in auto mode, **MW21=1, MW22=0**. This ensures the PLC internal program always knows the active mode.

**Mode coordination** — Both components read `controller_state.json` (path configurable via `control.state_file` in config.json). The chat terminal writes mode changes; the auto controller polls the file each cycle. `auto` mode = controller drives PLC; `manual` mode = chat terminal drives PLC, controller sleeps.

**`config.json`** — Single config file with three sections:
- `local_mqtt` — broker host/port/credentials, input and command topics
- `dify` — Workflow A endpoint and API key (for auto controller)
- `dify_chat` — Workflow B endpoint and API key (for chat terminal, uses longer timeout)
- `control` — state file path, device SN, poll interval, dedup toggles

On first run, if `config.json` doesn't exist, a default is generated and the program exits so the user can fill in API keys.

## Important notes

- **PyInstaller packaging is the intended distribution method** for customer machines.
- The Dify workflows should have their built-in MQTT Trigger and MQTT Publisher nodes **disabled** — this controller handles all MQTT I/O directly.
- The `controller_state.json` file coordinates Python-side mode. The MW21/MW22 registers coordinate PLC-side mode. Both must be kept in sync.
- The project is Windows-first (the launcher is `run.bat`), but the Python source is cross-platform.

## DJ Mode (exp/lights-dj branch)

When the chat terminal receives `dj_mode: "on"` from Dify, it starts a background daemon thread that toggles MW20 between 0 and 1 every `dj_interval` seconds (default 0.5s). The DJ thread stops when:
- Dify returns `dj_mode: "off"` / `"stop"`
- Mode is switched (auto↔manual)
- User exits (Ctrl+C)
- User sends "关灯" / "关闭DJ模式"

While DJ mode is active, static `mw20` commands from Dify are ignored (the DJ toggle thread owns MW20).

DJ-relevant Dify outputs:
- `dj_mode`: `"on"` / `"off"` / `"start"` / `"stop"` / `true` / `false`
- `dj_interval`: float (seconds, optional, defaults to 0.5)

## Dify Workflow B Prompt (Chat/Light)

The Dify workflow (deepseek LLM) needs its prompt updated to handle light + DJ commands. Key output variables:

| Variable | Type | Description |
|---|---|---|
| `text` | string | AI natural language response |
| `mode` | string | `"auto"` or `"manual"` to switch mode |
| `mw20` | number | 0 = off, 1 = on (light/power control) |
| `mw21` | number | Auto mode flag (1=auto) |
| `mw22` | number | Manual mode flag (1=manual) |
| `dj_mode` | string | `"on"` / `"off"` to control DJ flashing |
| `dj_interval` | number | DJ flash interval in seconds (optional) |

Example prompt additions for the Dify workflow:

```
你是PLC灯光控制助手。MW20控制灯光的供电(0=关,1=开)。

灯光指令：
- 用户说"开灯"/"打开灯"/"亮灯" → mw20=1
- 用户说"关灯"/"关闭灯"/"灭灯" → mw20=0

DJ模式：
- 用户说"开启DJ模式"/"DJ"/"蹦迪" → dj_mode="on", dj_interval=0.5
- 用户说"关闭DJ模式"/"停止DJ" → dj_mode="off"
- 用户说"快一点"/"加快" → dj_interval减半
- 用户说"慢一点"/"减慢" → dj_interval加倍

模式切换：
- 用户说"自动模式"/"自动" → mode="auto"
- 用户说"自定义模式"/"手动模式"/"手动" → mode="manual"

注意：MW20是通用供电信号，1=通电(灯亮)，0=断电(灯灭)。
DJ模式下mw20由终端程序自动翻转，你不需要返回mw20值。
```
