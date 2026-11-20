"""Document presentation, bank forwarding and importer collection via student menus."""
import re
from html import unescape
from urllib.parse import parse_qs, quote, urljoin, urlsplit

from foreign_trade import (_action_url, _actions, _marks, _notes, _select_contract,
                           open_action, open_desk, TradeReview)
from live_platform import _menu_link
from settlement import _decimal, _entry, _guard, _ledger, _message, SettlementReview


_BANKS = {"export_bank": ("已送进口地银行", "成功发送单据给进口地银行"),
          "import_bank": ("已通知进口商", "成功通知进口商领取单据")}
_BANK_PAYMENTS = {"D/P", "D/A", "L/C"}


def _packet(case):
    if case["payment"] not in _BANK_PAYMENTS:
        raise TradeReview("银行交单需要 D/P、D/A 或 L/C 销货合同。")
    expected = ["M19", "M21", "M03B", "M01", "M42"]
    if case["terms"] == "CIF":
        expected.append("M26")
    packet = case.get("bank_packet", expected)
    if len(packet) != len(expected) or set(packet) != set(expected):
        raise TradeReview("银行单据清单与本合同的付款方式和术语不一致。")
    if not all(isinstance(case["documents"].get(k), str) and case["documents"][k] for k in packet):
        raise TradeReview("银行单据清单中的文号须逐项核实。")
    return packet


def _find_queue(session, case, role, category):
    """Filter the bank's own list and match both named parties in the contract row."""
    screen = session.get(_menu_link(session.home, "Bill.aspx"))
    choices = [o.get("value") for c in screen.controls if c.get("name") == "ddlType"
               for o in c.get("options", []) if o.get("text", "").strip() == category]
    if len(choices) != 1:
        raise TradeReview(f"银行单据页面缺少“{category}”查询选项。")
    choice = choices[0]
    if screen.fields.get("ddlType") != choice:
        screen = session.post(screen, "ddlType", {"ddlType": choice})
    page = session.get(_menu_link(screen, "BillOperate.aspx", no=choice))
    indexes = [o["value"] for c in page.controls if c.get("name") == "ctrPages:pages"
               for o in c.get("options", []) if o["value"] != page.fields.get("ctrPages:pages")]
    for idx in [None, *indexes]:
        if idx:
            page = session.post(page, "ctrPages$pages", {"ctrPages:pages": idx})
            if page.fields.get("ctrPages:pages") != idx:
                raise TradeReview("银行待审单据列表页码与请求不一致。")
        matches = [r for r in page.rows if len(r) >= 6 and r[2] == case["contract"]]
        if not matches:
            continue
        row = matches[0]
        parties = [v.split("(", 1)[0].strip() for v in row[3:5]]
        expected = ([case["seller_account"], case["import_bank"]] if role == "export_bank"
                    else [case["export_bank"], case["buyer_account"]])
        if len(matches) != 1 or parties != expected:
            raise TradeReview("银行单据中的合同、银行或交易对手与本轮不一致。")
        return page
    return None


def _document_checks(session, queue, case):
    ct = case["contract"]
    template = _menu_link(queue, "BillShow.aspx")
    if parse_qs(urlsplit(template).query, keep_blank_values=True).get("no") != [""]:
        raise TradeReview("银行页面未提供选中合同的单据明细入口。")
    url = template + quote(ct, safe="")
    page = session.get(url)
    expected = {case["documents"][t] for t in _packet(case)}
    if case["payment"] == "L/C":
        credit = case["documents"].get("M30")
        bank_form = {"export_bank": "M34", "import_bank": "M31"}[session.role]
        attachment = case["documents"].get(bank_form)
        if not credit or not attachment:
            raise TradeReview("银行审单还需核对该行信用证、通知书或开证申请书的文号。")
        expected.update((credit, attachment))
    listed = [r[2] for r in page.rows if len(r) >= 4 and r[1] == ct]
    events = sorted(e for e in page.events if re.fullmatch(r"oContractList\$_ctl\d+\$lbtn1", e))
    if set(listed) != expected or len(listed) != len(expected) or len(events) != len(expected):
        raise TradeReview("银行收到的单据编号或检查按钮与出口商交单清单不完全一致。")
    for event in events:
        response = session.post(session.get(url), event)
        links = set(re.findall(r"""showModalDialog\(\s*['"]([^'"]+)['"]""",
                               "\n".join(response.scripts), re.I))
        if len(links) != 1:
            raise TradeReview("银行审单的结果入口不明确。")
        target = urljoin(response.url, unescape(links.pop()))
        if not urlsplit(target).path.lower().endswith("/checkresult.aspx"):
            raise TradeReview("审单窗口未返回学生端单据检查结果。")
        result = session.get(target)
        if "100%" not in result.text and "未发现错误" not in result.text:
            raise TradeReview("银行审单发现单据不符，请按当前学生端检查结果修正。")
    return sorted(expected)


def _received(session, case, bank_role):
    states = ([_BANKS[bank_role][0]] if bank_role == "export_bank" else
              ["已通知进口商", "进口商取回单据"])
    return next((state for state in states if _find_queue(session, case, bank_role, state)), None)


def _present(session, case):
    ct = case["contract"]
    desk = open_desk(session, "E10")
    _select_contract(session, desk, ct)
    op, expected = ("E1001", "成功押汇") if case["payment"] == "L/C" else ("E1002", "成功办理托收")
    actions = [label for url, _, _, _, label in _actions(desk)
               if urlsplit(url).path.lower().endswith("/notelist.aspx")
               and parse_qs(urlsplit(url).query).get("op") == [op]]
    if len(actions) != 1:
        raise TradeReview("出口地银行没有提供本合同对应的托收或押汇入口。")
    page = session.get(_action_url(desk, actions[0], ct))
    rows = _notes(page)
    marks = []
    for typ in _packet(case):
        row = rows.get(typ)
        if not row or row[2] != case["documents"][typ]:
            raise TradeReview("提交窗口的单据和已核文号不一致。")
        marks.append(row[0])
    if set(marks) - set(_marks(page, "checkbox")):
        raise TradeReview("银行交单窗口没有提供所需的全部勾选项。")
    reply = session.post(page, "_ctl0:btnOk", {"flag": ";".join(marks) + ";", "mark": marks})
    return _message(reply, expected)


