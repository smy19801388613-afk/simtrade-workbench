import unittest

from market import PRODUCTS, import_inspection_required, product_pool, product_rotation


class ProductSelectionTests(unittest.TestCase):
    def test_verified_batch_wraps_to_the_first_completed_product(self):
        verified = ("17001", "03001", "13001", "15002", "19001", "21003", "25002")
        self.assertEqual(product_pool("verified"), verified)
        self.assertEqual(product_rotation("verified", "25002", 12),
                         ["25002", *verified, *verified[:4]])

    def test_all_products_keeps_the_original_order_for_explicit_skus(self):
        self.assertEqual(set(product_pool("all")), set(PRODUCTS))
        self.assertEqual(product_rotation("all", "22003", 3), ["22003", "30001", "08001"])

    def test_invalid_scope_and_out_of_group_start_are_rejected(self):
        for scope, sku in [("unknown", "17001"), ("verified", "22003"), ("all", "missing")]:
            with self.subTest(scope=scope, sku=sku), self.assertRaises(ValueError):
                product_rotation(scope, sku, 1)

    def test_only_the_reviewed_tax_rows_trigger_import_inspection(self):
        for sku, hs, code in [("21003", "8418212000", "A"), ("25002", "9018199000", "6A")]:
            with self.subTest(sku=sku):
                self.assertTrue(import_inspection_required(dict(id=sku, hs=hs, regulatory_code=code)))
                for other_sku, other_hs, other_code in [
                    ("22003", hs, code), (sku, "0000000000", code), (sku, hs, ""), (sku, hs, "AB")
                ]:
                    with self.subTest(other_sku=other_sku, hs=other_hs, code=other_code):
                        with self.assertRaises(ValueError):
                            import_inspection_required(dict(id=other_sku, hs=other_hs, regulatory_code=other_code))
        self.assertFalse(import_inspection_required(dict(id="17001", regulatory_code="")))


if __name__ == "__main__":
    unittest.main()
