"""Host-side SIMtrade recovery contract. Does not open school sessions."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
from typing import Callable, Protocol

SKILL = Path(__file__).parent / "skills" / "simtrade-operator"
ROLES = {"exporter", "importer", "factory", "export_bank", "import_bank"}
ACCOUNT_ALIASES = tuple("ABCDEFGHIJ")
REASONS = {"payment_result_unknown", "document_mismatch", "unknown_stage"}
FACTS = {"receipt_pending", "payment_seen", "bank_reply_missing", "inspection_required",
         "document_check_failed", "contract_confirmed", "draft_open", "draft_corrected",
         "reviewed_patch_ready"}
# IDs denote host capabilities, never URLs or scripts supplied by a model.
EFFECTS = {"read_payment_ledger": "read", "read_contract": "read",
           "read_bank_notice": "read", "read_document_check": "read",
           "save_reviewed_draft": "write"}


@dataclass(frozen=True)
class Snapshot:
    actor: str
    role: str
    contract: str
    terms: str
    payment: str
    reason: str
    facts: tuple[str, ...]
    candidates: tuple[str, ...]
    revision: int  # local host version, incremented when relevant evidence changes

    def packet(self):
        if (self.actor not in ACCOUNT_ALIASES or self.role not in ROLES
                or not re.fullmatch(r"JOB-[1-9][0-9]{0,4}", self.contract)
                or self.terms not in ("FOB", "CFR", "CIF")
                or self.payment not in ("T/T", "D/P", "D/A", "L/C")
                or self.reason not in REASONS or not set(self.facts) <= FACTS
                or not set(self.candidates) <= EFFECTS.keys()):
            raise ValueError("本次状态未能映射到匿名协议字段，已留在本机。")
        packet = asdict(self)
        del packet["revision"]
        packet["candidates"] = [{"id": c, "effect": EFFECTS[c]} for c in self.candidates
                                if self.allows(c)]
        return packet

    def allows(self, action):
        if action not in self.candidates:
            return False
        if EFFECTS[action] == "write":
            return (self.reason == "document_mismatch" and {"draft_open", "reviewed_patch_ready"} <= set(self.facts)
                    and "receipt_pending" not in self.facts)
        return True


class Host(Protocol):
    def rules_ready(self) -> bool: ...
    def observe(self) -> Snapshot: ...
    def perform_and_verify(self, snapshot: Snapshot, action: str) -> str:
        """Under role/contract lock, recheck state, budget & authorization; persist intent
        before a change. Return 'verified' only on independent receipt/readback."""
        ...


def messages_for(snapshot):
    packet = snapshot.packet()
    ref = ("ledger-and-recovery.md" if snapshot.reason == "payment_result_unknown"
           else "platform-flow.md")
    instructions = "\n\n".join(p.read_text(encoding="utf-8") for p in (
        SKILL / "SKILL.md", SKILL / "references/handoff.md", SKILL / "references" / ref))
    return [{"role": "system", "content": instructions},
            {"role": "user", "content": json.dumps(packet, ensure_ascii=False)}]


def parse_decision(raw, snapshot):
    if not isinstance(raw, str) or len(raw) > 3000:
        raise ValueError("模型回复超出接管协议。")
    obj = json.loads(raw)
    if not isinstance(obj, dict) or obj.keys() != {"decision", "action", "why"}:
        raise ValueError("模型回复字段不符合协议。")
    decision, action, why = obj["decision"], obj["action"], obj["why"]
    if not isinstance(why, str) or not 1 <= len(why.strip()) <= 300 or not why.isprintable():
        raise ValueError("模型理由不可用。")
    if decision == "choose" and isinstance(action, str) and snapshot.allows(action):
        return decision, action, why
    if decision in ("wait", "stop") and action is None:
        return decision, None, why
    raise ValueError("回复所选动作与本轮授权或业务状态不符。")


def recover(host: Host, ask: Callable[[list], str] | None, max_calls: int = 3):
    """Return control to the rule engine as soon as it knows what to do."""
    if not 1 <= max_calls <= 3:
        raise ValueError("每次接管允许 1 至 3 次推理。")
    history = []
    for _ in range(max_calls):
        if host.rules_ready():
            return "resume_rules", history
        if ask is None:
            return "model_off", history
        state = host.observe()
        try:
            messages = messages_for(state)
            if not state.packet()["candidates"]:
                return "no_supported_action", history
            decision, action, why = parse_decision(ask(messages), state)
        except Exception:
            return "model_unavailable_or_invalid", history
        if host.observe() != state:
            return "state_changed", history
        history.append((decision, action, why))
        if decision != "choose":
            return decision, history
        try:
            receipt = host.perform_and_verify(state, action)
        except Exception:
            return "reconcile_first", history
        if receipt != "verified":
            return "reconcile_first", history
        if host.rules_ready():
            return "resume_rules", history
        if host.observe() == state:
            return "no_progress", history
    return "call_limit", history
