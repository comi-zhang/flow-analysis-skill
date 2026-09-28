#!/usr/bin/env python3
"""流水数据质检：余额链 / 时间精度 / 跨渠道重复 / 退款未对冲 / 解析错位。

每一项都对应一类真实事故，跑一遍比事后人工翻账单便宜得多。

用法
----
```bash
python qa_flow.py --input-dir masked/ --report qa.md
python qa_flow.py --input-dir masked/ --json qa.json       # 机器可读
```

输入：NDJSON，每行一条记录。字段名见 FIELDS，可用 --field-map 覆盖。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

FIELDS = {
    "date": "日期",
    "time": "交易时间",
    "amount": "金额",
    "direction": "收支方向",
    "category": "收支大类",
    "counterparty": "交易对手",
    "memo": "原始摘要",
    "balance": "账户余额",
    "channel": "渠道",
    "source": "来源文件",
    "account": "账户尾号",
}

PLATFORM_CHANNELS = {"微信支付", "支付宝", "微信", "财付通"}
REFUND_FULL = re.compile(r"已全额退款")
REFUND_PART = re.compile(r"已退款\(¥([\d,\.]+)\)")
INTEREST = re.compile(r"(应付利息|结息|存款利息)")
# PDF 单元格串行的典型形态：以 1-3 个汉字碎片开头接编码
FRAGMENT = re.compile(r"^[\u4e00-\u9fa5]{1,3}\s+[A-Z]-[A-Z0-9]{4}-[A-Z0-9]{4}")
PURE_DIGITS = re.compile(r"^[\d\s\-]{6,}$")


def load(paths: list[Path]) -> list[dict]:
    rows: list[dict] = []
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            rows += [json.loads(line) for line in fh if line.strip()]
    return rows


def get(row: dict, key: str, fmap: dict):
    return row.get(fmap.get(key, FIELDS[key]))


def num(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def has_time(value) -> bool:
    """交易时间是否精确到分（不是被补成的 00:00）。"""
    m = re.match(r"\d{4}-\d{2}-\d{2} (\d{2}):(\d{2})", str(value or ""))
    return bool(m) and not (m.group(1) == "00" and m.group(2) == "00")


def norm_name(value) -> str:
    s = re.sub(r"[\s（）()【】\[\]]", "", str(value or ""))
    return re.sub(r"(股份有限公司|有限责任公司|有限公司|平台商户|平台)", "", s).lower()


def minute_of(value) -> int | None:
    m = re.match(r"\d{4}-\d{2}-\d{2} (\d{2}):(\d{2})", str(value or ""))
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    return None if (hh == 0 and mm == 0) else hh * 60 + mm


def _unmatched_rows(items: list[dict], fmap: dict, tol: float) -> list[tuple[dict, float]]:
    """返回「前序余额在同组余额集合中找不到」的记录，附带算出的前序余额。

    与记录顺序无关：只问「本笔的前序余额是否有对应记录」，不要求相邻。

        支出：前序余额 = 本笔余额 + 金额
        收入：前序余额 = 本笔余额 − 金额
    """
    pool = {round(num(get(r, "balance", fmap)), 2) for r in items}
    bad: list[tuple[dict, float]] = []
    for row in items:
        balance = round(num(get(row, "balance", fmap)), 2)
        signed = -num(get(row, "amount", fmap)) if get(row, "direction", fmap) == "支出" else num(get(row, "amount", fmap))
        before = round(balance - signed, 2)
        if not any(abs(before - b) <= tol for b in pool):
            bad.append((row, before))
    return bad


def check_balance_chain(rows: list[dict], fmap: dict, tol: float = 0.02) -> dict:
    """余额链校验（**与记录顺序无关**），按「来源文件」判定。

    三个都踩过的坑，按踩的顺序写在这里：

    1. **不能用相邻两笔相减**。它要求「记录顺序 = 真实发生顺序」，而这个前提经常
       不成立：来源只有日期时日内顺序不可知，导出的行序也不保证与源文件一致。
       实测教训：同一份数据按顺序比只有 49.6% 自洽，改成与顺序无关的判定后是 100%
       ——差点据此判定这家银行的金额"只有一半可信"。
    2. **不能按「账户/卡号」分组**。同一个来源里的多个卡号可能属于**同一个余额主体**
       （同一账户的多张卡），也可能是卡号列本身解析不可靠。按卡号分组会把这些行切成
       几条断链，制造出十几处**假断裂**。实测教训：某来源按卡号分组有 15 处断裂，
       按来源合并后**为 0**——而且它的未匹配行的前序余额恰好都能在另一个"账户"里找到。
    3. **健康值不是 100%**。账期首笔的前序余额发生在账期之前，天然找不到，所以
       一个账期**完整的来源恰好有 1 笔未匹配**（若来源本身就是多张独立账户的合集，
       则为账户数）。所以判据是「未匹配 ≤ 1」而不是「自洽率 = 100%」。

    结论口径：

    - `unmatched <= 1` → 链闭合，**金额与行数可信**（漏行/金额错都会把未匹配数推高）。
    - `unmatched > 1` → 有 `unmatched - 1` 处断裂，逐条看 `samples` 回原始账单核对。
    - `unmatched_if_split_by_account > accounts` 且来源级正常 → **账户/卡号列不可信**，
      不要用它做账户维度分析（该列多半来自 PDF 折行的卡号单元格）。
    """
    by_source: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if get(row, "balance", fmap) is not None:
            by_source[str(get(row, "source", fmap))].append(row)

    out = {}
    for source, items in by_source.items():
        accounts: dict[str, list[dict]] = defaultdict(list)
        for r in items:
            accounts[str(get(r, "account", fmap))].append(r)

        bad = _unmatched_rows(items, fmap, tol)
        bad_split = sum(len(_unmatched_rows(v, fmap, tol)) for v in accounts.values())
        samples = [
            f"{get(row, 'date', fmap)} {num(get(row, 'amount', fmap)):,.2f} "
            f"余额 {num(get(row, 'balance', fmap)):,.2f}（前序 {before:,.2f} 无对应记录）"
            for row, before in bad[:3]
        ]
        total = len(items)
        out[source] = {
            "checked": total,
            "consistent": total - len(bad),
            "unmatched": len(bad),
            "breaks": max(0, len(bad) - 1),
            "rate": round((total - len(bad)) / total, 4) if total else None,
            "accounts": len(accounts),
            "unmatched_if_split_by_account": bad_split,
            "chain_closed": len(bad) <= 1,
            "account_column_suspect": bad_split > len(accounts) and len(bad) <= 1,
            "date_only": not any(has_time(get(r, "time", fmap)) for r in items),
            "samples": samples,
        }
    return out


def check_time_precision(rows: list[dict], fmap: dict) -> dict:
    """时间精度：只有日期的记录会被补成 00:00，不能参与时段类规则。"""
    per_source: dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        per_source[str(get(row, "source", fmap) or "未知")]["精确到分" if has_time(get(row, "time", fmap)) else "仅日期"] += 1
    total = len(rows)
    date_only = sum(c["仅日期"] for c in per_source.values())
    return {
        "total": total,
        "date_only": date_only,
        "date_only_ratio": round(date_only / total, 4) if total else 0,
        "per_source": {k: dict(v) for k, v in sorted(per_source.items(), key=lambda x: -sum(x[1].values()))},
        "note": "仅日期记录的 00:00 是补出来的，时段/凌晨类规则必须先按来源排除这些记录",
    }


def check_cross_channel_dupes(rows: list[dict], fmap: dict, max_gap: int = 120) -> list[dict]:
    """跨渠道重复：金额一致 + 同一天 + 时间相近 + 对手名归一后一致。"""
    buckets: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        if get(row, "direction", fmap) == "支出":
            buckets[(str(get(row, "date", fmap)), round(num(get(row, "amount", fmap)), 2))].append(row)

    pairs = []
    for key, members in buckets.items():
        if len(members) < 2 or key[1] < 100:
            continue
        plat = [r for r in members if get(r, "channel", fmap) in PLATFORM_CHANNELS]
        bank = [r for r in members if get(r, "channel", fmap) not in PLATFORM_CHANNELS]
        for b in bank:
            for p in plat:
                ta, tb = minute_of(get(p, "time", fmap)), minute_of(get(b, "time", fmap))
                if ta is not None and tb is not None and abs(ta - tb) > max_gap:
                    continue
                na, nb = norm_name(get(p, "counterparty", fmap)), norm_name(get(b, "counterparty", fmap))
                if na and nb and (na == nb or na in nb or nb in na):
                    pairs.append({
                        "date": key[0],
                        "amount": key[1],
                        "platform": f"{get(p, 'channel', fmap)} {get(p, 'counterparty', fmap)}",
                        "bank": f"{get(b, 'channel', fmap)} {get(b, 'counterparty', fmap)}",
                        "time_gap": "—" if ta is None or tb is None else abs(ta - tb),
                    })
                    plat.remove(p)
                    break
    return pairs


def check_refund_offsets(rows: list[dict], fmap: dict) -> dict:
    """退款未对冲：摘要写了退款但整笔仍按支出存在。"""
    full = part = 0.0
    n_full = n_part = 0
    for row in rows:
        if get(row, "direction", fmap) != "支出":
            continue
        memo = str(get(row, "memo", fmap) or "")
        if REFUND_FULL.search(memo):
            full += num(get(row, "amount", fmap))
            n_full += 1
            continue
        m = REFUND_PART.search(memo)
        if m:
            part += float(m.group(1).replace(",", ""))
            n_part += 1
    return {
        "full_refund_rows": n_full,
        "full_refund_amount": round(full, 2),
        "partial_refund_rows": n_part,
        "partial_refund_amount": round(part, 2),
        "note": "这些是仍然计入支出的退款；应从消费中剔除（全额）或扣减（部分）",
    }


def check_parse_noise(rows: list[dict], fmap: dict) -> dict:
    """解析错位迹象：对手名含碎片/纯数字/账号，或把银行计息当成消费。"""
    by_kind: Counter = Counter()
    samples: dict[str, list[str]] = defaultdict(list)
    pdf_rows = 0
    for row in rows:
        source = str(get(row, "source", fmap) or "")
        pdf_rows += int(source.lower().endswith(".pdf"))
        cp = str(get(row, "counterparty", fmap) or "")
        memo = str(get(row, "memo", fmap) or "")
        checks = [
            ("对手名夹账号", bool(re.search(r"(?<!\d)\d{10,}(?!\d)", cp)), cp),
            ("以碎片开头", bool(FRAGMENT.match(cp)), cp),
            ("对手名纯数字", bool(PURE_DIGITS.match(cp)), cp),
            ("银行计息", bool(INTEREST.search(cp + memo)),
             f"{get(row, 'date', fmap)} {num(get(row, 'amount', fmap)):,.2f} {cp[:40]}"),
        ]
        for kind, hit, sample in checks:
            if hit:
                by_kind[kind] += 1
                if len(samples[kind]) < 3:
                    samples[kind].append(sample[:60])
    return {
        "pdf_rows": pdf_rows,
        "total_rows": len(rows),
        "kinds": dict(by_kind),
        "samples": dict(samples),
    }


def render_markdown(result: dict) -> str:
    lines = ["# 流水数据质检报告\n", f"记录总数：{result['total_rows']}\n"]

    lines.append("## 1. 余额链自洽性\n")
    lines.append("| 来源 | 参与校验 | 自洽 | 未匹配 | 断裂 | 自洽率 | 判定 |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | --- |")
    for source, stat in sorted(result["balance"].items(), key=lambda x: -(x[1]["checked"] or 0)):
        rate = "—" if stat["rate"] is None else f"{stat['rate']:.1%}"
        verdict = "链闭合" if stat.get("chain_closed") else f"{stat.get('breaks', 0)} 处断裂"
        if stat.get("account_column_suspect"):
            verdict += "；账户列可疑"
        lines.append(
            f"| {source} | {stat['checked']} | {stat['consistent']} | "
            f"{stat['unmatched']} | {stat.get('breaks', 0)} | {rate} | {verdict} |"
        )
    lines.append(
        "\n> 读法（**与记录顺序无关、按来源整体判定**）：\n> "
        "1. 校验的是「本笔的前序余额能否在该来源的余额里找到」，所以导出丢失行序不影响结论。\n> "
        "2. **未匹配 = 1 是健康的**——那是账期首笔，它的前序余额发生在账期之前。"
        "判据是「未匹配 ≤ 1（链闭合）」，不是「自洽率 = 100%」。\n> "
        "3. 自洽率天然接近 `1 − 1/笔数`，笔数少时偏低属正常，**不要**据此判定金额不可信。\n> "
        "4. 判定为「账户列可疑」= 按账户/卡号拆分反而多出大量未匹配（同一账户多卡，"
        "或卡号列来自 PDF 折行解析不可靠）。**不要用该列做账户维度分析。**\n> "
        "5. 若要逐笔核对，用「未匹配」样例行回到原始对账单查证。\n"
    )

    tp = result["time_precision"]
    lines.append("## 2. 时间精度\n")
    lines.append(f"仅日期（无时间）记录 **{tp['date_only']} / {tp['total']} = {tp['date_only_ratio']:.1%}**\n")
    lines.append("| 来源 | 精确到分 | 仅日期 |")
    lines.append("| --- | ---: | ---: |")
    for source, stat in list(tp["per_source"].items())[:15]:
        lines.append(f"| {source} | {stat.get('精确到分', 0)} | {stat.get('仅日期', 0)} |")
    lines.append(f"\n> {tp['note']}\n")

    dupes = result["cross_channel_dupes"]
    lines.append("## 3. 跨渠道重复\n")
    lines.append(f"共 **{len(dupes)}** 组，剔除后应减少 **{sum(d['amount'] for d in dupes):,.2f}**\n")
    if dupes:
        lines.append("| 日期 | 金额 | 平台腿 | 银行腿 | 时差(分) |")
        lines.append("| --- | ---: | --- | --- | ---: |")
        for d in sorted(dupes, key=lambda x: -x["amount"])[:20]:
            lines.append(f"| {d['date']} | {d['amount']:,.2f} | {d['platform'][:26]} | {d['bank'][:26]} | {d['time_gap']} |")
    lines.append("")

    ref = result["refunds"]
    lines.append("## 4. 退款未对冲\n")
    lines.append(f"- 全额退款未对冲：**{ref['full_refund_rows']} 笔 / {ref['full_refund_amount']:,.2f}**")
    lines.append(f"- 部分退款未扣减：**{ref['partial_refund_rows']} 笔 / {ref['partial_refund_amount']:,.2f}**\n")

    noise = result["parse_noise"]
    lines.append("## 5. 解析错位与异常\n")
    lines.append(f"PDF 来源记录 {noise['pdf_rows']} / {noise['total_rows']}\n")
    lines.append("| 现象 | 命中 |")
    lines.append("| --- | ---: |")
    for kind, count in sorted(noise["kinds"].items(), key=lambda x: -x[1]):
        lines.append(f"| {kind} | {count} |")
    for kind, items in noise["samples"].items():
        if items:
            lines.append(f"\n**{kind}** 样例：")
            lines += [f"- `{s}`" for s in items]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="流水数据质检")
    ap.add_argument("--input-dir", required=True)
    ap.add_argument("--glob", default="*.ndjson")
    ap.add_argument("--field-map", help="字段映射 JSON")
    ap.add_argument("--report", help="Markdown 报告输出路径")
    ap.add_argument("--json", dest="json_out", help="JSON 结果输出路径")
    args = ap.parse_args()

    paths = sorted(Path(args.input_dir).glob(args.glob))
    if not paths:
        print(f"未找到文件：{args.input_dir}/{args.glob}", file=sys.stderr)
        return 2
    fmap = json.loads(Path(args.field_map).read_text(encoding="utf-8")) if args.field_map else {}
    rows = load(paths)
    print(f"读入 {len(rows)} 条记录")

    result = {
        "total_rows": len(rows),
        "balance": check_balance_chain(rows, fmap),
        "time_precision": check_time_precision(rows, fmap),
        "cross_channel_dupes": check_cross_channel_dupes(rows, fmap),
        "refunds": check_refund_offsets(rows, fmap),
        "parse_noise": check_parse_noise(rows, fmap),
    }

    markdown = render_markdown(result)
    if args.report:
        Path(args.report).write_text(markdown, encoding="utf-8")
        print(f"报告 → {args.report}")
    else:
        print(markdown)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"JSON → {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
