"""SIMtrade student workbench, interactive terminal entry point."""
import argparse
from getpass import getpass
from pathlib import Path
import re
import sys
from secrets import token_hex

from live_platform import DEFAULT_SCHOOL_URL, LivePlatform, ROLES, archive_origins
from market import PRODUCTS, PortSelectionError, product_pool, product_rotation
from pipeline import read, write, store_new, advance, run_lock
from planning import rotation, new_job
from results import summarize
from supply_mode import plan_supply, execute_supply
from correspondence import read_inbox, send_closing
from live_recovery import inspect_exception
from model_provider import ask_model
from trade_modes import AUTOMATIC_PAIRS, observed_history, pick_route

LABEL={"exporter":"出口商","importer":"进口商","factory":"工厂"}
STAGES={"quoted":"待执行报价","contracted":"外贸合同已确认","supplied":"国内已交货","shipped":"已装运交单","completed":"已结案"}


def integer(prompt, default, low, high):
    raw=input(f"{prompt} [{default}]：").strip()
    n=int(raw or default)
    if not low<=n<=high:raise ValueError(f"请输入 {low} 至 {high}。")
    return n


def choose(items, prompt):
    if not items:raise ValueError("当前没有可选记录。")
    for i,(label,_) in enumerate(items,1):print(f"  {i}. {label}")
    return items[integer(prompt,1,1,len(items))-1][1]


def login(account_text,base):
    ids=[x for x in re.split(r"[,，\s]+",account_text.strip()) if x]
    if not 1<=len(ids)<=3 or len(set(ids))!=len(ids) or any(not re.fullmatch(r"[A-Za-z0-9_-]{1,40}",a) for a in ids):
        raise ValueError("请输入 1–3 个不同账号，以空格或逗号分开。")
    auth={a:getpass(f"账号 {a} 的密码（可留空）：") for a in ids}
    return LivePlatform(base,auth),ids


def dashboard(platform,ids):
    for acct in ids:
        print(f"\n账号 {acct}")
        for role in LABEL:
            snap=platform.snapshot(acct,role)
            pending=[c for c in snap["contracts"] if c["status"]!="已经完成"]
            print(f"  {LABEL[role]}  {snap['level']}级  余额 {snap['currency']} {snap['cash']:,.2f}  "
                  f"贷款 {snap['loan']:,.2f}  库存账面 {snap['stock']:,.2f}  未完成 {len(pending)}")
            if pending:print("    未完合同："+ "、".join(f"{x['number']}（{x['status']}）" for x in pending[:5]))
            if snap["todo"]:print("    平台提示："+" / ".join(snap["todo"][:3]))


def preview(job):
    c=job["case"];b=job["budget"];e=b["economics"];p=c["product"]
    print(f"\n{c['contract']}  {p['name_cn']} × {p['quantity']} {p['unit']}（{p['cartons']} 箱）  {c['terms']} {c['destination']} · {c['payment']}")
    print(f"  出口 {c['seller_account']} → 进口 {c['buyer_account']}；供货工厂 {c['domestic']['factory_account']}")
    print(f"  外销 USD {b['foreign_unit_USD']}/{p['unit']}（总额 {b['amount_USD']}）；内采 RMB {b['domestic_unit_RMB']}/{p['unit']}。")
    print(f"  预计利润：出口 RMB {e['export_profit_RMB']}，进口 {c['buyer']['currency']} {e['import_normal_profit_local']}，工厂 RMB {e['factory_profit_RMB']}。")
    print("  本轮保留现金（各角色本币）：", "、".join(LABEL[k]+" "+v for k,v in c["cash_floor"].items()))


def api_settings():
    ep=input("兼容 Chat Completions 的完整地址（回车关闭模型）：").strip()
    if not ep:return None
    model=input("模型名：").strip();key=getpass("API Key（本机免密服务可留空）：")
    print("模型仅收到临时角色别名、业务状态和候选动作。异常发生时最多调用三次。")
    return lambda msgs:ask_model(ep,model,key,msgs)


def print_result(job, emit=print):
    result=summarize(job)
    emit("平台已结案，按角色列出本轮余额变化：")
    for role,snap in job["closing"].items():
        delta=snap["cash"]-job["opening"][role]["cash"]
        row=result["roles"][role]
        emit(f"  {LABEL[role]} {snap['currency']} {delta:+,.2f}；余额 {snap['cash']:,.2f}，等级 {snap['level']}。")
        if row.get("actual_profit") is not None:emit(f"    合同流水利润 {snap['currency']} {row['actual_profit']}")


