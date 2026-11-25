from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from account_memory import AccountMemory, _school_key
from live_platform import DEFAULT_SCHOOL_URL


SCHOOL = "https://school.example/SimTrade/"
OTHER = "https://other.example/SimTrade/"


class AccountScopeTests(unittest.TestCase):
    def test_scope_follows_login_origin_and_campus_aliases(self):
        self.assertEqual(_school_key(SCHOOL), _school_key("HTTPS://SCHOOL.example:443/SimTrade"))
        self.assertEqual(_school_key(DEFAULT_SCHOOL_URL), _school_key(DEFAULT_SCHOOL_URL.replace("simtrade/", "SIMtrade")))
        for different in (OTHER, SCHOOL.replace("https:", "http:"), SCHOOL.replace(".example", ".example:8443"),
                          SCHOOL.replace("SimTrade", "different"), SCHOOL.replace("SimTrade", "simtrade")):
            self.assertNotEqual(_school_key(SCHOOL), _school_key(different))
        for invalid in ("http://user@other.example/a", "bad data", SCHOOL+"?x", SCHOOL+"#", "https://x:0"):
            with self.assertRaises(ValueError):
                _school_key(invalid)

    def test_unavailable_has_no_plaintext_fallback(self):
        with TemporaryDirectory() as tmp:
            memory = AccountMemory(tmp)
            memory.available = False
            self.assertEqual(memory.accounts(SCHOOL), [])
            with self.assertRaises(ValueError):
                memory.save(SCHOOL, {"first": "demo"})
            self.assertEqual(list(Path(tmp).iterdir()), [])


class WindowsAccountMemoryTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.memory = AccountMemory(self.root)
        if not self.memory.available:
            self.skipTest("Windows current-user DPAPI is required")

    def test_reopen_filtered_credentials_order_and_explicit_forget(self):
        credentials = {"student-ten": "演示の密码-234!", "student-first": "", "student-last": "fictitious-pw"}
        self.memory.save(SCHOOL, credentials)
        reopened = AccountMemory(self.root)
        self.assertEqual(reopened.accounts(SCHOOL), list(credentials))
        self.assertEqual(reopened.credentials(SCHOOL, ["absent", "student-last", "student-first"]),
                         {"student-last": "fictitious-pw", "student-first": ""})
        blob = next(self.root.rglob("*.bin")).read_bytes()
        for value in ("student-ten", "student-first", "fictitious-pw"):
            self.assertNotIn(value.encode(), blob)
            self.assertNotIn(value.encode("utf-16-le"), blob)
        reopened.save(OTHER, {"another": "theirs"})
        reopened.forget(SCHOOL)
        reopened.forget(SCHOOL)
        self.assertEqual(self.memory.accounts(SCHOOL), [])
        self.assertEqual(self.memory.credentials(OTHER, ["another"]), {"another": "theirs"})

    def test_swapped_or_corrupt_ciphertext_cannot_provide_credentials(self):
        self.memory.save(SCHOOL, {"alpha": "fictitious-secret"})
        file = next(self.root.rglob("*.bin"))
        ciphertext = file.read_bytes()
        self.memory.save(OTHER, {"beta": "test"})
        other = next(p for p in self.root.rglob("*.bin") if p != file)
        for invalid in (ciphertext, b"bad data"):
            other.write_bytes(invalid)
            with self.assertRaisesRegex(ValueError, "清除本机记忆"):
                self.memory.accounts(OTHER)
        self.memory.forget(OTHER)
        self.assertEqual(self.memory.accounts(OTHER), [])
        self.assertEqual(self.memory.accounts(SCHOOL), ["alpha"])

    def test_replacement_error_preserves_old_memory_and_no_temporary_files(self):
        self.memory.save(SCHOOL, {"old": "one"})
        with patch("account_memory.os.replace", side_effect=OSError("disk problem")):
            with self.assertRaisesRegex(ValueError, "无法写入"):
                self.memory.save(SCHOOL, {"new": "two"})
        self.assertEqual(self.memory.accounts(SCHOOL), ["old"])
        self.assertEqual(len(list(self.memory.directory.iterdir())), 1)

    def test_ten_accounts_and_concurrent_replacements(self):
        group = {f"sample-{i}": f"sample-value-{i}" for i in range(10)}
        self.memory.save(SCHOOL, group)
        self.assertEqual(self.memory.credentials(SCHOOL, group), group)
        with self.assertRaises(ValueError):
            self.memory.save(SCHOOL, {**group, "eleventh": ""})
        groups = [group, {"alternate": ""}]
        def replace(index):
            local = AccountMemory(self.root)
            local.save(SCHOOL, groups[index % 2])
            return local.accounts(SCHOOL)
        with ThreadPoolExecutor(max_workers=5) as pool:
            for result in pool.map(replace, range(16)):
                self.assertIn(result, [list(g) for g in groups])
