import unittest
from collections import Counter
from itertools import permutations

from planning import participant_plan, rotation


def completed(seller, buyer, factory=None):
    case = {"seller_account": seller, "buyer_account": buyer}
    if factory:
        case["domestic"] = {"factory_account": factory}
    return {"case": case}


class PartnerRotationTests(unittest.TestCase):
    def test_ten_accounts_visit_every_buyer_before_repeating(self):
        ids = list("ABCDEFGHIJ")
        plan = participant_plan(ids, 100, [])
        self.assertEqual({(seller, buyer) for seller, buyer, _ in plan[:90]}, set(permutations(ids, 2)))
        for start in range(0, 100, 10):
            group = plan[start:start + 10]
            self.assertTrue(all(len(set(order)) == 3 for order in group))
            for assignments in zip(*group):
                self.assertCountEqual(assignments, ids)

    def test_small_groups_and_explicit_priority(self):
        self.assertEqual(participant_plan(["A"], 7, []), [("A", "A", "A")] * 7)
        self.assertEqual(participant_plan(["A","B"], 4, []), rotation(["A","B"]) * 2)
        ids = list("ABC")
        self.assertEqual(participant_plan(ids, 3, []), rotation(ids))
        for role, index in [("importer",1), ("exporter",0)]:
            plan = participant_plan(ids, 7, [], focus="C", focus_role=role)
            self.assertTrue(all(plan[i][index] == "C" for i in (0,3,6)))
            self.assertEqual(len({row[:2] for row in plan[:6]}), 6)

    def test_previous_foreign_sales_choose_different_buyers(self):
        ids = list("ABC")
        past = [completed(*row) for row in rotation(ids)]
        past.append(dict(completed("A","C"), stage="quoted"))
        plan = participant_plan(ids, 3, past)
        self.assertEqual({row[:2] for row in plan}, {("A","C"),("B","A"),("C","B")})

    def test_even_an_irregular_history_can_supply_a_new_buyer_for_every_seller(self):
        ids = list("ABCDE")
        remaining = set(zip(ids, "CDEBA"))
        history = [completed(s,b) for s,b in permutations(ids,2) if (s,b) not in remaining]
        self.assertEqual({r[:2] for r in participant_plan(ids, len(ids), history)}, remaining)

    def test_short_batches_consider_all_three_role_levels(self):
        ids = list("ABCD")
        roles = ("exporter", "importer", "factory")
        snapshots = [{"account": a, "role": role, "level": 1 if a == lowest else 10}
                     for role, lowest in zip(roles, "DAB") for a in ids]
        self.assertEqual(participant_plan(ids, 1, [], snapshots=snapshots), [("D","A","B")])
        history, orders = [], []
        for _ in range(12):
            row = participant_plan(ids, 1, history, snapshots=snapshots)[0]
            history.append(completed(*row))
            orders.append(row)
        self.assertEqual({r[:2] for r in orders}, set(permutations(ids,2)))
        for column in zip(*orders):
            self.assertEqual(Counter(column), dict.fromkeys(ids, 3))

    def test_standalone_procurement_is_counted_only_for_supplier_relationship(self):
        ids = list("ABCD")
        self.assertEqual(participant_plan(ids, 1, [])[0], ("A","B","D"))
        procurement = {"kind": "purchase", "case": {"seller_account": "A", "domestic": {"factory_account":"D"}}}
        self.assertEqual(participant_plan(ids, 1, [procurement]*8)[0], ("A","B","C"))

    def test_school_limits(self):
        for ids, count in [([],1), (["A","A"],1), (list("ABCDEFGHIJK"),1), (["A"],0), (["A"],101)]:
            with self.subTest(ids=ids, count=count), self.assertRaises(ValueError):
                participant_plan(ids, count, [])