def drive(platform, directory, ask, emit=print):
    p=directory/"job.json";j=read(p)
    if j.get("kind")=="purchase":
        if j["stage"]!="completed":
            with run_lock(directory.parent):
                r=execute_supply(platform,j["supply"],directory)
                j.update(stage="completed",result=r);write(p,j)
        emit(f"采购结案：工厂净利 RMB {j['result']['actual_profit_rmb']}，出口商收到 {j['result']['buyer_stock_received']} 件库存。")
        return
    try:
        final=advance(platform,directory,progress=lambda t:emit(f"{directory.name}　{STAGES.get(t.split()[-1],t)}"))
    except Exception:
        if ask is None:raise
        with run_lock(directory.parent):
            status,history,evidence=inspect_exception(platform,directory,ask)
        for decision,action,why in history:emit(" ".join(("模型",decision,action or "",why)))
        for line in evidence:emit(line)
        if status!="resume_rules":raise
        # One verified reconciliation only. The ordinary executor rechecks its journal.
        final=advance(platform,directory,progress=emit)
    print_result(final, emit)
    if final.get("closing_mail"):
        with run_lock(directory.parent):
            receipt=send_closing(platform,final,directory)
        emit("结款函："+receipt["status"])


def scope_jobs(data,platform,ids):
    ret=[]
    for p in data.glob("*/job.json"):
        j=read(p)
        if j.get("origin") in archive_origins(platform.base_url) and set(j.get("accounts",[]))==set(ids):
            ret.append((p.parent,j))
    return sorted(ret,key=lambda x:x[1].get("created",""),reverse=True)


def new_round(platform,ids,data,ask):
    order=rotation(ids)
    if len(ids)>1:
        focus=choose([("均衡轮换",None)]+[(f"先做 {a} 的进口业务",a) for a in ids],"本轮目标")
        if focus:order.sort(key=lambda roles:roles[1]!=focus)
    n=integer("本轮订单数（最多 12 笔）",len(order),1,12)
    route=AUTOMATIC_PAIRS[0]
    if len(AUTOMATIC_PAIRS)>1:
        route=choose([("按覆盖情况轮换",None),("逐笔指定组合","custom")]+[(f"{t} ＋ {p}",(t,p)) for t,p in AUTOMATIC_PAIRS],"履约路线")
    history=[]
    if route is None:
        from diversity import read_current_method
        for path in data.glob("*/job.json"):
            past=read(path)
            if past.get("origin") in archive_origins(platform.base_url) and past.get("stage")=="completed" and set(past.get("accounts",[])) & set(ids):
                history.append(past)
        snaps=[read_current_method(platform,platform.snapshot(a,r),data)
               for a in ids for r in ("exporter","importer")]
        history=observed_history(history,snaps)
    scope=choose([("已结案商品","verified"),("全部商品","all")],"商品轮换范围")
    first=choose([(f"{sku}  {PRODUCTS[sku][0]}",sku) for sku in product_pool(scope)],"第一笔商品")
    products=product_rotation(scope,first,n)
    carton=integer("每笔最多箱数",3,1,100)
    floor=integer("每个角色至少保留当前现金的百分之几",70,0,100)
    quotes=[]
    for i in range(n):
        roles=order[i%len(order)]
        if route=="custom":
            pair=choose([(f"{t} ＋ {p}",(t,p)) for t,p in AUTOMATIC_PAIRS],f"第 {i+1} 笔的条款")
        else:pair=route or pick_route(roles,history+quotes)
        options={"terms":pair[0],"payment":pair[1]}
        product=products[i]
        print("正在读取当期货价、关税、目的港和角色余额……",flush=True)
        try:j=new_job(platform,ids,roles,product,carton,floor,**options)
        except PortSelectionError as exc:
            print(exc)
            j=new_job(platform,ids,roles,product,carton,floor,input("输入本笔英文目的港：").strip(),**options)
        preview(j);quotes.append(j)
    notify=input("每单结案后，自动给本单进口商发送一封结款确认函？[y/N]：").strip().lower()=="y"
    if input(f"以上 {n} 笔将正式签约、采购、收付并结案。输入 开始 执行：").strip()!="开始":return
    group=token_hex(6)
    for i,j in enumerate(quotes):j.update(round_id=group,round_index=i+1,closing_mail=notify)
    paths=[store_new(data,j) for j in quotes]
    for d in paths:drive(platform,d,ask)


def supply(platform,ids,data):
    buyer=choose([(a,a) for a in ids],"采购方出口商")
    factory=choose([(a,a) for a in ids],"供货工厂")
    product=choose([(v[0],k) for k,v in PRODUCTS.items()],"采购商品")
    cartons=integer("最多箱数",1,1,100);floor=integer("保留现金百分比",70,0,100)
    plan=plan_supply(platform,buyer,factory,product,cartons,floor)
    print(f"采购合同 {plan['contract']}，数量 {plan['order']['quantity']}，单价 RMB {plan['order']['unit_price_rmb']}，"
          f"预计工厂净利 RMB {plan['order']['expected_profit_rmb']}。出口商将保留这批库存供后续使用。")
    if input("输入 采购 正式执行：").strip()!="采购":return
    j=dict(stage="quoted",kind="purchase",origin=platform.base_url,accounts=list(ids),
           case={"contract":plan["contract"]},supply=plan)
    drive(platform,store_new(data,j),None)


