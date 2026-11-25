"""Conversational access to the same accounts, quotes and receipts as the workbench."""
from collections import deque
from copy import deepcopy
import re
from secrets import token_urlsafe
from threading import Thread

from agent_brain import EDUCATE, NEGATE, RESUME, plain, plan, review_result
from agent_terminal import run_terminal
from market import PRODUCTS, VERIFIED_PRODUCTS
from trade_modes import route_choices


ROLE = {"exporter":"出口商","importer":"进口商","factory":"工厂"}
STAGE = {"quoted":"待执行报价","contracted":"已签约","supplied":"已供货","shipped":"已装运交单","completed":"已结案"}
PUBLIC = ("product","product_scope","terms","payment","count","cartons","floor","focus","focus_role","plans","port")


def quote_lines(orders):
    lines=[]
    for i,o in enumerate(orders,1):
        lines.extend((f"{i}. {o['contract']}　{o['product']} × {o['quantity']}，{o['terms']}＋{o['payment']}，{o['port']}",
                      f"出口 {o['seller']} → 进口 {o['buyer']}，工厂 {o['factory']}。合同总额 USD {o['foreign_total']}。",
                      f"预计利润：出口 RMB {o['export_profit']}，进口 {o['currency']} {o['import_profit']}，工厂 RMB {o['factory_profit']}。"))
    return lines


def strip_secrets(text,secrets,*,structured=False):
    text=str(text)
    for secret in sorted({v for v in secrets if v},key=len,reverse=True):
        if secret.isascii() and secret.isdigit():
            # Receipts legitimately contain digits that may match a numeric login password.
            # The user input is scrubbed before it enters history or the provider context.
            if not structured:
                text=re.sub(r"(?<![A-Za-z0-9.,])"+re.escape(secret)+r"(?![A-Za-z0-9.,]|\s*(?:箱|笔|单|件|%|％))","[已隐藏]",text)
        elif len(secret)>3:
            text=text.replace(secret,"[已隐藏]")
        else:
            text=re.sub(r"(?<![A-Za-z0-9])"+re.escape(secret)+r"(?![A-Za-z0-9])","[已隐藏]",text)
    return re.sub(r"(?i)\bsk-[a-z\d_-]{8,}|(?:密码|密钥|api[ _-]?key|password)\s*(?:[:：=]|是|为)\s*[^\s，。；,;]+", "[已隐藏]", text)


