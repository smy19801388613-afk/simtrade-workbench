"""One desktop session, with a single background worker for all school operations."""
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import re
import sys
from secrets import token_urlsafe
from threading import RLock, Thread

from live_platform import LivePlatform, archive_origins
from market import PRODUCTS, batch_ports, port_input
from model_provider import ModelProviderError, _destination, ask_model
from pipeline import read, store_new
from planning import new_job, rotation
from trade_modes import PAYMENTS, TERMS, observed_history, pick_route, route_choices, route_status
from agent_session import WorkbenchAgent
from workbench import LABEL, drive, school_address


def whole_number(value, low, high, label):
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]+", str(value)):
        raise ValueError(f"{label}须填写整数。")
    number = int(value)
    if not low <= number <= high:
        raise ValueError(f"{label}须在 {low} 到 {high} 之间。")
    return number


def quote_view(job):
    case, budget = job["case"], job["budget"]
    product, econ = case["product"], budget["economics"]
    return dict(contract=case["contract"], product=product["name_cn"], quantity=product["quantity"],
                cartons=product["cartons"], seller=case["seller_account"], buyer=case["buyer_account"],
                factory=case["domestic"]["factory_account"], terms=case["terms"], payment=case["payment"],
                port=case["destination"], foreign_unit=budget["foreign_unit_USD"], foreign_total=budget["amount_USD"],
                domestic_unit=budget["domestic_unit_RMB"], currency=case["buyer"]["currency"],
                export_profit=econ["export_profit_RMB"], import_profit=econ["import_normal_profit_local"],
                factory_profit=econ["factory_profit_RMB"], floors=case["cash_floor"],
                status=route_status((case["terms"],case["payment"])))


