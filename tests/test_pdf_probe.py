"""pdf_probe 的纯函数测试（不需要真实 PDF）。"""

from __future__ import annotations


def test_compact_date_rejects_card_fragment(pdf_probe):
    # 折行的卡号尾段看着像 8 位日期，但月=63 非法（示例为虚构值）
    assert pdf_probe.is_compact_date("07639999") is False
    assert pdf_probe.is_compact_date("20251302") is False  # 月 13
    assert pdf_probe.is_compact_date("20250230") is False  # 2 月没有 30 日


def test_compact_date_accepts_real_dates(pdf_probe):
    assert pdf_probe.is_compact_date("20250802") is True
    assert pdf_probe.is_compact_date("20251231") is True


def test_compact_date_year_window(pdf_probe):
    assert pdf_probe.is_compact_date("19800101") is False
    assert pdf_probe.is_compact_date("20240101") is True
    assert pdf_probe.is_compact_date("20240229") is True   # 闰年
    assert pdf_probe.is_compact_date("20250229") is False  # 平年


def test_redact_masks_long_digit_runs_but_is_stable(pdf_probe):
    text = "卡号 6222000011112222 手机 13800000000 金额 -7.80 余额 2109.55"
    out = pdf_probe.redact(text)
    assert "6222000011112222" not in out
    assert "13800000000" not in out
    assert "-7.80" in out and "2109.55" in out  # 金额保留，探针才有意义
    assert out.count("C-") == 2  # 卡号一段（连续数字算一段）+ 手机号
    assert pdf_probe.redact(text) == out  # 同一输入恒定同码