class WorkbenchAgent:
    def __init__(self,session):
        self.session=session
        self.messages=deque(maxlen=80)
        self.request_ids=deque(maxlen=100)
        self.job_alias={}
        self.busy=False
        self.pending=None
        self.watching={}

    def view(self):
        pending={k:self.pending[k] for k in ("id","title","tool","details")} if self.pending else None
        return dict(messages=list(self.messages),busy=self.busy,pending=pending,
                    mode="model" if self.session.ask else "local")

    def say(self,role,text,tool=None):
        text=self.clean(text,structured=role=="tool").strip()
        if not text:return
        item=dict(id=token_urlsafe(8),role=role,text=text[:12000])
        if tool:item["tool"]=tool
        self.messages.append(item)

    def clean(self,text,*,structured=False):
        platform=self.session.platform
        credentials=getattr(platform,"_accounts",{}) if platform else {}
        # Production credentials live only in the session; simulation platforms may expose an ID list.
        private=list(credentials.values()) if isinstance(credentials,dict) else []
        return strip_secrets(text,[x for x in private+[self.session.model_key] if isinstance(x,str)],structured=structured)

    def scope(self):
        s=self.session
        return (s.platform.base_url if s.platform else None,tuple(s.accounts))

    def context(self):
        s=self.session
        accounts={a:chr(ord('A')+i) for i,a in enumerate(s.accounts)}
        for j in s.jobs:
            ct=j["contract"]
            if ct not in self.job_alias:self.job_alias[ct]=f"J{len(self.job_alias)+1}"
        for o in (s.preview or {}).get("orders",[]):
            ct=o["contract"]
            if ct not in self.job_alias:self.job_alias[ct]=f"J{len(self.job_alias)+1}"
        replacements={**self.job_alias,**accounts}
        def anon(value):
            if isinstance(value,list):return [anon(x) for x in value]
            if isinstance(value,dict):return {k:anon(v) for k,v in value.items()}
            if not isinstance(value,str):return value
            value=self.clean(value,structured=True)
            for raw,alias in sorted(replacements.items(),key=lambda item:-len(item[0])):
                value=re.sub(r"(?<![A-Za-z0-9_])"+re.escape(raw)+r"(?![A-Za-z0-9_])",lambda _:alias,value)
            # Unknown student numbers occasionally occur inside school's free-text prompts.
            value=re.sub(r"(?<!\d)20\d{7}(?!\d)","[其他账号]",value)
            value=re.sub(r"(?i)(?<![A-Z0-9])W[BD]\d[A-Z0-9_-]{3,}(?![A-Z0-9])","[其他合同]",value)
            value=re.sub(r"(?i)https?://[^\s，。；）)]+|[\w.+%-]+@[\w.-]+\.[A-Za-z]{2,}", "[地址已隐藏]",value)
            return re.sub(r"(?i)(?<![a-z])[a-z]:[\\/][^\n，。；]+","[本机路径]",value)
        task=s.task
        ctx=dict(accounts=list(accounts.values()),products=[dict(id=k,name=v[0],verified=k in VERIFIED_PRODUCTS) for k,v in PRODUCTS.items()],
                 routes=[{"terms":r["terms"],"payment":r["payment"]} for r in route_choices()],
                 roles=anon(s.rows),jobs=anon([{k:j.get(k,"") for k in ("contract","stage","product","terms","payment","resume_hint")}
                                              for j in s.jobs[:100]]), synced_at=s.synced_at,
                 preview=anon({"orders":s.preview["orders"]} if s.preview else None),
                 preview_params=anon(s.preview_params),
                 task=anon({k:task.get(k) for k in ("kind","status","done","total","message")} if task else None))
        return ctx,accounts,anon

    def message(self,body):
        text=body.get("message")
        rid=body.get("request_id")
        if not isinstance(text,str) or not text.strip() or len(text)>2000:raise ValueError("请填写 1–2000 字的消息。")
        if not isinstance(rid,str) or not re.fullmatch(r"[A-Za-z0-9_-]{6,100}",rid):
            raise ValueError("消息编号有误，请重试。")
        if rid in self.request_ids:return
        if self.busy:raise ValueError("Agent 正在处理上一条消息。")
        self.request_ids.append(rid)
        raw=self.clean(text.strip())
        if self.pending and raw in ("取消","取消执行","取消续办","先不执行","先不办"):
            self.say("user",raw)
            self.respond({"id":self.pending["id"],"choice":"cancel"})
            return
        ctx,accounts,anon=self.context()
        history=[dict(role="user" if m["role"]=="user" else "assistant",content=anon(m["text"])[:2800])
                 for m in list(self.messages)[-10:]]
        prompt=anon(raw)
        # Permit a short student suffix only if it identifies one connected account and is used as an account.
        for acct,alias in accounts.items():
            suffix=acct[-3:]
            if len(acct)>3 and suffix.isdigit() and sum(x.endswith(suffix) for x in accounts)==1:
                prompt=re.sub(r"(?<![0-9])"+suffix+r"(?=\s*(?:的|账号|出口|进口|工厂))",alias,prompt)
                prompt=re.sub(r"(账号\s*)"+suffix+r"(?![0-9])",lambda m:m[1]+alias,prompt)
        self.say("user",raw)
        if "[其他账号]" in prompt or "[其他合同]" in prompt:
            self.say("assistant","这个账号或合同号不在当前会话中。请核对号码，或先连接持有这份合同的账号组合。")
            return
        scope=self.scope()
        preview_id=(self.session.preview or {}).get("id")
        ask=self.session.ask
        self.busy=True
        def work():
            try:
                decision=plan(prompt,history,ctx,ask)
                with self.session.lock:
                    if not self.current(scope):return
                    tool=decision["tool"]
                    args=decision.get("args") or {}
                    if tool not in ("terminal_check","terminal_audit"):
                        self.apply(tool,args,decision.get("text",""),accounts,preview_id,raw)
                        return
                    ct=self.contract(args,unfinished=False) if tool=="terminal_audit" else None
                    data=self.session.data
                    base,ids=scope
                    self.say("tool","正在启动本机终端"+("核对合同 "+ct if ct else "执行工作台自检")+"。",tool)
                output=run_terminal(tool,data=data,base=base or "",accounts=list(ids),contract=ct)
                with self.session.lock:
                    if not self.current(scope):return
                    self.say("tool",output,tool)
                    fresh,_,scrub=self.context()
                    observation={"tool":tool,"text":scrub(output)}
                    review_model=ask if self.session.ask is ask else None
                next_step=review_result(prompt,history,fresh,observation,review_model)
                with self.session.lock:
                    if not self.current(scope):return
                    next_tool=next_step["tool"]
                    if next_tool!="answer" or next_step.get("text"):
                        self.apply(next_tool,next_step.get("args",{}),next_step.get("text",""),accounts,preview_id,raw)
                    if (tool=="terminal_audit" and next_tool=="answer" and re.search(RESUME,plain(raw))
                            and not re.search(NEGATE+"|"+EDUCATE,plain(raw))
                            and any(j["contract"]==ct and j["stage"]!="completed" for j in self.session.jobs)):
                        self.propose("resume",ct)
            except Exception as exc:
                with self.session.lock:
                    if self.current(scope):self.say("assistant",str(exc) or "本次请求未完成，请重试。")
            finally:
                with self.session.lock:self.busy=False
        Thread(target=work,name="SIMtrade-Agent",daemon=True).start()

    def current(self,scope):
        if self.session.agent is not self:return False
        if self.scope()!=scope:
            self.say("assistant","账号会话已变化，请按当前账号重新提问。")
            return False
        return True

    def contract(self,args,*,unfinished):
        if not self.session.platform:raise ValueError("请先连接持有该合同的账号组合。")
        jobs=[j for j in self.session.jobs if not unfinished or j["stage"]!="completed"]
        value=args.get("contract","")
        if value:
            match=[j for j in jobs if value in (j["contract"],self.job_alias.get(j["contract"]))]
        else:match=jobs if len(jobs)==1 else []
        if len(match)==1:return match[0]["contract"]
        if not jobs:raise ValueError("当前账号组合没有可续办的合同。" if unfinished else "当前账号组合还没有本机合同存档。")
        choices="、".join(j["contract"] for j in jobs[:6])
        raise ValueError("请指定当前合同存档中的合同号。可选："+choices+"。")

    def apply(self,tool,args,text,accounts,preview_id,request):
        s=self.session
        if tool=="answer":
            # Render symbolic references in a model answer only where they denote a role or contract.
            for ct,alias in self.job_alias.items():
                text=re.sub(r"(?<![A-Za-z0-9])"+alias+r"(?![A-Za-z0-9])",lambda _:ct,text)
            for acct,alias in accounts.items():
                text=re.sub(r"(账号\s*)"+alias+r"(?![A-Za-z0-9])",lambda m:m[1]+acct,text)
            self.say("assistant",text or "请告诉我你想处理的合同、角色或操作。")
            return
        if not s.platform:raise ValueError("请先在交易总览连接账号，再处理这项业务。终端自检和使用问题现在就可以问。")
        reverse={v:k for k,v in accounts.items()}
        if tool=="status":
            self.say("tool",self.status_text(args,reverse),tool)
        elif tool=="jobs":
            jobs=[j for j in s.jobs if not args.get("stage") or args["stage"] in (j["stage"],"all") or
                  (args["stage"] in ("open","pending","unfinished") and j["stage"]!="completed")]
            lines=[f"{j['contract']}　{j['product'] or '国内采购'}　{STAGE.get(j['stage'],j['stage'])}"
                   +("，"+j["resume_hint"] if j.get("resume_hint") else "") for j in jobs[:20]]
            self.say("tool",f"当前账号组合匹配 {len(jobs)} 份存档。"+("\n"+"\n".join(lines) if lines else ""),tool)
        elif tool in ("refresh","preview"):
            params={}
            if tool=="preview":
                params={k:args[k] for k in PUBLIC if k in args}
                if params.get("focus"):
                    if params["focus"] not in reverse:raise ValueError("请选择已连接的账号作为优先角色。")
                    params["focus"]=reverse[params["focus"]]
                params.setdefault("product",next(iter(PRODUCTS)))
                params.setdefault("product_scope","verified" if params["product"] in VERIFIED_PRODUCTS else "all")
                params["closing_mail"]=False
            s.dispatch(tool,params)
            self.watching[s.task["id"]]=(tool,request)
            self.say("tool","已开始根据当前行情试算。" if tool=="preview" else "正在读取所选账号的学校资料。",tool)
        elif tool=="start":
            if not preview_id or not s.preview or s.preview["id"]!=preview_id:
                raise ValueError("请先生成并核对本次报价，再执行。")
            self.propose("start",preview_id)
        elif tool=="resume":
            self.propose("resume",self.contract(args,unfinished=True))
        else:raise ValueError("当前工具未识别。可查看余额、预览订单、续办合同或运行终端自检。")

    def status_text(self,args,reverse):
        s=self.session
        if args.get("view")=="task":
            task=s.task
            return f"{task['message']}（{task['done']}/{task['total']}）" if task else "当前没有工作台任务。"
        acct=args.get("account")
        if acct and acct not in reverse:raise ValueError("请指定当前已连接的账号。")
        rows=[r for r in s.rows if (not acct or r["account"]==reverse[acct]) and
              (args.get("role") in (None,"","all") or r["role"]==args["role"])]
        if not rows:return "当前没有匹配的角色资料，可先同步资料。"
        lines=[]
        for r in rows:
            label=f"{r['account']} · {ROLE[r['role']]}"
            if args.get("view")!="todo":
                lines.append(f"{label}　{r['level']} 级，余额 {r['currency']} {r['cash']:,.2f}，库存金额 {r['stock']:,.2f}，贷款 {r['loan']:,.2f}。")
            if args.get("view") in ("todo","all"):
                lines.append(label+"待办："+"；".join(r["todo"] or ["暂无"])+"。")
        return "\n".join(lines)

    def propose(self,tool,target):
        s=self.session
        if s.task and s.task["status"]=="running":raise ValueError("当前业务尚在运行，完成后可提交下一项。")
        if tool=="start":
            orders=s.preview["orders"]
            details=quote_lines(orders)
            if any(j.get("closing_mail") for j in s.quoted):details.append("这份报价已选择每单结案后发送结案函。")
            title=f"按当前报价正式执行 {len(orders)} 笔交易"
            signature=target
        else:
            job=next(j for j in s.jobs if j["contract"]==target)
            title="续办合同 "+target
            details=[f"{job['product']}　{job['terms']}＋{job['payment']}　{STAGE.get(job['stage'],job['stage'])}",
                     job.get("resume_hint") or "将核对原合同的已办回执，再办理后续步骤。"]
            signature=(job["stage"],job["updated"])
        self.pending=dict(id=token_urlsafe(20),tool=tool,target=target,signature=signature,title=title,details=details)
        self.say("assistant","请核对下方确认单。确认后，工作台将使用当前学生账号正式办理。")

    def invalidate(self,tool,target=None):
        if self.pending and self.pending["tool"]==tool and (target is None or self.pending["target"]==target):
            self.pending=None
            self.say("assistant","之前的确认单已收起。可在对话中查看最新报价或合同进度。")

    def respond(self,body):
        pending=self.pending
        if not pending or body.get("id")!=pending["id"]:raise ValueError("这张确认单已失效。")
        choice=body.get("choice")
        if choice not in ("confirm","cancel"):raise ValueError("请选择确认或取消。")
        if choice=="cancel":
            self.pending=None
            self.say("assistant","已取消本次操作。")
            return
        s=self.session
        if s.task and s.task["status"]=="running":raise ValueError("当前业务仍在运行。")
        tool,target=pending["tool"],pending["target"]
        if tool=="start":
            current=(s.preview or {}).get("id")
            payload={"id":target}
        else:
            job=next((j for j in s.jobs if j["contract"]==target),{})
            current=(job.get("stage"),job.get("updated"))
            payload={"contract":target}
        self.pending=None
        if current!=pending["signature"]:
            self.say("assistant","报价或存档已更新，请重新选择当前这一份。")
            raise ValueError("原确认单已失效，未提交。")
        s.dispatch(tool,payload)
        self.watching[s.task["id"]]=(tool,"")
        self.say("tool","已收到确认，正在"+pending["title"]+"。",tool)

    def task_finished(self,task):
        watch=self.watching.pop(task["id"],None)
        if not watch:return
        tool,request=watch
        if task["status"]=="error":
            self.say("tool","本次操作暂停："+task["message"],tool)
        elif tool=="preview" and self.session.preview:
            lines=quote_lines(self.session.preview["orders"])
            self.say("tool","报价预览\n"+"\n".join(lines),tool)
            wants_trade=re.search(r"下单|签约|成交|执行交易|正式执行|完成交易",request)
            if wants_trade and not re.search(r"只(?:要|做)?(?:预览|测算|报价)|不要.{0,4}(?:执行|下单|签约)",request):
                self.propose("start",self.session.preview["id"])
        elif tool=="refresh":
            self.say("tool",f"已同步 {len(self.session.rows)} 个角色。最新余额和学校待办已显示在交易总览。",tool)
            followup=re.sub(r"同步|刷新|更新","",request)
            if re.search(r"待办|等级|余额|贷款|库存",followup):
                ctx,accounts,anon=self.context()
                decision=plan(anon(followup),[],ctx)
                if decision["tool"]=="status":
                    self.apply("status",decision["args"],"",accounts,None,request)
        else:
            lines=[x for x in task["logs"] if "利润" in x or "结案" in x]
            self.say("tool","\n".join(lines[-16:] or [task["message"]]),tool)
