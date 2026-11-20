"""Run a synthetic interrupted transaction. No school credentials or platform access."""
import argparse
from dataclasses import replace
from getpass import getpass
import json

from handoff import Snapshot, recover


class ExampleHost:
    def __init__(self):
        self.state = Snapshot("B", "importer", "JOB-1", "FOB", "D/P",
                              "payment_result_unknown", ("receipt_pending",),
                              ("read_payment_ledger", "read_contract"), 1)
        # A receipt exists in the example ledger before recovery begins.
        self.ledger = [{"contract": "JOB-1", "actor": "B", "denom": "USD",
                        "amount": "3200.00", "posted": True, "payee": "A", "direction": "out"}]

    def rules_ready(self):
        return "payment_seen" in self.state.facts

    def observe(self):
        return self.state

    def perform_and_verify(self, snapshot, action):
        if self.state != snapshot or not self.state.allows(action):
            return "unconfirmed"
        if action == "read_contract":
            print("示例合同显示已签约，合同页没有付款凭证。")
            self.state = replace(snapshot, facts=("receipt_pending", "contract_confirmed"),
                                 candidates=("read_payment_ledger",), revision=snapshot.revision + 1)
            return "verified"
        if action == "read_payment_ledger":
            matched = [r for r in self.ledger if r == {
                "contract": snapshot.contract, "actor": snapshot.actor, "denom": "USD",
                "amount": "3200.00", "posted": True, "payee": "A", "direction": "out"}]
            if len(matched) != 1:
                return "unconfirmed"
            print("示例流水核实：B 已按 JOB-1 向 A 支付 USD 3,200.00。")
            self.state = replace(snapshot, facts=("payment_seen",), candidates=(),
                                 revision=snapshot.revision + 1)
            return "verified"
        return "unconfirmed"


class DraftExampleHost:
    """Only exposes one local correction grounded in the example packing receipt."""
    def __init__(self):
        self.state = Snapshot("A", "exporter", "JOB-2", "CIF", "T/T", "document_mismatch",
                              ("draft_open", "document_check_failed"), ("read_document_check",), 1)
        self.gross_in_draft = 102
        self.cartons, self.gross_per_carton = 12, 10

    def observe(self):
        return self.state

    def rules_ready(self):
        return "draft_corrected" in self.state.facts

    def perform_and_verify(self, snapshot, action):
        if self.state != snapshot or not self.state.allows(action):
            return "unconfirmed"
        expected = self.cartons * self.gross_per_carton
        if action == "read_document_check" and self.gross_in_draft != expected:
            print("示例原始装箱记录：12 箱 × 每箱毛重 10 kg；草稿录为 102 kg。")
            self.state = replace(snapshot, facts=snapshot.facts + ("reviewed_patch_ready",),
                                 candidates=("save_reviewed_draft",), revision=2)
            return "verified"
        if action == "save_reviewed_draft":
            self.gross_in_draft = expected
            if self.gross_in_draft != self.cartons * self.gross_per_carton:
                return "unconfirmed"
            print("示例草稿保存后复核：原始装箱记录与草稿总毛重均为 120 kg。")
            self.state = replace(snapshot, facts=("draft_corrected",), candidates=(), revision=3)
            return "verified"
        return "unconfirmed"


def scripted_answer(messages):
    packet = json.loads(messages[-1]["content"])
    key = packet["candidates"][0]["id"]
    reasons = {"read_payment_ledger": "按合同核对已入账的付款流水。",
               "read_document_check": "先读取单据核验和原始装箱记录。",
               "save_reviewed_draft": "保存本机已依据实际装箱记录核对的毛重修订。"}
    return json.dumps({"decision": "choose", "action": key, "why": reasons[key]}, ensure_ascii=False)


def main():
    parser = argparse.ArgumentParser(description="SIMtrade 模型接管演练（仅使用虚构业务数据）")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--api", action="store_true", help="输入供应商地址、模型名和可选 Key")
    mode.add_argument("--off", action="store_true", help="关闭模型，显示需要人工处理的状态")
    parser.add_argument("--case", choices=("payment", "packing"), default="payment",
                        help="演练付款断线或装箱单毛重不符")
    args = parser.parse_args()
    host = ExampleHost() if args.case == "payment" else DraftExampleHost()
    print("本地虚构场景：" + ("进口商付款后断线" if args.case == "payment" else "出口装箱单毛重不符"))
    ask = None if args.off else scripted_answer
    if args.api:
        from model_provider import ask_model
        endpoint = input("兼容 Chat Completions 的完整地址：").strip()
        model = input("模型名：").strip()
        key = getpass("API Key（不回显，本地免密服务可留空）：").strip()
        ask = lambda messages: ask_model(endpoint, model, key, messages)
        print("本次仅向所填服务发送 Skill 和匿名示例状态，最多 3 次请求。")
    elif not args.off:
        print("使用预置模型回答检验交接流程。")
    status, history = recover(host, ask)
    for decision, action, why in history:
        print("模型决定", decision, action or "", why)
    print("结果", status, {
        "resume_rules": "已核对，常规流程可继续。", "model_off": "API 未启用，保留异常供本地处理。",
        "model_unavailable_or_invalid": "服务不可用或回复不符合协议，请核对地址、模型和访问权限。",
        "wait": "等待外部业务步骤。", "stop": "请人工核对证据。",
        "reconcile_first": "执行结果未确认，先重新核账。", "state_changed": "状态已变化，请重新读取。",
        "call_limit": "已达到本次接管调用上限。", "no_progress": "本步没有新证据。",
        "no_supported_action": "当前没有可映射的本地动作。"
    }.get(status, ""))


if __name__ == "__main__":
    main()
