"""Read the selected school's student-facing catalogue, market, FX and port pages."""
from decimal import Decimal as D
import re
from urllib.parse import quote, urlsplit, parse_qs
from live_platform import PlatformError, _menu_link

# These are market-list page hints, not prices. If the catalogue order changes, search the remaining pages.
PRODUCTS = {"17001": ("床单", 33), "03001": ("壁钟", 5), "13001": ("直杆伞", 25),
            "15002": ("手提包", 29), "19001": ("男士皮鞋", 37),
            "22003": ("组合螺丝刀", 42), "30001": ("网球", 47), "08001": ("儿童踏板车", 15)}
PORTS = {"indonesia": "JAKARTA", "united kingdom": "LONDON", "australia": "MELBOURNE",
         "france": "MARSEILLES", "uk": "LONDON"}


def _pick(page, filename, **params):
    return _menu_link(page, filename, **params)


def market_price(session, product, hint):
    factory = session.role == "factory"
    folder, mapname, desk_id = ("Factory", "MapB", "F3") if factory else ("Import", "MapC", "I7")
    site_map = session.get(_pick(session.home, mapname+".aspx"))
    session.get(_pick(site_map, "ShowPopUp.aspx", p3=desk_id))
    page = session.get("Common/MarketList.aspx")
    options = next((c["options"] for c in page.controls if c.get("name")=="ctrPages:pages"), [])
    available = [o.get("value", o.get("text", "")) for o in options]
    for n in dict.fromkeys(["1", str(hint)] + available):
        if n != "1":
            if n not in available:
                continue
            page = session.post(page, "ctrPages$pages", {"ctrPages:pages": n})
        for row in page.rows:
            if len(row)>=6 and row[1]==product:
                match = re.fullmatch(r"([A-Z]{3})\s+([\d,.]+)",row[-1])
                if not match: raise ValueError("市场价格格式变化，请重新核对。")
                return match[1], D(match[2].replace(",", ""))
    raise ValueError("角色当前市场没有该商品。")


def catalogue_item(s, product, *, require_export_route=True):
    if product not in PRODUCTS: raise ValueError("请选择本版本支持的商品。")
    home=s.get(_pick(s.home,"query_first.aspx"))
    catalogue=s.get(_pick(home,"Query_Product.aspx"))
    found=s.post(catalogue,"btnSearch",{"searchKey":product})
    detail=s.get(_pick(found,"Info.aspx",cls="P",no=product))
    g=s.get(_pick(detail,"QueryProductDetail.aspx",no=product)).fields
    mapping={"id":"01","hs":"02","category":"03","name_cn":"06","name_en":"07","spec_cn":"08",
             "spec_en":"09","unit":"10","per_carton":"11","packing_unit":"12","gross_per_carton":"13",
             "net_per_carton":"15","cbm_per_carton":"32"}
    item={k:g["txtB04"+v].strip() for k,v in mapping.items()}
    if item["id"]!=product or not re.fullmatch(r"\d{8,12}",item["hs"]):raise ValueError("商品目录不匹配。")
    s.get(_pick(home,"Query_Cess.aspx")); tax=s.get("Query/QueryCess.aspx?a1="+item["hs"]+"&a2=&b=")
    rows=[x for x in tax.rows if len(x)==12 and x[0]==item["hs"] and not x[1]]
    if len(rows)!=1:raise ValueError("税则需按实际附加码重新核对。")
    item["tax"]=dict(zip(("hs","extra","description","normal_duty_pct","duty_pct","export_tax_pct","VAT_pct","consumption_per_unit","consumption_pct","refund_pct","statistical_unit","regulatory_code"),rows[0]))
    item["regulatory_code"]=item["tax"]["regulatory_code"]
    if require_export_route and item["regulatory_code"]: raise ValueError("当前自动履约线路只支持无额外监管条件的商品。")
    return item,home


def lookup(platform, buyer, factory, product, account_ids, port=None, *, payment="T/T"):
    s=platform.session(buyer,"importer"); f=platform.session(factory,"factory")
    country = platform.profile(buyer,"importer")["txtB0337"]
    port=(port or PORTS.get(country.strip().lower(),"")).upper()
    if not port or not re.fullmatch(r"[A-Z .'-]{2,50}",port):
        raise ValueError("请按进口商所在国填写平台收录的英文目的港。")
    item,home=catalogue_item(s,product)
    hint=PRODUCTS[product][1]
    c1,cost=market_price(f,product,hint); c2,sale=market_price(s,product,hint)
    if c1!="RMB" or min(cost,sale)<=0: raise ValueError("市场数据不完整。")
    item["cost_rmb"]=str(cost)
    bank=s.get(_pick(home,"Query_Bank.aspx"))
    rates={m[0]:D(m[1])/100 for m in re.findall(r"\b([A-Z]{3})\s+(\d+(?:\.\d+)?)\b",bank.text)}
    if rates.get("RMB")!=1 or min(rates.get("USD",0),rates.get(c2,0))<=0: raise ValueError("银行汇率未核实。")
    bankers={}
    checked_profiles={}
    required_bank_fields={
        "export_bank":("txtB0306","txtB0307","txtB0309","txtB0312","txtB0313"),
        "import_bank":("txtB0307","txtB0309"),
    }
    # Select only bank roles belonging to the configured students, from actual directory listings.
    opts=next((c["options"] for c in bank.controls if c.get("name")=="currentpage"),[])
    for idx in [None]+[o["value"] for o in opts if o["value"]!="1"]:
        if idx: bank=s.post(bank,"currentpage",{"currentpage":idx})
        for kind,key in [("出口地银行","export_bank"),("进口地银行","import_bank")]:
            if key in bankers:
                continue
            for acct in account_ids:
                if not re.search(r"\["+kind+r"\s*编号\s*[:：]\s*"+re.escape(acct)+r"\]",bank.text):
                    continue
                if payment == "L/C":
                    token = (acct,key)
                    if token not in checked_profiles:
                        profile = platform.profile(acct,key)
                        checked_profiles[token] = all(profile.get(f,"").strip() for f in required_bank_fields[key])
                    if not checked_profiles[token]:
                        continue
                bankers[key]=acct
                break
        if len(bankers)==2:break
    if len(bankers)!=2:
        if payment=="L/C":
            raise ValueError("信用证需在所选账号内找到有完整资料的两种银行角色。出口地银行需中英文名、英文地址、电话和传真，进口地银行需英文名和英文地址。请补齐缺项后重新报价。")
        raise ValueError("参与账号在银行目录中缺少出口地或进口地银行，不能自动填写合同。")
    freight=s.get(_pick(home,"Query_Freight.aspx"));s.get(_pick(freight,"QueryFreight.aspx"))
    ports=s.get("Query/outPort.aspx?cls=export&c=&p="+quote(port)+"&l=0")
    v=s.get(_pick(ports,"viewPort_o.aspx",no=port)).fields
    if v["txtB0501"].upper()!=port or v["txtB0502"].lower()!=country.lower():
        raise ValueError("目的港国家与进口商档案不同。")
    return dict(product=item,market=str(sale),currency=c2,usd_fx=str(rates["USD"]),local_fx=str(rates[c2]),
                port=port,port_cn=v["txtB0503"],country=country.upper(),country_cn=v["txtB0504"],
                freight_m=v["txtB0513"],freight_w=v["txtB0514"],**bankers)
