# 测试与验证指南 (Test & Verification Guide)

本项目包含完备的自动化单元测试与端到端集成测试脚本，覆盖仿真引擎核心计算、多线程生命周期、Webview REST 接口网关、以及 Controller 和 Pilot 的大模型协同逻辑。

---

## 1. 单元测试架构与执行

所有单元测试基于 Python 标准库 `unittest` 构建，针对大模型（Gemini/OpenAI）与跨微服务 HTTP 调用均采用 Mock 技术，测试执行完全本地化，无需外部 API Key 或真实网络依赖。

### 1.1 执行全部测试用例

在项目根目录下执行以下命令：

```bash
# 使用本地虚拟环境运行所有单元测试
.venv/bin/python -m unittest discover -s test
```

### 1.2 单元测试套件构成

测试套件包含以下三个核心测试模块：

| 测试文件 | 目标模块 | 验证关键点 |
| :--- | :--- | :--- |
| [`test/test_airplane_simulator.py`](file:///home/mickey/git/test_of_realtime_AI_agent/test/test_airplane_simulator.py) | `airplane_simulator.py`<br>`webview.py` | 1. 验证仿真线程名称是否为 `airplane_simulator`；<br>2. 验证非自启及 `start()` / `stop()` 线程生命周期；<br>3. 验证飞行器实体增删改查及 `get_motion_state()` 快照读取；<br>4. 验证大圆航向运动学物理位移积分；<br>5. 验证 Webview 的 `/api/simulator/state`、`/api/simulator/start`、`/api/simulator/stop`、`/api/objects`、`/api/add`、`/api/update` 接口代理。 |
| [`test/test_controller_ai.py`](file:///home/mickey/git/test_of_realtime_AI_agent/test/test_controller_ai.py) | `controller.py` | 1. 验证空管 10s 轮询感知状态流；<br>2. 验证提示词注入与大模型推理参数构造；<br>3. 验证有效管制指令向 Platform 与 Pilot 的双发逻辑；<br>4. 验证 `/test_ai` 大模型连通性测试接口。 |
| [`test/test_pilot_ai.py`](file:///home/mickey/git/test_of_realtime_AI_agent/test/test_pilot_ai.py) | `pilot.py` | 1. 验证 `/radio_RX` 接口接收自然语言无线电报文；<br>2. 验证大模型 Few-Shot 参数结构化提取；<br>3. 验证目标飞机名称模糊匹配与 UUID 消歧；<br>4. 验证调用 Platform 更新航行参数；<br>5. 验证 `/test_ai` 大模型连通性测试接口。 |
| [`test/test_cli_test_ai.py`](file:///home/mickey/git/test_of_realtime_AI_agent/test/test_cli_test_ai.py) | `cli/cli.py`<br>`ai_client.py`<br>`webview.py` | 1. 验证 CLI 参数解析器 `test-ai` / `test-ai-endian`；<br>2. 验证管制员与飞行员 AI 测试基于 Platform Webview 代理接口机制；<br>3. 验证 `check_ai_connectivity` 状态提取与延迟度量；<br>4. 验证 Webview 代理接口 `/api/controller/test_ai` 与 `/api/pilot/test_ai`。 |

---

## 2. 端到端与集成测试脚本

在容器或本地各微服务启动后，可通过下列脚本进行端到端全链路联动测试：

### 2.1 AI Pilot 全链路编队控制测试 ([`test/test_pilot.sh`](file:///home/mickey/git/test_of_realtime_AI_agent/test/test_pilot.sh))
- **执行方式**：
  ```bash
  ./test/test_pilot.sh
  ```
- **测试流程**：
  1. 调用 `http://localhost:8010/api/add` 创建两个同名无人机 `MultiTest`；
  2. 向 Pilot 接口 `http://localhost:8020/radio_RX` 发送自然语言指令：`"请让 MultiTest 把航速提升至 450 节，爬升到 35000 英尺。"`；
  3. 验证 Pilot 返回的 `resolved_count == 2` 以及 `platform_responses` 成功响应；
  4. 查询 Platform 的 `/api/objects`，确认所有同名飞机的航速已变更至 450 节、目标高度变更为 35000 英尺。

### 2.2 基础 API 与物理参数修改测试 ([`test/test_api.sh`](file:///home/mickey/git/test_of_realtime_AI_agent/test/test_api.sh))
- **执行方式**：
  ```bash
  ./test/test_api.sh
  ```
- **测试流程**：创建新实体并获取 ID，修改其航速、航向与高度，验证实体参数在地图与接口中的实时生效。

### 2.3 终端控制台与命令行工具测试 ([`cli/cli.py`](file:///home/mickey/git/test_of_realtime_AI_agent/cli/cli.py))
- **执行方式**：
  ```bash
  # 交互式菜单模式 (包含 1级菜单: Test AI Endian)
  uv run cli/cli.py

  # 命令行参数模式 (AI 管制员与飞行模拟器开关)
  uv run cli/cli.py status
  uv run cli/cli.py on
  uv run cli/cli.py off
  uv run cli/cli.py simulator status
  uv run cli/cli.py on-off simulator on

  # 飞机管理与机场配置
  uv run cli/cli.py airport set --random-pos
  uv run cli/cli.py airplane add --name Drone-1 --altitude 6000 --heading 180
  uv run cli/cli.py airplane update --id <UUID> --heading 090 --altitude 3000
  uv run cli/cli.py airplane status

  # AI 接口联通性测试 (Test AI Endian)
  uv run cli/cli.py --test-ai
  uv run cli/cli.py --test-ai-endian --target pilot --json
  uv run cli/cli.py test-ai
  uv run cli/cli.py test-ai-endian
  uv run cli/cli.py test-ai --target controller --timeout 30.0
  uv run cli/cli.py test-ai --target pilot --json
  ```