def console(platform,ids,data):
    ask=None
    while True:
        print("\n1 角色余额与等级  2 自动外贸  3 存档与续办  4 独立国内采购\n"
              "5 读站内来信    6 发结案函  7 可选模型 API  0 退出")
        try:
            opt=input("选择：").strip()
            if opt=="0":return
            if opt=="1":dashboard(platform,ids)
            elif opt=="2":new_round(platform,ids,data,ask)
            elif opt=="3":
                jobs=scope_jobs(data,platform,ids)
                d=choose([(f"{d.name}  {STAGES.get(j['stage'],j['stage'])}",d) for d,j in jobs],"选择存档")
                j=read(d/"job.json")
                if "attention" in j:print("上次暂停：",j["attention"]["message"])
                if j["stage"]=="completed":
                    if j.get("kind")=="purchase":print("国内采购已完成，工厂净利 RMB "+str(j["result"]["actual_profit_rmb"]))
                    else:print_result(j)
                else:
                    cmd=input("输入 继续 办这笔，或输入 整批 依次办完同一批未完成订单：").strip()
                    if cmd=="继续":drive(platform,d,ask)
                    if cmd=="整批" and j.get("round_id"):
                        for folder,other in sorted(jobs,key=lambda e:e[1].get("round_index",0)):
                            if other.get("round_id")==j["round_id"] and other["stage"]!="completed":drive(platform,folder,ask)
            elif opt=="4":supply(platform,ids,data)
            elif opt=="5":
                acct=choose([(a,a) for a in ids],"邮箱账号");role=choose([(v,k) for k,v in LABEL.items()],"角色")
                for m in read_inbox(platform.session(acct,role),limit=integer("打开最近几封",5,1,20)):
                    print(f"\n{m['sender']}  {m['subject']}\n{m['body']}")
            elif opt=="6":
                jobs=[(d,j) for d,j in scope_jobs(data,platform,ids) if j["stage"]=="completed" and j.get("kind")!="purchase"]
                d=choose([(d.name,d) for d,j in jobs],"选择已结外贸合同");j=read(d/"job.json")
                c=j["case"];print(f"{c['seller_account']} 出口商 → {c['buyer_account']} 进口商，结案确认函，合同 {c['contract']}。")
                if input("输入 发送 确认：").strip()=="发送":
                    with run_lock(data):print(send_closing(platform,j,d)["status"])
            elif opt=="7":ask=api_settings()
            else:print("请输入菜单编号。")
        except (EOFError,KeyboardInterrupt):
            print("\n已退出。本地存档可继续使用。");return
        except Exception as exc:print(f"本次操作暂停：{exc}")


def school_address(data):
    try:
        saved=(data/"学校平台地址.txt").read_text(encoding="utf-8-sig").strip()
    except (OSError,UnicodeError):
        saved=""
    return saved or DEFAULT_SCHOOL_URL


def default_data():
    base=Path(sys.executable if getattr(sys,"frozen",False) else __file__).resolve().parent
    data=base/"工作台数据"
    if base.name=="SIMtrade-workbench-Agent-Windows-x64" and not data.exists():
        previous=base.parent/"SIMtrade-workbench-GUI-Windows-x64"/"工作台数据"
        if previous.is_dir():
            return previous
    return data


def main():
    parser=argparse.ArgumentParser(description="SIMtrade 终端工作台")
    parser.add_argument("--data",type=Path,help="保存合同和收据的位置，默认程序旁的 工作台数据")
    parser.add_argument("--self-check",action="store_true",help="离线验证打包依赖及本地模型接管流程")
    args=parser.parse_args()
    if args.self_check:
        from market import port_catalogue
        from try_handoff import DraftExampleHost,scripted_answer
        from handoff import recover
        from settlement import settle_trade
        from foreign_trade import foreign_ship
        if recover(DraftExampleHost(),scripted_answer)[0]!="resume_rules":raise RuntimeError("接管自检失败")
        if not port_catalogue():raise RuntimeError("目的港目录为空")
        print("工作台自检通过。");return
    data=(args.data or default_data()).resolve()
    print("SIMtrade 实训工作台\n支持 1–3 个学生账号 · 常规交易使用本地规则 · 0 退出")
    first=input("输入账号（以空格分开，0 退出）：").strip()
    if first in ("","0"):return
    saved=school_address(data)
    base=input(f"学校平台地址 [{saved}]：").strip() or saved
    print("本次登录地址："+base)
    platform,ids=login(first,base)
    data.mkdir(parents=True,exist_ok=True);print("存档位置："+str(data))
    console(platform,ids,data)


if __name__=="__main__":
    try:main()
    except (EOFError,KeyboardInterrupt):pass
    except (OSError,ValueError,RuntimeError) as err:
        print("启动失败："+str(err),file=sys.stderr);sys.exit(1)
