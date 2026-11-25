"""Ocean T/T delivery and settlement via importer and exporter student menus."""
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from urllib.parse import urlencode

from import_declaration import customs, inspection
from market import import_inspection_required
from student_document import read_document, save_document


class SettlementReview(RuntimeError):
    pass


def _now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _decimal(value):
    number = Decimal(str(value).replace(",", ""))
    if not number.is_finite() or number < 0:
        raise SettlementReview("金额或数量无法核对。")
    return number


@contextmanager
def _exclusive(path):
    """Nonblocking OS lock; released automatically if the process exits."""
    with open(path, "a+b") as stream:
        stream.seek(0, 2)
        if not stream.tell():
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise SettlementReview("此合同已在另一进程收尾。") from None
        else:
            import fcntl
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SettlementReview("此合同已在另一进程收尾。") from None
        yield


class _Journal:
    def __init__(self, path, case):
        self.path = path
        identity = {k: str(case[k]) for k in ("contract", "seller_account", "buyer_account", "terms", "payment")}
        self.data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {
            "identity": identity, "steps": {}, "facts": {}}
        if self.data["identity"] != identity:
            raise SettlementReview("收尾记录的合同或交易双方不匹配。")

    def save(self):
        with NamedTemporaryFile(mode="w", suffix=".tmp", dir=self.path.parent, encoding="utf-8",
                               delete=False) as tmp:
            json.dump(self.data, tmp, ensure_ascii=False, indent=2)
            tmp.flush()
            os.fsync(tmp.fileno())
        try:
            os.replace(tmp.name, self.path)
        finally:
            Path(tmp.name).unlink(missing_ok=True)

    def done(self, step):
        return self.data["steps"].get(step, {}).get("status") == "verified"

    def run(self, step, action, reconcile=None):
        steps = self.data["steps"]
        old = steps.get(step)
        if self.done(step):
            return old["receipt"]
        if old:
            result = reconcile() if reconcile else None
            if not result:
                raise SettlementReview(f"{step} 已提交或中断，尚无可独立核对的成功凭证，请核对平台记录。")
        else:
            steps[step] = {"status": "pending", "started_at": _now()}
            self.save()
            result = action()
            if not result:
                raise SettlementReview(f"{step} 缺少回执。")
        steps[step].update(status="verified", verified_at=_now(), receipt=result)
        self.save()
        return result


def _messages(page):
    texts = [page.text, *page.messages]
    for script in page.scripts:
        texts.extend(re.findall(r"""alert\(\s*["']([^"']+)["']\s*\)""", script))
    return list(dict.fromkeys(texts))


def _message(page, expected):
    texts = _messages(page)
    if not any(expected in value for value in texts):
        raise SettlementReview("平台未返回预期回执：" + " ".join(texts)[:240])
    return expected


_LOSS_EVENTS = (
    "运输船只在海上意外触礁，连同货物一起沉没，无法办理相关进口手续",
    "在运输过程中遭遇船员罢工暴动，造成货物毁损", "在运输过程中受潮，造成货物受损",
    "运输船只若干天前在海上突然行踪不明，至今失去音讯已逾法定时间，可确认为失踪",
    "在装卸过程中由于意外情况，全部落海", "运输船只在海上发生爆炸，船货俱损，事故原因不明",
    "在运输过程中被全部偷走，不能抵达目的港",
)
_LOSS_RECEIPT = re.compile(
    r"(?P<notice>本批货物(?:" + "|".join(map(re.escape, _LOSS_EVENTS)) +
    r")[，,]\s*请(?:速|尽快|立即)?向保险公司(?:申请|办理)索赔(?:手续)?[。！!]?)(?:\s+(?P=notice))*"
)
_CLAIMS_REQUIRING_DOCUMENT_CHECK = {
    "本批货物在运输过程中混进了杂质，货物被沾污受损，请速向保险公司办理索赔手续。",
    "本批货物运输船只在运输过程中发生火灾，货物遭到毁损，请速向保险公司办理索赔手续。",
}


def _claim_notice(text):
    # These exact school notices have been observed on the no-delivery branch.
    # A partial loss or a conditional suggestion can also mention a claim.
    text = text.removeprefix("平台未返回预期回执：").strip().removeprefix("提示信息").strip()
    copies = set(text.split())
    if len(copies) == 1 and copies <= _CLAIMS_REQUIRING_DOCUMENT_CHECK:
        return copies.pop()
    match = _LOSS_RECEIPT.fullmatch(text)
    return match["notice"] if match else None


