# 第一阶段实施与验收记录

首次实施日期：2026-09-11 至 2026-09-12。依据主设计/开发计划 0.3 实现，同步为 0.4；2026-09-15 简化重构后文档同步为 0.5。GraphSpec 仍为 1.0，Python 包版本为 0.1.0。首次阶段代码、离线语义闭环、真实规划观察和本地分发已完成；首次验证范围为 [兼容性记录](COMPATIBILITY.md) 中的环境，本次回归证据单独列于下节。

负责人：本次 Codex 实施任务。首次验收时尚未建立 Git 仓库；2026-09-27 已创建私有 GitHub 仓库并提交。首次验收的代码 Hash 见 [历史审计报告](experiments/final-audit.json)。未采集净工程工时，原任务表的人日估算不视为实际工时。凭据仅保存在被忽略的本地 `.env`，权限 0600，未纳入发布包。用户已授权真实模型测试，无费用总上限；配额耗尽时停止开发并通知。

## 2026-10-04 Reducer选择、冲突诊断与并行搜索结果

中英文规划模板明确：单节点生成完整值/列表时优先replace；明确聚合且有稳定条目标识才按键合并。按用户确认，保留多查询搜索的具体说明：实体标识不必是观察记录标识，各搜索分支分别保存完整结果，汇总节点等待并读取全部分支后整理重复来源。Tavily描述同步说明相同URL的评分及摘录可随查询变化。

没有新增Reducer，也未改变冲突语义。内置按键合并仍拒绝同键不同内容；冲突诊断新增reason=key_conflict、field及适用的key_field，节点预检同时补充node_id，不暴露键值或业务正文。冲突不自动重试，失败节点不提交部分输出。

新增11项行为测试：单写完整列表保留同来源不同摘录、按键合并拒绝冲突且完全相同项正常去重；三个并行查询保留全部结果、空分支及不同完成顺序、同URL不同评分/摘录仍冲突、多写者误用replace在执行前被拒绝。Python3.12全量278/278通过（-W error），见[回归记录](experiments/reducer-search-results-pytest-312.xml)；Ruff和差异检查通过。使用HTTP/模型替身，不代表真实模型能稳定选择这些图；本次未发送真实Karen目标或新模型请求。

## 2026-10-02 通用网页获取与本地交付工具

新增file.read_text、file.write_text、browser.open_local_page、web.fetch四个1.0.0工具。文件与浏览器默认root=/tmp，可指定其他已存在目录；拒绝空路径、目录本身、越界及指向目录外的符号链接。UTF-8读写默认上限1MiB，写入原子提交，覆盖须显式指定。浏览器仅打开已有HTML，返回launch_requested，不声称渲染成功；不提供任意命令执行。

ExecutionPolicy增加默认空的allowed_side_effect_tools，非只读tool须同时取得普通与副作用授权，否则不进入规划快照。副作用不自动重试，即使声明幂等或返回可重试错误也只调用一次；图后续失败/取消不回滚已完成动作。check仍只读。注册时effect/concurrency声明必须为布尔值。

web.fetch获取HTTP/HTTPS文本，无域名/端口白名单，支持最多三次跨域重定向、正文大小与期限限制，不执行JavaScript。返回最终URL、正文、Content-Type和UTC抓取时间，不对来源的历史或实时性质预先作判断。网页中的所需信息由任务LLM节点按输出Schema提取，不提供站点专用工具或解析器。结构校验不保证语义提取准确性，原始HTML也可能超过模型上下文限制。

experiments/web_page_demo.py接受--url和--information，演示抓取→信息提取→HTML生成→文件写入→浏览器请求；默认输出/tmp/page.html，覆盖须--overwrite。该示例需要DeepSeek凭据，会发出付费模型请求并执行本地副作用；本次仅验证命令帮助和替身链路，未运行真实模型或浏览器。

新增57项测试，同一提取/交付图支持GitHub和另一个网页地址；覆盖正文与来源传递、结构化提取结果、提取失败时不执行本地副作用、双重授权、路径/字节/超时/取消边界、macOS/Linux/Windows启动参数替身，以及HTTP错误和重定向。Python3.12全量267/267通过（-W error），见[回归记录](experiments/local-web-tools-pytest-312.xml)；Ruff检查和格式检查通过。不将历史专用工具或旧提示词的真实模型记录作为当前通用流程的验证证据。

## 2026-10-01 中英文规划提示词

新增与中文版本等义的英文 system/repair 模板，按 GoalSpec.objective 中是否包含汉字选择中文或英文；混合目标走中文，业务输入不参与判定。初次规划和修复保持同一语言，manifest与请求记录增加prompt_language，Hash对应实际模板。中文模板正文未变；无汉字目标的提示词已经变化，历史中文模型验收结果不代表英文版本质量。