class DesktopSession:
    def __init__(self, data, platform_factory=LivePlatform):
        self.data = Path(data)
        self.default_base = school_address(self.data)
        self.platform_factory = platform_factory
        self.lock = RLock()
        self.token = token_urlsafe(32)
        self.clear()

    def clear(self):
        self.platform = self.ask = self.task = self.preview = None
        self.model_key = ""
        self.accounts, self.rows, self.jobs, self.quoted = [], [], [], []
        self.history, self.snapshots = [], []
        self.synced_at = None
        self.preview_params = None
        self.agent = WorkbenchAgent(self)

    def state(self):
        with self.lock:
            return deepcopy(dict(token=self.token, accounts=self.accounts, base=self.platform.base_url if self.platform else self.default_base,
                             products=[dict(id=k, name=v[0]) for k,v in PRODUCTS.items()], routes=route_choices(),
                             task=self.task, preview=self.preview, role_rows=self.rows, jobs=self.jobs, synced_at=self.synced_at,
                             agent=self.agent.view(), model_enabled=self.ask is not None))

    def log(self, line):
        with self.lock:
            self.task["message"] = str(line)
            self.task["logs"].append(str(line))
            del self.task["logs"][:-180]
        if sys.stdout and sys.stdout.isatty():
            print(line,flush=True)

    def run(self, kind, total, action):
        self.task = dict(id=token_urlsafe(12), kind=kind, status="running", message="正在准备", logs=[], done=0, total=total)

        def worker():
            try:
                action()
            except Exception as exc:
                self.log(str(exc) or "操作中断，请核对当前合同及平台回执。")
                with self.lock:
                    self.task["status"] = "error"
                    self.finish_task()
            else:
                with self.lock:
                    self.task.update(status="done", done=self.task["total"])
                    self.finish_task()

        Thread(target=worker, name="SIMtrade-"+kind, daemon=True).start()

    def finish_task(self):
        self.task["finished_at"]=datetime.now().astimezone().isoformat(timespec="seconds")
        self.agent.task_finished(self.task)

    def refresh_archives(self):
        active, all_jobs = set(self.accounts), []
        visible = []
        for path in self.data.glob("*/job.json"):
            job = read(path)
            actors = set(job.get("accounts", []))
            if job.get("origin") not in archive_origins(self.platform.base_url) or not actors & active:
                continue
            all_jobs.append(job)
            if actors != active:
                continue
            case = job["case"]
            visible.append(dict(contract=case["contract"],stage=job["stage"],product=case.get("product",{}).get("name_cn",""),
                                terms=case.get("terms",""), payment=case.get("payment",""), accounts=job["accounts"],
                                updated=job.get("updated",job.get("created","")),kind=job.get("kind","foreign"),
                                resume_hint=job.get("attention",{}).get("message","")))
        history = observed_history(all_jobs,self.snapshots)
        with self.lock:
            self.jobs = sorted(visible,key=lambda v:v["updated"],reverse=True)
            self.history = history

    def fetch_roles(self, platform, ids, logging=True):
        from diversity import read_current_method
        snaps = []
        for account in ids:
            for role in LABEL:
                if logging:
                    self.log(f"读取 {account} 的{LABEL[role]}余额、等级及任务")
                snapshot=platform.snapshot(account,role)
                if role in ("importer","exporter"):
                    snapshot=read_current_method(platform,snapshot,self.data)
                snaps.append(snapshot)
                if logging:
                    with self.lock:self.task["done"]=len(snaps)
        return snaps

    def set_roles(self, snaps):
        with self.lock:
            self.snapshots = snaps
            self.rows = [{k:s[k] for k in ("account","role","level","currency","cash","loan","stock","todo")} for s in snaps]
            self.synced_at=datetime.now().astimezone().isoformat(timespec="seconds")

    def dispatch(self, path, body):
        with self.lock:
            if path=="chat":
                self.agent.message(body)
                return
            if path=="chat-action":
                self.agent.respond(body)
                return
            if self.task and self.task["status"]=="running":
                raise ValueError("当前任务仍在运行，请等它结束再操作。")
            if path=="login":
                if self.platform:
                    raise ValueError("请先退出当前账号组合。")
                records = body.get("accounts")
                if not isinstance(records,list) or not 1<=len(records)<=3:
                    raise ValueError("请输入 1–3 个账号。")
                credentials = {}
                for row in records:
                    if not isinstance(row,dict): raise ValueError("账号格式有误。")
                    account, password = row.get("id",""), row.get("password","")
                    if not isinstance(account,str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}",account) or account in credentials:
                        raise ValueError("请填写互不重复的有效账号。")
                    if not isinstance(password,str): raise ValueError("密码格式有误。")
                    credentials[account] = password
                platform = self.platform_factory(body.get("base") or self.default_base,credentials)
                ids = list(credentials)
                def login():
                    snaps = self.fetch_roles(platform,ids)
                    with self.lock:
                        self.platform, self.accounts = platform,ids
                        self.set_roles(snaps)
                    self.refresh_archives()
                    self.log("已连接，角色与任务已更新。")
                self.run("login",len(ids)*3,login)
                return
            if path=="model":
                ep, name, key = (body.get(k,"") for k in ("endpoint","model","key"))
                if not all(isinstance(v,str) for v in (ep,name,key)):raise ValueError("模型配置格式有误。")
                if ep:
                    try:
                        _destination(ep)
                    except ModelProviderError as exc:
                        raise ValueError(str(exc)) from None
                    if not name.strip():raise ValueError("请填写模型名。")
                self.model_key = key if ep else ""
                self.ask = (lambda messages:ask_model(ep,name,key,messages)) if ep else None
                return
            if not self.platform: raise ValueError("请先连接学校账号。")
            if path=="logout":
                self.clear()
            elif path=="refresh":
                def refresh():
                    self.set_roles(self.fetch_roles(self.platform,self.accounts))
                    self.refresh_archives()
                    self.log("角色、存档和平台提示已更新。")
                self.run("refresh",len(self.accounts)*3,refresh)
            elif path=="preview":
                self.prepare(body)
            elif path=="start":
                if not self.preview or body.get("id")!=self.preview["id"]:
                    raise ValueError("这份报价已经失效，请重新预览。")
                self.agent.invalidate("start",self.preview["id"])
                jobs, self.quoted, self.preview, self.preview_params = self.quoted, [], None, None
                def execute():
                    group=token_urlsafe(12)
                    for i,j in enumerate(jobs,1):j.update(round_id=group,round_index=i)
                    folders=[store_new(self.data,j) for j in jobs]
                    self.execute(folders)
                self.run("start",len(jobs),execute)
            elif path=="resume":
                ct = body.get("contract")
                if not any(j["contract"]==ct and j["stage"]!="completed" for j in self.jobs):
                    raise ValueError("当前账号组合没有这笔待续办的存档。")
                folders = [p.parent for p in self.data.glob("*/job.json") if p.parent.name==ct]
                if len(folders)!=1: raise ValueError("未找到对应存档。")
                self.agent.invalidate("resume",ct)
                self.run("resume",1,lambda:self.execute(folders))
            else:
                raise ValueError("未知操作。")

    def prepare(self, body):
        count = whole_number(body.get("count",1),1,12,"订单数")
        preferred_port = port_input(body.get("port",""))
        cartons = whole_number(body.get("cartons",3),1,100,"箱数")
        floor = whole_number(body.get("floor",70),0,100,"保留资金百分比")
        sku, term = body.get("product"), body.get("terms","CIF")
        payment = body.get("payment","T/T")
        if sku not in PRODUCTS or term not in (*TERMS,"ROTATE") or payment not in (*PAYMENTS,"AUTO"):
            raise ValueError("请选择有效的商品、贸易术语及结算方式。")
        plans=body.get("plans")
        if plans is not None:
            if not isinstance(plans,list) or len(plans)!=count:
                raise ValueError("逐笔搭配的行数须与本轮订单数一致。")
            if any(not isinstance(s,dict) or s.get("terms") not in (*TERMS,"ROTATE")
                   or s.get("payment") not in (*PAYMENTS,"AUTO") for s in plans):
                raise ValueError("请为各笔订单选择有效的搭配。")
        ids = self.accounts
        order = rotation(ids)
        focus = body.get("focus","")
        focus_role = body.get("focus_role", "importer")
        if focus_role not in ("exporter", "importer"):
            raise ValueError("优先角色须选择出口商或进口商。")
        if focus:
            if focus not in ids:raise ValueError("优先账号须在当前账号组合中。")
            position = 0 if focus_role == "exporter" else 1
            order.sort(key=lambda actors:actors[position]!=focus)
        send_mail = body.get("closing_mail",False)
        if not isinstance(send_mail,bool):raise ValueError("请确认结案函选项。")
        product_ids = list(PRODUCTS)
        first = product_ids.index(sku)
        self.agent.invalidate("start")
        self.quoted, self.preview, self.preview_params = [], None, None
        def quote_batch():
            quotes=[]
            participants = [order[i%len(order)] for i in range(count)]
            ports = batch_ports(self.platform, [actors[1] for actors in participants], preferred_port or None)
            for i,actors in enumerate(participants):
                product=product_ids[(first+i)%len(product_ids)]
                t,p=(plans[i]["terms"],plans[i]["payment"]) if plans is not None else (term,payment)
                pair=pick_route(actors,self.history+quotes,t,p)
                self.log(f"第 {i+1}/{count} 笔：核对{PRODUCTS[product][0]}行情、运费和三方余额")
                options = {"port":ports[actors[1]],"terms":pair[0],"payment":pair[1]}
                job=new_job(self.platform,ids,actors,product,cartons,floor,**options)
                job["closing_mail"]=send_mail
                if (job["case"]["terms"],job["case"]["payment"])!=pair:raise ValueError("生成的合同方式与所选方式不符。")
                quotes.append(job)
                with self.lock:self.task["done"]=len(quotes)
            with self.lock:
                self.quoted=quotes
                self.preview=dict(id=token_urlsafe(24),orders=[quote_view(j) for j in quotes])
                self.preview_params={k:deepcopy(v) for k,v in body.items()
                                     if k in ("product","terms","payment","count","cartons","floor","focus","focus_role","plans","port")}
            self.log("报价已生成，等待确认。")
        self.run("preview",count,quote_batch)

    def execute(self, folders):
        try:
            for i,folder in enumerate(folders,1):
                self.log(f"办理第 {i}/{len(folders)} 笔，合同 {folder.name}")
                drive(self.platform,folder,self.ask,emit=self.log)
                with self.lock:self.task["done"]=i
        finally:
            self.refresh_archives()
        try:
            self.set_roles(self.fetch_roles(self.platform,self.accounts,logging=False))
            self.refresh_archives()
        except (OSError,RuntimeError,ValueError):
            self.log("合同已结案。本次刷新角色资料失败，可稍后手动刷新。")
        else:
            self.log("已完成，平台角色数据与本地存档已更新。")
