"""Quote an ocean trade using the selected terms, payment and each role's cash floor."""
from datetime import date, timedelta, datetime
from decimal import Decimal as D
from secrets import token_hex

from market import lookup
from pricing import build, q
from wording import words_usd, cn_money


def rotation(accounts, focus=None):
    ids=list(dict.fromkeys(accounts))
    if not 1 <= len(ids) <= 3: raise ValueError("请输入 1 至 3 个不同的账号。")
    if focus:
        if focus not in ids: raise ValueError("优先账号不在本轮名单中。")
        ids=[focus]+[a for a in ids if a!=focus]
    # These are independent business roles. The platform finally decides whether a pairing is offered.
    if len(ids)==1:return [(ids[0],ids[0],ids[0])]
    return [(ids[i],ids[(i+1)%len(ids)],ids[(i-1)%len(ids)]) for i in range(len(ids))]


def party(p, importer):
    return dict(company_cn=p["txtB0306"],company_en=p["txtB0306" if importer else "txtB0307"],
                address_cn=p["txtB0308"],address_en=p["txtB0308" if importer else "txtB0309"],
                person_cn=p["txtB0305"], person_en=p["txtB0305" if importer else "txtB0345"],
                credit_code=p["txtB0303"],phone=p["txtB0312"],fax=p.get("txtB0313",""),
                postcode=p["txtB0311"],bank_account=p["txtB0330"],email=p["txtB0327"],
                declaration_city_en="SHANGHAI")


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
    snaps={role:platform.snapshot(acct,role) for acct,role in zip(actors,["exporter","importer","factory"])}
    if [snaps[k]["currency"] for k in ("exporter","importer","factory")]!=["RMB",quote["currency"],"RMB"]:
        raise ValueError("当期余额币种与行情不一致。")
    cash={k:D(str(v["cash"])) for k,v in snaps.items()}
    floors={k:q(v*D(floor_percent)/100) for k,v in cash.items()}
    p=quote["product"];cost=D(p["cost_rmb"]);per=int(D(p["per_carton"]))
    dom=q(cost*D("1.6")); failure="无法满足预算"
    # Price grid in USD around the live factory cost. Prefer balanced exporter/importer margins.
    for count in range(cartons,0,-1):
        candidates=[]
        for mult in ("2.6","2.8","3.0","3.2","3.4"):
            try:
                b=build(p,quantity=count*per,foreign_unit_USD=q(cost*D(mult)/D(quote["usd_fx"])),
                        domestic_unit_RMB=dom,payment=payment,terms=terms,FX_USD=quote["usd_fx"],
                        FX_local=quote["local_fx"],market_unit_local=quote["market"],
                        freight_M=quote["freight_m"],freight_W=quote["freight_w"])
            except ValueError as e:failure=str(e);continue
            e=b["economics"]
            required=dict(exporter=e["export_new_cash_required_RMB"],importer=e["import_cash_required_local"],
                          factory=e["factory_production_cash_RMB"])
            if any(cash[k]-D(t)<D(floors[k]) for k,t in required.items()):
                failure="预计最低余额低于角色保留额";continue
            if D(e["insured_total_loss_profit_local"])<=1:failure="承保全损情景的预计结余不足";continue
            score=min(D(e["export_profit_RMB"]),D(e["import_normal_profit_local"])*D(quote["local_fx"]))
            candidates.append((score,b))
        if candidates:
            b=max(candidates,key=lambda t:t[0])[1];break
    else:raise ValueError("无法形成满足本轮条件的报价："+failure)
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
