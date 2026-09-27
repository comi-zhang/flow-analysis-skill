#!/usr/bin/env python3
"""账单 PII 掩码：账单人姓名、他人姓名、卡号、手机号、订单号。

只依赖标准库。核心设计：**确定性编码**而不是 `****` 掩码——
同一张卡/同一个号恒定同码，保住"是不是同一个"这个事实（配对规则要用），
但真值不可还原。

用法
----
```bash
# 掩码（默认 encode）
python mask_pii.py --input-dir raw/ --output-dir masked/ \
  --owner-names '真实姓名,常用别名'

# 只审计，不改文件（用于检查产物是否已干净）
python mask_pii.py --input-dir masked/ --audit-only

# 换档位：mask=保留首尾 / drop=删除 / off=不动
python mask_pii.py --input-dir raw/ --output-dir out/ --mode mask
```

密钥：环境变量 `FLOW_ANALYZER_PII_KEY`。不设置则用固定开发密钥（结果可复现，
但不抗暴力破解，仅适合本地/测试）。**生产环境必须设置。**

输入格式：每行一个 JSON 对象的 NDJSON。字段名通过 `--field-map` 覆盖。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import sys
import unicodedata
from pathlib import Path

DEV_KEY = b"flow-analysis-local-dev-key"
OWNER_ALIAS = "张三"
KIND_PREFIX = {"card": "C", "phone": "M", "id": "I", "order": "T", "student": "S", "unknown": "N"}

# 数字类形态。边界用 (?<!\d)/(?!\d) 而不是 \b——中文属于 \w，
# 「卡号6214…」这种紧贴中文的写法用 \b 匹配不到。
RE_ID = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
RE_CARD = re.compile(r"(?<!\d)\d{16,19}(?!\d)")
RE_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
RE_LONG = re.compile(r"(?<!\d)\d{11,}(?!\d)")


def _norm(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value or ""))).lower()


class Masker:
    def __init__(self, owner_names=(), mode="encode", key=None, alias=OWNER_ALIAS):
        self.owners = tuple(sorted((n for n in owner_names if n), key=len, reverse=True))
        self.mode = mode
        self.alias = alias
        self.key = key or os.environ.get("FLOW_ANALYZER_PII_KEY", "").encode() or DEV_KEY
        self._cache: dict[str, str] = {}

    def code(self, value: str, kind: str = "unknown") -> str:
        norm = _norm(value)
        if not norm:
            return ""
        hit = self._cache.get(f"{kind}:{norm}")
        if hit:
            return hit
        digest = hmac.new(self.key, norm.encode(), hashlib.sha256).digest()
        b32 = base64.b32encode(digest).decode().rstrip("=")[:8]
        token = f"{KIND_PREFIX.get(kind, 'N')}-{b32[:4]}-{b32[4:]}"
        self._cache[f"{kind}:{norm}"] = token
        return token

    def _keep_ends(self, digits: str, head: int, tail: int) -> str:
        if len(digits) < head + tail:
            return "*" * len(digits)
        return f"{digits[:head]}{'*' * (len(digits) - head - tail)}{digits[-tail:]}"

    def number(self, value: str, kind: str) -> str:
        raw = str(value or "")
        digits = re.sub(r"\D", "", raw)
        if not digits or self.mode == "off":
            return raw
        if self.mode == "drop":
            return ""
        if self.mode == "encode":
            return self.code(digits, kind)
        return self._keep_ends(digits, 3 if kind == "phone" else 4, 4)

    def text(self, value: str, default_kind: str = "unknown") -> str:
        """自由文本：账单人姓名 → 代称；数字类按档位处理。"""
        if not value or self.mode == "off":
            return value
        s = str(value)
        for name in self.owners:
            if name in s:
                s = s.replace(name, self.alias)
        s = RE_ID.sub(lambda m: self.number(m.group(), "id"), s)
        s = RE_CARD.sub(lambda m: self.number(m.group(), "card"), s)
        s = RE_PHONE.sub(lambda m: self.number(m.group(), "phone"), s)
        return RE_LONG.sub(lambda m: self.number(m.group(), default_kind), s)

    def person(self, value: str) -> str:
        s = str(value or "").strip()
        if not s:
            return s
        for name in self.owners:
            if name in s:
                return s.replace(name, self.alias)
        return self.code(s, "person")


# 默认字段名 → 处理方式。用 --field-map 覆盖。
DEFAULT_MAP = {
    "text": ["交易对手", "归一收款方", "原始摘要", "商品说明", "商户实体", "商户品牌",
             "账户尾号", "命中关键词", "备注", "来源文件"],
    "person": ["收款人", "付款人", "姓名", "户名"],
    "order": ["交易单号", "订单号", "交易号"],
    "skip": ["金额", "余额", "账户余额", "日期", "年月", "自然年", "小时", "时段"],
}

SCAN_PATTERNS = {
    "phone": RE_PHONE,
    "card": RE_CARD,
    "id": RE_ID,
    "long": RE_LONG,
}


def load_field_map(path: str | None) -> dict:
    if not path:
        return DEFAULT_MAP
    return json.loads(Path(path).read_text(encoding="utf-8"))


def mask_row(row: dict, masker: Masker, fmap: dict) -> dict:
    out = {}
    text_fields, person_fields = set(fmap.get("text", [])), set(fmap.get("person", []))
    order_fields, skip = set(fmap.get("order", [])), set(fmap.get("skip", []))
    for key, value in row.items():
        if not isinstance(value, str) or not value or key in skip:
            out[key] = value
        elif key in person_fields:
            out[key] = masker.person(value)
        elif key in order_fields and re.fullmatch(r"[\d\s\-]+", value):
            out[key] = masker.number(value, "order")
        else:
            # 未知字段也处理：宽表随时新增列，白名单必漏
            out[key] = masker.text(value)
    return out


def iter_files(directory: str, pattern: str) -> list[Path]:
    return sorted(Path(directory).glob(pattern))


def audit(paths: list[Path], kinds: list[str]) -> int:
    hits = 0
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                row = json.loads(line)
                for field, value in row.items():
                    if not isinstance(value, str):
                        continue
                    for kind in kinds:
                        if SCAN_PATTERNS[kind].search(value):
                            hits += 1
                            print(f"  {path.name}:{lineno} [{kind}] {field}: {value[:60]}")
                            break
    if hits:
        print(f"\n❌ 发现 {hits} 处未处理的关键信息", file=sys.stderr)
        return 1
    print("✅ 未发现目标类型的关键信息")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="账单 PII 掩码")
    ap.add_argument("--input-dir", required=True)
    ap.add_argument("--glob", default="*.ndjson")
    ap.add_argument("--output-dir")
    ap.add_argument("--owner-names", default="", help="账单人姓名，逗号分隔")
    ap.add_argument("--owner-alias", default=OWNER_ALIAS)
    ap.add_argument("--mode", default="encode", choices=["encode", "mask", "drop", "off"])
    ap.add_argument("--field-map", help="字段映射 JSON 文件")
    ap.add_argument("--audit-only", action="store_true")
    args = ap.parse_args()

    paths = iter_files(args.input_dir, args.glob)
    if not paths:
        print(f"未找到文件：{args.input_dir}/{args.glob}", file=sys.stderr)
        return 2

    if args.audit_only:
        return audit(paths, ["phone", "card", "id", "long"])

    if not args.output_dir:
        print("需要 --output-dir（或使用 --audit-only）", file=sys.stderr)
        return 2

    owners = tuple(x.strip() for x in args.owner_names.split(",") if x.strip())
    masker = Masker(owners, args.mode, alias=args.owner_alias)
    fmap = load_field_map(args.field_map)
    if owners:
        print(f"账单人代称：{args.owner_alias}（匹配 {len(owners)} 种写法）")
    else:
        print("⚠ 未指定 --owner-names，账单人真名不会被替换")
    if masker.key == DEV_KEY:
        print("⚠ 未设置 FLOW_ANALYZER_PII_KEY，使用固定开发密钥")

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    total = changed = 0
    for path in paths:
        dest = out_dir / path.name
        with open(path, encoding="utf-8") as fin, open(dest, "w", encoding="utf-8") as fout:
            for line in fin:
                if not line.strip():
                    continue
                row = json.loads(line)
                total += 1
                before = json.dumps(row, ensure_ascii=False, sort_keys=True)
                masked = mask_row(row, masker, fmap)
                after = json.dumps(masked, ensure_ascii=False, sort_keys=True)
                if before != after:
                    changed += 1
                fout.write(after + "\n")
        print(f"  {path.name} → {dest.name}")
    print(f"\n共 {total} 行，{changed} 行被改写。")

    print("\n复核输出目录：")
    return audit(iter_files(args.output_dir, args.glob), ["phone", "card", "id"])


if __name__ == "__main__":
    raise SystemExit(main())
