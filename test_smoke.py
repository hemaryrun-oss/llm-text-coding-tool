# -*- coding: utf-8 -*-
"""冒烟测试：离线验证工具核心逻辑，无需 API key。

用法：python3 test_smoke.py
全部通过 → 打印 ALL PASS，退出码 0；任一失败 → 打印明细，退出码 1。

本测试用 codebook.json（招聘信息示例手册）驱动，验证「换手册不改代码」：
所有断言都从手册推导，不写死具体维度名。
"""

import json
import sys
from pathlib import Path
import importlib.util

BASE = Path(__file__).resolve().parent


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


code = load_module("code", BASE / "code.py")
qa = load_module("qa", BASE / "qa.py")

PASSED = []
FAILED = []


def expect(name, cond, detail=""):
    if cond:
        PASSED.append(name)
        print(f"  PASS  {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL  {name}  {detail}")


cb = json.loads((BASE / "codebook.json").read_text(encoding="utf-8"))

# ---- 1. 编码手册加载与字段数 ----
expect("codebook dimension_count=4", cb["meta"]["dimension_count"] == 4,
       f"实际={cb['meta']['dimension_count']}")
expect("codebook field_count=17", cb["meta"]["field_count"] == 17,
       f"实际={cb['meta']['field_count']}")
expect("动态算 field_count=17（与 meta 一致）", code._field_count(cb) == 17,
       f"实际={code._field_count(cb)}")
expect("维度类型齐全（single/scale/multi_group/multi_flat）",
       {d['type'] for d in cb['dimensions']} == {"single", "scale", "multi_group", "multi_flat"})

# ---- 2. prompt 渲染 ----
prompt = code.render_prompt_body(cb)
for name in ["岗位类别", "经验要求强度", "技能要求", "福利待遇"]:
    expect(f"prompt 含维度「{name}」", name in prompt)

# ---- 3. schema 校验 ----
good = {
    "file_id": "1",
    "岗位类别": "技术研发",
    "经验要求强度": 2,
    "技能要求": ["H1", "S1"],
    "福利待遇": ["W1", "W3"],
    "coder_note": "",
}
expect("validate_record 合法样例通过", code.validate_record(good, cb) == [],
       str(code.validate_record(good, cb)))

bad_single = dict(good, 岗位类别="乱写")
expect("validate_record 拒绝单选乱值", any("岗位类别" in e for e in code.validate_record(bad_single, cb)))

bad_scale = dict(good, 经验要求强度=5)
expect("validate_record 拒绝量表越界", any("经验要求强度" in e for e in code.validate_record(bad_scale, cb)))

bad_multi = dict(good, 技能要求=["H1", "X9"])
expect("validate_record 拒绝多选含非法项", any("技能要求" in e for e in code.validate_record(bad_multi, cb)))

bad_type = dict(good, 福利待遇="不是数组")
expect("validate_record 拒绝多选非数组", any("福利待遇" in e for e in code.validate_record(bad_type, cb)))

# ---- 4. Cohen's Kappa（手算期望值）----
# gold=[A,A,A,B] vs pred=[A,A,B,B] → po=0.75, pe=0.5, κ=0.5
k, po, pe, paradox = qa.cohens_kappa(
    ["技术研发", "技术研发", "技术研发", "市场营销"],
    ["技术研发", "技术研发", "市场营销", "市场营销"],
)
expect("Kappa 手算 0.5", round(k, 3) == 0.500, f"实际={k:.3f}")

# 完全一致 → κ=1.0
k2, _, _, _ = qa.cohens_kappa(["A", "A", "B", "B"], ["A", "A", "B", "B"])
expect("Kappa 完全一致 = 1.0", abs(k2 - 1.0) < 1e-9, f"实际={k2:.3f}")

# 量表（指定类别）完全一致 → κ=1.0
k3, _, _, _ = qa.cohens_kappa([0, 1, 2, 3], [0, 1, 2, 3], categories=[0, 1, 2, 3])
expect("Kappa 量表指定类别 = 1.0", abs(k3 - 1.0) < 1e-9, f"实际={k3:.3f}")

# ---- 5. 模板效应检测（完全一致）----
recs = [
    {"file_id": "a", "岗位类别": "技术研发", "经验要求强度": 2,
     "技能要求": ["H1", "S1"], "福利待遇": ["W1", "W3"]},
    {"file_id": "b", "岗位类别": "技术研发", "经验要求强度": 2,
     "技能要求": ["H1", "S1"], "福利待遇": ["W1", "W3"]},
    {"file_id": "c", "岗位类别": "市场营销", "经验要求强度": 1,
     "技能要求": ["H3"], "福利待遇": ["W2", "W4"]},
]
dup = qa.detect_template(recs, cb)
groups = [set(ids) for ids in dup.values()]
expect("模板检测共 1 组", len(dup) == 1, f"实际={len(dup)}")
expect("模板检测 {a,b}", {"a", "b"} in groups)
expect("模板检测涉及 2 份", sum(len(v) for v in dup.values()) == 2)

# ---- 6. 断点续传的纯逻辑（load_existing）----
import tempfile
with tempfile.TemporaryDirectory() as td:
    jp = Path(td) / "编码结果.json"
    jp.write_text(json.dumps([
        {"file_id": "a", "岗位类别": "技术研发"},
        {"file_id": "b", "error": "调用失败"},
    ], ensure_ascii=False), encoding="utf-8")
    recs2, done = code.load_existing(jp)
    expect("load_existing 提取已完成 file_id", done == {"a"}, f"实际={done}")
    expect("load_existing 保留全部 records（含 error）", len(recs2) == 2, f"实际={len(recs2)}")
    recs3, done2 = code.load_existing(Path(td) / "不存在.json")
    expect("load_existing 文件不存在时返回空", recs3 == [] and done2 == set())

    # ---- 6b. 原子写往返 ----
    code._save_json(jp, recs2)
    back = json.loads(jp.read_text(encoding="utf-8"))
    expect("_save_json 原子写往返内容一致", back == recs2, f"实际={back}")
    expect("_save_json 无 .tmp 残留", not Path(str(jp) + ".tmp").exists())

    # ---- 6c. 损坏文件保护 ----
    bad = Path(td) / "编码结果_坏.json"
    bad.write_text('{"file_id": "a", "岗位类别": "技', encoding="utf-8")  # 截断
    recs4, done3 = code.load_existing(bad)
    expect("load_existing 损坏文件返回空", recs4 == [] and done3 == set())
    expect("load_existing 损坏文件改名 .corrupt 保留现场",
           Path(str(bad) + ".corrupt").exists())

# ---- 汇总 ----
print()
if FAILED:
    print(f"FAILED {len(FAILED)} 项，PASSED {len(PASSED)} 项")
    sys.exit(1)
print(f"ALL PASS（{len(PASSED)} 项）")
sys.exit(0)
