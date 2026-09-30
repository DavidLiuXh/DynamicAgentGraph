# DynamicAgentGraph

将已确认的目标规划为有限 DAG，由程序验证、保存、编译成 LangGraph 并执行的 Python 库。每个任务可以有不同的 State Schema 和注册 Reducer。编译过程不调用模型；最终业务验收由调用方负责。

第一阶段实现与验证状态见 [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md)；环境与兼容性见 [COMPATIBILITY.md](COMPATIBILITY.md)。

## 安装与离线运行

支持 Python 3.11、3.12。开发环境使用 `uv.lock` 锁定依赖。

```bash
uv sync --python 3.12
uv run dynamic-graph-demo
uv run pytest -q -W error
```

离线示例使用 FakeModelClient 和合成工具数据，仍完整经过规划响应解析、静态验证、graph.json 落盘、LangGraph 执行及 RunResult 收口。它不会调用模型服务。

## 接入自己的目标和工具

将 `DEEPSEEK_API_KEY` 配置为环境变量。库不会自动加载 `.env`；仓库中的实测脚本显式读取本地 `.env`，该文件不属于发布包。

```python
import asyncio
from dynamic_graph import (
    DynamicGraphEngine,
    EngineConfig,
    ExecutionPolicy,
    GoalSpec,
    ModelBindings,
    ToolDefinition,
)
from dynamic_graph.models.adapters import LangChainModelClient

TEXT = {"type": "string"}
QUERY = {
    "type": "object",
    "properties": {"query": TEXT},
    "required": ["query"],
    "additionalProperties": False,
}
ANSWER = {
    "type": "object",
    "properties": {"answer": TEXT},
    "required": ["answer"],
    "additionalProperties": False,
}


async def lookup(data, context):
    return {"answer": "查询内容：" + data["query"]}


async def main():
    model = LangChainModelClient(model="deepseek-flash", mode="function_calling")
    engine = DynamicGraphEngine(
        config=EngineConfig(runs_dir="runs"),
        models=ModelBindings(planner=model, worker=model),
    )
    engine.register_tool(
        ToolDefinition(
            name="company.lookup",
            version="1.0.0",
            description="只读查询已登记资料",
            input_schema=QUERY,
            output_schema=ANSWER,
            handler=lookup,
        )
    )
    result = await engine.run(
        goal=GoalSpec(
            objective="调用查询工具，返回工具提供的答案",
            inputs={"query": "示例主题"},
            input_schema=QUERY,
            output_schema=ANSWER,
        ),
        policy=ExecutionPolicy(allowed_tools=["company.lookup@1.0.0"]),
    )
    print(result.execution_status, result.outputs, result.diagnostics)
    # 调用方在这里判断是否满足业务成功标准。


asyncio.run(main())
```

## 契约与扩展

### Tavily 网页搜索

配置环境变量 `TAVILY_API_KEY`，或在创建工具时传入 `api_key`。库不会自动读取 `.env`。使用已有 Engine 注册工具，并在该次运行的允许列表中授权：

```python
from dynamic_graph import ExecutionPolicy, GoalSpec
from dynamic_graph.tools import tavily_search_tool


async def search_web(engine):
    tool = tavily_search_tool()
    engine.register_tool(tool)  # 同一 Engine 只需注册一次。
    return await engine.run(
        goal=GoalSpec(
            objective="搜索 LangGraph 官方文档，返回搜索结果和来源链接",
            inputs={"query": "LangGraph official graph API documentation"},
            input_schema=tool.input_schema,
            output_schema=tool.output_schema,
        ),
        policy=ExecutionPolicy(allowed_tools=["tavily.search@1.0.0"]),
    )
```

工具名为 `tavily.search`，版本 `1.0.0`。输入 `query` 是非空字符串，`max_results` 可省略（默认 5），范围 1–20。查询的输入 Schema 应使用上述契约；静态验证会保守检查字符串长度和结果数量约束。输出包含 `query` 和 `results`，每条结果保留 `title`、`url`、`content` 摘要及 `score`。无结果时返回空数组；网页内容作为来源数据交给下游节点处理。

