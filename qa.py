# -*- coding: utf-8 -*-
"""
质检引擎：读 LLM 编码结果，自动做两件事——

  1. 信度检验（需提供人工黄金标准 --gold）：
     逐字段算 Cohen's Kappa，自动识别「κ 悖论」（边际分布偏斜导致 κ 假低），
     悖论项改报一致率。哪些字段纳入信度检验、用什么算法，都从
     codebook.json 的 qa 配置块 + 维度 type 推导。

  2. 模板效应检测（竖查，无需黄金标准）：
     把每份文件的「编码指纹」算出来，找出编码完全一致的文件对/组。
     内容不同、编码却一字不差，是批量套模板的铁证——常规横查查不出，
     竖着排开才露馅。指纹字段从 qa 配置块的 fingerprint_fields 读。

用法：
    python3 qa.py --pred sample_output/编码结果.json [--gold sample_output/黄金标准.json]
"""

import argparse
import json
from collections import defaultdict
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CODEBOOK_PATH = BASE_DIR / "codebook.json"
OUTPUT_DIR = BASE_DIR / "sample_output"


# ------------------------------------------------------------
# 从 codebook.json 推导维度信息
# ------------------------------------------------------------

def _dims(cb):
    return cb["dimensions"]


def _dim_by_key(cb, key):
    for d in _dims(cb):
        if d["key"] == key:
            return d
    return None


def _all_item_ids(dim):
    if dim["type"] == "multi_group":
        return [it["id"] for g in dim["groups"] for it in g["items"]]
    return [it["id"] for it in dim["items"]]


def _kappa_fields(cb):
    qa = cb.get("qa", {})
    if "kappa_fields" in qa:
        return qa["kappa_fields"]
    return [d["key"] for d in _dims(cb)]


def _fingerprint_fields(cb):
    qa = cb.get("qa", {})
    if "fingerprint_fields" in qa:
        return qa["fingerprint_fields"]
    # 默认：所有非 scale 维度（量表波动大，不适合做「完全一致」指纹）
    return [d["key"] for d in _dims(cb) if d["type"] != "scale"]


def _norm_multi(dim, record):
    """把多选结果规整成 {id: 0/1} 的字典。"""
    ids = _all_item_ids(dim)
    val = record.get(dim["key"], [])
    if isinstance(val, dict):  # 兼容 {H1:1, H2:0} 形式
        return {it: int(bool(val.get(it, 0))) for it in ids}
    s = set(val or [])
    return {it: (1 if it in s else 0) for it in ids}


# ------------------------------------------------------------
# Cohen's Kappa
# ------------------------------------------------------------

def cohens_kappa(gold, pred, categories=None):
    """Cohen's Kappa。返回 (kappa, po, pe, 是否悖论风险)。

    categories 缺省时从数据并集中取值。
    """
    n = len(gold)
    if n == 0:
        return None, None, None, False
    if categories is None:
        categories = sorted(set(gold) | set(pred))

    po = sum(1 for g, p in zip(gold, pred) if g == p) / n

    pe = 0.0
    for c in categories:
        pg = gold.count(c) / n
        pp = pred.count(c) / n
        pe += pg * pp

    # κ 悖论风险：某取值占比过高（≥90%）→ κ 被边际分布压低，不可靠
    paradox = any(
        (gold.count(c) + pred.count(c)) / (2 * n) >= 0.90
        for c in categories
    )

    if abs(pe - 1.0) < 1e-9:
        # Pe=1 意味着所有人答案完全一边倒，κ 无定义
        return 1.0 if po == 1.0 else 0.0, po, pe, True
    kappa = (po - pe) / (1.0 - pe)
    return kappa, po, pe, paradox


def _verdict(k, paradox):
    if k is None:
        return "—"
    if not paradox and k >= 0.60:
        return "≥0.60"
    if paradox:
        return "⚠ 悖论"
    return "⚠ <0.60"


# ------------------------------------------------------------
# 模板效应检测（竖查）
# ------------------------------------------------------------

def fingerprint(cb, record):
    """一份文件的编码指纹 = 按 fingerprint_fields 声明的字段顺序拼接。

    单选/量表取原值，多选取排序后的组合。"""
    parts = []
    for key in _fingerprint_fields(cb):
        dim = _dim_by_key(cb, key)
        val = record.get(key)
        if dim and dim["type"] in ("multi_group", "multi_flat"):
            parts.append(tuple(sorted(val or [])))
        else:
            parts.append(val)
    return tuple(parts)


def detect_template(records, cb):
    """找出编码指纹完全相同的文件组。"""
    groups = defaultdict(list)
    for r in records:
        groups[fingerprint(cb, r)].append(r.get("file_id", "?"))
    dup = {fp: ids for fp, ids in groups.items() if len(ids) >= 2}
    return dup


