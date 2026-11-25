import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import unittest
from unittest.mock import patch

from web_session import DesktopSession


class School:
    def __init__(self, base, credentials):
        self.base_url = base.rstrip("/") + "/"
        self._accounts = credentials


class RememberedLoginTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data = Path(directory.name)
        self.session = DesktopSession(self.data, School)
        if not self.session.account_memory.available:
            self.skipTest("Windows account protection required")
        self.base = self.session.default_base

    def connect(self, session, records, *, success=True, **options):
        with patch.object(session, "fetch_roles", return_value=[],
                          side_effect=None if success else ValueError("登录失败")):
            session.dispatch("login", {"accounts": records, **options})
            deadline = monotonic() + 5
            while session.state()["task"]["status"] == "running" and monotonic() < deadline:
                sleep(0.01)
        self.assertEqual(session.state()["task"]["status"], "done" if success else "error")

    def test_reopen_edit_blank_and_forget(self):
        group = [{"id": "second", "password": "local-private-value"}, {"id": "first", "password": ""}]
        self.connect(self.session, group, remember=True)
        self.session.dispatch("logout", {})
        reopened = DesktopSession(self.data, School)
        state = reopened.state()
        self.assertEqual(state["remembered_accounts"], ["second", "first"])
        self.assertNotIn("local-private-value", json.dumps(state))
        self.assertEqual(reopened.dispatch("remembered-accounts", {"base": self.base})["accounts"],
                         ["second", "first"])
        self.connect(reopened, [{"id": "first", "use_saved": True},
                                {"id": "second", "password": ""}], remember=True)
        self.assertEqual(list(reopened.platform._accounts.items()), [("first", ""), ("second", "")])
        self.assertEqual(reopened.account_memory.credentials(self.base, ["second"]), {"second": ""})
        reopened.dispatch("forget-accounts", {"base": self.base})
        self.assertEqual(reopened.accounts, ["first", "second"])
        self.assertEqual(DesktopSession(self.data, School).state()["remembered_accounts"], [])

    def test_failed_one_off_and_old_clients_preserve_prior_login(self):
        self.session.account_memory.save(self.base, {"saved": "previous-private-value"})
        self.connect(self.session, [{"id": "saved", "password": "incorrect"}], remember=True, success=False)
        self.assertIsNone(self.session.platform)
        self.connect(self.session, [{"id": "once", "password": "one-off"}])
        self.session.dispatch("logout", {})
        self.assertEqual(self.session.account_memory.credentials(self.base, ["saved", "once"]),
                         {"saved": "previous-private-value"})
        with self.assertRaises(ValueError):
            self.session.dispatch("login", {"base": "https://example.invalid/school/",
                                           "accounts": [{"id": "saved", "use_saved": True}]})
        self.assertIsNone(self.session.platform)

    def test_saved_password_only_used_when_explicitly_requested(self):
        self.session.account_memory.save(self.base, {"a": "previous-private-value"})
        self.connect(self.session, [{"id": "a", "use_saved": True}])
        self.assertEqual(self.session.platform._accounts["a"], "previous-private-value")
        self.session.dispatch("logout", {})
        self.connect(self.session, [{"id": "a"}])
        self.assertEqual(self.session.platform._accounts["a"], "")
