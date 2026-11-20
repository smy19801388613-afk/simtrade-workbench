"""Read-only workbench commands for the Agent, run in a separate local process."""
from contextlib import redirect_stdout
from decimal import Decimal
from html.parser import HTMLParser
from io import StringIO
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from pipeline import accounts_for, read
from results import summarize
from trade_modes import AUTOMATIC_PAIRS, PAYMENTS, TERMS
from workbench import LABEL, STAGES


TOOLS = {"terminal_check", "terminal_audit"}
_SCOPE_ERROR = "当前学校与账号组合下没有这份合同存档。"


class _Assets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.files = []

    def handle_starttag(self, tag, attrs):
        fields = dict(attrs)
        if tag == "script" and fields.get("src"):
            self.files.append(fields["src"])
        elif tag == "link" and fields.get("rel") == "stylesheet":
            self.files.append(fields["href"])


def _check():
    from handoff import SKILL, recover
    from try_handoff import DraftExampleHost, scripted_answer

    ui = Path(__file__).resolve().parent / "ui"
    if not (ui / "index.html").is_file():
        raise ValueError("工作台缺少界面入口，请重新完整解压安装包。")
    assets = _Assets()
    assets.feed((ui / "index.html").read_text("utf-8"))
    if not assets.files:
        raise ValueError("界面入口没有列出需要加载的本地文件。")
    for name in assets.files:
        path = (ui / name).resolve()
        if not path.is_relative_to(ui) or not path.is_file() or not path.stat().st_size:
            raise ValueError("工作台的页面资源不完整，请重新完整解压安装包。")
    if not (SKILL / "SKILL.md").is_file():
        raise ValueError("缺少工作台操作手册，请重新完整解压安装包。")
    if set(AUTOMATIC_PAIRS) != {(t,p) for t in TERMS for p in PAYMENTS}:
        raise ValueError("交易路线表覆盖不完整。")
    with redirect_stdout(StringIO()):
        status, _ = recover(DraftExampleHost(), scripted_answer)
    if status != "resume_rules":
        raise ValueError("离线单据恢复自检未通过。")
    return (f"本机终端自检通过。界面入口及 {len(assets.files)} 个页面文件可读取，"
            f"操作手册已就位，{len(AUTOMATIC_PAIRS)} 种交易组合已加载，离线单据恢复验证通过。")


def _pause_note(job):
    reason = (job.get("attention") or {}).get("message")
    if not reason:
        return ""
    # A remote error can include an entire form or URL. Report the relevant
    # business category, keeping raw school responses inside the job archive.
    for words, hint in (
        (("资金","余额","现金"), "资金校验需要复核。请检查相应角色的可用余额和保留额。"),
        (("回执","流水"), "业务回执或流水仍待核对。"),
        (("信用证","开证"), "信用证资料或银行办理进度需要复核。"),
        (("单据","表单","凭证"), "平台单据需要复核。"),
        (("行情","价格","报价"), "当前报价或行情需要复核。"),
        (("超时","网络","连接"), "连接中断，请核对学校平台可用性。"),
        (("到期","付款期"), "请核对合同约定的付款日期。"),
    ):
        if any(word in str(reason) for word in words):
            return "上次暂停原因：" + hint
    return "本单上次办理已暂停，可在合同存档查看原始提示。"