新增12项离线测试，覆盖简繁体、混合目标、扩展汉字、无汉字目标、模板Hash与共用契约，以及中英文目标从首次规划到修复、执行和记录的完整链路。本PR直接基于main，不包含尚未进入main的统一注册功能。Python 3.12全量 **210/210通过**（`-W error`），见 [回归记录](experiments/prompt-language-pytest-312.xml)。Ruff检查及格式检查通过，sdist/wheel构建通过，四份模板均纳入wheel并与源码一致。基准脚本同步记录双语资源版本，历史审计仍明确检查旧中文模板。本次未发出付费模型请求，未进行中英效果对照实验。

## 2026-09-30 Tavily 搜索工具

新增 `dynamic_graph.tools.tavily_search_tool()`，返回可直接注册的 `tavily.search@1.0.0`。基于官方 Search REST 接口异步调用，使用现有 ToolDefinition、允许列表、调用预算、超时、重试和节点输出校验；没有增加搜索框架或第二套工具调度。HTTPX 从既有传递依赖声明为直接依赖，锁定版本仍为0.28.1。

输入为非空query及可选max_results（1–20，默认5）；输出保留查询、标题、来源URL、摘要和相关度。固定basic/general搜索且关闭自动参数和生成答案。密钥通过参数或TAVILY_API_KEY绑定，不放入注册元数据、规划输入或图；内置凭据规则增加tvly-模式。网络/429/5xx由引擎有限重试，认证/输入/432/433配额错误不重试，取消传播至HTTP调用。

工具工厂为每份定义建立独立Schema，调用方修改一份定义不会影响后续实例。临时sdist/wheel构建通过，在源码目录外从解压wheel导入工具、注册Schema及检查允许列表均通过；未覆盖首次验收的dist产物。

新增23项HTTP替身测试，覆盖目标到图到结果、默认/显式结果数、空结果、HTTP错误重试、网络超时后成功、格式及类型错误、授权与结果数量约束、协作取消、已过截止时间及密钥反射。Python 3.12全量 **198/198通过**（`-W error`），Ruff检查及格式检查通过；验证结果见 [本次回归](experiments/tavily-pytest-312.xml)。当前Python 3.11临时解释器已失效，本次仅核验Python 3.12。

用户提供Tavily密钥后，已保存到Git忽略的本地.env（0600）。2026-09-30使用experiments/tavily_smoke.py进行一次真实basic/general搜索，约2.97秒返回2条LangGraph官方文档来源，工具输出Schema和凭据检查通过；[实测报告](experiments/tavily-smoke-result.json)只保留公开查询、来源标题/链接/分数及耗时。首次沙箱探测在传输层失败，获准网络访问后单请求成功，无额外模型调用。该探测验证配置与响应兼容性，不替代真实任务规划或长期搜索质量基准。

## 2026-09-15 复制边界约定修订

根据后续确认的契约，`initial_validator`是可信只读函数，不得修改参数或保留引用供后续修改。`BoundReducer.initial()`保留建立独立初始状态所需的一次深复制，删除交给校验器前的第二次复制。原先针对初始值校验器保留引用后修改的防御测试已调整；handler参数及返回值的隔离保持不变。

同时将Schema展开改为一次递归完成展开与复制，避免先深复制子树再覆盖；排列测试辅助函数改为只复制外层列表后打乱顺序。新增验证覆盖初值与调用方互不影响、初值拒绝、Schema展开不修改源数据及重复局部引用互不影响。

Python 3.11、3.12各 **175/175通过**，均启用`-W error`；Ruff检查与格式检查通过。证据：[3.11结果](experiments/copy-boundaries-pytest-311.xml)、[3.12结果](experiments/copy-boundaries-pytest-312.xml)。本次未调用真实模型；以下173项测试及重构审计是此次契约调整前的历史记录。

## 2026-09-15 简化重构与回归

按当前需求、高内聚、低耦合和明确边界优化，没有增加业务功能、配置项或公共扩展协议。

