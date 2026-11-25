"""Quote an ocean trade using the selected terms, payment and each role's cash floor."""
from collections import Counter
from datetime import date, timedelta, datetime
from decimal import Decimal as D
from itertools import permutations, tee
from secrets import token_hex

from market import alternate_ports, import_inspection_required, lookup
from pricing import UnprofitableQuote, build, q
from wording import words_usd, cn_money


def rotation(accounts, focus=None):
    ids=list(dict.fromkeys(accounts))
    if not 1 <= len(ids) <= 10: raise ValueError("请输入 1 至 10 个不同的账号。")
    if focus:
        if focus not in ids: raise ValueError("优先账号不在本轮名单中。")
        ids=[focus]+[a for a in ids if a!=focus]
    # These are independent business roles. The platform finally decides whether a pairing is offered.
    if len(ids)==1:return [(ids[0],ids[0],ids[0])]
    return [(ids[i],ids[(i+1)%len(ids)],ids[(i-1)%len(ids)]) for i in range(len(ids))]


def _match_partners(ids, anchors, history, *, reverse=False, factory_buyers=None):
    """Assign each partner once in a complete round.

    The best partial assignment depends only on the set of partners already used.
    Tracking those sets gives O(n² 2ⁿ) work for at most ten accounts.
    """
    zero = (0, 0)
    states = {0: (zero, ())}
    size = len(ids)
    # Use the original forward buyer / backward supplier order to break ties.
    step = -1 if reverse or factory_buyers is not None else 1
    for anchor in anchors:
        options = []
        start = ids.index(anchor)
        for offset in range(1, size):
            index = (start + step * offset) % size
            partner = ids[index]
            if factory_buyers is not None and size > 2 and factory_buyers[anchor] == partner:
                continue
            pair = (partner, anchor) if reverse else (anchor, partner)
            uses = history[pair]
            cost = (int(uses > 0), uses)
            options.append((1 << index, partner, cost))

        next_states = {}
        for mask, (score, chosen) in states.items():
            for bit, partner, cost in options:
                if mask & bit:
                    continue
                new_mask = mask | bit
                new_score = (score[0] + cost[0], score[1] + cost[1])
                if new_mask not in next_states or new_score < next_states[new_mask][0]:
                    next_states[new_mask] = (new_score, chosen + (partner,))
        states = next_states
    return states[(1 << size) - 1][1]


def _partial_round(ids, slots, sales, purchases, activity, gaps, focus, focus_role):
    """Fill a partial round while still giving each role to distinct accounts.

    A highly ranked triple can leave the last slot without three eligible parties.
    Backtrack only when a choice would prevent allocating the requested slots.
    """
    size = len(ids)
    indices = {account: i for i, account in enumerate(ids)}
    candidates = permutations(ids, 3) if size > 2 else [(ids[0],ids[1],ids[1]), (ids[1],ids[0],ids[0])]

    def priority(roles):
        seller, buyer, factory = roles
        trade = sales[seller, buyer]
        procurement = purchases[seller, factory]
        participation = sum(counts[a] for counts, a in zip(activity, roles))
        level_gap = sum(gap.get(a, 0) for gap, a in zip(gaps, roles))
        return (trade, participation, procurement, -level_gap, indices[seller],
                (indices[buyer] - indices[seller]) % size, (indices[seller] - indices[factory]) % size)

    candidates = sorted(candidates, key=priority)
    focused_index = 1 if focus_role == "importer" else 0

    def fill(rows, sellers, buyers, factories):
        if len(rows) == slots:
            return rows
        for roles in candidates:
            seller, buyer, factory = roles
            if seller in sellers or buyer in buyers or factory in factories:
                continue
            if not rows and focus and roles[focused_index] != focus:
                continue
            result = fill(rows+[roles], sellers|{seller}, buyers|{buyer}, factories|{factory})
            if result is not None:
                return result
        return None

    return fill([], set(), set(), set())


