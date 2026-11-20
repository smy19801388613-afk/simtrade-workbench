"""Routes the current executor can actually fulfil, plus the school's comparison grid."""
from collections import Counter

TERMS = ("CIF", "CFR", "FOB")
PAYMENTS = ("T/T", "D/P", "D/A", "L/C")
# End-to-end student receipts on this campus, including bank settlement, 2026-11-20.
VERIFIED = frozenset((term,payment) for term in ("CIF","CFR","FOB")
                     for payment in ("T/T","D/P","D/A","L/C"))
FIRST = (("CIF","T/T"),("CFR","D/P"),("FOB","L/C"),("CIF","D/A"))
AUTOMATIC_PAIRS = tuple(dict.fromkeys((*FIRST,*((t,p) for p in PAYMENTS for t in TERMS))))


def route_status(pair):
    return "verified" if pair in VERIFIED else "integrated"


def route_choices():
    return [dict(terms=t, payment=p, label=f"{t} ＋ {p}", automatic=True, status=route_status((t,p)))
            for p in PAYMENTS for t in TERMS]


def observed_history(jobs, snapshots):
    """Join the current roles' readable contract summaries to completed local archives."""
    records = {}
    conflicts = set()
    for job in jobs:
        if job.get("kind")!="purchase" and job.get("stage")=="completed":
            case=job["case"]
            records[case["contract"]]=dict(case)
    for snapshot in snapshots:
        field={"exporter":"seller_account","importer":"buyer_account"}.get(snapshot.get("role"))
        if not field:continue
        for item in snapshot.get("contracts",[]):
            pair=item.get("terms"),item.get("payment")
            if item.get("kind")!="foreign" or item.get("status")!="已经完成" or pair not in AUTOMATIC_PAIRS:continue
            number=item["number"]
            case=records.setdefault(number,dict(contract=number,terms=pair[0],payment=pair[1]))
            if ((case["terms"],case["payment"])!=pair
                    or case.get(field,snapshot["account"])!=snapshot["account"]):
                conflicts.add(number)
            else:case[field]=snapshot["account"]
    return [{"case":v} for k,v in records.items() if k not in conflicts]


def pick_route(roles, history, terms="ROTATE", payment="AUTO"):
    """Prefer a route used least by the two trading roles; history includes this batch."""
    counts, totals, terms_used, pay_used = Counter(), Counter(), Counter(), Counter()
    for j in history:
        c = j.get("case", {})
        pair = c.get("terms"), c.get("payment")
        counts[pair] += int(c.get("seller_account")==roles[0]) + int(c.get("buyer_account")==roles[1])
        totals[pair] += 1
        terms_used[pair[0]]+=1
        pay_used[pair[1]]+=1
    choices=[(t,p) for t,p in AUTOMATIC_PAIRS if terms in ("ROTATE",t) and payment in ("AUTO",p)]
    if not choices:raise ValueError("不支持这个术语与结算组合。")
    return min(choices,key=lambda pair:(counts[pair],totals[pair],terms_used[pair[0]]+pay_used[pair[1]]))
