"""Read the selected school's student-facing catalogue, market, FX and port pages."""
from decimal import Decimal as D, InvalidOperation
from functools import lru_cache
import json
from pathlib import Path
import re
import unicodedata
from urllib.parse import parse_qs, urlencode, urlsplit
from live_platform import PlatformError, _menu_link

# These are market-list page hints, not prices. If the catalogue order changes, search the remaining pages.
PRODUCTS = {"17001": ("床单", 33), "03001": ("壁钟", 5), "13001": ("直杆伞", 25),
            "15002": ("手提包", 29), "19001": ("男士皮鞋", 37),
            "22003": ("组合螺丝刀", 42), "30001": ("网球", 47), "08001": ("儿童踏板车", 15),
            "21003": ("双门冰箱", 41), "25002": ("便携式脑电图仪", 42)}
VERIFIED_PRODUCTS = ("17001", "03001", "13001", "15002", "19001", "21003", "25002")
IMPORT_INSPECTIONS = {"21003": ("8418212000", "A"), "25002": ("9018199000", "6A")}
PREFERRED_PORTS = {"印度尼西亚":"JAKARTA", "英国":"LONDON", "澳大利亚":"MELBOURNE",
                   "法国":"MARSEILLES", "美国":"LOS ANGELES"}


def product_pool(scope):
    if scope == "verified":
        return VERIFIED_PRODUCTS
    if scope == "all":
        return tuple(PRODUCTS)
    raise ValueError("请选择有效的商品轮换范围。")


def product_rotation(scope, first, count):
    products = product_pool(scope)
    if first not in products:
        raise ValueError("起始商品须属于本次选择的商品组。")
    start = products.index(first)
    return [products[(start + index) % len(products)] for index in range(count)]


def import_inspection_required(product):
    code = product.get("regulatory_code", "").strip()
    expected = IMPORT_INSPECTIONS.get(product.get("id"))
    if expected and (product.get("hs"), code) == expected:
        return True
    if not expected and code in ("", "-", "无"):
        return False
    raise ValueError("该商品的海关编码或监管条件超出工作台支持的履约范围，请核对学校商品目录和税则。")


@lru_cache(maxsize=1)
def port_catalogue():
    with Path(__file__).with_name("port_catalogue.json").open(encoding="utf-8") as file:
        return json.load(file)


def _name(text):
    return " ".join(unicodedata.normalize("NFKC", text).split()).casefold()


@lru_cache(maxsize=1)
def _country_names():
    # Some school labels (e.g. "Malasia" and "Malaysia") share one Chinese name.
    names = {}
    for english, record in port_catalogue().items():
        chinese = record["name_cn"]
        names[_name(english)] = names[_name(chinese)] = chinese
    aliases = {"us":"美国", "u.s.":"美国", "usa":"美国", "united states":"美国",
               "united states of america":"美国", "uk":"英国", "u.k.":"英国",
               "germany":"德国", "myanmar":"缅甸", "philippines":"菲律宾",
               "romania":"罗马尼亚", "sri lanka":"斯里兰卡", "south korea":"韩国",
               "republic of korea":"韩国"}
    return {**aliases, **names}


def _country(text):
    key = _name(text)
    return _country_names().get(key, key)


class PortSelectionError(ValueError):
    """The student can select another port or correct the importer profile."""


def port_input(text):
    if not isinstance(text, str) or len(text)>100 or any(
            not (ch.isalnum() or unicodedata.category(ch).startswith("M") or ch in " .,'’-()/&")
            for ch in text):
        raise PortSelectionError("请填写学校目录中的完整目的港名，或留空自动匹配。")
    return text.strip()


def _open_ports(session, home):
    freight = session.get(_pick(home, "Query_Freight.aspx"))
    session.get(_pick(freight, "QueryFreight.aspx"))


def _port_rows(session, *, name="", country=""):
    query = urlencode({"cls":"export", "c":country, "p":name, "l":0})
    page = session.get("Query/outPort.aspx?" + query)
    pages = next((c["options"] for c in page.controls if c.get("name")=="ctrPages:pages"), [])
    rows = []
    for number in [None] + [o["value"] for o in pages if o["value"]!="1"]:
        if number:
            page = session.post(page, "ctrPages$pages", {"ctrPages:pages":number})
        links = {}
        for url in page.urls:
            parsed = urlsplit(url)
            if parsed.path.lower().endswith("/viewport_o.aspx"):
                for key in parse_qs(parsed.query).get("no", ()):
                    links.setdefault(_name(key), set()).add(url)
        for row in page.rows:
            if len(row)<6:
                continue
            english, chinese, region = row[2:5]
            targets = links.get(_name(english), ())
            if len(targets)==1:
                rows.append((english, chinese, region, next(iter(targets))))
    return rows


def _named_rows(session, name):
    return [r for r in _port_rows(session, name=name) if _name(name) in (_name(r[0]), _name(r[1]))]