def _delivery_review(journal, text):
    journal.data["facts"]["delivery_review"] = text[:500]
    journal.save()
    raise SettlementReview("学校换单回执需要核对货损范围和可提货数量：" + text[:500])


def _restore_delivery_notice(journal, previous_attention):
    # v1.3.0 persisted the school's reply in job.json, but had already marked
    # the exchange pending. Migrate it before a later retry replaces attention.
    step = journal.data["steps"].get("delivery_order", {})
    if (step.get("status") != "pending" or journal.data["facts"].get("delivery_review")
            or not previous_attention or previous_attention.get("stage") != "shipped"):
        return
    message = previous_attention.get("message", "")
    notice = _claim_notice(message) if message.startswith("平台未返回预期回执：") else None
    if notice in _CLAIMS_REQUIRING_DOCUMENT_CHECK:
        journal.data["facts"]["delivery_review"] = message
        journal.save()
    elif notice:
        journal.run("delivery_order", None, lambda: {"result": "claim_notice", "notice": notice})
    elif message.startswith("平台未返回预期回执：") and "索赔" in message:
        _delivery_review(journal, message)


def _ledger(session, ct=None):
    code = "1" if session.role == "exporter" else "2"
    session.get("Common/Finance.aspx?cls=" + code)
    page = session.get("Common/Finance_Dialog.aspx")
    select = next((item for item in page.controls if item.get("name") == "ctrPages:pages"), {})
    rows = []
    for n in [None] + [o["value"] for o in select.get("options", []) if o["value"] != "1"]:
        if n:
            page = session.post(page, "ctrPages$pages", {"ctrPages:pages": n})
            if page.fields.get("ctrPages:pages") != n:
                raise SettlementReview("日记账翻页回读不一致。")
        rows.extend({"at": r[0], "income": r[1], "expense": r[2], "contract": r[3], "item": r[4]}
                    for r in page.rows if len(r) == 5 and re.match(r"^\d{4}[-/]\d", r[0])
                    and (ct is None or r[3] == ct))
    return rows


def _entry(ledger, label, side, expected=None):
    rows = [r for r in ledger if r["item"] == label]
    if len(rows) > 1:
        raise SettlementReview(f"日记账中有多条 {label}，需要核对。")
    if not rows:
        return None
    row = rows[0]
    other = "expense" if side == "income" else "income"
    if not row[side] or row[other]:
        raise SettlementReview(f"日记账 {label} 的收支方向不匹配。")
    if expected:
        currency, amount = expected
        parts = row[side].split()
        if len(parts) != 2 or parts[0] != currency or _decimal(parts[1]) != _decimal(amount):
            raise SettlementReview(f"合同日记账 {label} 本金或币种未核平：{row[side]}，应为 {currency} {amount}。")
    return row


CLAIM_ITEMS = {"货物保险金", "保险赔款", "保险赔付", "保险赔偿金"}


def _claim_credit(rows, case):
    hits = [r for r in rows if r.get("contract") == case["contract"] and r["item"] in CLAIM_ITEMS]
    if not hits:
        return None
    if len(hits) != 1:
        raise SettlementReview("同一合同有多条保险赔款流水，请核对。")
    row = hits[0]
    money = row["income"].split()
    if row["expense"] or len(money) != 2 or money[0] != case["buyer"]["currency"] or _decimal(money[1]) <= 0:
        raise SettlementReview("保险赔款流水的币种、收支方向或金额不符。")
    return row


