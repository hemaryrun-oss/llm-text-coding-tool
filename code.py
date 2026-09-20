# -*- coding: utf-8 -*-
"""
编码引擎：把一批文本 + 结构化编码手册 → 调 LLM 逐份编码 → 输出结构化结果。

用法：
    1. 配好 config.json（api_key / base_url / model）
    2. 把待编码的 .txt 放进 sample_input/
    3. 运行：python3 code.py

设计要点：
    - 模型无关：走 OpenAI 兼容接口（默认 DeepSeek，可换任何兼容端点）
    - 编码手册与脚本分离：换领域只需换 codebook.json，脚本一行不用改
      维度在 codebook.json 里用 dimensions 数组声明，type 取
      single（单选）/ scale（量表）/ multi_group（多选分组）/ multi_flat（多选平铺）
      四种之一，脚本按 type 驱动渲染、校验、落盘。
    - 逐份独立编码：每份文件一个独立请求，互不污染（避免跨文件套模板）
    - 严格 JSON 输出：按 schema 校验，不合规自动重试
"""

import json
import os
import re
import ssl
import sys
import time
from pathlib import Path
from urllib import request, error as urlerror

try:
    import certifi
    _DEFAULT_SSL_CA = certifi.where()
except ImportError:
    _DEFAULT_SSL_CA = None


def _ssl_context():
    """构建 SSL 上下文：优先 certifi（证书新、跨平台），否则系统默认。"""
    if _DEFAULT_SSL_CA:
        return ssl.create_default_context(cafile=_DEFAULT_SSL_CA)
    return ssl.create_default_context()

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
CODEBOOK_PATH = BASE_DIR / "codebook.json"
INPUT_DIR = BASE_DIR / "sample_input"
OUTPUT_DIR = BASE_DIR / "sample_output"

# ------------------------------------------------------------
# 1. 配置与手册加载
# ------------------------------------------------------------

def load_config():
    """读取 config.json；api_key 优先取环境变量 DEEPSEEK_API_KEY。"""
    cfg = {"api_key": "", "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat", "temperature": 0}
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    cfg["api_key"] = os.environ.get("DEEPSEEK_API_KEY", cfg.get("api_key", ""))
    return cfg


def load_codebook():
    return json.loads(CODEBOOK_PATH.read_text(encoding="utf-8"))


# ------------------------------------------------------------
# 2. 从 codebook.json 推导维度信息
# ------------------------------------------------------------

def _dims(cb):
    """维度列表（数组，顺序即编码顺序）。"""
    return cb["dimensions"]


def _all_item_ids(dim):
    """多选维度：返回所有 item id（分组展开或平铺）。"""
    if dim["type"] == "multi_group":
        return [it["id"] for g in dim["groups"] for it in g["items"]]
    return [it["id"] for it in dim["items"]]


def _field_count(cb):
    """动态算字段数：single/scale 各 1，multi 按 item 数。"""
    n = 0
    for d in _dims(cb):
        if d["type"] in ("single", "scale"):
            n += 1
        else:
            n += len(_all_item_ids(d))
    return n


# ------------------------------------------------------------
# 3. 从 codebook.json 渲染编码指令（prompt 正文）
# ------------------------------------------------------------

def _render_single(d):
    L = [f"## {d['name']}（单选）", d["instruction"]]
    for v in d["values"]:
        L.append(f"- **{v['value']}**：{v['rules']}")
    if d.get("exclusions"):
        L.append("排除清单：")
        for e in d["exclusions"]:
            L.append(f"- {e}")
    L.append("")
    return "\n".join(L)