def present_bank_documents(platform, rec):
    """Called after shipment and 100% form checks. Limit operations to the existing sale."""
    case = rec.case
    _packet(case)
    if "bank_presentation" in rec.data["steps"]:
        return rec.data["steps"]["bank_presentation"]
    def already_presented():
        bank = platform.session(case["export_bank"], "export_bank")
        for label in ("等待处理", "已送进口地银行"):
            if _find_queue(bank, case, "export_bank", label):
                return f"出口地银行记录已核对（{label}）"
        return None
    # Do the bank lookup before recording a submission intent, so a failed read can
    # be retried without assuming that the seller sent anything.
    already = already_presented()
    return rec.run("bank_presentation",
                   lambda: already or _present(platform.session(case["seller_account"], "exporter"), case),
                   recover=lambda: already)


def forward_bank_documents(platform, case, journal):
    """Check every submitted form, forward using the two designated bank accounts."""
    for role, (_, success) in _BANKS.items():
        key = "forward_" + role
        if journal.done(key):
            continue
        session = platform.session(case[role], role)
        def reconciled():
            return _received(session, case, role)
        queue = _find_queue(session, case, role, "等待处理")
        if queue is None:
            evidence = reconciled()
            if not evidence:
                raise SettlementReview(f"{'出口地' if role=='export_bank' else '进口地'}银行尚无本合同交单记录。")
            journal.run(key, lambda: evidence, reconciled)
            continue
        # A failed read-only check remains correctable before recording a transfer intent.
        ids = _document_checks(session, queue, case)
        fresh = _find_queue(session, case, role, "等待处理")
        if not fresh or case["contract"] not in _marks(fresh, "radio"):
            raise SettlementReview("审单后待处理队列发生变化，请重新核对。")
        def act():
            reply = session.post(fresh, "btnFF", {"flag": case["contract"], "mark": case["contract"]})
            return {"receipt": _message(reply, success), "checked_documents": ids}
        journal.run(key, act, reconciled)
    return "buyer_notified"


def _buyer_paid(session, case, budget):
    return _entry(_ledger(session, case["contract"]), case["payment"] + "货款",
                  "expense", (case["buyer"]["currency"], budget["importer"]["M5007"]))


def _taken(session, case):
    return "进口商取回单据" if _find_queue(session, case, "import_bank", "进口商取回单据") else None


def _pay(buyer, case, budget, journal, deferred):
    if not journal.done("payment"):
        old = _buyer_paid(buyer, case, budget)
        if old:
            if "payment" not in journal.data["steps"]:
                journal.data["facts"]["payment_verified_without_submission_time"] = True
                journal.save()
            journal.run("payment", lambda: old, lambda: old)
        else:
            if deferred:
                required = _decimal(budget["importer"]["M5007"]) + _decimal(budget["importer"]["M5017"])
            else:
                excluded = {"M5008", "M5016"}  # insurance is issued before releasing bank documents
                required = sum(_decimal(v) for k, v in budget["importer"].items() if k not in excluded)
            _guard(buyer, case, case["buyer"]["currency"], required)
            def pay():
                page = open_action(buyer, case["contract"], "I6", "付款")
                if deferred and any("未到期" in x or "未到付款" in x for x in (page.text, *page.messages)):
                    if not _buyer_paid(buyer, case, budget):
                        journal.data["steps"].pop("payment", None)
                        journal.save()
                    raise SettlementReview("学生端提示尚未到付款期，请届时从原合同续办。")
                return _message(page, "成功付款")
            journal.run("payment", pay, lambda: _buyer_paid(buyer, case, budget))
    row = _buyer_paid(buyer, case, budget)
    if not row:
        raise SettlementReview("尚未核到本合同准确金额、币种与方向的进口付款本金。")
    journal.data["facts"]["payment_principal"] = row
    journal.save()
    case["payment_done"] = True


def collect_documents(platform, case, budget, journal):
    """Pay at sight or accept the time draft, then release documents to the buyer."""
    buyer = platform.session(case["buyer_account"], "importer")
    bank = platform.session(case["import_bank"], "import_bank")
    if case["payment"] != "D/A":
        _pay(buyer, case, budget, journal, False)
    else:
        def accept():
            taken = _taken(bank, case)
            if taken:
                return taken
            return _message(open_action(buyer, case["contract"], "I6", "承兑"), "成功办理承兑")
        journal.run("draft_acceptance", accept, lambda: _taken(bank, case))
        case["draft_accepted"] = True
    def take():
        return _taken(bank, case) or _message(open_action(buyer, case["contract"], "I6", "取回单据"), "成功取回单据")
    journal.run("collect_documents", take, lambda: _taken(bank, case))
    case["documents_received"] = True
    return "documents_received"


def pay_deferred(platform, case, budget, journal):
    """D/A settles after sale or an insured loss; defer to the school's due-date checks."""
    if case["payment"] != "D/A":
        return
    if not case.get("draft_accepted") or not (case.get("sale_done") or case.get("claim_settled")):
        raise SettlementReview("D/A 等待已承兑且已销售或已理赔的凭证。")
    _pay(platform.session(case["buyer_account"], "importer"), case, budget, journal, True)
    return "paid"

