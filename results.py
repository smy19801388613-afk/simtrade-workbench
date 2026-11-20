"""Summarize local workbench evidence. All monetary values are decimal strings in the role's currency."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re

from settlement import _sale_rows


_ROLES = ("exporter", "importer", "factory")
_CENT = Decimal(".01")


def _amount(value):
    try:
        n = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("记录中的金额或数量格式有误。") from exc
    if not n.is_finite():
        raise ValueError("记录中的金额或数量不是有限数。")
    return n


def _money(number):
    return None if number is None else format(number.quantize(_CENT, rounding=ROUND_HALF_UP), "f")


def _change(start, end, level=False):
    a = _amount(start) if start is not None else None
    b = _amount(end) if end is not None else None
    convert = (lambda n: int(n) if n is not None else None) if level else _money
    return {"opening": convert(a), "closing": convert(b),
            "change": convert(b - a) if a is not None and b is not None else None}


def _journal_amount(value, currency):
    if value in ("", None):
        return Decimal(0)
    pattern = r"([A-Z]{3})\s+(-?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?)"
    match = re.fullmatch(pattern, str(value).strip())
    if not match or match[1] != currency:
        raise ValueError("合同流水中出现金额格式或币种不符的记录。")
    return _amount(match[2].replace(",", ""))


def _ledger(rows, role, currency, domestic_ct):
    income = expense = Decimal(0)
    incoming_items, outgoing_items, observed = [], [], set()
    repeated = False
    for row in rows:
        if role == "factory":
            if len(row) != 5 or row[3] != domestic_ct:
                raise ValueError("工厂流水不属于关联采购合同。")
            at, inc, exp, _, item = row
        else:
            at, inc, exp, item = (row[k] for k in ("at", "income", "expense", "item"))
        token = (at, inc, exp, item)
        repeated |= token in observed
        observed.add(token)
        if bool(inc) == bool(exp):
            raise ValueError("无法判定流水的收支方向。")
        if inc:
            income += _journal_amount(inc, currency)
            incoming_items.append(item)
        else:
            expense += _journal_amount(exp, currency)
            outgoing_items.append(item)
    return ({"entries": len(rows), "income": _money(income), "expense": _money(expense),
             "net": _money(income - expense), "duplicate_rows": repeated},
            incoming_items, outgoing_items)


def _branch(case, settlement):
    claim = bool(settlement.get("claim_settled") or case.get("claim_settled"))
    sale = bool(case.get("normal_delivery_and_sale") or case.get("sale_done"))
    if sale and claim:
        raise ValueError("同一合同同时记载正常销售和保险理赔，请核对记录。")
    if claim:
        return "insured_loss"
    if sale:
        return "normal_sale"
    return "pending"


def summarize(job):
    """Return snapshot changes and attributable contract earnings from a single local job."""
    case = job["case"]
    ct = case["contract"]
    domestic = case.get("domestic") or {}
    dc = domestic.get("contract")
    completed_purchase = job.get("domestic_result") or {}
    settlement = job.get("settlement") or {}
    if completed_purchase and completed_purchase.get("contract") != dc:
        raise ValueError("采购结果的合同号与本单不一致。")
    if settlement and settlement.get("contract") != ct:
        raise ValueError("结算结果的合同号与本单不一致。")
    branch = _branch(case, settlement)
    actors = {"exporter": case.get("seller_account"), "importer": case.get("buyer_account"),
              "factory": domestic.get("factory_account")}
    roles = {}
    for role in _ROLES:
        first = (job.get("opening") or {}).get(role) or {}
        last = (job.get("closing") or {}).get(role) or {}
        proof = (settlement.get("evidence") or {}).get(role) or {}
        for snap in (first, last):
            if snap and (snap.get("role") != role or snap.get("account") != actors[role]):
                raise ValueError("期初或期末快照与交易角色不一致。")
        currencies = {src["currency"] for src in (first, last, proof) if src.get("currency")}
        if len(currencies) > 1:
            raise ValueError("同一角色的期初、期末或合同凭证币种不一致。")
        currency = next(iter(currencies), None)
        out = {field: _change(first.get(field), last.get(field), field == "level")
               for field in ("cash", "stock", "loan", "level")}
        out.update(currency=currency, opening_at=first.get("as_of"), closing_at=last.get("as_of"),
                   actual_profit=None, profit_basis="等待合同结算凭证。", ledger=None)
        roles[role] = out
        if role == "factory":
            rows = completed_purchase.get("factory_ledger")
        else:
            rows = proof.get("ledger")
        if not rows or not currency:
            continue
        totals, credits, debits = _ledger(rows, role, currency, dc)
        out["ledger"] = totals
        if totals["duplicate_rows"]:
            out["profit_basis"] = "流水有完全相同的记录，须核对后才能计算单笔利润。"
            continue
        net = _amount(totals["net"])
        purchase_done = completed_purchase.get("status") == "completed"
        if role == "factory" and purchase_done:
            audited = completed_purchase.get("actual_profit_rmb")
            unit_cost = domestic.get("production_unit_cost_rmb")
            delivered = completed_purchase.get("buyer_stock_received")
            if (currency == "RMB" and audited is not None and unit_cost is not None and delivered is not None
                    and credits.count("货款") == 1 and "增值税" in debits and "公司综合费用" in debits):
                cost = _amount(unit_cost) * _amount(delivered)
                if _money(net - cost) != _money(_amount(audited)):
                    raise ValueError("工厂的流水、交付数量和核实的生产单价未能核平。")
                out.update(actual_profit=_money(net - cost), cost_of_goods=_money(cost),
                           profit_basis="采购合同的货款收入扣除合同费用、税款及已交付货物的生产成本。")
            continue
        if settlement.get("status") != "completed" or proof.get("contract_status") != "已经完成":
            continue
        method = case.get("payment","T/T")
        term = case.get("terms","CIF")
        if role == "exporter" and purchase_done and credits.count(method+"货款") == 1:
            spend = completed_purchase.get("buyer_cash_spent_rmb")
            filled = completed_purchase.get("buyer_stock_received")
            required_fees = {"公司综合费用","内陆运费","产地证明书费","报关费"}
            if term!="FOB": required_fees.add("海运费")
            required_fees.update({"T/T":{"邮费"},"D/P":{"托收手续费"},"D/A":{"托收手续费"},
                                  "L/C":{"信用证通知费","信用证议付费"}}[method])
            if term == "CIF":
                required_fees.add("保险费")
            if (currency == "RMB" and spend is not None and filled is not None and
                    _amount(filled) == _amount(case["product"]["quantity"]) and required_fees <= set(debits)):
                out.update(actual_profit=_money(net - _amount(spend)), cost_of_goods=_money(_amount(spend)),
                           profit_basis="外销合同全部实收减实付，再扣关联采购合同已经支付的货款。")
        elif role == "importer":
            sale = proof.get("sale")
            if branch == "normal_sale" and sale:
                if (not _sale_rows([sale], case) or not sale.get("at") or sale.get("expense") or
                        not sale.get("income") or not proof.get("principal")):
                    raise ValueError("销售流水缺少对应货物、日期、收款或付款凭证。")
                receipt = _journal_amount(sale["income"], currency)
                if receipt <= 0:
                    raise ValueError("销售流水的收入金额须大于零。")
                out["sale_proceeds"] = _money(receipt)
                keys = ("at", "income", "expense", "item")
                if not any(all(row.get(k) == sale.get(k) for k in keys) for row in rows):
                    net += receipt
            import_fees = {"T/T":{"T/T电汇手续费"},"D/P":{"D/P手续费"},"D/A":{"D/A手续费"},
                           "L/C":{"信用证开证手续费","信用证付款手续费"}}[method]
            fees_complete = debits.count(method+"货款") == 1 and "公司综合费用" in debits and import_fees<=set(debits)
            if term in ("CFR","FOB"):
                fees_complete = fees_complete and "保险费" in debits
            if branch == "normal_sale":
                fees_complete = fees_complete and {"报关费","内陆运费"} <= set(debits)
                if term=="FOB": fees_complete = fees_complete and "海运费" in debits
                good = bool(sale) or any(k in {"销售收入","销货收入","商品销售收入","销售货款"} for k in credits)
                basis = "匹配商品和数量的实际销货收入，加减该合同日记账归集的各项收支。"
            else:
                good = branch == "insured_loss" and any(k in {"货物保险金","保险赔款","保险赔付","保险赔偿金"} for k in credits)
                basis = "按合同记载的保险赔款以及本单收支计算。"
            if fees_complete and good:
                out.update(actual_profit=_money(net), profit_basis=basis)
            else:
                out["profit_basis"] = "销售收入、赔款或费用的逐单凭证仍不齐全。"
    econ = (job.get("budget") or {}).get("economics") or {}
    return {"contract": ct, "stage": job.get("stage"), "branch": branch, "roles": roles,
            "budget_profit": {"exporter": econ.get("export_profit_RMB"),
                              "factory": econ.get("factory_profit_RMB"),
                              "import_normal_sale": econ.get("import_normal_profit_local"),
                              "import_insured_total_loss": econ.get("insured_total_loss_profit_local")}}