def _delivery_result(buyer, case, journal):
    from foreign_trade import _list_page, _notes, note, open_action
    ct = case["contract"]

    def claim_receipt(notice):
        if notice in _CLAIMS_REQUIRING_DOCUMENT_CHECK:
            facts = journal.data["facts"]
            if "delivery_review" not in facts:
                # Persist the school reply before querying documents, so a failed
                # lookup can be retried without repeating the exchange action.
                facts["delivery_review"] = notice
                journal.save()
            docs = _notes(_list_page(buyer, ct, "I2"))
            policy = docs.get("M26")
            if "M41" in docs or not policy or policy[2] != case["documents"].get("M26"):
                raise SettlementReview("货损回执与当前单据尚未核平，请核对保单及是否已签发提货单。")
            del facts["delivery_review"]
        return {"result": "claim_notice", "notice": notice}

    def exchange():
        page = open_action(buyer, ct, "I3", "换提货单")
        texts = [body for t in _messages(page) if (body := t.strip().removeprefix("提示信息").strip())]
        if texts and all(re.fullmatch(r"成功换取提货单[！!。]?", t) for t in texts):
            return {"result": "delivery_order", "notice": "成功换取提货单"}
        notices = {_claim_notice(t) for t in texts}
        if len(notices) == 1 and None not in notices:
            return claim_receipt(notices.pop())
        _delivery_review(journal, " ".join(texts) or "平台返回空回执")

    def reconcile():
        paid = _claim_credit(_ledger(buyer, ct), case)
        if paid:
            raise SettlementReview("本合同已有保险赔款，但原换单回执缺失，请核对是否还有货物可提。")
        if "M41" in _notes(_list_page(buyer, ct, "I2")):
            order = read_document(buyer, note(buyer, ct, "I2", "M41"))[0]
            if (order.tag == "M41" and order.findtext("M4106") == ct and
                    order.findtext("M4114") == case["documents"]["M01"] and order.findtext("M4101")):
                return {"result": "delivery_order", "notice": "已核实与合同和提单相符的提货单"}
        raise SettlementReview("换提货单曾中断，尚无可核实的本合同事故通知、提货单或保险赔款。请核对进口商的换单回执。")

    review = journal.data["facts"].get("delivery_review")
    if review:
        step = journal.data["steps"].get("delivery_order", {})
        notice = _claim_notice(review)
        if notice in _CLAIMS_REQUIRING_DOCUMENT_CHECK:
            if step.get("status") == "pending":
                return journal.run("delivery_order", None, lambda: claim_receipt(notice))
            # An older workbench may flag a receipt already verified by a newer
            # one. Clear only an identical notice after checking the documents.
            previous = step.get("receipt")
            if (step.get("status") == "verified" and isinstance(previous, dict)
                    and previous.get("result") == "claim_notice" and isinstance(previous.get("notice"), str)
                    and _claim_notice(previous["notice"]) == notice):
                confirmed = claim_receipt(notice)
                journal.save()
                return confirmed
        raise SettlementReview("学校换单回执需要核对货损范围和可提货数量：" + review)
    result = journal.run("delivery_order", exchange, reconcile)
    if result["result"] == "claim_notice" and not _claim_notice(result["notice"]):
        _delivery_review(journal, result["notice"])
    return result


def _settle_claim(buyer, case, journal, *, before_claim=None):
    from foreign_trade import open_action
    ct = case["contract"]

    def credit():
        return _claim_credit(_ledger(buyer, ct), case)

    unsubmitted = "claim" not in journal.data["steps"]
    existing = credit() if unsubmitted else None
    # The exchange receipt is durable. An observer may pause here without
    # leaving a pending intent for an insurance request that was never sent.
    if before_claim is not None and unsubmitted and existing is None:
        before_claim(case, journal)

    def claim():
        return existing or _message(open_action(buyer, ct, "I8", "保险索赔"), "成功办理索赔")

    receipt = journal.run("claim", claim, credit)
    # A success page alone cannot prove the amount or currency received.
    paid = receipt if isinstance(receipt, dict) else credit()
    if not paid:
        raise SettlementReview("已收到办理索赔的回执，尚未查到本合同保险赔款入账。稍后续办可重新核对。")
    journal.data["facts"]["claim_credit"] = paid
    journal.save()


def _sale_rows(rows, case):
    product = case["product"]
    matches = []
    for row in rows:
        m = re.fullmatch(r"销货\s*[,，]\s*商品编号\s*([^,，\s]+)\s*[,，]\s*数量\s*([\d.]+)\s*(\S+)", row["item"])
        if (m and m[1] == str(product["id"]) and _decimal(m[2]) == _decimal(product["quantity"])
                and m[3].upper() == product["unit"].upper() and row.get("contract") in ("", case["contract"])):
            matches.append(row)
    return matches


