"""Import inspection on normal delivery, including an interrupted submission."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from import_declaration import inspection
from settlement import _inspect_import, _Journal


def sample():
    return dict(contract="DEMO", seller_account="A", buyer_account="B", terms="CIF", payment="T/T",
                date="2026-11-10", operation_date="2026-11-12", currency="USD", amount_usd="50000",
                claim_days_after_actual_arrival=30, destination_cn="洛杉矶", loading_port_cn="上海港", mark="DEMO",
                product=dict(id="25002", hs="9018199000", regulatory_code="6A", name_cn="便携式脑电图仪",
                             name_en="PORTABLE ELECTROENCEPHALOGRAPH", cartons=1, quantity=10, unit="SET", packing_unit="CARTON"),
                ship=dict(date="2026-11-10",actual_arrival="2026-11-11",warehouse_entry="2026-11-12",
                          vessel="Test", voyage="V2", container="EXAMPLE"),
                documents=dict(M01="STBLN100", M41="STBLO100", M19="STINV100", M21="STPLT100", M17B="STIPC100"),
                buyer=dict(company_en="Buyer", credit_code="A123", person_en="Jones", phone="555", currency="USD"),
                seller=dict(company_cn="出口企业", company_en="Seller"))


class InspectionTests(unittest.TestCase):
    def test_partial_carton_uses_issued_arrival_and_contract_deadline(self):
        draft = inspection(sample())
        v, item = draft["values"], draft["items"][0]
        self.assertEqual((v["M1724"], v["M1790"], v["M1793"]), ("2026-11-11","2026-11-12","2026-12-11"))
        self.assertEqual((v["M1792"], v["M1789"], v["M1730"]), ("洛杉矶","STBLN100","EXAMPLE"))
        self.assertEqual((item["M1807"],item["M1808"],item["M1811"],item["M1812"]), ("10","SET","1","CARTON"))
        self.assertEqual(draft["child_url"],"CM18.aspx?a1=STIPC100&a2=")

    def test_application_requires_actual_arrival_documents(self):
        c = sample()
        del c["documents"]["M41"]
        with self.assertRaises(ValueError):
            inspection(c)
        c = sample()
        c["ship"]["warehouse_entry"] = "2026-11-13"
        with self.assertRaises(ValueError):
            inspection(c)

    def test_paid_inspection_is_reconciled_without_resubmission(self):
        case = sample()
        budget = {"importer": {"M5010":"25.80"}}
        school = SimpleNamespace(text="检查结果 100%", messages=[], scripts=[])
        buyer = SimpleNamespace(role="importer")
        ledger = [dict(item="检验费",contract="DEMO",income="",expense="USD 25.80")]
        with TemporaryDirectory() as tmp, patch("foreign_trade.add_note", return_value="STIPC100") as add, \
                patch("foreign_trade.note", return_value=school), patch("settlement.save_document") as save, \
                patch("settlement._guard", side_effect=[ValueError("余额不足"),None]), \
                patch("settlement._ledger", return_value=ledger), \
                patch("foreign_trade.prepare_doc_submission", return_value=Mock(side_effect=ConnectionError)) as prepare:
            path = Path(tmp)/"checkpoint.json"
            journal = _Journal(path,case)
            journal.data["facts"]["inspection_date"] = "2026-11-12"
            with self.assertRaises(ValueError):
                _inspect_import(buyer,case,budget,journal)
            self.assertNotIn("import_inspection",journal.data["steps"])
            with self.assertRaises(ConnectionError):
                _inspect_import(buyer,case,budget,journal)
            prepare.assert_called_once_with(buyer, "DEMO", "I4", ["M07","M19","M21","M41","M17B"])
            for _ in range(2):
                _inspect_import(buyer,case,budget,_Journal(path,case))
            self.assertTrue(_Journal(path,case).done("import_inspection"))
            self.assertEqual((add.call_count,save.call_count,prepare.return_value.call_count),(1,1,1))