def _render_scale(d):
    lo = min(v["value"] for v in d["scale"])
    hi = max(v["value"] for v in d["scale"])
    L = [f"## {d['name']}（{lo}-{hi} 量表）", d["instruction"]]
    for s in d["scale"]:
        L.append(f"- **{s['value']}（{s['label']}）**：{s['rules']}")
    if d.get("keyword_groups"):
        L.append("关键词（搜索时参考）：")
        for g in d["keyword_groups"]:
            L.append(f"- {g['group']}：{'、'.join(g['keywords'])}")
    if d.get("hard_rules"):
        L.append("硬规则：")
        for r in d["hard_rules"]:
            L.append(f"- {r}")
    L.append("")
    return "\n".join(L)


def _render_multi_group(d):
    L = [f"## {d['name']}（多选）", d["instruction"]]
    for g in d["groups"]:
        L.append(f"### {g['group']}")
        for it in g["items"]:
            line = f"- **{it['id']} {it['name']}**：触发词「{'、'.join(it['triggers'])}」"
            if it.get("note"):
                line += f"。注意：{it['note']}"
            L.append(line)
    for r in d.get("coding_rules", []):
        L.append(f"- {r}")
    L.append("")
    return "\n".join(L)


def _render_multi_flat(d):
    L = [f"## {d['name']}（多选）", d["instruction"]]
    for it in d["items"]:
        line = f"- **{it['id']} {it['name']}**：触发词「{'、'.join(it['triggers'])}」"
        if it.get("negative"):
            line += f"。不触发：{it['negative']}"
        L.append(line)
    for r in d.get("coding_rules", []):
        L.append(f"- {r}")
    L.append("")
    return "\n".join(L)


def render_prompt_body(cb):
    L = []
    L.append("你是文本编码员。请把下面这份文本，按编码手册编码成结构化数据。")
    L.append("")
    L.append("## 编码单元")
    L.append(cb["meta"].get("coding_unit", "一份文本 = 一条编码记录"))
    L.append("")
    L.append("## 输出格式（严格 JSON，不要输出任何其他文字）")
    L.append("```json")
    L.append("{")
    L.append('  "file_id": "文件序号",')
    for d in _dims(cb):
        key = d["key"]
        t = d["type"]
        if t == "single":
            L.append(f'  "{key}": "单选值",')
        elif t == "scale":
            lo = min(v["value"] for v in d["scale"])
            hi = max(v["value"] for v in d["scale"])
            L.append(f'  "{key}": {lo}到{hi}的整数,')
        else:
            ids = _all_item_ids(d)
            L.append(f'  "{key}": ["{ids[0]}", "{ids[1]}"],')
    L.append('  "coder_note": "编码备注（边界案例说明，可空）"')
    L.append("}")
    L.append("```")
    L.append("")

    for d in _dims(cb):
        t = d["type"]
        if t == "single":
            L.append(_render_single(d))
        elif t == "scale":
            L.append(_render_scale(d))
        elif t == "multi_group":
            L.append(_render_multi_group(d))
        else:
            L.append(_render_multi_flat(d))
    return "\n".join(L)


# ------------------------------------------------------------
# 4. LLM 调用
# ------------------------------------------------------------

def call_llm(cfg, messages, max_retries=3):
    """OpenAI 兼容 chat/completions 调用，带重试。"""
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    payload = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": cfg.get("temperature", 0),
    }
    headers = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
    }
    for attempt in range(1, max_retries + 1):
        try:
            req = request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers)
            with request.urlopen(req, timeout=180, context=_ssl_context()) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            return body["choices"][0]["message"]["content"]
        except Exception as e:
            print(f"  [调用失败 第{attempt}次] {e}", file=sys.stderr)
            if attempt == max_retries:
                raise
            time.sleep(3 * attempt)


def parse_json_response(text):
    """从 LLM 输出里稳健地抽取 JSON 对象（容忍 markdown 代码块）。"""
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        text = m.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1:
        text = text[start:end + 1]
    return json.loads(text)


# ------------------------------------------------------------
# 5. schema 校验：拦截 LLM 的非法输出，避免脏数据落盘
# ------------------------------------------------------------

