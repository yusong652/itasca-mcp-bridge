# itasca-bridge

[English](README.md) | [简体中文](README.zh-CN.md)

[![PyPI](https://img.shields.io/pypi/v/itasca-bridge)](https://pypi.org/project/itasca-bridge/)

运行在 ITASCA 产品进程内（PFC、FLAC3D 等）的 bridge，把产品的 Python SDK
以 WebSocket API 暴露出来，为 [pfc-mcp](https://pypi.org/project/pfc-mcp/)
等 MCP 服务端提供执行类工具能力。

本 bridge 与具体产品解耦：其核心机制——通过 `program log` 捕获控制台输出、
通过 `itasca.set_callback` 注册周期回调、通过 `python-reset-state` 保持
Python 状态——使用的是 ITASCA 通用命令语言 / SDK，已验证在 PFC 与 FLAC3D
上行为一致。

## 快速开始

在产品的 Python 环境中运行（GUI IPython 控制台或控制台 CLI）：

### 从 PyPI 安装

在产品的 IPython 控制台中：

```python
from pip._internal.cli.main import main as pip_main
pip_main(["install", "--user", "itasca-bridge"])

import itasca_bridge
itasca_bridge.start()
```

`websockets` 会作为依赖自动安装，并按内嵌 Python 版本匹配（Python 3.6 用
`9.1`，Python 3.10 用 `16.0`）。若缺失或版本不匹配，用同样方式安装即可
（Python 3.6：`pip_main(["install", "--user", "websockets==9.1"])`；
Python 3.10 用 `websockets==16.0`）。

### 从源码运行

```python
%run C:/path/to/itasca-bridge/start_bridge.py
```

> 路径使用正斜杠，不要加引号。

修改代码后重新 `%run` 即可生效，开发时推荐这种方式。

Bridge 会自动检测运行环境：GUI 使用 Qt 定时器，控制台使用阻塞循环。

预期输出：

```text
============================================================
Itasca Bridge Server
============================================================
  URL:         ws://localhost:9001
  Log:         /your-working-dir/.itasca-bridge/bridge.log
  Callbacks:   Interrupt, Executor (registered)
============================================================
```

## 运行要求

- 带内嵌 Python 解释器的 ITASCA 产品。
  - 已验证：PFC 6.0 / 7.0 / 9.0。
  - FLAC3D：bridge 的核心 SDK / 命令机制已验证兼容，端到端完整验证进行中。
- Python >= 3.6（PFC 6/7 用 Python 3.6，PFC 9 用 Python 3.10）。
- `websockets`（Python 3.6 用 `==9.1`，Python 3.10 用 `==16.0`），作为依赖自动安装。

## 故障排查

| 现象 | 处理方式 |
|---------|-----|
| 服务无法启动 | 在产品 IPython 控制台中重新执行安装 / 启动步骤；查看 `.itasca-bridge/bridge.log` |
| `websockets` 版本不匹配 | 在产品 IPython 控制台中执行 `from pip._internal.cli.main import main as pip_main; pip_main(["install", "--user", "websockets==16.0"])`（Python 3.6 用 `9.1`） |
| 端口被占用 | `itasca_bridge.start(port=9002)`，并把 MCP 客户端的 bridge 地址指向 `ws://localhost:9002` |
| 连接失败 | 确认 bridge 正在运行且端口可达，查看 `.itasca-bridge/bridge.log` |
| 无法执行任务 / MCP 无法连接 | 若执行工具返回 `ok=false`、`error.code=bridge_unavailable`、`error.details.reason=cannot connect to bridge service`，请确认 `itasca_bridge.start()` 正在运行，并检查 MCP 客户端的 bridge 地址是否一致 |

## 与 MCP 服务端的关系

本包仅是进程内运行时。请搭配能够使用其 WebSocket 协议的 MCP 服务端
（例如 [pfc-mcp](https://pypi.org/project/pfc-mcp/)）完成完整的客户端配置。

许可证：MIT。