def audit_role_ledger(rows, role, case, budget, steps, facts):
    """Bind principal to budget and market proceeds to the exact goods and sale operation."""
    contract = [r for r in rows if r.get("contract", case["contract"]) == case["contract"]]
    currency = case["buyer"]["currency"] if role == "importer" else "RMB"
    side = "expense" if role == "importer" else "income"
    principal = _entry(contract, case.get("payment","T/T")+"货款", side, (currency, budget[role]["M5007"]))
    if not principal:
        raise SettlementReview("未查到与合同结算方式、币种和金额匹配的收付本金。")
    out = {"principal": principal}
    if role != "importer":
        return out
    if case.get("claim_settled"):
        credit = _claim_credit(rows, case)
        if not credit:
            raise SettlementReview("结案核对未找到本合同的保险赔款收入。")
        return dict(out, claim_credit=credit)
    sale = steps["sale"]
    if sale["status"] != "verified":
        raise SettlementReview("销货操作尚无成功回执。")
    matches = _sale_rows(rows, case)
    if "sale_before" in facts:
        # Subtract as a multiset since the server displays time only to the second.
        if not isinstance(facts["sale_before"], list):
            raise SettlementReview("销货前流水快照格式有误，请先核对存档。")
        for old in facts["sale_before"]:
            if old not in matches:
                raise SettlementReview("销货前已记录的同商品流水不在当前完整日记账中。")
            matches.remove(old)
    if facts.get("payment_verified_without_submission_time"):
        if "sale_before" not in facts:
            raise SettlementReview("手动付款缺少提交时间；需完整的销货前流水快照才能核对销售收入。")
        window = {"basis": "已核实货款；以销货前后流水差分及商品、数量、单位核实唯一新增收入"}
    else:
        # The matching payment maps the workbench clock onto the platform's clock.
        def host_time(value):
            return datetime.fromisoformat(value).astimezone(timezone.utc).replace(tzinfo=None)
        server_paid = datetime.fromisoformat(principal["at"])
        payment = steps["payment"]
        granularity = timedelta(seconds=1)
        lo = host_time(sale["started_at"]) + (server_paid - host_time(payment["verified_at"])) - granularity
        hi = host_time(sale["verified_at"]) + (server_paid - host_time(payment["started_at"])) + granularity
        matches = [r for r in matches if lo <= datetime.fromisoformat(r["at"]) <= hi]
        window = {"from": lo.isoformat(sep=" "), "to": hi.isoformat(sep=" "),
                  "basis": "付款校时、商品/数量/单位、销货前后流水差分" if "sale_before" in facts
                           else "付款校时、商品/数量/单位、销货成功回执时间"}
    if len(matches) != 1:
        raise SettlementReview("销货流水中无法按商品、数量及单位唯一匹配本笔收入。")
    row = matches[0]
    amount = row["income"].split()
    if row["expense"] or len(amount) != 2 or amount[0] != currency or _decimal(amount[1]) <= 0:
        raise SettlementReview("销货日记账方向、币种或金额不匹配。")
    return dict(out, sale=row, sale_window=window)


def _guard(session, case, expected, spend):
    page = session.get("Common/Finance.aspx?cls=" + ("2" if session.role == "importer" else "1"))
    wallet = page.fields.get("B0342", "").split()
    if len(wallet) != 2 or wallet[0] != expected:
        raise SettlementReview("财务页本币与交易预算不一致。")
    floor = case.get("cash_floor", 0)
    if isinstance(floor, dict):
        floor = floor.get(session.role, floor.get("buyer" if session.role == "importer" else "seller",
                                                  floor.get(session.account, 0)))
        if isinstance(floor, dict):
            floor = floor.get(session.role, 0)
    if _decimal(wallet[1]) < _decimal(floor) + _decimal(spend):
        raise SettlementReview("当前可用余额不足以覆盖该阶段预算和资金保留额。")