def participant_plan(accounts, count, history, focus=None, focus_role="importer", *, snapshots=()):
    """Plan balanced rounds using completed contracts and the pairs scheduled so far."""
    ids = list(accounts)
    if not 1 <= len(ids) <= 10 or len(set(ids)) != len(ids):
        raise ValueError("请输入 1 至 10 个不同的账号。")
    if type(count) is not int or not 1 <= count <= 100:
        raise ValueError("订单数须为 1 至 100 的整数。")
    if (focus and focus not in ids) or focus_role not in ("importer", "exporter"):
        raise ValueError("请选择已连接账号的进口商或出口商作为优先角色。")
    if len(ids) == 1:
        return [(ids[0], ids[0], ids[0])] * count

    exports, imports, production = Counter(), Counter(), Counter()
    sales, purchases = Counter(), Counter()
    gaps = []
    for role in ("exporter", "importer", "factory"):
        levels = {s["account"]: s["level"] for s in snapshots if s.get("role") == role
                  and s.get("account") in ids and type(s.get("level")) is int}
        high = max(levels.values(), default=0)
        gaps.append({account: high - level for account, level in levels.items()})

    def record(seller, buyer, factory):
        if seller:
            exports[seller] += 1
        if buyer:
            imports[buyer] += 1
        if factory:
            production[factory] += 1
        if seller and buyer:
            sales[seller, buyer] += 1
        if seller and factory:
            purchases[seller, factory] += 1

    for job in history:
        if job.get("stage", "completed") != "completed":
            continue
        case = job.get("case", {})
        seller = case.get("seller_account")
        factory = case.get("domestic", {}).get("factory_account")
        if job.get("kind", "foreign") == "foreign":
            record(seller, case.get("buyer_account"), factory)
        elif job.get("kind") == "purchase" and seller and factory:
            production[factory] += 1
            purchases[seller, factory] += 1

    plan = []
    while len(plan) < count:
        slots = min(len(ids), count - len(plan))
        if slots < len(ids):
            plan.extend(_partial_round(ids, slots, sales, purchases, (exports,imports,production),
                                       gaps, focus, focus_role))
            break
        if focus and focus_role == "importer":
            buyers = sorted(ids, key=lambda a: (a != focus, imports[a], -gaps[1].get(a, 0)))
            sellers = _match_partners(ids, buyers, sales, reverse=True)
        else:
            sellers = sorted(ids, key=lambda a: (a != focus, exports[a], -gaps[0].get(a, 0)))
            buyers = _match_partners(ids, sellers, sales)
        factories = _match_partners(ids, sellers, purchases,
                                    factory_buyers=dict(zip(sellers, buyers)))
        for roles in zip(sellers, buyers, factories):
            plan.append(roles)
            record(*roles)
    return plan


def party(p, importer):
    return dict(company_cn=p["txtB0306"],company_en=p["txtB0306" if importer else "txtB0307"],
                address_cn=p["txtB0308"],address_en=p["txtB0308" if importer else "txtB0309"],
                person_cn=p["txtB0305"], person_en=p["txtB0305" if importer else "txtB0345"],
                credit_code=p["txtB0303"],phone=p["txtB0312"],fax=p.get("txtB0313",""),
                postcode=p["txtB0311"],bank_account=p["txtB0330"],email=p["txtB0327"],
                declaration_city_en="SHANGHAI")


def _route_quotes(platform, buyer, first, requested_port):
    yield first
    if not requested_port:
        for port_fields in alternate_ports(platform, buyer, first):
            yield {**first, **port_fields}


def _budget_for_port(quote, quantities, domestic_unit, available, terms, payment):
    product = quote["product"]
    cost = D(product["cost_rmb"])
    reason = "预计无法让三方均获利"
    for quantity in quantities:
        if D(q(cost * quantity)) > available["factory"]:
            reason = "预计最低余额低于角色保留额"
            continue
        candidates = []
        for multiple in ("2.6", "2.8", "3.0", "3.2", "3.4"):
            price = q(cost * D(multiple) / D(quote["usd_fx"]))
            try:
                budget = build(product, quantity=quantity, foreign_unit_USD=price,
                               domestic_unit_RMB=domestic_unit, payment=payment, terms=terms,
                               FX_USD=quote["usd_fx"], FX_local=quote["local_fx"],
                               market_unit_local=quote["market"], freight_M=quote["freight_m"],
                               freight_W=quote["freight_w"])
            except UnprofitableQuote:
                reason = "预计无法让三方均获利"
                continue
            econ = budget["economics"]
            required = {"exporter": econ["export_new_cash_required_RMB"],
                        "importer": econ["import_cash_required_local"],
                        "factory": econ["factory_production_cash_RMB"]}
            if any(D(amount) > available[role] for role, amount in required.items()):
                reason = "预计最低余额低于角色保留额"
                continue
            if D(econ["insured_total_loss_profit_local"]) <= 1:
                reason = "承保全损情景的预计结余不足"
                continue
            margin = min(D(econ["export_profit_RMB"]), D(econ["import_normal_profit_local"]) * D(quote["local_fx"]))
            candidates.append((margin, budget))
        if candidates:
            return max(candidates, key=lambda item: item[0])[1], None
    return None, reason


