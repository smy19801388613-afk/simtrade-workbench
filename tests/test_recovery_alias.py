import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from live_recovery import inspect_exception


class RecoveryAliasTests(unittest.TestCase):
    def test_tenth_importer_can_call_model_and_read_own_contract(self):
        accounts = [f"student-{i}" for i in range(10)]
        contract = "trade-11"
        case = {"contract": contract, "seller_account": accounts[0],
                "buyer_account": accounts[-1], "terms": "CIF", "payment": "T/T"}
        job = {"case": case, "stage": "shipped", "accounts": accounts}
        calls, snapshots = [], []

        def ask(messages):
            packet = json.loads(messages[-1]["content"])
            calls.append(packet)
            self.assertEqual(packet["actor"], "J")
            self.assertEqual(packet["contract"], "JOB-1")
            self.assertNotIn(accounts[-1], messages[-1]["content"])
            return json.dumps({"decision": "choose", "action": "read_contract", "why": "核对合同进度"})

        class Platform:
            def snapshot(self, account, role):
                snapshots.append((account, role))
                return {"contracts": [{"kind": "foreign", "number": contract, "status": "已装运"}]}

        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "job.json").write_text(json.dumps(job), encoding="utf-8")
            status, _, evidence = inspect_exception(Platform(), directory, ask)
        self.assertEqual(len(calls), 1)
        self.assertEqual(snapshots, [(accounts[-1], "importer")])
        self.assertEqual(evidence, ["合同当前状态：已装运"])
        self.assertEqual(status, "no_supported_action")
