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

# 4. 校验日志完整性
python3 -m blackbox verify ~/.blackbox/logs/first-run.jsonl --expect-hash <最终哈希>

# 5. 查看指标
python3 -m blackbox summary ~/.blackbox/logs/first-run.jsonl
```

示例输出：

```
#  path               status  in   out  cached  reason  tools                finish      ms
-  -----------------  ------  ---  ---  ------  ------  -------------------  ----------  ----
1  /chat/completions  200     50   10   -       -       read_file,read_file  tool_calls  4.0
2  /chat/completions  200     120  30   100     12      bash                 tool_calls  64.7

totals
  requests: 2
  input_tokens: 170
  ...
  repeated_identical_tool_calls: 1
```

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
