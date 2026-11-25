from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, call, patch

from workbench import main, terminal_login


class TerminalAccountMemoryTests(unittest.TestCase):
    base = "https://campus.example/SIMtrade/"
    ids = ["second", "first"]
    passwords = {"second": "", "first": "changed"}

    def setUp(self):
        self.store = Mock(available=True)
        self.store.accounts.return_value = self.ids
        self.store.credentials.return_value = self.passwords
        self.memory = self.enter_patch("workbench.AccountMemory", return_value=self.store)
        self.platform = self.enter_patch("workbench.LivePlatform")
        self.prompt = self.enter_patch("workbench.getpass", side_effect=["", "changed"])

    def enter_patch(self, name, **kwargs):
        p = patch(name, **kwargs)
        self.addCleanup(p.stop)
        return p.start()

    def test_enter_uses_saved_passwords_in_order_and_uses_custom_data(self):
        with TemporaryDirectory() as directory, patch("workbench.console") as console, \
                patch("builtins.input", side_effect=[self.base, ""]), \
                patch("sys.argv", ["workbench", "--data", directory]):
            main()
            self.memory.assert_called_once_with(Path(directory).resolve())
            self.store.credentials.assert_called_once_with(self.base, self.ids)
            self.prompt.assert_not_called()
            self.platform.assert_called_once_with(self.base, self.passwords)
            console.assert_called_once_with(self.platform.return_value, self.ids, Path(directory).resolve())

    def test_typed_list_can_replace_empty_or_nonempty_password_after_validation(self):
        with patch("builtins.input", side_effect=["second first", "记住"]):
            terminal_login(self.base, Path("."))
        self.store.credentials.assert_not_called()
        self.assertEqual(self.platform.return_value.session.call_args_list,
                         [call(account, "exporter") for account in self.ids])
        self.store.save.assert_called_once_with(self.base, self.passwords)

    def test_failed_school_login_keeps_old_credentials(self):
        self.platform.return_value.session.side_effect = [object(), ValueError("登录失败")]
        with patch("builtins.input", side_effect=["second first", "记住"]), self.assertRaises(ValueError):
            terminal_login(self.base, Path("."))
        self.store.save.assert_not_called()

    def test_clear_then_manual_login_without_saving(self):
        with patch("builtins.input", side_effect=["忘记", "second first", ""]):
            terminal_login(self.base, Path("."))
        self.store.forget.assert_called_once_with(self.base)
        self.store.credentials.assert_not_called()
        self.store.save.assert_not_called()