def _fp_str(cb, fp):
    """指纹的人类可读表示（多选显示项数）。"""
    fields = _fingerprint_fields(cb)
    parts = []
    for key, val in zip(fields, fp):
        dim = _dim_by_key(cb, key)
        if dim and dim["type"] in ("multi_group", "multi_flat"):
            parts.append(f"{key}[{len(val)}项]")
        else:
            parts.append(str(val))
    return " · ".join(parts)


# ------------------------------------------------------------
# 报告
# ------------------------------------------------------------

def build_report(cb, pred, gold=None):
    L = []
    L.append("# LLM 文本编码质检报告")
    L.append("")
    L.append(f"- 编码文件数：{len(pred)}")
    if gold:
        L.append(f"- 黄金标准文件数：{len(gold)}")
        L.append(f"- 比对文件数：{sum(1 for p in pred if any(g['file_id'] == p['file_id'] for g in gold))}")
    L.append("")
    L.append("---")
    L.append("")

    # ---- 一、信度检验 ----
    if gold:
        L.append("## 一、信度检验（Cohen's Kappa）")
        L.append("")
        by_id = {g["file_id"]: g for g in gold}
        common = [p for p in pred if p["file_id"] in by_id]
        L.append(f"黄金标准与 LLM 编码共同覆盖 {len(common)} 份文件。")
        L.append("")

        kappa_fields = _kappa_fields(cb)

        # 汇总维度（single + scale）
        summary = [k for k in kappa_fields if _dim_by_key(cb, k)["type"] in ("single", "scale")]
        if summary:
            L.append("### 汇总维度")
            L.append("")
            L.append("| 维度 | κ | 一致率 | 判定 |")
            L.append("|------|:---:|:---:|:---:|")
            for key in summary:
                dim = _dim_by_key(cb, key)
                cat = None
                if dim["type"] == "scale":
                    cat = [v["value"] for v in dim["scale"]]
                g = [by_id[p["file_id"]][key] for p in common]
                p = [r[key] for r in common]
                k, po, pe, paradox = cohens_kappa(g, p, categories=cat)
                k_str = f"{k:.3f}" if k is not None else "—"
                L.append(f"| {key} | {k_str} | {po*100:.0f}% | {_verdict(k, paradox)} |")
            L.append("")
            L.append("> κ 悖论：某取值占比 ≥90% 时 κ 被边际分布压低，此时以一致率为准。")
            L.append("")

        # multi 逐项
        for key in kappa_fields:
            dim = _dim_by_key(cb, key)
            if dim["type"] not in ("multi_group", "multi_flat"):
                continue
            L.append(f"### {key} 逐项")
            L.append("")
            L.append("| 项目 | κ | 一致率 | 判定 |")
            L.append("|------|:---:|:---:|:---:|")
            for it_id in _all_item_ids(dim):
                g = [_norm_multi(dim, by_id[p["file_id"]])[it_id] for p in common]
                p = [_norm_multi(dim, r)[it_id] for r in common]
                k, po, pe, paradox = cohens_kappa(g, p, categories=[0, 1])
                k_str = f"{k:.3f}" if k is not None else "—"
                name = it_id
                L.append(f"| {it_id} | {k_str} | {po*100:.0f}% | {_verdict(k, paradox)} |")
            L.append("")

    # ---- 二、模板效应检测 ----
    L.append("## 二、模板效应检测（竖查）")
    L.append("")
    L.append("编码指纹完全相同的文件组（内容不同、编码却一字不差 = 疑似套模板）：")
    L.append("")
    dup = detect_template(pred, cb)
    if not dup:
        L.append("未发现编码指纹完全相同的文件。")
    else:
        L.append("| 指纹 | 文件 |")
        L.append("|------|------|")
        for fp, ids in sorted(dup.items(), key=lambda x: -len(x[1])):
            L.append(f"| {_fp_str(cb, fp)} | {'、'.join(ids)} |")
        L.append("")
        L.append(f"共 {len(dup)} 组、涉及 {sum(len(v) for v in dup.values())} 份文件。建议把这些文件竖着排开、逐份重读重编。")
    L.append("")

    L.append("---")
    L.append("")
    L.append("*本报告由 qa.py 自动生成。*")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pred", required=True, help="LLM 编码结果 JSON（code.py 输出）")
    ap.add_argument("--gold", help="人工黄金标准 JSON（可选，提供则做信度检验）")
    args = ap.parse_args()

    cb = json.loads(CODEBOOK_PATH.read_text(encoding="utf-8"))
    pred = json.loads(Path(args.pred).read_text(encoding="utf-8"))
    gold = json.loads(Path(args.gold).read_text(encoding="utf-8")) if args.gold else None

    report = build_report(cb, pred, gold)

    OUTPUT_DIR.mkdir(exist_ok=True)
    out = OUTPUT_DIR / "质检报告.md"
    out.write_text(report, encoding="utf-8")
    print(report)
    print(f"\n报告已写入：{out}")


if __name__ == "__main__":
    main()