def _save_json(path, records):
    """把 records 原子写回 JSON：先写临时文件再 os.replace 替换。"""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def load_existing(json_out):
    """读已有编码结果，返回 (records, 已完成无 error 的 file_id 集合)。

    文件损坏时不静默清空：先把损坏文件改名为 .corrupt 备份，再打印警告，从零重编。"""
    records = []
    done_ids = set()
    if json_out.exists():
        try:
            records = json.loads(json_out.read_text(encoding="utf-8"))
            done_ids = {r["file_id"] for r in records if r.get("file_id") and "error" not in r}
        except Exception as e:
            corrupt = json_out.with_name(json_out.name + ".corrupt")
            try:
                json_out.rename(corrupt)
                print(f"[警告] {json_out} 无法解析（{e}），已改名为 {corrupt.name} 保留现场，"
                      f"将从零重编。", file=sys.stderr)
            except OSError:
                print(f"[警告] {json_out} 无法解析（{e}），且改名备份失败，请手动检查。",
                      file=sys.stderr)
            records = []
    return records, done_ids


def build_validators(cb):
    """从 codebook.json 的 dimensions 推导各维度的合法取值。返回 {key: (kind, valid)}。

    kind ∈ {single, scale, multi}；valid 对 single/multi 是合法值集合，对 scale 是 (lo, hi)。"""
    spec = {}
    for d in _dims(cb):
        key = d["key"]
        t = d["type"]
        if t == "single":
            spec[key] = ("single", {v["value"] for v in d["values"]})
        elif t == "scale":
            vals = [v["value"] for v in d["scale"]]
            spec[key] = ("scale", (min(vals), max(vals)))
        elif t == "multi_group":
            spec[key] = ("multi", {it["id"] for g in d["groups"] for it in g["items"]})
        else:  # multi_flat
            spec[key] = ("multi", {it["id"] for it in d["items"]})
    return spec


def _is_int(v):
    """整数判断：bool 不算 int，2.0 这类整数浮点可容忍。"""
    if isinstance(v, bool):
        return False
    if isinstance(v, int):
        return True
    if isinstance(v, float):
        return v.is_integer()
    return False


def validate_record(record, cb):
    """校验一份编码结果是否合规，返回错误列表（空列表 = 通过）。"""
    spec = build_validators(cb)
    errors = []
    for key, (kind, valid) in spec.items():
        val = record.get(key)
        if kind == "single":
            if val not in valid:
                errors.append(f"{key} 取值 {val!r} 非法，应为 {sorted(valid)} 之一")
        elif kind == "scale":
            lo, hi = valid
            if not (_is_int(val) and lo <= val <= hi):
                errors.append(f"{key} 取值 {val!r} 非法，应为 {lo}-{hi} 的整数")
        else:  # multi
            if not isinstance(val, list):
                errors.append(f"{key} 应为数组，实际为 {type(val).__name__}")
            else:
                unknown = [x for x in val if x not in valid]
                if unknown:
                    errors.append(f"{key} 含非法项 {unknown}")
    return errors


# ------------------------------------------------------------
# 6. 主流程
# ------------------------------------------------------------

