"""qa_flow 的行为锁定测试。

重点锁住余额链的三个方法陷阱——它们都真实导致过**完全错误**的结论：
1. 相邻两笔相减（依赖顺序）
2. 按账户/卡号分组（会把一条链切成假断裂）
3. 把「自洽率 = 100%」当健康值
"""

from __future__ import annotations

import random

ROW_DEFAULTS = {
    "日期": "2025-07-03",
    "交易时间": "2025-07-03 12:00:00",
    "收支大类": "餐饮美食",
    "交易对手": "某商户",
    "原始摘要": "消费",
    "渠道": "招商银行",
    "来源文件": "bank.pdf",
    "账户尾号": "0609",
}


def row(amount, direction, balance, **over):
    r = {**ROW_DEFAULTS, "金额": amount, "收支方向": direction, "账户余额": balance}
    r.update(over)
    return r


def chain_rows():
    """一条真实形状的链：1000 --支出100--> 900 --收入50--> 950 --支出25--> 925"""
    return [
        row(100, "支出", 900),
        row(50, "收入", 950),
        row(25, "支出", 925),
    ]


def test_balance_chain_is_order_independent(qa_flow):
    rows = chain_rows()
    random.Random(0).shuffle(rows)
    stat = qa_flow.check_balance_chain(rows, {})["bank.pdf"]
    # 只有账期首笔找不到前序（1000 不在余额集合里），其余两笔都应在链上
    assert stat["unmatched"] == 1
    assert stat["chain_closed"] is True
    assert stat["breaks"] == 0
    # 自洽率因此天然是 2/3，不是 100% —— 判据不能用自洽率
    assert stat["rate"] == 0.6667


def test_balance_chain_detects_real_break(qa_flow):
    """拿掉中间一笔后，链真的断了：未匹配从 1 变成 2。"""
    rows = [r for r in chain_rows() if r["账户余额"] != 950]
    stat = qa_flow.check_balance_chain(rows, {})["bank.pdf"]
    assert stat["unmatched"] == 2
    assert stat["breaks"] == 1
    assert stat["chain_closed"] is False
    assert stat["samples"]  # 应当给出可回查的样例行


def test_account_split_creates_false_breaks(qa_flow):
    """两个卡号其实是同一条余额链时，按账户分组会凭空造出断裂。"""
    rows = [
        row(100, "支出", 900, 账户尾号="0609"),
        row(50, "收入", 950, 账户尾号="0626"),
        row(25, "支出", 925, 账户尾号="0609"),
        row(500, "支出", 425, 账户尾号="0626"),
    ]
    stat = qa_flow.check_balance_chain(rows, {})["bank.pdf"]
    assert stat["unmatched"] == 1            # 按来源：链闭合
    assert stat["chain_closed"] is True
    assert stat["unmatched_if_split_by_account"] == 4  # 按账户：4 处假断裂
    assert stat["account_column_suspect"] is True


def test_balance_chain_groups_by_source(qa_flow):
    """不同来源各自独立成链，不能互相借用余额。"""
    rows = [
        row(100, "支出", 900, 来源文件="a.pdf"),
        row(25, "支出", 925, 来源文件="b.pdf"),
    ]
    out = qa_flow.check_balance_chain(rows, {})
    assert set(out) == {"a.pdf", "b.pdf"}
    assert all(s["unmatched"] == 1 for s in out.values())


def test_time_precision_flags_date_only_rows(qa_flow):
    rows = [
        row(10, "支出", 100, 交易时间="2025-07-03 00:00:00"),
        row(10, "支出", 90, 交易时间="2025-07-03 18:30:00"),
    ]
    stat = qa_flow.check_time_precision(rows, {})
    assert stat["date_only"] == 1
    assert stat["date_only_ratio"] == 0.5


def test_cross_channel_dupes_need_time_and_name(qa_flow):
    plat = row(
        128.0, "支出", 872, 渠道="微信支付", 交易对手="某某餐厅",
        交易时间="2025-07-03 12:10:00", 来源文件="wx.xlsx",
    )
    bank_ok = row(
        128.0, "支出", 900, 渠道="招商银行", 交易对手="某某餐厅有限公司",
        交易时间="2025-07-03 12:11:00", 来源文件="bank.pdf",
    )
    bank_far = row(
        128.0, "支出", 900, 渠道="招商银行", 交易对手="某某餐厅有限公司",
        交易时间="2025-07-03 20:30:00", 来源文件="bank.pdf",
    )
    bank_other = row(
        128.0, "支出", 900, 渠道="招商银行", 交易对手="另一家店",
        交易时间="2025-07-03 12:11:00", 来源文件="bank.pdf",
    )

    assert len(qa_flow.check_cross_channel_dupes([plat, bank_ok], {})) == 1
    # 时间差 > 120 分钟 → 不算重复
    assert qa_flow.check_cross_channel_dupes([plat, bank_far], {}) == []
    # 对手名不一致 → 不算重复（同额同日但确实是两笔）
    assert qa_flow.check_cross_channel_dupes([plat, bank_other], {}) == []


def test_refund_offsets_are_detected(qa_flow):
    rows = [
        row(500, "支出", 500, 原始摘要="微信支付-已全额退款"),
        row(736, "支出", -236, 原始摘要="消费-已退款(¥736.00)"),
        row(20, "支出", -256, 原始摘要="普通消费"),
    ]
    stat = qa_flow.check_refund_offsets(rows, {})
    assert stat["full_refund_rows"] == 1
    assert stat["full_refund_amount"] == 500
    assert stat["partial_refund_rows"] == 1
    assert stat["partial_refund_amount"] == 736


def test_render_markdown_matches_check_output(qa_flow):
    """渲染层必须与检查层的字段一致（曾因字段改名漏改渲染而崩）。"""
    rows = chain_rows()
    result = {
        "total_rows": len(rows),
        "balance": qa_flow.check_balance_chain(rows, {}),
        "time_precision": qa_flow.check_time_precision(rows, {}),
        "cross_channel_dupes": qa_flow.check_cross_channel_dupes(rows, {}),
        "refunds": qa_flow.check_refund_offsets(rows, {}),
        "parse_noise": qa_flow.check_parse_noise(rows, {}),
    }
    md = qa_flow.render_markdown(result)
    assert "余额链自洽性" in md
    assert "链闭合" in md
