"""Destination selection using the bundled directory and local school responses."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from market import PortSelectionError, _country, _port_details, batch_ports, port_catalogue


class PortSelectionTests(unittest.TestCase):
    def setUp(self):
        self.directory = port_catalogue()
        self.rows = {
            port["name"]: (port["name"], port["name_cn"], country, port["name"])
            for country, record in self.directory.items() for port in record["ports"]
        }
        self.session = SimpleNamespace(get=self.detail, home=SimpleNamespace(urls={"http://school.test/query_first.aspx"}))
        lookup = patch("market._named_rows", side_effect=lambda _, name: [self.rows[name]] if name in self.rows else [])
        lookup.start()
        self.addCleanup(lookup.stop)

    def detail(self, name):
        if name not in self.rows:
            return SimpleNamespace()
        english, chinese, country, _ = self.rows[name]
        return SimpleNamespace(fields={"txtB0501": english, "txtB0502": country, "txtB0503": chinese})

    def test_automatic_and_explicit_selection_for_every_directory_entry(self):
        for country, record in self.directory.items():
            with self.subTest(country=country):
                detail = _port_details(self.session, country)
                self.assertEqual(_country(detail["txtB0502"]), record["name_cn"])
                for port in record["ports"]:
                    self.assertEqual(_port_details(self.session, country, port["name"])["txtB0501"], port["name"])

    def test_mixed_batch_applies_manual_port_only_to_matching_importers(self):
        countries = {"A": "U.S.A.", "B": "Indonesia", "C": "Belgium"}
        platform = SimpleNamespace(
            profile=lambda buyer, _: {"txtB0337": countries[buyer]},
            session=lambda *_: self.session,
        )
        with patch("market._open_ports"):
            overrides = batch_ports(platform, list(countries) * 4, "HOUSTON")
            with self.assertRaises(PortSelectionError):
                batch_ports(platform, list(countries), "MARSEILLES")
        self.assertEqual(overrides, {"A": "HOUSTON", "B": None, "C": None})
        self.assertEqual([_port_details(self.session, countries[k], overrides[k])["txtB0501"]
                          for k in countries], ["HOUSTON", "JAKARTA", "ANTWERP"])
        self.assertEqual(_port_details(self.session, "U.S.A.")["txtB0501"], "LOS ANGELES")

    def test_malaysian_labels_share_ports_and_other_countries_stay_separate(self):
        self.assertEqual(_port_details(self.session, "Malasia", "PASIR GUDANG")["txtB0502"], "Malaysia")
        self.assertEqual(_port_details(self.session, "Malaysia", "PENANG")["txtB0502"], "Malasia")
        with self.assertRaises(PortSelectionError):
            _port_details(self.session, "Indonesia", "LOS ANGELES")