def main():
    import argparse

    ap = argparse.ArgumentParser(description="编码引擎：文档 + 手册 → 调 LLM → 结构化结果")
    ap.add_argument("--fresh", action="store_true", help="忽略已有编码结果，全量重编")
    args = ap.parse_args()

    if not CONFIG_PATH.exists():
        print("缺少 config.json，请先创建（参考 README.md）。", file=sys.stderr)
        sys.exit(1)

    cfg = load_config()
    cb = load_codebook()
    if not cfg["api_key"]:
        print("未找到 API key：请在 config.json 里填 api_key，或设置环境变量 DEEPSEEK_API_KEY。", file=sys.stderr)
        sys.exit(1)

    prompt_body = render_prompt_body(cb)
    txt_files = sorted(INPUT_DIR.glob("*.txt"))
    if not txt_files:
        print(f"{INPUT_DIR} 下没有 .txt 文件。", file=sys.stderr)
        sys.exit(1)

    OUTPUT_DIR.mkdir(exist_ok=True)
    json_out = OUTPUT_DIR / "编码结果.json"
    csv_out = OUTPUT_DIR / "编码结果.csv"

    # ---- 断点续传：读已有结果，跳过已完成（无 error）的文件 ----
    records, done_ids = ([], set()) if args.fresh else load_existing(json_out)

    todo = [fp for fp in txt_files if fp.stem not in done_ids]
    if not todo:
        print(f"全部 {len(done_ids)} 份文件已编码完成，无需重编（可用 --fresh 强制重编）。")
        _write_csv(records, cb, csv_out)
        return

    print(f"共 {len(todo)} 份待编码（跳过 {len(done_ids)} 份已完成）……")
    for i, fp in enumerate(todo, 1):
        doc_text = fp.read_text(encoding="utf-8", errors="ignore")
        doc_text = doc_text[:60000]  # 截断过长文本

        user_msg = f"待编码文件：{fp.stem}\n\n--- 文件正文开始 ---\n{doc_text}\n--- 文件正文结束 ---"
        messages = [
            {"role": "system", "content": prompt_body},
            {"role": "user", "content": user_msg},
        ]

        print(f"[{i}/{len(todo)}] 编码 {fp.stem} ……")
        result = None
        errs = ["编码失败"]
        for _ in range(3):  # 1 次初始 + 2 次语义重试
            try:
                content = call_llm(cfg, messages)
            except Exception as e:
                errs = [f"调用失败：{e}"]
                break  # 网络层已重试 3 次仍失败，不再语义重试
            try:
                result = parse_json_response(content)
            except Exception as e:
                errs = [f"JSON 解析失败：{e}"]
                messages += [
                    {"role": "assistant", "content": content},
                    {"role": "user", "content": "你上一次的输出不是合法 JSON，请重新输出严格 JSON，不要输出任何其他文字。"},
                ]
                result = None
                continue
            errs = validate_record(result, cb)
            if not errs:
                break
            messages += [
                {"role": "assistant", "content": content},
                {"role": "user", "content": "你上一次输出不符合编码规范，请修正后重新输出严格 JSON。错误如下：\n" + "\n".join(f"- {e}" for e in errs)},
            ]
            result = None

        if result is None:
            result = {"file_id": fp.stem, "error": "; ".join(errs)}
        else:
            result.setdefault("file_id", fp.stem)
        records.append(result)
        _save_json(json_out, records)  # 每份落盘一次，实现断点续传

        if "error" in result:
            print(f"  [失败] {fp.stem}: {result['error']}", file=sys.stderr)
        else:
            parts = []
            for d in _dims(cb):
                key = d["key"]
                if d["type"] in ("multi_group", "multi_flat"):
                    parts.append(f"{key}={len(result.get(key, []))}项")
                else:
                    parts.append(f"{key}={result.get(key)}")
            print("  -> " + ", ".join(parts))
        time.sleep(1)  # 温和限速

    _write_csv(records, cb, csv_out)
    print(f"\n完成。结果已写入：\n  {json_out}\n  {csv_out}")


def _write_csv(records, cb, path):
    """把编码结果扁平化成 CSV（按 schema 动态生成列）。"""
    import csv

    cols = ["file_id"]
    for d in _dims(cb):
        if d["type"] in ("single", "scale"):
            cols.append(d["key"])
        else:
            cols += _all_item_ids(d)
    cols.append("coder_note")

    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in records:
            row = [r.get("file_id", "")]
            for d in _dims(cb):
                if d["type"] in ("single", "scale"):
                    row.append(r.get(d["key"], ""))
                else:
                    ids = _all_item_ids(d)
                    s = set(r.get(d["key"], []))
                    row += [1 if i in s else 0 for i in ids]
            row.append(r.get("coder_note", r.get("error", "")))
            w.writerow(row)


if __name__ == "__main__":
    main()