def new_job(platform, ids, actors, product_id="17001", cartons=3, floor_percent=70, port=None, *, terms="CIF", payment="T/T"):
    if terms not in ("CIF", "CFR", "FOB") or payment not in ("T/T", "D/P", "D/A", "L/C"):
        raise ValueError("请选择平台列出的贸易术语与结算方式。")
    if not 1<=cartons<=100 or not 0<=floor_percent<=100: raise ValueError("箱数应为 1–100，资金保留百分比为 0–100。")
    seller,buyer,factory=actors
    if set(actors)-set(ids) or (len(ids)>1 and seller==buyer):
        raise ValueError("本轮交易角色与所选账号不匹配。")
    market_options = {"payment":payment} if payment=="L/C" else {}
    quote=lookup(platform,buyer,factory,product_id,ids,port,**market_options)
    profile={role:platform.profile(acct,role) for acct,role in zip(actors,["exporter","importer","factory"])}
    inspection = import_inspection_required(quote["product"])
    if inspection:
        if not (profile["importer"].get("txtB0312") or "").strip():
            raise ValueError("该商品须办理入境报检，请先在进口商资料中填写电话。")
        if not (profile["exporter"].get("txtB0306") or "").strip():
            raise ValueError("该商品须办理入境报检，请先在出口商资料中填写中文公司全称。")
    snaps={role:platform.snapshot(acct,role) for acct,role in zip(actors,["exporter","importer","factory"])}
    if [snaps[k]["currency"] for k in ("exporter","importer","factory")]!=["RMB",quote["currency"],"RMB"]:
        raise ValueError("当期余额币种与行情不一致。")
    cash={k:D(str(v["cash"])) for k,v in snaps.items()}
    floors={k:q(v*D(floor_percent)/100) for k,v in cash.items()}
    p=quote["product"];cost=D(p["cost_rmb"]);dom=q(cost*D("1.6"))
    available={role:balance-D(floors[role]) for role,balance in cash.items()}
    per_carton = int(D(p["per_carton"]))
    minimum_units = 1 if inspection else per_carton
    if available["factory"] < D(q(cost * minimum_units)):
        minimum = "一台" if inspection else "一箱"
        raise UnprofitableQuote(f"工厂保留资金后不足以生产{minimum}商品。请核对工厂余额或本轮保留比例。")
    if available["exporter"] <= 0 or available["importer"] <= 0:
        raise UnprofitableQuote("进出口商保留资金后缺少可用于本单的余额。请核对角色资金或保留比例。")
    quantity_groups = []
    if available["factory"] >= D(q(cost * per_carton)):
        quantity_groups.append(range(cartons * per_carton, 0, -per_carton))
    if inspection and per_carton > 1:
        quantity_groups.append(range(per_carton - 1, 0, -1))
    # Try full cartons across eligible ports before falling back to a partly filled device carton.
    routes = _route_quotes(platform, buyer, quote, port)
    for quantities, candidates in zip(quantity_groups, tee(routes, len(quantity_groups))):
        for tried, candidate in enumerate(candidates, 1):
            b, failure = _budget_for_port(candidate, quantities, dom, available, terms, payment)
            if b is not None:
                quote = candidate
                break
        if b is not None:
            break
    else:
        scope = f"指定目的港 {port}" if port else f"{quote['country']} 可用的 {tried} 个目的港"
        raise UnprofitableQuote(f"已试算{scope}，在本轮箱数、报价和保留资金条件下均无法成交。最后一次试算：{failure}。")
    start=date.today(); until=(start+timedelta(days=21)).isoformat(); suffix=start.strftime("%y%m%d")+token_hex(3).upper()
    ct="WB"+suffix; dc="WD"+suffix; pk=b["packing"]; tax=p["tax"]
    product=dict(p,quantity=b["quantity"],cartons=pk["cartons"],gross_kg=pk["gross_kg"],
                 net_kg=pk["net_kg"],volume_cbm=pk["volume_cbm"],units_per_carton=p["per_carton"],
                 gross_per_carton_kg=p["gross_per_carton"],net_per_carton_kg=p["net_per_carton"],
                 duty_preferential=str(D(tax["duty_pct"])/100),VAT=str(D(tax["VAT_pct"])/100),
                 refund=str(D(tax["refund_pct"])/100),consumption="0",stock_cost_rmb=b["economics"]["inventory_cost_consumed_RMB"])
    f=profile["factory"]; total=D(dom)*b["quantity"]; port=quote["port"]; unit=p["unit"]
    fac=dict(contract=dc,factory_account=factory,factory_company=f["txtB0306"],factory_person=f["txtB0305"],
             address_cn=f["txtB0308"],credit_code=f.get("txtB0328") or f.get("txtB0303",""),phone=f["txtB0312"],
             fax=f.get("txtB0313",""),postcode=f["txtB0311"],bank_account=f["txtB0330"],unit_price_rmb=float(dom),
             total_rmb=float(total),production_unit_cost_rmb=float(cost),amount_words_cn=cn_money(total),
             delivery_deadline=(start+timedelta(days=10)).isoformat(),status="not_created",signing_city="上海")
    freight_mark = "COLLECT" if terms == "FOB" else "PREPAID"
    origin_form = "CERTIFICATE OF ORIGIN" if payment == "T/T" else "FORM A"
    expiry = start + timedelta(days=42)
    tenor = "30 days after B/L date" if payment == "D/A" else "----"
    pay_clause = {
        "T/T": "100% of invoice value by T/T after shipment and receipt of the agreed shipping documents.",
        "D/P": "100% documents against payment at sight through the agreed banks.",
        "D/A": "100% documents against acceptance of a draft drawn at 30 days after B/L date, payable at maturity.",
        "L/C": f"100% by irrevocable letter of credit available by negotiation at sight. Banking charges outside the issuing bank are for the beneficiary account. Credit expiry {expiry.isoformat()} in China; presentation within 21 days after issuance of transport documents and within credit validity.",
    }
    documents_clause = ("SIGNED COMMERCIAL INVOICE IN 3 COPIES; PACKING LIST IN 3 COPIES; "
                        f"FULL SET CLEAN ON BOARD BILLS OF LADING MARKED FREIGHT {freight_mark}; {origin_form} IN 1 ORIGINAL.")
    if payment in ("L/C", "D/P"):
        documents_clause += " SIGHT DRAFT."
    elif payment == "D/A":
        documents_clause += " DRAFT AT 30 DAYS AFTER B/L DATE."
    if terms == "CIF":
        documents_clause += " FULL SET OF 5 ORIGINAL INSURANCE POLICIES, ENDORSED TO THE BUYER."
        insurance_clause = "Insurance arranged by seller before shipment for 110% of invoice value"
    else:
        timing = "upon shipping advice" if payment == "T/T" else "before shipment"
        insurance_clause = f"Buyer to arrange insurance {timing} for 110% of CIF equivalent"
    insurance_clause += f" under PICC ALL RISKS, WAR RISKS AND STRIKE; claims payable in {port} in USD."
    case=dict(contract=ct,date=start.isoformat(),operation_date=start.isoformat(),status="negotiating",
              seller_account=seller,buyer_account=buyer,seller=party(profile["exporter"],False),
              buyer=dict(party(profile["importer"],True),country_en=quote["country"],country_cn=quote["country_cn"],currency=quote["currency"]),
              product=product,export_bank=quote["export_bank"],import_bank=quote["import_bank"],currency="USD",
              price_usd=float(b["foreign_unit_USD"]),amount_usd=float(b["amount_USD"]),amount_words=words_usd(b["amount_USD"]),
              payment=payment,payment_tenor=tenor,terms=terms,loading_port="SHANGHAI",loading_port_cn="上海港",
              destination=port,destination_cn=quote["port_cn"],destination_country_en=quote["country"],destination_country_cn=quote["country_cn"],
              mark="\n".join((profile["importer"].get("txtB0348") or "IMPORT",ct,port,"1/"+str(pk["cartons"]))),
              payment_clause=pay_clause[payment],
              documents_clause=documents_clause, insurance_clause=insurance_clause,
              packing_clause=f"{b['quantity']} {unit} IN {pk['cartons']} EXPORT CARTONS. {p['spec_en']}. GROSS {pk['gross_kg']} KGS, NET {pk['net_kg']} KGS, MEASUREMENT {pk['volume_cbm']} CBM.",
              shipment_clause=f"Shipment from SHANGHAI to {port} on or before {until}. Partial shipment not allowed. Transshipment allowed.",
              shipment_deadline=until,claim_days_after_actual_arrival=30,
              claim_clause="Any quality or quantity claim shall be made in writing within 30 days after actual arrival and supported by inspection evidence.",
              freight=b["freight"],insurance=dict(b["insurance"],originals=5,risks="ALL RISKS, WAR RISKS AND STRIKE",claim_city=port,claim_currency="USD"),
              documents={},ship={},domestic=fac,economics=b["economics"],cash_floor=floors)
    if payment == "L/C":
        case["lc"] = dict(tenor="AT SIGHT", expiry_date=expiry.isoformat(),
                          expiry_text=expiry.strftime("%Y%m%d")+" IN CHINA", presentation_days=21, insurance_originals=5)
    required_text=[case[k][n] for k in ("buyer","seller") for n in ("company_en","address_en","person_en","credit_code","bank_account")]
    if not all(str(x).strip() for x in required_text+[fac["factory_company"],fac["credit_code"]]):raise ValueError("参与方基本资料未填全。")
    return dict(version=1,created=datetime.now().isoformat(timespec="seconds"),origin=platform.base_url,case=case,
                budget=b,accounts=list(ids),market=quote,stage="quoted",opening=snaps)