def _import_country(platform, buyer):
    country = platform.profile(buyer, "importer").get("txtB0337", "").strip()
    if not country:
        raise PortSelectionError(f"进口商 {buyer} 的所属国家为空，请先核对进口商档案。")
    return country


def batch_ports(platform, buyers, preferred):
    """Return the optional manual override for each importer in this preview."""
    buyers = list(dict.fromkeys(buyers))
    selected = {buyer: preferred for buyer in buyers}
    if not preferred or len(buyers)<2:
        return selected
    countries = {buyer:_import_country(platform, buyer) for buyer in buyers}
    if len({_country(country) for country in countries.values()})==1:
        return selected
    session = platform.session(buyers[0], "importer")
    _open_ports(session, session.get(_pick(session.home, "query_first.aspx")))
    matches = _named_rows(session, preferred)
    if not matches:
        raise PortSelectionError(f"当前学校的目的港目录找不到「{preferred}」。请核对港名，或留空自动匹配。")
    regions = {_country(row[2]) for row in matches}
    selected = {buyer:preferred if _country(country) in regions else None for buyer,country in countries.items()}
    if not any(selected.values()):
        names = "、".join(dict.fromkeys(countries.values()))
        raise PortSelectionError(f"目的港「{preferred}」与本批进口商国家（{names}）都不匹配。请更换港口或留空。")
    return selected


def _candidate_rows(session, country, requested, excluded_names):
    region = _country(country)
    candidate = requested
    if not candidate and not excluded_names:
        names = sorted(p["name"] for d in port_catalogue().values() if d["name_cn"]==region for p in d["ports"])
        candidate = PREFERRED_PORTS.get(region) or (names[0] if names else "")
    found = _named_rows(session, candidate) if candidate else []
    allowed = [r for r in found if _country(r[2])==region]
    if requested and not allowed:
        if not found:
            raise PortSelectionError(f"学校目录找不到目的港「{requested}」。请填写 {country} 的英文目的港，或留空。")
        where = "、".join(dict.fromkeys(r[2] for r in found))
        raise PortSelectionError(f"目的港「{requested}」属于 {where}，本笔进口商在 {country}。请改选该国港口，或留空。")
    yield from allowed
    if requested:
        return
    # A school may spell the same country in more than one way in its live directory.
    labels = [country] + [label for label,entry in port_catalogue().items() if entry["name_cn"]==region]
    for label in {_name(text):text for text in labels}.values():
        current = _port_rows(session, country=label)
        yield from sorted((r for r in current if _country(r[2])==region), key=lambda row:_name(row[0]))


def _usable_rates(fields):
    try:
        rates = [D(str(fields.get(key,""))) for key in ("txtB0513","txtB0514")]
    except InvalidOperation:
        return False
    return all(rate.is_finite() and rate>0 for rate in rates)


def iter_port_details(session, country, requested=None, *, exclude=()):
    """Yield current-school destinations with usable freight; only read further ports on demand."""
    requested = port_input(requested) if requested is not None else ""
    region = _country(country)
    names = {_name(name) for name in exclude}
    visited = set()
    for name, _, _, url in _candidate_rows(session, country, requested, names):
        key = _name(name)
        if key in names or url in visited:
            continue
        visited.add(url)
        fields = session.get(url).fields
        if (_name(fields.get("txtB0501",""))!=key or _country(fields.get("txtB0502",""))!=region
                or not fields.get("txtB0503","").strip() or not fields.get("txtB0504","").strip()
                or not _usable_rates(fields)):
            continue
        names.add(key)
        yield fields


def _port_details(session, country, requested=None):
    first = next(iter_port_details(session, country, requested), None)
    if first is not None:
        return first
    if requested:
        raise PortSelectionError(f"目的港「{requested}」的详情或实时运费未能核实。可改选 {country} 的其他港，或留空自动匹配。")
    raise PortSelectionError(f"本次没有查到国别和实时运费均可核实的 {country} 目的港。请核对学校海运运费目录。")


def _port_quote(fields):
    return dict(port=fields["txtB0501"].strip(), port_cn=fields["txtB0503"], country_cn=fields["txtB0504"],
                freight_m=fields["txtB0513"], freight_w=fields["txtB0514"])


def alternate_ports(platform, buyer, quote):
    """Reprice only the destination transport costs after a budget failure."""
    session = platform.session(buyer, "importer")
    # lookup() on this same student session already followed the freight menu.
    for fields in iter_port_details(session, quote["country"], exclude=(quote["port"],)):
        yield _port_quote(fields)


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
    if require_export_route:
        import_inspection_required(item)
    return item,home


def lookup(platform, buyer, factory, product, account_ids, port=None, *, payment="T/T"):
    s=platform.session(buyer,"importer"); f=platform.session(factory,"factory")
    country = _import_country(platform, buyer)
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
    _open_ports(s, home)
    v=_port_details(s, country, port)
    return dict(product=item,market=str(sale),currency=c2,usd_fx=str(rates["USD"]),local_fx=str(rates[c2]),
                country=country.upper(), **_port_quote(v), **bankers)