def _audit(data, base, accounts, contract):
    if (not isinstance(accounts, list) or not 1 <= len(accounts) <= 3
            or not all(isinstance(a,str) and re.fullmatch(r"[A-Za-z0-9_-]{1,40}",a) for a in accounts)
            or len(set(accounts)) != len(accounts) or not isinstance(base,str) or not base):
        raise ValueError("请先连接本次交易所用的学校与账号组合。")
    if not isinstance(contract,str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,60}",contract):
        raise ValueError("请选择存档中的有效合同号。")
    if not isinstance(data,str) or not Path(data).is_absolute():
        raise ValueError("未找到当前工作台的数据目录。")
    root = Path(data).resolve()
    path = root / contract / "job.json"
    if path.resolve() != path or not path.is_file():
        raise ValueError(_SCOPE_ERROR)
    job = read(path)
    case = job["case"]
    scope = set(accounts)
    if job.get("origin") != base or set(job.get("accounts",[])) != scope or case.get("contract") != contract:
        raise ValueError(_SCOPE_ERROR)
    stage = job.get("stage")
    lines = [f"合同 {contract} · {STAGES.get(stage, '进度待核')}"]
    if job.get("kind") == "purchase":
        supply = job["supply"]
        actors = supply["roles"]
        if (supply.get("contract") != contract or supply.get("origin") != base
                or set(supply.get("accounts", [])) != scope or set(actors.values()) - scope):
            raise ValueError(_SCOPE_ERROR)
        result = job.get("result")
        if result and result.get("status") == "completed":
            if result.get("contract") != contract or supply["order"].get("contract") != contract:
                raise ValueError("采购单号与其结算凭证不一致。")
            # Reuse the standard ledger checks for the supplier, including
            # verification of VAT, expenses, production cost and the saved profit.
            partial = {"case":{"contract":contract,"domestic":{"contract":contract,
                        "factory_account":actors["factory"],
                        "production_unit_cost_rmb":supply["order"]["expected_unit_cost_rmb"]}},
                       "domestic_result":result, "opening":{"factory":supply["opening"]["factory"]}}
            profit = summarize(partial)["roles"]["factory"]["actual_profit"]
            cash_delta = Decimal(result["factory_cash_after"]) - Decimal(result["factory_cash_before"])
            if profit is None:
                raise ValueError("工厂的采购结算回执尚不足以核平利润。")
            lines.append(f"工厂按流水核算的利润 RMB {profit}，钱包变动 RMB {cash_delta:+,.2f}。")
            lines.append(f"出口商采购支出 RMB {Decimal(result['buyer_cash_spent_rmb']):,.2f}，已收货 {int(result['buyer_stock_received'])} 件。")
    else:
        if set(accounts_for(case).values()) - scope:
            raise ValueError(_SCOPE_ERROR)
        report = summarize(job)
        if case.get("terms") in TERMS and case.get("payment") in PAYMENTS:
            lines[0] += f" · {case['terms']} ＋ {case['payment']}"
        for role, row in report["roles"].items():
            currency = row["currency"]
            if currency is not None and not re.fullmatch(r"[A-Z]{3}",currency):
                raise ValueError("账本币种待核。")
            profit = row["actual_profit"]
            delta = row["cash"]["change"]
            if profit is not None or delta is not None:
                money = f"按流水核算的利润 {currency} {profit}" if profit is not None else "合同利润待核"
                wallet = f"，钱包变动 {currency} {Decimal(delta):+,.2f}" if delta is not None else ""
                lines.append(f"{LABEL[role]}{money}{wallet}。")
        if report["branch"] == "insured_loss":
            lines.append("本合同按实际到账的保险赔款核算。")
    if len(lines) == 1:
        lines.append("当前存档尚缺能够核平收支的凭证。")
    note = _pause_note(job)
    if note:
        lines.append(note)
    return "\n".join(lines)


def terminal_main(argv):
    """The internal CLI writes exactly one UTF-8 JSON reply to its stdout pipe."""
    try:
        if len(argv) != 1 or argv[0] not in TOOLS:
            raise ValueError("请选择工作台提供的终端工具。")
        if argv[0] == "terminal_check":
            text = _check()
        else:
            payload = json.loads(sys.stdin.buffer.read(16385).decode("utf-8"))
            text = _audit(payload["data"],payload["base"],payload["accounts"],payload["contract"])
        reply = {"ok":True,"text":text}
    except (OSError, ImportError, UnicodeError, KeyError, TypeError, AttributeError, ArithmeticError, json.JSONDecodeError):
        reply = {"ok":False,"text":"终端无法读取所需的程序文件或合同凭证，请核对安装包及原存档。"}
    except ValueError as exc:
        reply = {"ok":False,"text":str(exc)}
    sys.stdout.buffer.write(json.dumps(reply,ensure_ascii=False).encode("utf-8")+b"\n")
    return 0 if reply["ok"] else 1


def run_terminal(tool, *, data, base, accounts, contract=None):
    """Run only a built-in business command, passing the trusted session scope over stdin."""
    if tool not in TOOLS:
        raise ValueError("工作台没有这个终端工具。")
    executable = [sys.executable]
    if not getattr(sys,"frozen",False):
        executable += ["-E","-s","-B","-X","utf8",str(Path(__file__).with_name("desktop.py").resolve())]
    command = [*executable, "--agent-tool", tool]
    payload = {} if tool=="terminal_check" else {
        "data":str(Path(data).resolve()),"base":base,"accounts":list(accounts),"contract":contract}
    try:
        result = subprocess.run(command, input=json.dumps(payload).encode("utf-8"), capture_output=True,
                                timeout=20, creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0), shell=False)
        reply = json.loads(result.stdout.decode("utf-8"))
    except subprocess.TimeoutExpired:
        raise ValueError("本机终端检查超时，子进程已终止。") from None
    except (OSError, ValueError):
        raise ValueError("本机终端未返回有效的检查结果，请确认正在使用完整的工作台版本。") from None
    if not isinstance(reply,dict) or not isinstance(reply.get("text"),str):
        raise ValueError("本机终端返回了无法识别的结果。")
    if result.returncode or reply.get("ok") is not True:
        raise ValueError(reply["text"])
    return reply["text"]
