# 公文校核测评：测出真实数字

用真实公文测出“政务公文智核”的检出率、误报、单篇耗时和成本。只用 Python 3.10+ 标准库，不需要安装任何包。

## 原理

1. 准备一批**没有错误的公文**（原文）。
2. 程序在每份原文里**每次只埋一处已知错误**，生成若干测试样本：发文字号括号、标题末尾标点、主送机关半角冒号、正文半角逗号、缺少成文日期、错别字、文种用语不当。
3. 规则引擎和 DeepSeek 分别校核每个样本，统计埋入的错误有没有被找出来（**检出率**）。
4. 原文本身也跑一遍，统计没埋错时会冒出多少条提示（**误报参考**，需要人工看一眼是不是真错）。
5. 记录每次模型调用的耗时和 token，按官方价格折算**成本**。模型引用了原文里不存在的句子时，会自动丢弃并计数（**幻觉拦截**）。

## 运行步骤

```bash
git clone https://github.com/sudoku676767/ai-goverment
cd ai-goverment

# 1. 准备原文：建议从学校官网、教育厅或政府网站下载 20 份以上公开的通知、报告、请示，
#    另存为 .docx 或 .txt 放进一个文件夹（PDF 请先另存为 Word）。
#    文件名以 sources / readme 开头的文件（如来源清单 sources.txt）会被跳过。
#    eval/samples/ 里有 6 份虚构样例，可以先用来试跑。

# 2. 只测规则引擎（不需要 key，几秒钟）
python3 -m govdoc.evaluate --docs eval/samples --no-llm

# 3. 规则 + DeepSeek（key 只放在本机环境变量里）
export DEEPSEEK_API_KEY=你的key          # Windows PowerShell: $env:DEEPSEEK_API_KEY="你的key"
python3 -m govdoc.evaluate --docs 你的公文文件夹 --out eval-out
```

结果写在 `eval-out/report.md`（给人看）和 `eval-out/results.json`（全部明细）。

## 让每次调用进审计日志（可选，适合录镜头 13）

```bash
# 终端 1：启动黑匣子代理，上游指向 DeepSeek
python3 -m blackbox proxy --upstream https://api.deepseek.com --session eval-run

# 终端 2：测评走代理
python3 -m govdoc.evaluate --docs 你的公文文件夹 --base-url http://127.0.0.1:8787

# 跑完在终端 1 按 Ctrl+C，然后
python3 -m blackbox verify ~/.blackbox/logs/eval-run.jsonl
python3 -m blackbox summary ~/.blackbox/logs/eval-run.jsonl
```

## 参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--model` | `deepseek-flash`（或环境变量 `DEEPSEEK_MODEL`） | 官方 API 中 V4.1-Flash 的模型名，以 DeepSeek 文档为准 |
| `--base-url` | `https://api.deepseek.com` | 换成本地部署地址或黑匣子代理 |
| `--workers` | 4 | 并发数（深度推理单次约 1 分钟以上，可开到 12） |
| `--timeout` | 300 | 单次调用超时秒数 |
| `--price-in` / `--price-out` / `--price-cached` | 1 / 4 / 0.02 | 元每百万 token，默认官方闲时价；高峰期用 2 / 8 / 0.04 |

## 写进材料时要注意

- 这是“基于公开公文样本的测评”，不是试点结果，写的时候请如实标注样本来源和数量。
- 规则类错误是程序按规则埋入、再由规则查出，所以规则引擎的检出率接近 100% 是预料之中的。更有说服力的是：**大模型对错别字和文种用语的检出率**，以及**真实原文上的误报数**。
- 样本越多数字越稳，建议至少 20 份原文。
