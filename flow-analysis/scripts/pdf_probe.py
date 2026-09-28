#!/usr/bin/env python3
"""PDF 对账单结构探针：先看清这份 PDF 长什么样，再决定怎么解析。

回答三个问题（决定后面所有事）：

1. **表体在哪**：哪些行是每页重复的表头/页脚（解析时必须剔除），哪些行是交易行。
2. **金额能不能按行尾切**：行尾是否稳定出现「金额 余额」两个数字。
   能 → 可以直接从行尾反向切；不能 → 必须用坐标（pdfplumber）定位列。
3. **有没有折行**：卡号/账号被拆成多段（行首出现孤立数字片段）。
   有 → 账户列不可信，余额链校验必须按「来源」而不是按「账户」分组。

只依赖 pypdf（`pip install pypdf`）。坐标级提取请另用 pdfplumber，本脚本不代替它。

用法
----
```bash
python pdf_probe.py --pdf 交易流水.pdf            # 只探前 5 页
python pdf_probe.py --pdf 交易流水.pdf --pages 0  # 全量
python pdf_probe.py --pdf 交易流水.pdf --json out.json
```

⚠️ 输出含原文片段。长数字串（卡号/手机号/账号）会被替换为 `C-xxxx`，
但**姓名仍可能出现在样例行里**——只在本机看，不要把输出贴进仓库或报告。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from datetime import date
from pathlib import Path

# 行尾「金额 余额」：如 "… -7.80 2109.55" / "… 2.21 379.74"
TAIL_MONEY = re.compile(r"([-+]?\d[\d,]*\.\d{1,2})\s+([-+]?\d[\d,]*\.\d{1,2})\s*$")
EIGHT_DIGITS = re.compile(r"^(\d{8})\b")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}\b")
ISOLATED_DIGITS = re.compile(r"^\d{1,3}$")      # 折行残留的孤立数字
LONG_DIGITS = re.compile(r"\d{10,}")            # 卡号/账号/手机号


def is_compact_date(token: str) -> bool:
    """8 位数字是否像 YYYYMMDD。

    ⚠️ 折行的卡号尾段也是 8 位（形如 `07639999`），所以**必须校验月/日范围**，
    否则会把卡号当日期——这正是"账户列不可信"的根源之一。
    """
    if not re.fullmatch(r"\d{8}", token):
        return False
    year, month, day = int(token[:4]), int(token[4:6]), int(token[6:8])
    if not (1990 <= year <= 2099):
        return False
    try:
        date(year, month, day)  # 真的按日历校验：20250230 / 20250229 都要排除
    except ValueError:
        return False
    return True


def redact(text: str) -> str:
    """把长数字串换成稳定编码，保住「是不是同一个」但不泄露真值。"""

    def repl(m: re.Match) -> str:
        h = hashlib.sha1(m.group(0).encode()).hexdigest()[:4].upper()
        return f"C-{h}"

    return LONG_DIGITS.sub(repl, text)


def page_lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def probe(pdf: Path, max_pages: int | None) -> dict:
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover - 依赖缺失路径
        raise SystemExit("需要 pypdf：pip install pypdf")

    reader = PdfReader(str(pdf))
    total_pages = len(reader.pages)
    pages = range(total_pages) if not max_pages else range(min(max_pages, total_pages))

    per_page: list[list[str]] = []
    for i in pages:
        per_page.append(page_lines(reader.pages[i].extract_text() or ""))

    # 跨页重复的行 = 模板（表头/页脚/说明），解析时要剔除。
    # 阈值要用「几乎每页都出现」：实测落在 50%~90% 页之间的行多是**折行的字段残片**
    # （如「技有限公司 财付通扣款」），按模板剔掉会误删真实数据。
    seen: Counter = Counter()
    for lines in per_page:
        seen.update(set(lines))
    n_pages = len(per_page) or 1
    strict = max(2, int(n_pages * 0.9 + 0.5))
    template = {ln for ln, c in seen.items() if c >= strict}
    half = {ln for ln, c in seen.items() if n_pages * 0.5 <= c < strict}

    body: list[str] = []
    for lines in per_page:
        body += [ln for ln in lines if ln not in template]

    tail_money = [ln for ln in body if TAIL_MONEY.search(ln)]
    dated: list[str] = []
    cardish: list[str] = []  # 8 位数字开头但不像日期 = 折行卡号/账号片段
    for ln in body:
        m = EIGHT_DIGITS.match(ln)
        if m:
            (dated if is_compact_date(m.group(1)) else cardish).append(ln)
        elif ISO_DATE.match(ln):
            dated.append(ln)
    fragments = [ln for ln in body if ISOLATED_DIGITS.match(ln)]

    n_body = len(body) or 1
    n_dated = len(dated) or 1
    result = {
        "pdf": pdf.name,
        "pages_total": total_pages,
        "pages_probed": len(per_page),
        "lines_per_page": [len(x) for x in per_page],
        "template_lines": sorted(template),
        "template_line_count": len(template),
        "half_template_lines": sorted(half)[:20],
        "half_template_count": len(half),
        "body_lines": len(body),
        "dated_lines": len(dated),
        "tail_money_lines": len(tail_money),
        # 每条记录的最后一行才有「金额 余额」，所以按记录（日期行）归一
        "tail_money_per_record": round(len(tail_money) / n_dated, 4),
        "tail_money_ratio_of_body": round(len(tail_money) / n_body, 4),
        "card_fragment_lines": len(cardish),
        "isolated_fragment_lines": len(fragments),
        "samples": {
            "dated": [redact(x)[:110] for x in dated[:5]],
            "card_fragment": [redact(x)[:110] for x in cardish[:5]],
            "fragments": fragments[:5],
        },
    }
    return result


def verdict(r: dict) -> list[str]:
    lines = []
    per_record = r["tail_money_per_record"]
    if per_record >= 0.8:
        lines.append(
            f"每条记录（日期行）后有金额行，比值 {per_record:.0%} → "
            "**可以从行尾反向切金额/余额**，其余部分再按列模板切。"
        )
    else:
        lines.append(
            f"金额行/日期行 = {per_record:.0%}（低于 80%）→ 记录与金额并非一一对应，"
            "不要按空白切分，改用 pdfplumber 取词坐标按列切；"
            "或先做「多行合并成一条记录」再切。"
        )
    if r["card_fragment_lines"]:
        lines.append(
            f"有 {r['card_fragment_lines']} 行以「8 位但不像日期」的数字开头 → "
            "**卡号/账号被折行**。不要用「8 位数字开头」判断日期（卡号尾段也是 8 位），"
            "且账户/卡号列不可信，余额链校验必须按「来源」而不是「账户」分组。"
        )
    if r["isolated_fragment_lines"] > r["pages_probed"] * 0.5:
        lines.append(
            "存在大量孤立数字片段 → 单元格折行严重，先做行合并与跨行重组再解析。"
        )
    if r["half_template_count"]:
        lines.append(
            f"有 {r['half_template_count']} 行出现在 50%~90% 的页面上："
            "**不要直接当模板剔除**——折行的字段残片会长这样（实测「技有限公司 财付通扣款」"
            "每页都有）。剔除模板用「几乎每页都出现」的严格阈值，或按页首/页尾行区间剔除。"
        )
    lines.append(f"模板行 {r['template_line_count']} 条（≥90% 页面重复），解析时剔除。")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description="PDF 对账单结构探针")
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--pages", type=int, default=5, help="探测页数，0 = 全量（默认 5）")
    ap.add_argument("--json", dest="json_out", help="JSON 结果输出路径")
    args = ap.parse_args()

    pdf = Path(args.pdf)
    if not pdf.exists():
        print(f"文件不存在：{pdf}", file=sys.stderr)
        return 2

    result = probe(pdf, args.pages or None)

    print(f"# PDF 结构探针：{result['pdf']}")
    print(f"页数 {result['pages_total']}（探测 {result['pages_probed']}）")
    print(f"每页行数：{result['lines_per_page']}")
    print(f"\n正文行 {result['body_lines']}｜日期行 {result['dated_lines']}｜"
          f"金额行 {result['tail_money_lines']}（占日期行 {result['tail_money_per_record']:.1%}）｜"
          f"折行卡号片段 {result['card_fragment_lines']}｜孤立碎片 {result['isolated_fragment_lines']}")
    print("\n## 每页重复的模板行（解析时剔除）")
    for ln in result["template_lines"][:20]:
        print(f"- {redact(ln)[:100]}")
    print("\n## 判定")
    for ln in verdict(result):
        print(f"- {ln}")
    print("\n## 样例（已对长数字串打码）")
    for kind, items in result["samples"].items():
        for s in items[:3]:
            print(f"- [{kind}] {s}")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\nJSON → {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