def _safe_form(session, case, direction):
    ct = case["contract"]
    prefix = "Common/safesvc/safesvc-"
    session.get(prefix + "Log.aspx")
    session.get(prefix + "2.aspx")
    listing = session.get(prefix + direction + "-1.aspx")
    matches = [row for row in listing.rows if ct in row and
               any(re.fullmatch(r"\d+N\d+", value) for value in row)]
    if len(matches) != 1:
        raise SettlementReview("外汇申报清单无法唯一匹配本合同。")
    number = next(x for x in matches[0] if re.fullmatch(r"\d+N\d+", x))
    form = session.get(prefix + direction + "-2.aspx?" + urlencode({"M0701": ct, "ShenbaoNo": number}))
    amount = form.fields.get("txtM0718")
    if _decimal(amount) != _decimal(case["amount_usd"]) or form.fields.get("txtM0717") != case["currency"]:
        raise SettlementReview("外汇申报金额或币种与合同不一致。")
    if direction == "j":
        fields = {"txtM0704": case["ship"]["eta"], "P02": "1", "txtM0718B": amount}
        expected = "成功办理外汇监测系统网上申报"
    else:
        fields = {"txtcountry": case["destination_country_en"], "Trad": "radio1", "selbianma": "101010",
                  "txtMoney2": amount, "txtfuyan": "一般贸易收入"}
        expected = "成功办理国际收支网上申报"
    return form, fields, expected


def _declare_safe(journal, session, case, direction):
    step = "import_safe" if direction == "j" else "export_safe"
    if journal.done(step):
        return
    form, fields, expected = _safe_form(session, case, direction)
    journal.run(step, lambda: _message(session.post(form, "Button1", fields), expected))


def _inspect_import(buyer, case, budget, journal):
    from foreign_trade import _list_page, _notes, add_note, note, prepare_doc_submission
    ct = case["contract"]

    def existing_id():
        row = _notes(_list_page(buyer, ct, "I2")).get("M17B")
        return row[2] if row else None

    case["documents"]["M17B"] = journal.run("create_inspection", lambda: add_note(buyer, ct, "M17B"), existing_id)
    if not journal.done("save_inspection"):
        # Keep the form's application date stable when resuming a previous submission.
        case["inspection_date"] = journal.data["facts"].setdefault("inspection_date", date.today().isoformat())
        journal.save()
        draft = inspection(case)
        def checked():
            return _message(note(buyer, ct, "I2", "M17B", check=True), "100%")
        def save():
            save_document(buyer, note(buyer, ct, "I2", "M17B"), **draft)
            return checked()
        def recover_draft():
            report = note(buyer, ct, "I2", "M17B", check=True)
            if "100%" in report.text:
                return "100%"
            return save()
        journal.run("save_inspection", save, recover_draft)
    if not journal.done("import_inspection"):
        currency = case["buyer"]["currency"]
        fee = budget["importer"]["M5010"]
        def receipt():
            return _entry(_ledger(buyer, ct), "检验费", "expense", (currency, fee))
        submit = None
        if "import_inspection" not in journal.data["steps"]:
            _guard(buyer, case, currency, fee)
            post = prepare_doc_submission(buyer, ct, "I4", ["M07", "M19", "M21", "M41", "M17B"])
            submit = lambda: _message(post(), "成功办理检验")
        journal.run("import_inspection", submit, receipt)


