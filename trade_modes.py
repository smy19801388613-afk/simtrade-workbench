"""Routes the current executor can actually fulfil, plus the school's comparison grid."""
from collections import Counter
import re

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


def partner_history(jobs, snapshots):
    """Combine completed sales and purchases; count a linked factory delivery only once."""
    records = {}
    conflicts = set()

    def merge(kind, number, **fields):
        if not number:
            return
        key = kind, number
        record = records.setdefault(key, {})
        for field, value in fields.items():
            if not value:
                continue
            if field in record and record[field] != value:
                conflicts.add(key)
            record[field] = value

    for job in jobs:
        stage = job.get("stage")
        case = job.get("case", {})
        kind = job.get("kind", "foreign")
        if kind == "purchase" and stage == "completed":
            supply = job.get("supply", {})
            roles = supply.get("roles", {})
            merge("domestic", case.get("contract"), seller=roles.get("exporter"), factory=roles.get("factory"),
                  sku=supply.get("product", {}).get("id") or supply.get("order", {}).get("product_id"))
        if kind != "foreign" or stage not in ("supplied", "shipped", "completed"):
            continue
        domestic = case.get("domestic", {})
        purchase = domestic.get("contract")
        factory = domestic.get("factory_account")
        sku = case.get("product", {}).get("id")
        seller = case.get("seller_account")
        merge("domestic", purchase, seller=seller, factory=factory, sku=sku)
        if stage == "completed":
            merge("foreign", case.get("contract"), seller=seller, buyer=case.get("buyer_account"),
                  sku=sku, purchase=purchase, factory=factory if not purchase else None)

    role_fields = {("foreign","exporter"): ("seller","buyer"), ("foreign","importer"): ("buyer","seller"),
                   ("domestic","exporter"): ("seller","factory"), ("domestic","factory"): ("factory","seller")}
    for snapshot in snapshots:
        account, role = snapshot.get("account"), snapshot.get("role")
        for row in snapshot.get("contracts", []):
            kind = row.get("kind")
            fields = role_fields.get((kind, role))
            if not account or not fields or row.get("status") != "已经完成":
                continue
            label = (row.get("counterparty") or "").strip()
            match = re.fullmatch(r"([A-Za-z0-9_-]{1,40})(?:\s*[（(].*[)）])?", label, re.DOTALL)
            merge(kind, row.get("number"), **{fields[0]: account, fields[1]: match[1] if match else None})

    history = []
    linked = set()
    for (kind, number), record in records.items():
        if kind != "foreign" or (kind, number) in conflicts or not (record.get("seller") and record.get("buyer")):
            continue
        case = dict(contract=number, seller_account=record["seller"], buyer_account=record["buyer"])
        if record.get("sku"):
            case["product"] = {"id": record["sku"]}
        purchase = ("domestic", record.get("purchase"))
        supply = records.get(purchase, {})
        factory = record.get("factory")
        if (purchase not in conflicts and purchase not in linked and supply.get("seller") == record["seller"]
                and supply.get("factory") and supply.get("sku") in (None, record.get("sku"))):
            factory = supply["factory"]
            linked.add(purchase)
        if factory:
            case["domestic"] = {"factory_account": factory}
        history.append({"case": case})
    for (kind, number), record in records.items():
        if kind != "domestic" or (kind, number) in linked or (kind, number) in conflicts:
            continue
        if record.get("seller") and record.get("factory"):
            case = dict(contract=number, seller_account=record["seller"], domestic={"factory_account": record["factory"]})
            if record.get("sku"):
                case["product"] = {"id": record["sku"]}
            history.append({"kind": "purchase", "case": case})
    return history


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