按 [Tavily Search 官方接口](https://docs.tavily.com/documentation/api-reference/endpoint/search)，工具使用异步 HTTP 请求、Bearer 认证以及 `basic` / `general` 搜索，显式关闭自动参数和生成答案、原文、图片。每个节点尝试只发出一次请求，超时使用剩余节点期限；网络错误、429 和 5xx 交给引擎有限重试，400/401/403/422 及 432/433 配额错误不重试。执行失败通过既有 `TOOL_FAILED` 诊断返回；响应类型错误由节点输出校验拒绝。

API key 仅保存在 handler 绑定中，不进入能力目录、规划输入或图文件。提供商错误正文不返回到诊断；`tvly-` 密钥模式也由现有凭据规则处理。Tavily 请求消耗工具调用预算；实际信用点和费用没有可靠计价记录，不计为零。HTTP 替身测试验证完整执行链和故障边界；2026-09-30 单次真实搜索返回 2 条文档来源，输出 Schema 校验通过，见[实测记录](experiments/tavily-smoke-result.json)。这次单请求探测不代表长期可用性或搜索质量基准。

单次真实接口验收可运行 `.venv/bin/python experiments/tavily_smoke.py`。脚本显式读取本地 `.env`（环境中的 `TAVILY_API_KEY` 优先），仅发送公开文档查询并保存标题、链接和分数摘要；不保存密钥或完整正文。该脚本会消耗 Tavily 信用点，执行器库仍不自动读取 `.env`。

### 自定义能力

- `ModelClient` 只有异步 `generate(ModelRequest) -> ModelResponse`。普通用户可直接使用自带适配器；测试可使用 FakeModelClient。它不管理规划、重试或总预算。
- 工具和 Evaluator 是可信的异步 Python 函数，只收到声明的输入投影和 CallContext。首版只执行声明为只读的能力；注册成功不证明函数行为安全。
- `ReducerDefinition` 接受具体的 V/U/config Schema、初始值检查和纯同步函数。`assert_reducer_permutations` 提供顺序扰动测试；自定义代码的正确性仍由注册者负责。
- 允许列表使用 `name@1.0.0`；空列表表示不允许。`core.*`、`builtin.*` 是保留名称。每次 run 冻结配置与能力绑定。
- 节点重试由本库负责，适配器和 LangGraph 不再叠加重试。已知临时错误有限重试；认证、拒绝、配额耗尽和契约错误有独立诊断。
- `CancellationToken.cancel()` 返回 CANCELLED；直接取消调用方的 asyncio Task 会传播 CancelledError。异步扩展必须配合取消。

`initial_validator` 是可信、只读的初始值检查函数：不得修改参数，也不得保留引用供后续修改。返回 `False` 表示拒绝初值；它不能充当数据转换器。库复制调用方提供的初值以建立独立状态，但不再为这个内部检查额外复制一次；校验器遵守只读约定由扩展作者负责。

工具定义的完整 Schema、Evaluator 以及 V≠U Reducer 注册契约见[设计第 9、14、18 节](PHASE1_DESIGN.md)；可运行例子见 [fixtures.py](src/dynamic_graph/devtools/fixtures.py)。函数绑定会保留原服务实例，只复制 Schema 和元数据；需要跨 run 共享的客户端应由调用方管理生命周期。

测试不需要 LangChain 类型。例如，用脚本模拟规划阻塞：

```python
from dynamic_graph import FakeModelClient, ModelBindings

fake = FakeModelClient(
    [
        {
            "response_version": "1.0",
            "outcome": "blocked",
            "graph": None,
            "diagnostics": [
                {
                    "reason_code": "MISSING_INFORMATION",
                    "message": "请补充文档内容",
                    "related_input_paths": [],
                    "missing_information": ["文档内容"],
                    "required_capability_description": [],
                }
            ],
        }
    ]
)
models = ModelBindings(planner=fake, worker=fake)
# 将 models 传给 Engine；运行后得到 PLANNING_BLOCKED，且不生成 graph.json。
```

离线完整成功示例由 `dynamic-graph-demo` 提供，FakeModelClient 返回参考图，再交给同一 Validator 和 LangGraph 执行。自带真实实现 `LangChainModelClient` 已完成 ModelClient 接口，业务接入无需再实现一层协议。

## 结果与调试

`COMPLETED` 表示图执行和输出格式正确，不能替代业务验收。失败结果可能带有已提交的 `outputs`；节点曾返回但未提交的结果只出现在带 `commit_state` 的产物记录中。缺少生产者提交时不返回初始化占位值。

`PLANNING_BLOCKED` 表示模型报告当前信息、能力或约束不足，具体原因在 diagnostic.details；`MODEL_REFUSED`、`MODEL_AUTH_FAILED`、`MODEL_QUOTA_EXHAUSTED` 分别表示模型拒绝、认证失败和配额耗尽，不是规划 blocked。图格式或语义修复耗尽分别保留生成/验证诊断。

`Diagnostic.action` 是保留的兼容字段，首版始终为 `NONE`；调用方应根据 `code` 和 `details` 处理结果，目前没有自动生成处置建议的功能。

调用方补充信息后，可显式创建新的 `GoalSpec(parent_run_id=result.run_id, objective=..., inputs=..., output_schema=...)` 并再次调用 `engine.run`；这是新 run，不自动复用历史状态。调用方自行决定业务验收不通过时是否这样处理。

每个 run 的目录包含 manifest、目标/策略/能力快照、规划轮次、graph.json、事件和结果。图文件保存的是实际编译的规范化 JSON，SHA-256 与 manifest 对应。保存图失败时不会执行业务节点。

默认 minimal 模式不保留原始模型候选和完整渲染消息。debug 可保留合成任务或允许保存的数据；两种模式都会处理凭据，图中的受限字面量会被拒绝，不能通过脱敏改变实际执行图。目录保留和清理由调用方管理。

`EngineConfig.sensitive_values` 用于配置执行与记录中都禁止出现的字符串，与内置凭据规则一起检查目标、能力目录、图和节点输出。`redactor` 只处理记录副本和公开结果，不参与执行准入，也不改写模型的规划、修复或节点输入。`graph.json` 保存原始执行图，不经过自定义脱敏器；需要禁止进入图的数据应使用 `sensitive_values`。回调应保持记录和结果的结构；公开结果只脱敏一次，再原样落盘。

离线检查/编译不执行模型或业务工具：

```bash
check-graph graph.json --goal goal.json --demo sales_totals
compile-graph graph.json --goal goal.json --demo sales_totals
```

自定义能力通过 `--factory trusted_module:make_engine --policy policy.json` 显式加载。该函数应返回已配置 Engine；GraphSpec 不能指定 Python import。完整运行记录中的 goal.json 可作为 `--goal`。

## 首版限制

有限 DAG、完整聚合后读取、固定 worker 角色、进程内可信扩展、本地 JSON 记录。没有业务写工具、运行中改图、循环、resume、自动模型切换或代码生成。无需 Agent Server、Studio、数据库或 LangSmith 服务。

首版尚未实现严格金额/Token 总预算，`max_cost` / `max_tokens` 仅保留兼容字段，设置任何非 `None` 值都会在调用前返回 `BUDGET_UNENFORCEABLE`；当前没有可开启该功能的价格配置。调用次数、并发和时间限制始终有效。不能可靠获得的费用或 Token 用量保留 null，不记为零。

真实模型验收脚本：`python experiments/model_smoke.py`、`python experiments/live_benchmark.py --debug`，以及独立的 `python experiments/blocked_benchmark.py`。可规划集合执行 12 个固定目标各 3 次，阻塞集合执行 6 个目标各 3 次；发现 MODEL_QUOTA_EXHAUSTED 立即退出。详细失败记录与各轮报告保留，未通过的门控不会以跳过测试代替。