- `api.py`：用一个私有 `_Run` 对象管理单次运行的资源和状态；图准备、执行、取消等待、输出收集和记录收尾职责明确，移除长闭包中的 `nonlocal`。节点 `Runtime` 使用具体类型和关键字初始化。
- `schemas.py` / `builtins.py`：值校验不再隐式复制；Pointer读取和状态解包只读引用，输入投影、扩展调用、Reducer返回值及提交快照负责必要的隔离。Schema兼容比较复用已验证的展开结果；一次内置Reducer绑定的Schema校验从6次减少到2次。没有引入缓存，也没有删掉外部边界校验。
- `registry.py` / `catalog.py`：`metadata()`只提取元数据，`copy_definition()`明确承担快照隔离并保留服务对象身份；内置Reducer的名称、版本和说明由同一目录提供给默认策略、能力查询和Planner。
- `execution/privacy.py` / `recording/local.py`：内置凭据和`sensitive_values`决定数据是否受限；自定义`redactor`仅处理记录副本及公开结果，不再影响执行准入或修复输入。`graph.json`保持实际执行图；公开结果只脱敏一次，并原样落盘，避免回调重复执行造成差异。
- 保留`max_cost`、`max_tokens`与`Diagnostic.action`的公开兼容字段，明确前两者在首版不支持、非None即拒绝，后者始终为NONE。未为这些保留字段补造价格模型或建议规则框架。

回归结果：Python 3.11、3.12分别 **173/173通过**，均启用`-W error`；Ruff检查和格式检查通过。新增16项场景覆盖快照隔离、扩展修改参数/保留引用、重试输入不变、脱敏与执行分离，以及公开结果和记录一致性。证据：[3.11结果](experiments/refactor-pytest-311.xml)、[3.12结果](experiments/refactor-pytest-312.xml)。

[重构审计](experiments/refactor-audit.json)：1296组数值及嵌套Schema包含比较与重构前一致；3份公开JSON Schema未变；36份历史首轮规划输入及资源版本未变；35张历史合法图仍验证通过。本次没有发送真实模型请求，这些结果是离线回归，不是新一轮模型成功率测试。以下任务表及真实模型数据保留首次验收的历史结果。

临时目录中的sdist和wheel构建通过；确认wheel包含两个新增模块，并在源码目录外解压wheel、复用现有依赖环境运行销售汇总示例，结果COMPLETED且调用方验收通过。本次没有覆盖`dist/`中的首次发布产物，也不将该检查称为新的干净环境安装验收。

复验命令：

