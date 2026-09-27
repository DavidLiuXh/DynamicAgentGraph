# 兼容性与验证边界

核验日期：2026-09-12。发布包 `dynamic-agent-graph==0.1.0`，导入名 `dynamic_graph`。依赖解析基线保存在 [uv.lock](uv.lock)；以下是实际运行组合，不代表所有满足依赖范围的版本都已验证。

## 本地环境

| 项目 | 实测版本或结果 |
| --- | --- |
| 操作系统 | macOS 26.6.2，x86_64 |
| Python | 3.11.15、3.12.4；各 157 项测试通过，警告作为错误处理 |
| LangGraph | 1.2.11 |
| LangChain Core / OpenAI 集成 | 1.6.2 / 1.6.2 |
| Pydantic / jsonschema | 2.13.5 / 4.26.0 |
| OpenAI SDK（LangChain 内部依赖） | 3.13.0 |
| 包安装 | 独立虚拟环境，从 wheel 安装，在源码目录外运行示例与离线 CLI |
| Windows、Linux | 未做真实系统运行矩阵；Windows ACL 命令有替身测试，不能据此宣称跨平台认证 |

对应测试输出：[Python 3.11](experiments/pytest-311.xml)、[Python 3.12](experiments/pytest-312.xml)。公共 API 导入测试禁止导入 LangGraph/LangChain；实际执行时按需加载内部依赖。

## LangGraph 映射

实现使用 `langgraph.graph.StateGraph`、动态 `TypedDict`、`Annotated` Reducer 和 `astream(stream_mode="values")`。私有 Cell 区分 init/update/value，初始化不作为业务更新；每个字段仍有具体 V/U Schema。多个前置使用 `add_edge([a, b], target)`；不等长分支的反例证明，分别添加两条边可能令汇聚节点执行两次。

提交依据是状态流中的私有节点提交标记，不是 handler 已返回。并行步骤失败时保留产物并标明是否提交；多字段更新全部通过检查后才返回给引擎。直接取消 asyncio Task 传播 CancelledError，CancellationToken 形成 CANCELLED 结果。

在锁定版本上，原生 `max_concurrency` 的提前创建协程行为在取消时出现未等待协程警告。因此执行配置不启用该入口，由已有的 run/Engine 调用信号量限制实际活动模型和工具调用；调度仍交给 LangGraph。相关成功、取消和实际并发断言见 [引擎试验](tests/integration/test_engine_semantics.py) 与 [边界测试](tests/integration/test_boundaries.py)。升级引擎后应重跑这些测试，不直接恢复该参数。

## DeepSeek 实测

用户指定的 DeepSeek-V4.1-Flash 使用 API ID `deepseek-flash`，地址 `https://api.deepseek.com`。实测为 LangChain ChatOpenAI 的 `with_structured_output(method="function_calling", include_raw=True)`，temperature=0、thinking disabled、SDK max_retries=0。请求上限 16384 输出 Token；规划及节点的实际超时受剩余总期限约束。

模型集成只发起一次结构化调用，不执行工具；业务工具仍由已验证图的显式 tool/check 节点运行。`json_schema` 和显式开启的 `json_mode` 有代码路径及替身测试，此次不将它们列为 DeepSeek 真实兼容模式。

| 观察集 | 结果 | 证据 |
| --- | --- | --- |
| 完整 PlanningResponse/GraphSpec 兼容试验 | 4/4 有效且 JSON 无损往返：blocked、文档图、自定义 V≠U、nullable 加局部引用 | [模型试验](experiments/model_smoke_result.json) |
| 12 个可规划目标，各 3 次 | 首次合法 31/36；有限修复后合法、执行完成、上层验收均 35/36 | [完整报告](experiments/live-20260911T113444Z.json) |
| 6 个应阻塞目标，各 3 次 | 18/18 阻塞，0 图、0 业务调用；原因分类 12/18 与预期一致 | [阻塞报告](experiments/blocked-20260911T230307Z.json) |

剩余一次可规划失败为 evidence_counts 第 2 次：模型三轮响应格式错误，返回 GRAPH_GENERATION_FAILED，未调用业务工具。策略冲突的 6 次阻塞均被模型归为 CAPABILITY_GAP；授权过滤后的目录不展示被禁止工具，因此“缺少可用能力”和“策略冲突”在模型描述中存在混淆。本库保留原始原因，不伪造分类准确率。

仅在 `function_calling` 模式下，非 object 根的 worker 输出才包装为 `{"value": ...}`，以形成函数参数对象；局部 `$defs` 移至包装根，解析后解包，业务 Schema 不变。`json_schema` 和 `json_mode` 直接传递原根 Schema，不添加 `value` 字段。适配版本为 1.0；传输 Schema Hash 记录在响应元数据中。function_calling下的包装及解包由提供商替身测试覆盖；四个真实完整响应试验不替代所有 worker 类型的真实模型验证，其他结构化模式不在 DeepSeek 实测范围内。

PlanningResponse/GraphSpec/SchemaSpec 的导出文件在 [schemas](schemas)；结构元 Schema 之外，非递归、深度、类型包含、授权和依赖仍需本地 Validator。不能用提供商“接受 Schema”代替图验证。

真实请求 ID 优先来自响应头；LangChain 的 `lc_run...` 仅是本地调用标识。60 份早期响应记录已将该值移至 local_call_id，未知 provider_request_id 改为 null，审计见 [修正记录](experiments/request-id-normalization.json)。图字节与 Hash 未改。新试验已取得真正的提供商请求 ID；服务端未公开的精确模型构建版本仍未知。

## 证据适用范围

[最终核对](experiments/final-audit.json) 比较了现行模板/示例/Schema Hash 和 36 份首轮请求数据，并重新验证 35 张已保存图。全部一致。后续注册绑定复制、记录元数据和未使用 `$defs` 检查等修正不改变这些合成任务的模型输入；当前源文件 Hash 随审计保存。真实工具数据仍是固定合成数据，以上结果不等于生产数据源可用性或任意业务目标成功率。

费用无法从响应可靠计算，记为 null；已知 Token 用量单独累计。严格 max_cost/max_tokens 在执行前明确拒绝。配额耗尽与限流分开归类，实测脚本遇到 MODEL_QUOTA_EXHAUSTED 立即停止；此次没有出现配额耗尽。

最大 32 节点/64 字段基准在启用 tracemalloc 的 10 次测量中，验证加编译 p50 约 6.02 秒、p95 约 8.79 秒、峰值追踪内存约 27.24 MiB；包含追踪开销，不是无追踪生产延迟。见 [性能记录](experiments/performance.json)。