def _complete(platform, case, budget, journal, *, before_claim=None):
    from foreign_trade import add_note, note, open_action, send_docs
    ct = case["contract"]
    buyer = platform.session(case["buyer_account"], "importer")
    seller = platform.session(case["seller_account"], "exporter")
    product = case["product"]
    # Load the actual menus for fresh sessions, including the SAFE screens.
    buyer.get("Import/MapC.aspx")
    seller.get("Export/MapA.aspx")
    if case["insurance"].get("status") != "issued":
        raise SettlementReview("合同约定的经办方尚无已签发保单。")
    def principal(session, side):
        currency = case["buyer"]["currency"] if session.role == "importer" else "RMB"
        return _entry(_ledger(session, ct), case["payment"]+"货款", side, (currency, budget[session.role]["M5007"]))

    if case["payment"]!="T/T":
        from collection_workflow import forward_bank_documents, collect_documents
        forward_bank_documents(platform,case,journal)
        collect_documents(platform,case,budget,journal)
    elif not journal.done("payment"):
        # All importer fees are in bookkeeping currency; M5008 is CIF valuation.
        # For CFR the importer's premium was paid on issuance, before this step.
        exclude = {"M5008"}
        if case["terms"] in ("CFR","FOB"):exclude.add("M5016")
        spend = sum(_decimal(v) for k, v in budget["importer"].items() if k not in exclude)
        if "payment" not in journal.data["steps"]:
            _guard(buyer, case, case["buyer"]["currency"], spend)
            if principal(buyer, "expense"):
                raise SettlementReview("提交前已查到本合同的货款支出，请核对原付款记录。")
        journal.run("payment", lambda: _message(open_action(buyer, ct, "I6", "付款"), "成功付款"),
                    lambda: principal(buyer, "expense"))
    def confirm_payment():
        if "payment_principal" not in journal.data["facts"]:
            amount_row = principal(buyer, "expense")
            if not amount_row:
                raise SettlementReview("付款后尚未查到同合同、同币种和金额的付款本金。")
            journal.data["facts"]["payment_principal"] = amount_row
            journal.save()
        case["payment_done"]=True
    if case["payment"]!="D/A":confirm_payment()
    case["documents_received"]=True

    exchange = _delivery_result(buyer, case, journal)
    if exchange["result"] == "claim_notice":
        _settle_claim(buyer, case, journal, before_claim=before_claim)
        case.update(claim_settled=True, import_status="claim_settled", delivery_exception=exchange["notice"])
    else:
        if "arrival" not in journal.data["facts"]:
            order = read_document(buyer, note(buyer, ct, "I2", "M41"))[0]
            if (order.tag != "M41" or order.findtext("M4106") != ct or
                    order.findtext("M4114") != case["documents"]["M01"]):
                raise SettlementReview("提货单与本合同或已签发提单不一致。")
            journal.data["facts"]["arrival"] = {k: order.findtext(v) for k, v in (
                ("id", "M4101"), ("actual_arrival", "M4119"), ("warehouse_entry", "M4120"), ("container", "M4127"))}
            journal.save()
        arrival = journal.data["facts"]["arrival"]
        case["documents"]["M41"] = arrival["id"]
        case["ship"].update({k: v for k, v in arrival.items() if k != "id"})
        if import_inspection_required(product):
            _inspect_import(buyer, case, budget, journal)
        form_id = journal.run("create_declaration", lambda: add_note(buyer, ct, "M05B"))
        case["documents"]["M05B"] = form_id
        if not journal.done("save_declaration"):
            doc = note(buyer, ct, "I2", "M05B")
            case["operation_date"] = date.today().isoformat()
            draft = customs(case)
            def save():
                save_document(buyer, doc, **draft)
                return _message(note(buyer, ct, "I2", "M05B", check=True), "100%")
            journal.run("save_declaration", save)
        if not journal.done("clearance"):
            # The desk supplies the customs document submission list.
            from foreign_trade import open_desk
            open_desk(buyer, "I5")
            journal.run("clearance", lambda: _message(send_docs(buyer, ct, "I5",
                ["M07", "M19", "M21", "M41", "M05B"]), "成功报关"))
        if not journal.done("tax"):
            costs = budget["importer"]
            _guard(buyer, case, case["buyer"]["currency"], sum(_decimal(costs[k])
                   for k in ("M5011", "M5012", "M5013", "M5014")))
            journal.run("tax", lambda: _message(open_action(buyer, ct, "I5", "缴税"), "成功缴税"))
        journal.run("pickup", lambda: _message(open_action(buyer, ct, "I5", "提货"), "成功提货"))
        if not journal.done("sale"):
            if "sale" in journal.data["steps"]:
                raise SettlementReview("销货操作曾中断，请核对库存和销货流水，防止重复销售。")
            journal.data["facts"]["sale_before"] = _sale_rows(_ledger(buyer), case)
            journal.save()
            stock = open_action(buyer, ct, "I7", "销货")
            pno = str(product["id"])
            if pno not in {c.get("value") for c in stock.controls if c.get("name") == "mark"}:
                raise SettlementReview("销售页缺少本合同的到货商品。")
            sale = buyer.get("import/I7_SaleProduct.aspx?" + urlencode({"id": ct, "no": pno}))
            qty = re.match(r"[\d.,]+", sale.fields.get("txtNumber", ""))
            if (sale.fields.get("txtB0401") != pno or not qty
                    or _decimal(qty[0]) < _decimal(product["quantity"])):
                raise SettlementReview("可销售库存不足或商品编号不符。")
            journal.run("sale", lambda: _message(buyer.post(sale, "btnOk",
                        {"txtINumber": str(product["quantity"])}), "成功销售"))
        case.update(goods_delivered=True, sale_done=True, normal_delivery_and_sale=True, import_status="goods_sold_paid")

    if case["payment"]=="D/A":
        from collection_workflow import pay_deferred
        pay_deferred(platform,case,budget,journal)
        confirm_payment()
    _declare_safe(journal, buyer, case, "j")
    case["import_safe_done"] = True
    journal.run("export_settlement", lambda: _message(open_action(seller, ct, "E10", "结汇"), "成功办理结汇"),
                lambda: principal(seller, "income"))
    if "settled_principal" not in journal.data["facts"]:
        amount_row = principal(seller, "income")
        if not amount_row:
            raise SettlementReview("结汇成功提示后尚未查到同合同、同币种和金额的收入本金。")
        journal.data["facts"]["settled_principal"] = amount_row
        journal.save()
    case["settlement_done"] = True
    _declare_safe(journal, seller, case, "c")
    if not journal.done("refund"):
        from foreign_trade import open_desk
        if "refund" not in journal.data["steps"]:
            # The fee at contract close is 5% of the local-currency sales value.
            _guard(seller, case, "RMB", _decimal(budget["exporter"]["M5007"]) * Decimal(".05"))
        open_desk(seller, "E11")
        journal.run("refund", lambda: _message(send_docs(seller, ct, "E11", ["M19", "M05A"]),
                                             "成功办理出口退税"),
                    lambda: _entry(_ledger(seller, ct), "退税", "income"))
    case.update(safe_c=True, refund_done=True)

    if "completion" not in journal.data["facts"]:
        evidence = {}
        for s, desk, typ, side in ((buyer, "I2", "M50B", "expense"), (seller, "E2", "M50A", "income")):
            snap = platform.snapshot(s.account, s.role)
            records = [r for r in snap["contracts"] if r["kind"] == "foreign" and r["number"] == ct]
            if len(records) != 1 or records[0]["status"] != "已经完成":
                raise SettlementReview("平台合同列表尚未确认双方已完成。")
            checks = _message(note(s, ct, desk, typ, check=True), "100%")
            rows = _ledger(s)
            matched = audit_role_ledger(rows, s.role, case, budget, journal.data["steps"], journal.data["facts"])
            ledger = [r for r in rows if r["contract"] == ct]
            evidence[s.role] = dict(contract_status=records[0]["status"], budget_check=checks, ledger=ledger,
                                    **matched,
                                    **{k: snap[k] for k in ("as_of", "currency", "cash", "stock", "loan", "level")})
        journal.data["facts"]["completion"] = evidence
        journal.save()
    else:
        # Older checkpoints only kept rows tagged with a contract number; a
        # sale in this platform's marketplace typically has no contract tag.
        for s in (buyer, seller):
            evidence = journal.data["facts"]["completion"][s.role]
            if "principal" not in evidence or (s.role == "importer" and not case.get("claim_settled")
                                              and "sale" not in evidence):
                rows = _ledger(s) if s.role == "importer" else evidence["ledger"]
                evidence.update(audit_role_ledger(rows, s.role, case, budget, journal.data["steps"],
                                                  journal.data["facts"]))
                journal.save()
    case.update(status="completed", export_completed=True, import_completed=True, import_status="completed")
    return dict(status="completed", contract=ct, claim_settled=bool(case.get("claim_settled")),
                evidence=journal.data["facts"]["completion"], checkpoint=str(journal.path))


def settle_trade(platform, case, budget, state_dir, *, previous_attention=None, before_claim=None):
    """Run from confirmed shipment; returns 'completed' or a precise 'needs_review' result."""
    ct = str(case.get("contract", ""))
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,60}", ct):
        raise ValueError("合同号无法用于本机记录。")
    path = Path(state_dir) / ("settlement-" + ct + ".json")
    try:
        if case.get("terms") not in ("CIF","CFR","FOB") or case.get("payment") not in ("T/T","D/A","D/P","L/C"):
            raise SettlementReview("收尾流程未识别合同约定的贸易术语或结算方式。")
        import_inspection_required(case["product"])
        if case.get("status") not in ("shipped_documents_sent","shipped_for_bank","completed"):
            raise SettlementReview("出口交单尚未确认。")
        path.parent.mkdir(parents=True, exist_ok=True)
        with _exclusive(path.with_suffix(".lock")):
            journal = _Journal(path, case)
            _restore_delivery_notice(journal, previous_attention)
            return _complete(platform, case, budget, journal, before_claim=before_claim)
    except Exception as error:
        return {"status": "needs_review", "contract": ct, "reason": str(error),
                "checkpoint": str(path)}
