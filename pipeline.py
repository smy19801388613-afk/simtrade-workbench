"""A local, restartable queue. Every trade has a private folder and a fixed identity."""
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal as D
import json
import os
from pathlib import Path
import tempfile

from live_platform import archive_origins


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path, data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w",dir=path.parent,delete=False,encoding="utf-8") as f:
        json.dump(data,f,ensure_ascii=False,indent=2)
        f.flush();os.fsync(f.fileno());name=f.name
    os.replace(name,path)


@contextmanager
def run_lock(data):
    data=Path(data);data.mkdir(parents=True,exist_ok=True)
    with (data/".active.lock").open("a+b") as f:
        if not f.tell():f.write(b"0");f.flush()
        f.seek(0)
        try:
            if os.name=="nt":
                import msvcrt
                msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(f.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            raise ValueError("同一数据目录已有工作台在执行交易。") from None
        try:yield
        finally:
            f.seek(0)
            if os.name=="nt":msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(f.fileno(),fcntl.LOCK_UN)


def store_new(data, job):
    d=Path(data)/job["case"]["contract"]
    d.mkdir(parents=True,exist_ok=False);write(d/"job.json",job);return d


def accounts_for(case):
    return {"exporter":case["seller_account"],"importer":case["buyer_account"],
            "factory":case["domestic"]["factory_account"]}


def check_cash(platform, case, roles):
    cost=case["economics"];accounts=accounts_for(case)
    current={}
    for role,key in (("exporter","export_new_cash_required_RMB"),("importer","import_cash_required_local"),
                     ("factory","factory_production_cash_RMB")):
        if role not in roles:continue
        snap=platform.snapshot(accounts[role],role)
        current[role]=snap
        if snap["currency"]!=("RMB" if role!="importer" else case["buyer"]["currency"]):
            raise ValueError("角色结算币种发生变化，需要重新报价。")
        if D(str(snap["cash"]))-D(cost[key])<D(case["cash_floor"][role]):
            raise ValueError(f"{role} 最新余额不能覆盖本单预算和保留资金。")
    return current


def domestic_order(case):
    p=case["product"];d=case["domestic"]
    return dict(contract=d["contract"],product_id=p["id"],description_cn=p["spec_cn"],
                product_name_cn=p["name_cn"],unit=p["unit"],quantity=p["quantity"],cartons=p["cartons"],
                unit_price_rmb=d["unit_price_rmb"],expected_unit_cost_rmb=d["production_unit_cost_rmb"],
                expected_profit_rmb=case["economics"]["factory_profit_RMB"],signing_city=d["signing_city"],
                buyer_cash_floor_rmb=case["cash_floor"]["exporter"], factory_cash_floor_rmb=case["cash_floor"]["factory"],
                delivery_date=d["delivery_deadline"])


def advance(platform, directory, *, through="completed", progress=print):
    d=Path(directory);p=d/"job.json"
    stages=["quoted","contracted","supplied","shipped","completed"]
    if through not in stages:raise ValueError("未知阶段。")
    with run_lock(d.parent):
        j=read(p);c=j["case"];b=j["budget"]
        if (d.name!=c["contract"] or j["origin"] not in archive_origins(platform.base_url) or
            set(j["accounts"])!=set(platform._accounts) or not set(accounts_for(c).values())<=set(j["accounts"])):
            raise ValueError("存档的平台、合同或授权账号与本次登录不同。")
        def finish(stage):
            j["stage"]=stage;j["updated"]=datetime.now().isoformat(timespec="seconds")
            j.pop("attention",None);write(p,j);progress(f"{c['contract']}  {stage}")
        try:
            while stages.index(j["stage"])<stages.index(through):
                if j["stage"]=="quoted":
                    from foreign_trade import foreign_contract
                    if not (d/("foreign-"+c["contract"]+".json")).exists():
                        if (datetime.now()-datetime.fromisoformat(j["created"])).total_seconds()>86400:
                            raise ValueError("未提交的报价已超过一天，请用最新行情重新建单。")
                        from market import lookup
                        market_options = {"payment":c["payment"]} if c["payment"]=="L/C" else {}
                        refreshed=lookup(platform,c["buyer_account"],c["domestic"]["factory_account"],
                                         c["product"]["id"],j["accounts"],port=c["destination"],**market_options)
                        if refreshed!=j["market"]:
                            raise ValueError("签约前行情、监管条件、银行或运费发生变化，请重新报价。")
                        baseline=check_cash(platform,c,accounts_for(c))
                        if not j.get("execution_started"):
                            j["opening"]=baseline
                            j["execution_started"]=datetime.now().isoformat(timespec="seconds")
                            write(p,j)
                    foreign_contract(platform,c,b,d)
                    if c["status"]!="confirmed":raise ValueError("缺少双方签约回执。")
                    finish("contracted")
                elif j["stage"]=="contracted":
                    if c["payment"]=="L/C":
                        from letter_credit import ensure_credit
                        ensure_credit(platform,c,b,d)
                        if c.get("status")!="lc_accepted":
                            raise ValueError("等待已接受的信用证，确认通知后可续办原合同。")
                        write(p,j)
                    from domestic import run_domestic
                    parties=accounts_for(c)
                    result=run_domestic(platform.session(parties["exporter"],"exporter"),
                                        platform.session(parties["factory"],"factory"),domestic_order(c),d)
                    if result["status"]!="completed":raise ValueError("等待国内供货核验。")
                    j["domestic_result"]=result;c["domestic"]["status"]="completed";finish("supplied")
                elif j["stage"]=="supplied":
                    from foreign_trade import foreign_ship
                    foreign_ship(platform,c,b,d)
                    expected="shipped_documents_sent" if c["payment"]=="T/T" else "shipped_for_bank"
                    if c["status"]!=expected:raise ValueError("等待与合同结算方式一致的交单回执。")
                    finish("shipped")
                else:
                    from settlement import settle_trade
                    result=settle_trade(platform,c,b,d)
                    if result["status"]!="completed":raise ValueError(str(result.get("reason") or "结算状态待核对。"))
                    j["settlement"]=result
                    j["closing"]={k:platform.snapshot(v,k) for k,v in accounts_for(c).items()}
                    finish("completed")
        except (Exception,KeyboardInterrupt) as exc:
            j["attention"]={"stage":j["stage"],"message":str(exc)[:800],"at":datetime.now().isoformat(timespec="seconds")}
            write(p,j);raise
        return j
