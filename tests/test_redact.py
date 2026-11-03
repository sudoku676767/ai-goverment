"""Tests for log redaction, including Chinese personal information.

All numbers below are made up; the ID numbers only satisfy the check-digit rule.
"""

from __future__ import annotations

import unittest

from blackbox.redact import is_cn_id, is_luhn, redact_text, redact_value


class ChinesePiiTests(unittest.TestCase):
    def test_id_number_next_to_chinese_text(self) -> None:
        out = redact_text("学生张小明（身份证号330106200501011231）")
        self.assertNotIn("330106200501011231", out)
        self.assertIn("[REDACTED:cn-id:", out)
        self.assertIn("身份证号", out)

    def test_id_number_with_x_check_digit(self) -> None:
        self.assertTrue(is_cn_id("11010519491231002X"))
        self.assertIn("[REDACTED:cn-id:", redact_text("证件 11010519491231002x 已核验"))

    def test_bad_check_digit_is_left_alone(self) -> None:
        text = "订单号 330106200501011234"
        self.assertFalse(is_cn_id("330106200501011234"))
        self.assertEqual(redact_text(text), text)

    def test_mobile_numbers(self) -> None:
        for raw in ("13800138000", "138-0013-8000", "138 0013 8000", "+86 13800138000"):
            out = redact_text(f"手机{raw}，请回电")
            self.assertIn("[REDACTED:cn-mobile:", out, raw)
            self.assertNotIn("0013", out, raw)

    def test_landline(self) -> None:
        out = redact_text("电话0571-88881234转办公室")
        self.assertIn("[REDACTED:cn-landline:", out)
        self.assertNotIn("88881234", out)

    def test_bank_card_requires_luhn(self) -> None:
        self.assertTrue(is_luhn("6222020200112345679"))
        self.assertIn("[REDACTED:bank-card:", redact_text("卡号6222020200112345679"))
        self.assertEqual(redact_text("流水号6222020200112345670"), "流水号6222020200112345670")

    def test_plain_document_numbers_untouched(self) -> None:
        text = "示院办〔2026〕18号，2026年11月20日前完成，共128篇。"
        self.assertEqual(redact_text(text), text)

    def test_same_value_gives_same_label(self) -> None:
        a = redact_text("13800138000")
        b = redact_text("联系13800138000")
        self.assertIn(a, b)

    def test_nested_request_body(self) -> None:
        body = {"messages": [{"role": "user", "content": "王老师 13800138000"}]}
        out = redact_value(body)
        self.assertNotIn("13800138000", out["messages"][0]["content"])

    def test_existing_api_key_rule_still_works(self) -> None:
        self.assertIn("[REDACTED:api-key:", redact_text("key sk-abcdefghijklmnopqrstuv"))


if __name__ == "__main__":
    unittest.main()
