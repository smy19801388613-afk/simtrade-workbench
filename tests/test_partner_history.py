import unittest

from trade_modes import partner_history


def report(account, role, number="WX1", other="", status="已经完成", kind="foreign"):
    return {"account": account, "role": role, "contracts": [
        {"number": number, "counterparty": other, "status": status, "kind": kind}]}


def archive(seller="S", buyer="B", factory="F", stage="completed"):
    return {"stage": stage, "case": {"contract": "WX1", "seller_account": seller,
            "buyer_account": buyer, "domestic": {"factory_account": factory}}}


class PartnerHistoryTests(unittest.TestCase):
    def test_school_only_contracts_keep_direction_without_payment_or_terms(self):
        reports = [report("S", "exporter", other="B (Company Ltd.)"),
                   report("S", "importer", "WX2", other="B（公司）")]
        cases = [j["case"] for j in partner_history([], reports)]
        self.assertEqual(cases, [{"contract": "WX1", "seller_account": "S", "buyer_account": "B"},
                                 {"contract": "WX2", "seller_account": "B", "buyer_account": "S"}])

    def test_two_role_views_and_duplicate_archives_count_once_with_known_factory(self):
        reports = [report("S", "exporter", other="B (Example)"), report("B", "importer", other="S")]
        job = archive()
        history = partner_history([job,job], reports)
        self.assertEqual(history, [{"case": job["case"]}])
        self.assertEqual(job["case"]["domestic"], {"factory_account": "F"})

    def test_excludes_unfinished_incomplete_and_conflicting_contracts(self):
        jobs = [archive(stage="quoted"), dict(archive(), kind="purchase")]
        reports = [report("S", "exporter", status="正在进行", other="B"),
                   report("S", "exporter", other="F", status="正在进行", kind="domestic"),
                   report("F", "factory", other="S"), report("S", "exporter", other="公司名称")]
        self.assertEqual(partner_history(jobs, reports), [])
        self.assertEqual(partner_history([archive()], [report("B", "importer", other="X")]), [])

    def test_matching_school_views_can_supply_a_missing_counterparty(self):
        history = partner_history([], [report("S","exporter"), report("B","importer")])
        self.assertEqual(history, [{"case": {"contract":"WX1","seller_account":"S","buyer_account":"B"}}])

    def test_one_foreign_case_carries_the_product_and_linked_completed_purchase(self):
        job = archive()
        job["case"]["product"] = {"id":"P1", "name_cn":"商品一"}
        job["case"]["domestic"]["contract"] = "WD1"
        reports = [report("S","exporter","WD1",other="F",kind="domestic"),
                   report("F","factory","WD1",other="S",kind="domestic")]
        self.assertEqual(partner_history([job],reports), [
            {"case":{"contract":"WX1","seller_account":"S","buyer_account":"B",
                     "product":{"id":"P1"},"domestic":{"factory_account":"F"}}}])

    def test_purchased_items_are_available_before_export_completion(self):
        job = archive(stage="supplied")
        job["case"]["domestic"]["contract"] = "WD2"
        job["case"]["product"] = {"id":"P2"}
        reports = [report("F","factory","WD2",other="S",kind="domestic")]
        self.assertEqual(partner_history([job],reports), [
            {"kind":"purchase","case":{"contract":"WD2","seller_account":"S",
                                      "domestic":{"factory_account":"F"},"product":{"id":"P2"}}}])

    def test_standalone_purchases_dedup_and_do_not_count_as_foreign_trade(self):
        job = {"kind":"purchase","stage":"completed","case":{"contract":"WD3"},
               "supply":{"roles":{"exporter":"S","factory":"F"}, "order":{"product_id":"P3"}}}
        reports = [report("S","exporter","WD3",other="F",kind="domestic"),
                   report("F","factory","WD3",other="S",kind="domestic")]
        school_only = partner_history([],reports)
        self.assertEqual(school_only, [
            {"kind":"purchase","case":{"contract":"WD3","seller_account":"S","domestic":{"factory_account":"F"}}}])
        self.assertEqual(partner_history([job],reports)[0]["case"]["product"], {"id":"P3"})
        self.assertEqual(len(partner_history([job,job],reports)), 1)

    def test_foreign_and_unrelated_domestic_can_have_the_same_number(self):
        reports = [report("S","exporter","SAME",other="B"), report("F","factory","SAME",other="S",kind="domestic")]
        results = partner_history([],reports)
        self.assertEqual([row.get("kind","foreign") for row in results], ["foreign","purchase"])
        self.assertTrue(all("product" not in row["case"] for row in results))
