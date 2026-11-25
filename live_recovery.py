"""Offer the model only bounded student-UI lookups on the interrupted job."""
from decimal import Decimal
from dataclasses import replace
from handoff import ACCOUNT_ALIASES, Snapshot, recover
from pipeline import read


def inspect_exception(platform, directory, ask):
    job=read(directory/"job.json"); case=job["case"]; ct=case["contract"];stage=job["stage"]
    settlement=directory/("settlement-"+ct+".json")
    unknown_pay=settlement.exists() and read(settlement)["steps"].get("payment",{}).get("status")=="pending"
    role,acct=("importer",case["buyer_account"]) if unknown_pay or stage=="shipped" else ("exporter",case["seller_account"])
    alias=ACCOUNT_ALIASES[job["accounts"].index(acct)]
    state=Snapshot(alias,role,"JOB-1",case["terms"],case["payment"],"payment_result_unknown" if unknown_pay else "unknown_stage",
                   ("receipt_pending",) if unknown_pay else (),
                   ("read_payment_ledger","read_contract") if unknown_pay else ("read_contract",),1)
    class Review:
        def __init__(self):self.state=state;self.ready=False;self.evidence=[]
        def rules_ready(self):return self.ready
        def observe(self):return self.state
        def perform_and_verify(self,snapshot,action):
            if snapshot!=self.state or not self.state.allows(action):return "unconfirmed"
            if action=="read_payment_ledger":
                from settlement import _entry, _ledger
                row=_entry(_ledger(platform.session(acct,role),ct),case["payment"]+"货款","expense")
                if not row:return "unconfirmed"
                currency,raw=row["expense"].split()
                if currency!=case["buyer"]["currency"] or abs(Decimal(raw.replace(",",""))-Decimal(job["budget"]["importer"]["M5007"]))>Decimal(".01"):
                    return "unconfirmed"
                self.evidence.append("已核实本合同付款流水与预计本金、本币和支出方向一致。")
                self.ready=True
            elif action=="read_contract":
                current=platform.snapshot(acct,role)
                hits=[c for c in current["contracts"] if c["kind"]=="foreign" and c["number"]==ct]
                if len(hits)!=1:return "unconfirmed"
                self.evidence.append("合同当前状态："+hits[0]["status"])
            else:return "unconfirmed"
            self.state=replace(self.state,revision=self.state.revision+1,
                               candidates=tuple(v for v in self.state.candidates if v!=action))
            return "verified"
    review=Review();status,history=recover(review,ask)
    return status,history,review.evidence
