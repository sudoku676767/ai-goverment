# 黑匣子 skill-blackbox

第三方 agent 插件和 skill 的“入职体检”工具。

插件市场鱼龙混杂：没人评级，权限却很高。黑匣子像飞机的飞行记录仪一样，站在 agent 外面，完整记录它通过 API 实际做了什么，然后把数据摊开给人看。**它只记录、不判断**，好坏由你自己决定。

第一个目标平台是 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)（dsh）。

## 当前状态：v0.1 开发中

已完成：**API 层记录器**

- OpenAI 兼容的透传代理，支持流式（SSE）逐块中继
- 每个事件立即写盘（fsync），agent 崩溃或被强制终止也不丢数据
- 只追加的 JSONL 日志，每条记录带前一条的哈希，篡改或删除可被检测
- 写入日志前自动遮蔽 API key、令牌、私钥；转发给上游的流量不做任何修改
- 统一的指标：输入、输出、缓存命中、推理 token，工具调用，结束原因，耗时
- **零第三方依赖**，只用 Python 标准库：审计工具自身不引入供应链风险

## 快速开始

需要 Python 3.10+。

```bash
git clone https://github.com/sudoku676767/skill-blackbox
cd skill-blackbox

# 1. 启动记录代理（默认转发到 DeepSeek 官方 API）
python3 -m blackbox proxy --session first-run

# 2. 另开一个终端，让 dsh 走代理
DEEPSEEK_BASE_URL=http://127.0.0.1:8787 npx @deepseek-ai/dsh web

# 3. 用完后在代理终端按 Ctrl+C，会打印最终哈希，记下它

# 4. 校验日志完整性（把哈希直接粘贴进去，不要带尖括号）
python3 -m blackbox verify ~/.blackbox/logs/first-run.jsonl --expect-hash 63534e9b...

# 5. 查看指标
python3 -m blackbox summary ~/.blackbox/logs/first-run.jsonl
```

示例输出（结构取自第一次真实运行：dsh 桌面版让 DeepSeek-V4-Flash 创建并运行 hello.py）：

```
#  kind    path               st   in    cache%  Δin   out  reason  tools  finish      ms      flags
1  meta    /models            200  -     -       -     -    -       -      -           240.0
2  meta    /models            200  -     -       -     -    -       -      -           240.0
3  agent1  /chat/completions  200  7624  0.0     -     241  197     glob   tool_calls  5000.0
4  aux     /chat/completions  200  199   0.0     -     64   64      -      length      5000.0  truncated,reasoning_only
5  agent2  /chat/completions  200  7880  91.0    +256  80   0       write  tool_calls  5000.0
6  agent3  /chat/completions  200  8014  89.4    +134  65   0       pwsh   tool_calls  5000.0
7  agent4  /chat/completions  200  8094  88.6    +80   61   0       -      stop        5000.0

prompt composition, agent turn 1 (7624 input tokens, by characters)
  system prompt         ...
  tool definitions      ...
  conversation          ...

flagged
  #4 aux: truncated, reasoning_only, max_tokens=64
```

怎么读：

- **kind**：`agent` 是 agent 循环的一轮（请求里带了工具），`aux` 是 harness 顺手发的辅助请求（比如起会话标题），`meta` 是模型列表之类的非生成请求。三类分开统计。
- **cache%**：这一轮输入里命中缓存的比例。
- **Δin**：和上一轮 agent 相比，输入多了多少，能看出哪一步往上下文里塞了大量内容。
- **prompt composition**：第一轮输入里系统提示、工具定义、对话内容各占多少字符，看清固定开销有多大。
- **flags**：纯机械标记，不做好坏判断。`truncated` 是输出撞到长度上限，`reasoning_only` 是输出全花在思考上、没有正文也没有工具调用。

`--json` 输出完整数据，方便自己写分析。

## 对比两组运行

同一个任务，干净环境跑几次、装上插件再跑几次，每次用不同的 `--session` 名字，然后：

```bash
python3 -m blackbox compare base-1 base-2 base-3 --vs clever-1 clever-2 clever-3 --labels base,clever
```

可以直接写会话名，也可以写日志路径。输出每个指标两组的平均值和范围 `平均 [最小..最大]`、差值和百分比，以及插件新增了哪些工具。

- `separate`：两组范围不重叠，差异明确。
- `overlap`：范围重叠，只跑几次时看不出明确差异，不要下结论。
- `same`：两组完全一样。

工具不做“哪组更好”的判断。成功与否需要你自己确认（比如看文件是否真的创建、程序是否真的运行）。

也可以 `pip install -e .`，之后直接用 `blackbox` 命令。

## 安全须知

- 代理默认只监听 `127.0.0.1`。监听其他地址时，能连到这个端口的人都可以用你的 API key。
- 日志会保存完整的提示词和代码。默认存在 `~/.blackbox/logs`，权限 0600。**不要把日志目录放在 agent 的工作目录里**，也不要提交到 git。
- 哈希链能证明文件内部没被改过，但不能防止整个文件被重写或尾部被截断。所以关闭时打印的最终哈希要存到 agent 碰不到的地方。
- 只靠 API 层看不到插件脚本在你机器内部做的事（联网、读写文件）。这需要系统层监控，在路线图里。

## 运行测试

```bash
python3 -m unittest discover -s tests -v
```

## 设计

完整设计见 [docs/design.md](docs/design.md)：黑匣子类比、四层记录、“先声明、后对账”的双通道、指标清单、路线图和参考文献。
