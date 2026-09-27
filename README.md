# flow-analysis

一个 Codex skill：**个人流水分析**（银行/支付宝/微信对账单 → 能对账的收支口径）。

难点不在算总数，而在三件事：**同一笔被记两次**、**资金搬运被当成消费**、**来源数据本身有错**。
这个 skill 把这三件事的检查与修正固化成流程和脚本。

## 它解决什么

| 场景 | 用到的能力 |
| --- | --- |
| "我这个月到底花了多少" | 真实支出 vs 真实消费两个口径（差额本身就是结论） |
| "为什么账单里有我不认识的交易" | 解析错位检测 + 余额链校验（能定位到具体来源） |
| "金额对不对" | 余额链自洽性按来源分别报告 |
| "这条算消费吗" | 资金搬运识别（理财申赎、账户互转、人情往来） |
| "我要在飞书里看大盘" | 多维表格看板（`group_by` 两维度做「年 × 大类」） |
| 敏感信息泄露风险 | 读取阶段掩码（确定性编码，不是 `****`） |

## 安装

```bash
git clone https://github.com/comi-zhang/flow-analysis-skill.git
cp -R flow-analysis-skill/flow-analysis ~/.codex/skills/
```

之后在 Codex 里说"帮我分析这份流水"即可自动命中，或显式 `$flow-analysis` 调用。

## 结构

```
flow-analysis/
├── SKILL.md                     # 工作流与关键判据
├── agents/openai.yaml           # UI 元数据
├── references/
│   ├── caliber.md               # 收支口径定义与四类虚高修正
│   └── feishu-base.md           # 飞书多维表格操作要点与坑
└── scripts/
    ├── mask_pii.py              # PII 掩码（确定性编码）+ 审计
    ├── qa_flow.py               # 质检：余额链/时间精度/跨渠道重复/退款未对冲/解析错位
    └── caliber_report.py        # 口径计算 + 大类×年汇总
```

## 三个脚本（都只依赖标准库）

```bash
# 1. 读取即掩码（在落库/落盘之前）
python flow-analysis/scripts/mask_pii.py \
  --input-dir raw/ --output-dir masked/ --owner-names '真实姓名'

# 2. 质检
python flow-analysis/scripts/qa_flow.py --input-dir masked/ --report qa.md

# 3. 口径计算
python flow-analysis/scripts/caliber_report.py --input-dir masked/ --out-dir reports/
```

输入统一为 NDJSON（每行一条记录），字段名可用 `--field-map` 覆盖。

## 这个 skill 里最值钱的几条经验

1. **余额链是最划算的单项检查**——它同时验证"金额对不对"和"有没有解析错位"。
   但要注意：只有日期没有时间的来源，日内顺序不可知，结论只能算"顺序自洽"而非"余额正确"。
   （这条是踩过坑才写进去的：曾因按文件顺序遍历，把一家银行误判为 5% 自洽，实际是 94.8%。）
2. **`****` 掩码会毁掉分析能力**。掩码丢失"是不是同一张卡"这个事实，而配对规则要用它。
   改用确定性编码：同一输入恒定同码，真值不可还原。
3. **PDF 只有日期时会被补成 `00:00`**，于是全部落进"凌晨"桶。
   做时段分析前必须先查时间精度分布。
4. **跨渠道去重需要四条判据**（金额+同日+时间相近+对手名），少一条就会误合并或漏合并。
5. **别只报一个数**。"真实支出 118 万 / 真实消费 26 万"——差额才是用户最需要知道的。

详细内容见 `flow-analysis/SKILL.md` 与 `references/`。

## License

MIT
