#!/usr/bin/env python3
"""按口径计算真实支出 / 真实消费，并做四类虚高修正，输出「大类 × 年」汇总。

口径定义见 references/caliber.md。核心立场：
**别只报一个数**——"真实支出"与"真实消费"的差额本身就是最重要的结论。

用法
----
```bash
python caliber_report.py --input-dir masked/ --out-dir reports/ \
  --non-consumption 金融理财,转账划转,人情往来 \
  --tuition-keywords 代收扣款,财务处,学费,住宿
```

输出：`caliber.md`（报告）+ `category_year.csv`（大类×年，可直接导入多维表格）+ `caliber.json`
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

FIELDS = {
    "date": "日期",
    "time": "交易时间",
    "year": "自然年",
    "amount": "金额",
    "direction": "收支方向",
    "category": "收支大类",
    "counterparty": "交易对手",
    "memo": "原始摘要",
    "channel": "渠道",
    "internal": "是否内部划转",
    "duplicate": "是否重复记账",
    "nature": "记账性质",
    "pair": "配对确认",
}

EXCLUDE_NATURE = {"交易未成功", "清算过渡"}
CONFIRMED_HEDGE = "确认·对冲/退款"
PLATFORM_CHANNELS = {"微信支付", "支付宝", "微信", "财付通"}

REFUND_FULL = re.compile(r"已全额退款")
REFUND_PART = re.compile(r"已退款\(¥([\d,\.]+)\)")
INTEREST = re.compile(r"(应付利息|结息|存款利息)")


def get(row: dict, key: str, fmap: dict):
    return row.get(fmap.get(key, FIELDS[key]))


def num(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def norm_name(value) -> str:
    s = re.sub(r"[\s（）()【】\[\]]", "", str(value or ""))
    return re.sub(r"(股份有限公司|有限责任公司|有限公司|平台商户|平台)", "", s).lower()


def minute_of(value) -> int | None:
    m = re.match(r"\d{4}-\d{2}-\d{2} (\d{2}):(\d{2})", str(value or ""))
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    return None if (hh == 0 and mm == 0) else hh * 60 + mm


def in_scope(row: dict, fmap: dict) -> bool:
    """真实支出口径：排除假交易。"""
    if get(row, "internal", fmap) or get(row, "duplicate", fmap):
        return False
    if get(row, "nature", fmap) in EXCLUDE_NATURE:
        return False
    return get(row, "pair", fmap) != CONFIRMED_HEDGE


def load(paths: list[Path]) -> list[dict]:
    rows: list[dict] = []
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            rows += [json.loads(line) for line in fh if line.strip()]
    return rows


def compute(rows: list[dict], fmap: dict, non_consumption: set[str],
            tuition_keywords: tuple[str, ...]) -> tuple[list[dict], dict, dict]:
    """返回（修正后的消费行, 各级汇总, 调整明细）。"""
    scope = [r for r in rows if in_scope(r, fmap)]
    expenses = [r for r in scope if get(r, "direction", fmap) == "支出"]
    current = [r for r in expenses if get(r, "category", fmap) not in non_consumption]

    adjustments: dict[str, float] = defaultdict(float)
    dropped: set[int] = set()

    # 跨渠道重复：金额一致 + 同一天 + 时间相近 + 对手名一致（保留平台腿）
    buckets: dict[tuple, list[dict]] = defaultdict(list)
    for row in current:
        buckets[(str(get(row, "date", fmap)), round(num(get(row, "amount", fmap)), 2))].append(row)
    for key, members in buckets.items():
        if len(members) < 2 or key[1] < 100:
            continue
        plat = [r for r in members if get(r, "channel", fmap) in PLATFORM_CHANNELS]
        bank = [r for r in members if get(r, "channel", fmap) not in PLATFORM_CHANNELS]
        for b in bank:
            for p in plat:
                tp, tb = minute_of(get(p, "time", fmap)), minute_of(get(b, "time", fmap))
                if tp is not None and tb is not None and abs(tp - tb) > 120:
                    continue
                np_, nb_ = norm_name(get(p, "counterparty", fmap)), norm_name(get(b, "counterparty", fmap))
                if np_ and nb_ and (np_ == nb_ or np_ in nb_ or nb_ in np_):
                    dropped.add(id(b))
                    adjustments["跨渠道重复（剔除银行腿）"] += num(get(b, "amount", fmap))
                    plat.remove(p)
                    break

    fixed: list[dict] = []
    for row in current:
        if id(row) in dropped:
            continue
        amount = num(get(row, "amount", fmap))
        cp = str(get(row, "counterparty", fmap) or "")
        memo = str(get(row, "memo", fmap) or "")
        category = get(row, "category", fmap)

        # 分类修正：学费/住宿费常被误归到餐饮
        if category == "餐饮美食" and any(k in cp + memo for k in tuition_keywords):
            category = "学习教育"

        if INTEREST.search(cp + memo):
            adjustments["银行计息混入"] += amount
            continue

        if REFUND_FULL.search(memo):
            adjustments["退款未对冲（全额）"] += amount
            continue

        m = REFUND_PART.search(memo)
        if m:
            refunded = float(m.group(1).replace(",", ""))
            if refunded < amount:
                adjustments["退款未对冲（部分）"] += refunded
                amount = round(amount - refunded, 2)

        fixed.append({**row, "_category": category, "_amount": amount})

    summary = {
        "rows": len(rows),
        "expense_rows": len(expenses),
        "expense_total": round(sum(num(get(r, "amount", fmap)) for r in expenses), 2),
        "consumption_current": round(sum(num(get(r, "amount", fmap)) for r in current), 2),
        "consumption_current_rows": len(current),
        "consumption_fixed": round(sum(r["_amount"] for r in fixed), 2),
        "consumption_fixed_rows": len(fixed),
    }
    return fixed, summary, dict(adjustments)


def by_year_category(fixed: list[dict], fmap: dict) -> list[dict]:
    yearly: dict[str, float] = defaultdict(float)
    cells: dict[tuple, dict] = defaultdict(lambda: {"amount": 0.0, "n": 0})
    for row in fixed:
        date = str(get(row, "date", fmap) or "")
        year = str(get(row, "year", fmap) or date[:4])
        amount = row["_amount"]
        yearly[year] += amount
        cell = cells[(year, row["_category"])]
        cell["amount"] += amount
        cell["n"] += 1
    out = []
    for (year, category), cell in sorted(cells.items()):
        out.append({
            "年度": year,
            "收支大类": category,
            "金额": round(cell["amount"], 2),
            "笔数": cell["n"],
            "占当年消费比": round(cell["amount"] / yearly[year] * 100, 2) if yearly[year] else 0,
        })
    return out


def render_markdown(summary: dict, adjustments: dict, cells: list[dict]) -> str:
    years = sorted({c["年度"] for c in cells})
    pivot: dict[str, dict[str, float]] = defaultdict(dict)
    for cell in cells:
        pivot[cell["收支大类"]][cell["年度"]] = cell["金额"]
    totals = {y: sum(c["金额"] for c in cells if c["年度"] == y) for y in years}

    L = ["# 流水口径报告\n", "## 口径对照\n"]
    L.append("| 口径 | 金额 | 笔数 |")
    L.append("| --- | ---: | ---: |")
    L.append(f"| 真实支出 | {summary['expense_total']:,.2f} | {summary['expense_rows']:,} |")
    L.append(f"| 真实消费（现行） | {summary['consumption_current']:,.2f} | {summary['consumption_current_rows']:,} |")
    L.append(f"| **真实消费（修正后）** | **{summary['consumption_fixed']:,.2f}** | **{summary['consumption_fixed_rows']:,}** |")
    L.append("")
    L.append("## 修正明细\n")
    if adjustments:
        L.append("| 修正项 | 金额 |")
        L.append("| --- | ---: |")
        total = 0.0
        for key, value in sorted(adjustments.items(), key=lambda x: -x[1]):
            L.append(f"| {key} | {value:,.2f} |")
            total += value
        L.append(f"| **合计虚高** | **{total:,.2f}** |")
    else:
        L.append("（未发现需要修正的项）")
    L.append("")
    L.append("## 大类 × 年\n")
    L.append("| 大类 | " + " | ".join(years) + " |")
    L.append("| --- |" + " ---: |" * len(years))
    for category in sorted(pivot, key=lambda c: -sum(pivot[c].values())):
        row = " | ".join(f"{pivot[category].get(y, 0):,.0f}" for y in years)
        L.append(f"| {category} | {row} |")
    L.append("| **合计** | " + " | ".join(f"**{totals[y]:,.0f}**" for y in years) + " |")
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="流水口径报告")
    ap.add_argument("--input-dir", required=True)
    ap.add_argument("--glob", default="*.ndjson")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--field-map", help="字段映射 JSON")
    ap.add_argument("--non-consumption", default="金融理财,转账划转,人情往来")
    ap.add_argument("--tuition-keywords", default="代收扣款,财务处,学费,住宿")
    args = ap.parse_args()

    paths = sorted(Path(args.input_dir).glob(args.glob))
    if not paths:
        print(f"未找到文件：{args.input_dir}/{args.glob}", file=sys.stderr)
        return 2

    fmap = json.loads(Path(args.field_map).read_text(encoding="utf-8")) if args.field_map else {}
    non_consumption = {x.strip() for x in args.non_consumption.split(",") if x.strip()}
    tuition = tuple(x.strip() for x in args.tuition_keywords.split(",") if x.strip())

    fixed, summary, adjustments = compute(load(paths), fmap, non_consumption, tuition)
    cells = by_year_category(fixed, fmap)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "caliber.md").write_text(render_markdown(summary, adjustments, cells), encoding="utf-8")
    with open(out_dir / "category_year.csv", "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["年度", "收支大类", "金额", "笔数", "占当年消费比"])
        writer.writeheader()
        writer.writerows(cells)
    (out_dir / "caliber.json").write_text(
        json.dumps({"summary": summary, "adjustments": adjustments, "by_year_category": cells},
                   ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"真实支出   {summary['expense_total']:>14,.2f}")
    print(f"真实消费(现行) {summary['consumption_current']:>14,.2f}")
    print(f"真实消费(修正) {summary['consumption_fixed']:>14,.2f}")
    for key, value in sorted(adjustments.items(), key=lambda x: -x[1]):
        print(f"   修正 {key}: {value:,.2f}")
    print(f"\n输出 → {out_dir}/caliber.md · category_year.csv · caliber.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