```bash
.venv/bin/python -m pytest -q -W error --junitxml=experiments/refactor-pytest-312.xml
.venv311/bin/python -m pytest -q -W error --junitxml=experiments/refactor-pytest-311.xml
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

## 任务与门控

| 任务 | 状态 | 实现及主要证据 |
| --- | --- | --- |
| T01 / G01、G01-model | 通过 | 动态 Cell、V≠U、不等长 all-join、失败提交与取消独立试验；3.11/3.12 矩阵；真实完整 Schema 4/4 往返 |
| T02 / G02 | 通过 | 严格公共契约、三层 Schema、JSON/Pointer/引用/包含规则；test_schemas.py、test_models.py、test_recording_and_public_api.py |
| T03 / G03 | 通过 | 实例注册、授权过滤、快照、只读目录；绑定方法保持原服务对象，元数据单独复制；test_registry_reducers_budget.py |
| T04 / G04 | 通过 | 三种内置及自定义 V≠U Reducer、100 次排列、冲突与输入不变、多字段原子更新；单元及集成边界测试 |
| T05 / G05 | 通过 | 超过 24 个非法图场景、稳定诊断、祖先/写者/类型/输出覆盖、深度与大小限制；test_validation.py |
| T06 / G06 | 通过 | 10 次确定性摘要、零模型编译、列表 all-join、多个叶子 finalize；引擎与验证测试 |
| T07 / G07 | 通过 | llm/tool/check、真实 LangChain 单次适配器、FakeModelClient、提供商替身、输出验证与错误归类 |
| T08 / G08 | 通过 | 20 个竞争预算、实际调用并发上限、有限重试、期限与取消、returned/committed 区分 |
| T09 / G09 | 通过 | 版本化 Prompt/示例、冻结目标与能力、三轮共享预算、blocked 与格式修复、上下文超限拒绝 |
| T10 / G10 | 通过（本地实测平台） | 私有目录、原子文件、图 Hash、事件、脱敏、磁盘 errno、未完成记录识别、离线 check/compile；Windows 真机待验证 |
| T11 / G11 | 通过 | 结果与记录一致、失败保留已提交输出、收尾失败、业务验收由调用方、新目标 parent_run_id |
| T12 / G12a | 通过 | 12 个参考图成功及 12 个故障注入；全部确定性测试 157/157，两个 Python 版本分别通过 |
| T12 / G12b | 达到既定试用门槛 | 35/36 合法可执行图，达到 ≥33/36；合法图 35/35 完成并被测试上层接受；另完成 18 次阻塞观察 |
| T13 / G13 | 通过 | wheel/sdist、锁文件、快速入门、扩展/诊断指南；独立安装环境、源码目录外示例与 CLI，包内模板和样例 |

M0—M3 已闭环。M4 完成了有失败证据支持的规划错误反馈改进以及性能初测；缓存、编译优化和可视化不是首版必交项，不为完成度添加占位接口。

## 可复核的结果

- [Python 3.11 测试结果](experiments/pytest-311.xml)：157 通过，0 失败。
- [Python 3.12 测试结果](experiments/pytest-312.xml)：157 通过，0 失败。
- Ruff 检查与格式检查通过；测试启用 `-W error`。
- [模型完整 Schema 试验](experiments/model_smoke_result.json)：4/4 有效且精确往返。
- [最新可规划集合](experiments/live-20260911T113444Z.json)：首轮合法 31/36（86.1%），最终合法/完成/上层验收 35/36（97.2%），未出现意外 blocked。
- [独立阻塞集合](experiments/blocked-20260911T230307Z.json)：18/18 停止，图与业务工具调用均为 0；理由分类匹配 12/18（66.7%）。
- [最终证据审计](experiments/final-audit.json)：36 份首轮输入及资源 Hash 与当前实现一致，35 张历史图重新验证通过，字节 Hash 与 manifest 一致。
- [安装包核验](experiments/wheel-smoke.json)：独立安装、资源加载、离线示例、check/compile 与包内容检查。

36 次正式可规划集合共 51 次模型调用、61 次合成工具调用，已知输入 206691 Token、输出 35503 Token，费用未知。18 次阻塞观察共 18 次模型调用，输入 77781 Token、输出 4414 Token，费用未知。此处不将四次兼容探测或历史试跑费用混入正式分母。

正式可规划集合平均规划请求数 1.17，合法图平均 2 个节点、1.63 个字段。35 次成功规划从首轮事件到最后验证的中位耗时约 3.75 秒；保存图到编译结束约 0.012 秒；编译结束到运行收尾约 0.009 秒。事件区间包含记录开销，后者包括收尾，数据与分布见审计报告；它们不代表真实外部工具的生产延迟。

## 所有失败及已知限制

正式 36 次集合仅 evidence_counts 第 2 次失败（run `ff103110a0e940b59a5a844d44ad961d`）：三轮模型响应不符合格式，返回 GRAPH_GENERATION_FAILED，业务工具调用为 0。未通过图不进入执行，未放宽预算或契约以隐藏失败。

阻塞集合中 restricted_invoice 与 restricted_document 各 3 次均安全停止，但将预期 CONSTRAINT_CONFLICT 归为 CAPABILITY_GAP。授权过滤后的目录不暴露禁用工具；这里保留模型原始分类，后续若产品需要更精确的自然语言理由再依据独立集合改进。

历史报告全部保留，不能择优混合为一组结果：

| 报告 | 次数 | 结果和处理 |
| --- | --- | --- |
| [早期中止试跑](experiments/live-20260911T112028Z.json) | 5 | literature 三次、deduplication 两次均生成失败；修复反馈过于笼统，之后加入具体 Pydantic 错误消息 |
| [单项排障](experiments/live-20260911T112307Z.json) | 1 | literature 经修复通过；仅排障，不是完整基准 |
| [前一完整集合](experiments/live-20260911T112435Z.json) | 36 | 34 次通过；catalog 第 1 次验证失败、第 2 次格式失败 |
| [最终完整集合](experiments/live-20260911T113444Z.json) | 36 | 35 次通过，失败如上；首轮合法率未优于上一组，不宣称统计显著提升 |

上述任务与工具数据均为合成夹具。正式业务适配器、不同操作系统、不同模型/模式及未锁定依赖版本仍需调用方场景验证。Windows ACL 已实现并测试命令参数，尚无 Windows 真机证据。异步工具必须合作取消，纯 Reducer 性质依赖注册者正确实现。未知费用保持 null，严格金额/Token 上限不具备可靠估算时明确拒绝。

没有添加外部业务写、运行期改图、循环、resume、自动模型切换、代码生成、多后端或分布式执行；这些仍由 [路线图](FUTURE_ROADMAP.md) 管理。

## 复验命令

```bash
uv sync --locked --python 3.12
uv run ruff check .
uv run ruff format --check .
uv run pytest -q -W error
uv run dynamic-graph-demo
uv build
```

本次双版本使用 `.venv/bin/pytest` 与 `.venv311/bin/pytest`，加 `--junitxml=experiments/pytest-312.xml` / `pytest-311.xml`。完整 Schema、可规划、阻塞实测分别运行 `experiments/model_smoke.py`、`experiments/live_benchmark.py --debug`、`experiments/blocked_benchmark.py`，会产生付费请求；遇到 MODEL_QUOTA_EXHAUSTED 立即停止。此次未观察到余额不足。
