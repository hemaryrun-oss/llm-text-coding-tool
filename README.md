# LLM 文本编码工具（通用版）

[![smoke-test](https://github.com/hemaryrun-oss/llm-text-coding-tool/actions/workflows/smoke.yml/badge.svg)](https://github.com/hemaryrun-oss/llm-text-coding-tool/actions/workflows/smoke.yml)
[![GitHub](https://img.shields.io/badge/GitHub-主仓库-181717?logo=github)](https://github.com/hemaryrun-oss/llm-text-coding-tool)
[![Gitee](https://img.shields.io/badge/Gitee-国内镜像-C71D23?logo=gitee)](https://gitee.com/maryrun/llm-text-coding-tool)
![Python](https://img.shields.io/badge/Python-3.8%2B-3776AB?logo=python&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green)

> **English abstract** — A schema-driven toolkit for auditing LLM text annotation. It turns "coding texts with an LLM" from a one-off task into a reusable, auditable pipeline: `code.py` encodes documents against a structured coding manual, and `qa.py` audits the results for the two failure modes hand-sampling usually misses — **omission** (trigger terms present in the source but left untagged) and **template effects** (the model reusing one fixed answer across files while skipping the text). The auditor reports per-field **Cohen's Kappa** with automatic **κ-paradox** detection, plus a **template-effect sweep** that flags files whose coding fingerprints are identical despite different source text. The manual lives in `codebook.json`; switching domains means swapping the manual, not the scripts.

把「用大模型给文本做编码/归类」这件事，做成一套**可复用、可质检**的流程。

> 🪞 **国内访问**：GitHub 打不开或很慢时，可用 Gitee 镜像：https://gitee.com/maryrun/llm-text-coding-tool （与主仓库同步更新）。

核心思路：**编码手册（`codebook.json`）与脚本分离**。换一个研究领域，只需要换掉 `codebook.json`（维度、取值、触发词、边界规则），`code.py` 和 `qa.py` 一行不用改。本仓库自带一份虚构的「招聘信息编码」手册作为演示，用来验证「换手册不改代码」。

## 它解决什么问题

大模型做文本编码时，会**漏标**（触发词白纸黑字在原文里，模型没勾）、会**套模板偷懒**（批量处理时跳读原文、糊一套固定答案交差）。这些错，常规抽查往往查不出来。

这套工具把「编码」和「质检」拆成两个独立环节：

| 环节 | 脚本 | 做什么 |
|------|------|------|
| 编码 | `code.py` | 文档 + 手册 → 调 LLM → 结构化结果（逐份独立、schema 校验、断点续传） |
| 质检 | `qa.py` | 结果 → Cohen's Kappa（含 κ 悖论识别）+ 模板效应竖查 → 报告 |

## 目录结构

```
llm-text-coding-tool/
├── codebook.json        # 编码手册（示例：招聘信息编码，4 维度 17 字段）
├── code.py              # 编码引擎
├── qa.py                # 质检引擎
├── test_smoke.py        # 冒烟测试（离线自检，无需 API key）
├── config.json.example  # 配置模板
├── sample_input/        # 样例输入（虚构招聘信息）
└── sample_output/       # 产出（编码结果.json/.csv + 质检报告.md）
```

## 快速开始

### 1. 配置

```bash
cp config.json.example config.json   # 填入 api_key，或 export DEEPSEEK_API_KEY=...
```

模型走 OpenAI 兼容接口（默认 DeepSeek），换任何兼容端点只需改 `base_url` 和 `model`。

### 2. 编码

把待编码的 `.txt` 放进 `sample_input/`，运行：

```bash
python3 code.py            # 断点续传：跳过已编码完成的文件
python3 code.py --fresh    # 强制全量重编
```

产出 `sample_output/编码结果.json` 和 `编码结果.csv`。每份文件一个独立请求，互不污染（从设计上避免跨文件套模板）。

### 3. 质检

```bash
python3 qa.py --pred sample_output/编码结果.json --gold sample_output/黄金标准.json
```

- 提供 `--gold`（人工编码的标准答案）时，逐字段算 Cohen's Kappa，自动识别 κ 悖论；
- 不提供 `--gold` 时，只做模板效应竖查。

产出 `sample_output/质检报告.md`。

### 4. 冒烟测试（离线自检，无需 API key）

```bash
python3 test_smoke.py
```

断言手册字段数、prompt 渲染、schema 校验、Kappa 手算值、模板检测、断点续传/原子写/损坏保护。全部通过打印 `ALL PASS`。

## 用法演示

以 `sample_input/示例1_Java开发工程师.txt`（一段虚构的招聘信息）为例，走一遍「编码 → 质检」：

**输入**（节选）：

```
招聘职位：Java 开发工程师（中级）
任职资格：
  3-5 年 Java 开发经验，熟悉 Python、SQL；
  有良好的沟通协调能力，能跨部门对接。
福利待遇：五险一金；弹性上下班；带薪年假。
```

**① 编码结果**（`code.py` 输出）：

```json
{
  "file_id": "示例1_Java开发工程师",
  "岗位类别": "技术研发",
  "经验要求强度": 2,
  "技能要求": ["H1", "H2", "S1"],
  "福利待遇": ["W1", "W3", "W4"],
  "coder_note": ""
}
```

**② 质检报告**（`qa.py` 输出，节选）：

| 维度 | κ | 一致率 | 判定 |
|------|:---:|:---:|:---:|
| 岗位类别 | 1.000 | 100% | ≥0.60 |
| 经验要求强度 | 1.000 | 100% | ≥0.60 |

> `技能要求` 里的 `S1` 会标「⚠ 悖论」——3 份样本里 S1 几乎人人勾选，边际分布一边倒、Kappa 被压低，工具自动改报一致率。这不是 bug，正是「κ 悖论识别」在起作用。

## 编码手册怎么设计

`codebook.json` 里用 `dimensions` 数组声明维度，每个维度声明一个 `type`：

| type | 含义 | 数据形态 |
|------|------|---------|
| `single` | 单选 | 取 `values[].value` 之一 |
| `scale` | 量表 | `scale[].value` 范围内的整数 |
| `multi_group` | 多选（分组） | `groups[].items[].id` 的子集 |
| `multi_flat` | 多选（平铺） | `items[].id` 的子集 |

脚本按 `type` 自动驱动渲染 prompt、校验 schema、生成 CSV 列、计算 Kappa——**换手册不改代码**。

`qa` 配置块声明质检要用的字段：

```json
"qa": {
  "kappa_fields": ["岗位类别", "经验要求强度", "技能要求", "福利待遇"],
  "fingerprint_fields": ["岗位类别", "技能要求", "福利待遇"]
}
```

- `kappa_fields`：纳入信度检验的字段（不写则默认全部维度）；
- `fingerprint_fields`：模板效应竖查用的编码指纹字段（不写则默认所有非量表维度，量表波动大不适合做「完全一致」指纹）。

## 换领域怎么做

1. 复制一份本仓库；
2. 重写 `codebook.json`（你的维度、取值、触发词、边界规则）；
3. 换 `sample_input/` 里的文本；
4. `python3 test_smoke.py` 验证工具正常（测试从手册推导，不写死维度名）；
5. `python3 code.py` 编码 → `python3 qa.py` 质检。

## 可靠性设计

- **逐份独立编码**：每份文件一个独立请求，互不污染（模板效应检测能成立的前提）；
- **schema 校验 + 语义重试**：LLM 输出不合规时，把错误清单反馈给模型重试 2 次，仍失败标 `error`，脏数据不落盘；
- **断点续传**：每份编码完立即落盘，中途断了重跑自动跳过已完成文件；
- **原子写 + 损坏保护**：先写 `.tmp` 再 `os.replace`，读到的 JSON 损坏则改名 `.corrupt` 保留现场，不静默清空；
- **κ 悖论识别**：答案一边倒时 Kappa 被边际分布压低，自动改报一致率，避免误判。

## 一个被数据否定的设计（部分套模板检测）

模板效应竖查一度想再抓一类「高度相似」（字段一致率 ≥90%）的文件。实测发现：同领域文本天然同质时，字段一致率是一条连续分布，没有能区分「套模板」和「本来就相似」的断点。真正套模板的信号只有「编码完全一致」。所以只保留「完全一致」检测。

这条结论也提醒：**编码相似必须和原文相似做对照**（原文不相似、编码却相似，才是套模板），不能只看编码一侧。

## 方法论出处

工具的几个核心设计都有方法论文献依据：

- **Cohen's Kappa**：Cohen, J. (1960). A coefficient of agreement for nominal scales. *Educational and Psychological Measurement*, 20(1), 37–46.
- **κ 悖论识别**（答案一边倒时 Kappa 被边际分布压低、改报一致率）：Feinstein, A. R., & Cicchetti, D. V. (1990). High agreement but low kappa: I. The problems of two paradoxes. *Journal of Clinical Epidemiology*, 43(6), 543–549.
- **编码手册的「触发词 + 反例 + 边界规则」设计**：来自内容分析法对编码方案「可操作、可复现」的一贯要求。经典教材见 Krippendorff, K. (2018). *Content Analysis: An Introduction to Its Methodology* (4th ed.). Sage；Neuendorf, K. A. (2017). *The Content Analysis Guidebook* (2nd ed.). Sage.
- **模板效应竖查**（比对编码指纹、找完全一致的文件）：这是本工具的**自创设计**，暂无现成文献出处。它源于一个实证观察——批量编码时 LLM 会跳读原文、复用同一套答案，而「编码完全一致」是这种偷懒最硬的信号。若用于正式研究，建议先做可证伪性检验：**编码相似必须与原文相似做对照**（原文不相似、编码却一致，才构成套模板证据）。

## License

MIT
