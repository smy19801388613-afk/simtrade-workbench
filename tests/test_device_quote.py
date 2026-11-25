import unittest
from types import SimpleNamespace
from unittest.mock import patch

from planning import new_job


class DeviceQuoteTests(unittest.TestCase):
    def setUp(self):
        product = dict(id="25002", name_en="PORTABLE ELECTROENCEPHALOGRAPH", name_cn="便携式脑电图仪",
                       spec_en="16 bit", spec_cn="16位", hs="9018199000", regulatory_code="6A", unit="SET",
                       cost_rmb="10000", per_carton="30", gross_per_carton="63", net_per_carton="54",
                       cbm_per_carton="0.111", tax=dict(duty_pct="0", VAT_pct="13", refund_pct="13",
                       consumption_pct="0", consumption_per_unit="0", export_tax_pct="0"))
        self.quote = dict(product=product, currency="USD", usd_fx="7", local_fx="7", market="8500",
                          freight_m="50", freight_w="50", port="FIRST", port_cn="甲港", country="U.S.A.",
                          country_cn="美国", export_bank={}, import_bank={})
        self.cash = {"factory": "100000", "importer": "1000000", "exporter": "1000000"}
        fields = ('txtB0306', 'txtB0307', 'txtB0308', 'txtB0309', 'txtB0305', 'txtB0345',
                  'txtB0303', 'txtB0312', 'txtB0311', 'txtB0330', 'txtB0327')
        self.profiles = {role: dict.fromkeys(fields, "Example") for role in self.cash}
        self.platform = SimpleNamespace(
            base_url="http://school.test/", profile=lambda _, role: self.profiles[role],
            snapshot=lambda _, role: dict(cash=self.cash[role], currency="USD" if role == "importer" else "RMB"))

    def job(self, port="FIRST"):
        with patch("planning.lookup", return_value=self.quote):
            return new_job(self.platform, ["A", "B"], ["A", "B", "B"], self.quote["product"]["id"], 3, 0, port)

    def test_ten_devices_use_one_shipping_carton(self):
        result = self.job()
        self.assertEqual(result['budget']['packing'], dict(quantity=10, cartons=1, gross_kg="63", net_kg="54", volume_cbm="0.111"))
        self.assertEqual(result['case']['product']['quantity'], 10)
        self.assertEqual(result['budget']['importer']['M5010'], '28.57')
        self.cash['factory'] = '9999'
        with self.assertRaisesRegex(ValueError, '不足以生产一台'):
            self.job()

    def test_full_carton_at_other_port_precedes_partial_carton(self):
        self.cash.update(factory="400000", exporter="560000")
        self.quote["freight_m"] = "150000"
        first = self.job()
        self.assertLess(first["case"]["product"]["quantity"], 30)
        with patch("planning.alternate_ports", return_value=[dict(port="SECOND", port_cn="乙港", freight_m="50")]) as ports:
            selected = self.job(port=None)
        ports.assert_called_once()
        self.assertEqual((selected['case']['destination'], selected['case']['product']['quantity']), ('SECOND', 30))

    def test_partial_cartons_are_limited_to_supported_devices(self):
        self.quote['product'].update(id='17001', hs='6302319900', regulatory_code='')
        with self.assertRaisesRegex(ValueError, '不足以生产一箱'):
            self.job()

    def test_contacts_needed_on_inspection_form_are_checked_while_quoting(self):
        for role, field, message in [('exporter','txtB0306','中文公司全称'), ('importer','txtB0312','电话')]:
            with self.subTest(role=role), patch.dict(self.profiles[role], {field: ' '}), self.assertRaisesRegex(ValueError, message):
                self.job()
