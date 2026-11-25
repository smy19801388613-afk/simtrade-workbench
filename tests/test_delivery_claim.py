"""School insurance notices, interrupted exchange and proof of payment."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from xml.etree import ElementTree as ET

from settlement import (
    _claim_credit, _claim_notice, _delivery_result, _Journal,
    _restore_delivery_notice, _settle_claim, SettlementReview,
)


SINKING = "本批货物运输船只在海上意外触礁，连同货物一起沉没，无法办理相关进口手续，请速向保险公司申请索赔。"
CONTAMINATION = "本批货物在运输过程中混进了杂质，货物被沾污受损，请速向保险公司办理索赔手续。"
FIRE = "本批货物运输船只在运输过程中发生火灾，货物遭到毁损，请速向保险公司办理索赔手续。"


class DeliveryClaimTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "settlement.json"
        self.case = dict(contract="C", seller_account="S", buyer_account="B", terms="CIF", payment="T/T",
                         buyer={"currency":"EUR"}, documents={"M01":"BL"})
        self.buyer = SimpleNamespace(account="B", role="importer")
        self.payout = dict(contract="C", item="货物保险金", expense="", income="EUR 459.80")

    def journal(self):
        return _Journal(self.path, self.case)

    def pending(self, key):
        journal = self.journal()
        journal.data["steps"][key] = {"status": "pending"}
        journal.save()

    def test_school_notice_variants_and_unrelated_text(self):
        for issue in ("在运输过程中遭遇船员罢工暴动，造成货物毁损", "在运输过程中受潮，造成货物受损",
                      "运输船只若干天前在海上突然行踪不明，至今失去音讯已逾法定时间，可确认为失踪",
                      "在装卸过程中由于意外情况，全部落海", "运输船只在海上发生爆炸，船货俱损，事故原因不明",
                      "在运输过程中被全部偷走，不能抵达目的港"):
            text = "本批货物" + issue + "，请向保险公司办理索赔手续。"
            self.assertEqual(_claim_notice("提示信息 " + text), text)
        self.assertEqual(_claim_notice(SINKING), SINKING)
        for text in ("提示信息 成功换取提货单", "保险公司 保险索赔", "本批货物尚未到港，不能申请索赔。",
                     "货物发生意外以后凭保险单向保险公司办理索赔。",
                     "本批货物部分受损，其余可正常提货，请向保险公司办理索赔手续。",
                     "本批货物如有损失，请向保险公司申请索赔。", SINKING + "其余可正常提货。"):
            self.assertIsNone(_claim_notice(text))

    def test_unknown_or_partial_receipt_persists_for_review_across_retries(self):
        for text, additional in [
            ("本批货物部分受损，其余可正常提货，请向保险公司办理索赔手续。", []),
            ("本批货物如有损失，请向保险公司申请索赔。", ["成功换取提货单！"]),
            ("本批货物发生未经核实的货损，请向保险公司办理索赔手续。", []),
            (SINKING, ["成功换取提货单！"]), ("未成功换取提货单！", []),
        ]:
            with self.subTest(text=text):
                self.path.unlink(missing_ok=True)
                page = SimpleNamespace(text=text, messages=additional, scripts=[])
                with patch("foreign_trade.open_action", return_value=page) as exchange, \
                        patch("settlement._ledger", return_value=[self.payout]) as ledger:
                    for _ in range(2):
                        with self.assertRaisesRegex(SettlementReview, "可提货数量"):
                            _delivery_result(self.buyer, self.case, self.journal())
                    self.assertIn(text, self.journal().data["facts"]["delivery_review"])
                    exchange.assert_called_once()
                    ledger.assert_not_called()

    def test_confirmed_normal_receipt_with_matching_copies(self):
        page = SimpleNamespace(text="提示信息 成功换取提货单！", messages=["成功换取提货单！"], scripts=[])
        with patch("foreign_trade.open_action", return_value=page):
            self.assertEqual(_delivery_result(self.buyer, self.case, self.journal())["result"], "delivery_order")

    def test_contamination_receipt_fresh_and_pending(self):
        page = SimpleNamespace(text="提示信息 " + CONTAMINATION, messages=[CONTAMINATION], scripts=[])
        self.case["documents"]["M26"] = "POL"
        with patch("foreign_trade.open_action", return_value=page) as exchange, patch("foreign_trade._list_page"), \
                patch("foreign_trade._notes", return_value={"M26": ("", "", "POL")}):
            result = _delivery_result(self.buyer, self.case, self.journal())
            self.assertEqual(result, {"result": "claim_notice", "notice": CONTAMINATION})
            exchange.assert_called_once()
        self.pending("delivery_order")
        journal = self.journal()
        journal.data["facts"]["delivery_review"] = " ".join([CONTAMINATION] * 2)
        journal.data["steps"]["payment"] = {"status": "verified", "receipt": "成功付款"}
        journal.save()
        with patch("foreign_trade.open_action") as exchange, patch("foreign_trade._list_page") as listing, \
                patch("foreign_trade._notes", return_value={"M26": ("", "", "POL")}):
            for _ in range(2):
                self.assertEqual(_delivery_result(self.buyer, self.case, self.journal()), result)
            listing.assert_called_once_with(self.buyer, "C", "I2")
            exchange.assert_not_called()
        self.assertTrue(self.journal().done("payment"))
        self.assertNotIn("delivery_review", self.journal().data["facts"])

    def test_contamination_review_preserved_for_conflicting_documents(self):
        self.pending("delivery_order")
        journal = self.journal()
        journal.data["facts"]["delivery_review"] = CONTAMINATION
        journal.save()
        self.case["documents"]["M26"] = "POL"
        for docs in ({}, {"M26": ("", "", "OTHER")}, {"M26": ("", "", "POL"), "M41": object()}):
            with patch("foreign_trade.open_action") as op, patch("foreign_trade._list_page"), \
                    patch("foreign_trade._notes", return_value=docs):
                with self.assertRaisesRegex(SettlementReview, "单据尚未核平"):
                    _delivery_result(self.buyer, self.case, self.journal())
                op.assert_not_called()
                self.assertFalse(self.journal().done("delivery_order"))
                self.assertEqual(self.journal().data["facts"]["delivery_review"], CONTAMINATION)

    def test_fire_receipt_checks_documents_and_resumes_original_exchange(self):
        page = SimpleNamespace(text="提示信息 " + FIRE, messages=[FIRE], scripts=[])
        self.case["documents"]["M26"] = "POL"
        policy = {"M26": ("", "", "POL")}
        with patch("foreign_trade.open_action", return_value=page) as exchange, patch("foreign_trade._list_page") as listing, \
                patch("foreign_trade._notes", side_effect=[dict(policy, M41=object()), policy]):
            with self.assertRaisesRegex(SettlementReview, "单据尚未核平"):
                _delivery_result(self.buyer, self.case, self.journal())
            self.assertEqual(self.journal().data["facts"]["delivery_review"], FIRE)
            for _ in range(2):
                self.assertEqual(_delivery_result(self.buyer, self.case, self.journal())["notice"], FIRE)
            self.assertEqual(listing.call_count, 2)
            exchange.assert_called_once()
        for text in (FIRE.replace("货物遭到毁损", "部分货物遭到毁损"), FIRE + " " + CONTAMINATION,
                     FIRE.replace("发生火灾", "如发生火灾")):
            self.assertIsNone(_claim_notice(text))

    def test_new_exchange_reads_script_alert_and_saves_notice(self):
        page = SimpleNamespace(text="提示信息", messages=[], scripts=["alert('" + SINKING + "')"])
        with patch("foreign_trade.open_action", return_value=page) as exchange:
            for _ in range(2):
                self.assertEqual(_delivery_result(self.buyer, self.case, self.journal()),
                                 {"result": "claim_notice", "notice": SINKING})
        exchange.assert_called_once_with(self.buyer, "C", "I3", "换提货单")

    def test_older_release_flagged_the_same_verified_claim(self):
        self.case["documents"]["M26"] = "POL"
        for notice in (FIRE, CONTAMINATION):
            with self.subTest(notice=notice):
                journal = self.journal()
                receipt = {"result": "claim_notice", "notice": notice}
                step = {"status": "verified", "verified_at": "2026-11-25T16:21:38+08:00", "receipt": receipt}
                journal.data["steps"]["delivery_order"] = step
                journal.data["facts"]["delivery_review"] = " ".join([notice, notice])
                journal.save()
                policy = {"M26": ("", "", "POL")}
                with patch("foreign_trade.open_action") as exchange, patch("foreign_trade._list_page"), \
                        patch("foreign_trade._notes", side_effect=[dict(policy, M41=object()), policy]):
                    with self.assertRaisesRegex(SettlementReview, "单据尚未核平"):
                        _delivery_result(self.buyer, self.case, self.journal())
                    self.assertIn("delivery_review", self.journal().data["facts"])
                    self.assertEqual(_delivery_result(self.buyer, self.case, self.journal()), receipt)
                    self.assertEqual(_delivery_result(self.buyer, self.case, self.journal()), receipt)
                    exchange.assert_not_called()
                self.assertNotIn("delivery_review", self.journal().data["facts"])
                self.assertEqual(self.journal().data["steps"]["delivery_order"], step)

    def test_conflicting_verified_exchange_keeps_review_flag(self):
        for receipt in ({"result": "claim_notice", "notice": CONTAMINATION},
                        {"result": "delivery_order", "notice": FIRE}, {"result": "claim_notice"}):
            journal = self.journal()
            journal.data["steps"]["delivery_order"] = {"status": "verified", "receipt": receipt}
            journal.data["facts"]["delivery_review"] = FIRE
            journal.save()
            with patch("foreign_trade.open_action") as exchange, patch("foreign_trade._list_page") as listing:
                with self.assertRaisesRegex(SettlementReview, "核对货损"):
                    _delivery_result(self.buyer, self.case, self.journal())
                listing.assert_not_called()
                exchange.assert_not_called()
            self.assertEqual(self.journal().data["facts"]["delivery_review"], FIRE)

    def test_v130_pending_exchange_adopts_exact_previous_school_notice(self):
        self.pending("delivery_order")
        attention = dict(stage="shipped", message="平台未返回预期回执：提示信息 " + SINKING + " " + SINKING)
        _restore_delivery_notice(self.journal(), dict(attention, stage="contracted"))
        self.assertFalse(self.journal().done("delivery_order"))
        _restore_delivery_notice(self.journal(), attention)
        with patch("foreign_trade.open_action") as exchange, patch("settlement._ledger") as ledger:
            self.assertEqual(_delivery_result(self.buyer, self.case, self.journal())["notice"], SINKING)
            ledger.assert_not_called()
            exchange.assert_not_called()

    def test_pending_exchange_reconciles_issued_delivery_order_without_requesting_another(self):
        self.pending("delivery_order")
        order = ET.fromstring("<M41><M4101>O</M4101><M4106>C</M4106><M4114>BL</M4114></M41>")
        with patch("settlement._ledger", return_value=[]), patch("foreign_trade._list_page"), \
                patch("foreign_trade._notes", return_value={"M41":object()}), patch("foreign_trade.note"), \
                patch("settlement.read_document", return_value=[order]), patch("foreign_trade.open_action") as action:
            self.assertEqual(_delivery_result(self.buyer, self.case, self.journal())["result"], "delivery_order")
            action.assert_not_called()

    def test_pending_exchange_without_evidence_remains_pending(self):
        self.pending("delivery_order")
        with patch("settlement._ledger", return_value=[]), patch("foreign_trade._list_page"), \
                patch("foreign_trade._notes", return_value={}), patch("foreign_trade.open_action") as action:
            with self.assertRaisesRegex(SettlementReview, "换单回执"):
                _delivery_result(self.buyer, self.case, self.journal())
            action.assert_not_called()
        self.assertFalse(self.journal().done("delivery_order"))

    def test_claim_requires_payment_then_rechecks_without_claiming_again(self):
        page = SimpleNamespace(text="成功办理索赔！", messages=[], scripts=[])
        with patch("settlement._ledger", side_effect=[[], [], [self.payout]]), \
                patch("foreign_trade.open_action", return_value=page) as action:
            with self.assertRaisesRegex(SettlementReview, "尚未查到"):
                _settle_claim(self.buyer, self.case, self.journal())
            _settle_claim(self.buyer, self.case, self.journal())
            self.assertEqual(self.journal().data["facts"]["claim_credit"], self.payout)
            action.assert_called_once_with(self.buyer, "C", "I8", "保险索赔")

    def test_existing_payout_reconciles_new_and_interrupted_claims(self):
        with patch("settlement._ledger", return_value=[self.payout]), patch("foreign_trade.open_action") as action:
            _settle_claim(self.buyer, self.case, self.journal())
            self.pending("claim")
            _settle_claim(self.buyer, self.case, self.journal())
            self.assertTrue(self.journal().done("claim"))
            action.assert_not_called()

    def test_never_replays_an_uncertain_claim(self):
        self.pending("claim")
        with patch("settlement._ledger", return_value=[]), patch("foreign_trade.open_action") as action:
            with self.assertRaises(SettlementReview):
                _settle_claim(self.buyer, self.case, self.journal())
            action.assert_not_called()

    def test_before_claim_can_pause_and_resume_without_resubmitting_exchange(self):
        journal = self.journal()
        exchange = {"result": "claim_notice", "notice": SINKING}
        journal.run("delivery_order", lambda: exchange)

        def pause(case, current):
            self.assertEqual(case["contract"], "C")
            self.assertEqual(current.data["steps"]["delivery_order"]["receipt"], exchange)
            self.assertNotIn("claim", current.data["steps"])
            raise SettlementReview("等待 Agent 代办")

        page = SimpleNamespace(text="成功办理索赔！", messages=[], scripts=[])
        with patch("settlement._ledger", side_effect=[[], [], [], [self.payout], [self.payout]]), \
                patch("foreign_trade.open_action", return_value=page) as action:
            for _ in range(2):
                with self.assertRaisesRegex(SettlementReview, "等待 Agent 代办"):
                    _settle_claim(self.buyer, self.case, self.journal(), before_claim=pause)
                self.assertNotIn("claim", self.journal().data["steps"])
            action.assert_not_called()
            _settle_claim(self.buyer, self.case, self.journal())
            _settle_claim(self.buyer, self.case, self.journal(), before_claim=pause)
            self.assertEqual(self.journal().data["facts"]["claim_credit"], self.payout)
            action.assert_called_once_with(self.buyer, "C", "I8", "保险索赔")

    def test_before_claim_skips_existing_credit(self):
        with patch("settlement._ledger", return_value=[self.payout]), patch("foreign_trade.open_action") as action:
            _settle_claim(self.buyer, self.case, self.journal(), before_claim=self.fail)
            self.assertEqual(self.journal().data["facts"]["claim_credit"], self.payout)
            action.assert_not_called()

    def test_payout_requires_unique_contract_currency_and_positive_income(self):
        self.assertIsNone(_claim_credit([dict(self.payout, contract="OTHER")], self.case))
        for rows in ([self.payout, self.payout], [dict(self.payout, income="EUR 0")],
                     [dict(self.payout, income="USD 12")], [dict(self.payout, expense="EUR 10")]):
            with self.assertRaises(SettlementReview):
                _claim_credit(rows, self.case)
