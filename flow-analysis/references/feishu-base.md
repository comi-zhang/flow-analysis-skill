# 飞书多维表格操作要点

分析结果落到飞书多维表格时，下面这些坑都会真实遇到。

## 导入导出

### 导出记录分页：不要用 page_token 循环

```
✗ 错：while has_more: 用 page_token 取下一页
✓ 对：用 --offset 递增，直到某页返回行数 < limit
```

原因：接口可能返回 `has_more=true` 但**不返回 `page_token`**，循环会死转。
在定时任务里这等于静默卡死，且不会报错。

```bash
for off in 0 2000 4000 6000 8000 10000 12000 14000 16000; do
  lark-cli base +record-list --base-token <token> --table-id <table> \
    --offset $off --limit 2000 --format ndjson \
    --output ./tx_page_$off.ndjson --as user
done
```

注意：`--limit` 在 `--format json` 下上限是 **200**，只有 `ndjson` 允许 2000。

### 幂等导入的锚点

用来源系统自己的稳定主键（飞书 `record_id`）做去重锚点。

**不要用「渠道交易单号」**：退款会沿用原支付的单号，会把两行折叠成一行
（实测把 16,021 行折成 15,383 行，丢了 638 行）。

## 写记录

```bash
lark-cli base +record-batch-create --base-token <t> --table-id <t> \
  --json '{"fields":["字段A","字段B"],"rows":[["值1",123],["值2",null]]}'
```

- 单批最多 **200** 行，超出要分批
- `fields` 与每行 `rows` 的列顺序必须一一对应
- **空单元格显式传 `null`**，不要传 `""`
- **数值列不要写字符串**：日期字符串写进 number 列会报
  `800010407 The cell value does not match the expected input shape`
- **公式字段（formula/lookup）不能写**，会报 `READONLY`；它们随依赖字段自动重算

### 建表后再写字段：注意生效时序

表下的更新走异步链路。**同一次调用里刚 `+field-create` 完就 `+record-batch-update`
可能报 `not_found`**——字段还没生效。稳妥做法是分两步执行，或失败后重试一次。

## 看板（Dashboard）

### 图表组件能力

| 意图 | type |
| --- | --- |
| 比较类别 | `column` / `bar` |
| 看趋势 | `line` / `area` |
| 看占比 | `pie` / `ring` |
| 单值 | `statistics` |
| 说明文字 | `text`（支持 Markdown，仅标题/加粗/列表） |

### `group_by` 最多两个维度

这是做「年 × 大类」这类交叉视图的关键：

```json
{
  "table_name": "消费分析·大类×年",
  "series": [{"field_name": "金额", "rollup": "SUM"}],
  "group_by": [
    {"field_name": "年度", "mode": "integrated", "sort": {"type": "group", "order": "asc"}},
    {"field_name": "收支大类", "mode": "integrated", "sort": {"type": "value", "order": "desc"}}
  ]
}
```

两个维度 → 堆叠/分组柱状；一个维度 → 普通柱状/折线/饼图。

### 其他要点

- `data_config` 用**表名和字段名**，不是 id
- `sort` 只要出现就必须带 `order`（`asc`/`desc`），否则校验失败
- 组件**必须串行创建**，不能并发
- 创建完用 `+dashboard-arrange` 做一次智能排版；飞书**不开放**指定 x/y/w/h，
  所以文字块可能占位过大，需要用户手动拖
- 读图表计算结果用 `+dashboard-block-get-data`（不含名称/类型/布局）

## 权限

- 默认 `--as user`；`--as bot` 看不到用户的个人资源
- user 报 `missing_scopes` → 按 `lark-shared` 走授权恢复（`auth login --domain base`）
- **`--domain base` 给的是「多维表格应用/空间」权限，不含表/记录读取**。
  要读写表记录得显式申请：

```bash
lark-cli auth login --scope "base:table:read base:field:read base:record:read base:view:read base:dashboard:read"
```

- 写记录还要 `base:record:create` / `base:record:update`，建表要 `base:table:create`

## 高价值用法：把人工裁决做成数据

多维表格最适合承载**人工判断**（点一下就能改）。把机器不确定的记录导出成一张表，
加上单选项（如「本人划转/理财申赎/家庭供给/其他」）让用户勾选，
再用这些标注反推规则——比让用户在聊天里逐条描述高效得多。

导出前给每行带上**来源文件**列，用户"找不到这笔"时能直接回到源文件核对。
