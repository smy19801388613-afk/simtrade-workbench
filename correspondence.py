"""Student mailbox navigation and an opt-in post-settlement confirmation."""
from decimal import Decimal
from html import escape, unescape
from html.parser import HTMLParser
from pathlib import Path
import re
from urllib.parse import parse_qs, urljoin, urlsplit

from live_platform import PlatformError, _menu_link
from settlement import _exclusive, _Journal


class _Text(HTMLParser):
    def __init__(self, raw):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.feed(raw)

    def handle_starttag(self, tag, attrs):
        if tag in ("br", "div", "p", "li", "tr"):
            self.parts.append("\n")

    def handle_data(self, value):
        self.parts.append(value)

    def value(self):
        return re.sub(r"\n{3,}", "\n\n", "".join(self.parts)).strip()


def _box(session, sent):
    home = session.get(_menu_link(session.home, "mail/home.aspx"))
    mails = session.get(_menu_link(home, "Mails.aspx"))
    return session.post(mails, "btnSendBox" if sent else "btnInBox")


def _entries(box, *, sent):
    grid = re.search(r"""<table\b[^>]*\bid=["']MailGrid["'][^>]*>(.*?)</table>""", box.html, re.I | re.S)
    if not grid:
        # A truly empty folder may render no data grid.
        if re.search(r"共有\s*0\s*封邮件", box.text):
            return []
        raise PlatformError("邮件列表缺少可识别的表格。")
    result = []
    target = "newmail.aspx" if sent else "readmail.aspx"
    for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", grid[1], re.I | re.S):
        if not re.search(r"""\bname\s*=\s*["']d["']""", row, re.I):
            continue
        links = [urljoin(box.url, unescape(u)) for u in re.findall(r"""\bhref\s*=\s*["']([^"']+)""", row, re.I)]
        links = {u for u in links if urlsplit(u).path.lower().endswith("/" + target)}
        if len(links) != 1:
            raise PlatformError("邮件列表行未提供唯一的邮件入口。")
        url = links.pop()
        ids = parse_qs(urlsplit(url).query).get("id", [])
        if len(ids) != 1 or not ids[0].isdigit():
            raise PlatformError("平台未返回有效邮件编号。")
        cells = [_Text(c).value() for c in re.findall(r"<td\b[^>]*>(.*?)</td>", row, re.I | re.S)]
        aliases = [c for c in cells if c.lower().endswith("@simtrade")]
        result.append({"id": ids[0], "url": url, "alias": aliases[0] if len(aliases) == 1 else "",
                       "unread": bool(re.search(r"""src\s*=\s*["']images/0\.gif["']""", row, re.I))})
    if len({x["id"] for x in result}) != len(result):
        raise PlatformError("邮件列表含重复编号。")
    if sent:
        total = re.search(r"共有\s*(\d+)\s*封邮件", box.text)
        if total is None or int(total[1]) != len(result):
            raise PlatformError("发件箱条数未核平，无法排除以前已发送。")
    return result


def _mail_fields(page):
    fields = page.fields
    if not all(k in fields for k in ("E0110", "E0109", "E0106", "tbContent")):
        raise PlatformError("无法识别邮件详情页。")
    return {"subject": fields["E0110"], "contract": fields["E0109"], "recipient": fields["E0106"],
            "sender": fields.get("E0104", ""), "body": _Text(fields["tbContent"]).value()}


def read_inbox(session, limit=20):
    """Open and read up to limit actual messages in the order shown by the inbox."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("读取封数需为非负整数。")
    if not limit:
        return []
    with session.lock:
        box = _box(session, False)
        messages = []
        for entry in _entries(box, sent=False)[:limit]:
            page = session.get(entry["url"])
            message = _mail_fields(page)
            session.post(page, "btnCancel")
            messages.append(dict(message, id=entry["id"], was_unread=entry["unread"]))
        return messages


def _compose(case):
    ct = case["contract"]
    amount = Decimal(str(case["amount_usd"]))
    if case["currency"] != "USD" or not amount.is_finite() or amount <= 0:
        raise ValueError("结算金额或币种不匹配。")
    if case.get("claim_settled"):
        detail = "We have also recorded that your insurance claim for this shipment was settled."
    elif case.get("goods_delivered") and case.get("sale_done"):
        detail = "Your import records confirm that the goods were collected and sold."
    else:
        raise ValueError("尚未核实交货或理赔结果。")
    subject = f"{ct} settlement confirmation"
    parts = [f"Dear {case['buyer']['person_en']},",
             f"We confirm receipt of USD {amount:,.2f} under contract {ct}. {detail}",
             f"Kind regards,\n{case['seller']['person_en']}\n{case['seller']['company_en']}"]
    html = "".join("<p>" + escape(p).replace("\n", "<br>") + "</p>" for p in parts)
    return subject, html


def _find_sent(session, ct, alias, subject, content):
    box = _box(session, True)
    matches = []
    expected = " ".join(_Text(content).value().split())
    for entry in _entries(box, sent=True):
        if entry["alias"] and entry["alias"].lower() != alias.lower():
            continue
        message = _mail_fields(session.get(entry["url"]))
        if message["recipient"].lower() != alias.lower() or message["contract"] != ct:
            continue
        if message["subject"] == subject:
            if " ".join(message["body"].split()) != expected:
                raise PlatformError("已有同合同、同主题但正文不同的邮件，请核对发件箱。")
            matches.append({"id": entry["id"], "contract": ct, "recipient": alias, "subject": subject})
        elif re.search(r"(?:settlement confirmation|settlement and payment|payment and delivery records|结算确认|结案确认)",
                       message["subject"], re.I):
            raise PlatformError("发件箱已有本合同的结算确认记录，请核对原邮件。")
    if len(matches) > 1:
        raise PlatformError("发件箱有多封相同的结案函，请核对。")
    return matches[0] if matches else None


def send_closing(platform, job, state_dir):
    """Send one optional closing to this job's confirmed importer; reconcile uncertain sends read-only."""
    case = job["case"]
    ct = case["contract"]
    if job.get("stage") != "completed" or case.get("status") != "completed" or not case.get("settlement_done"):
        raise ValueError("须先完成双方合同并核实结汇。")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,60}", ct):
        raise ValueError("合同号不能用于本地回执。")
    seller, buyer = case["seller_account"], case["buyer_account"]
    if {seller, buyer} - set(job["accounts"]):
        raise ValueError("邮件双方须为当前任务已配置的交易对手。")
    sender = platform.session(seller, "exporter")
    alias = platform.profile(buyer, "importer").get("txtB0327", "")
    if alias.lower() != (buyer + "2@simtrade").lower():
        raise PlatformError("进口商资料页未能核对收件人的站内邮箱。")
    subject, content = _compose(case)
    journal_path = Path(state_dir) / ("closing-" + ct + ".json")
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    with _exclusive(journal_path.with_suffix(".lock")), sender.lock:
        journal = _Journal(journal_path, case)
        if journal.done("send"):
            return dict(status="sent_readback", **journal.data["steps"]["send"]["receipt"])

        def lookup():
            return _find_sent(sender, ct, alias, subject, content)

        if "send" not in journal.data["steps"]:
            existing = lookup()
            if existing:
                return dict(status="sent_readback", **journal.run("send", lambda: existing))
            form = sender.post(_box(sender, True), "btnCreate")
            if not {"E0106", "E0110", "E0109", "tbContent"} <= form.fields.keys() or "btnSend" not in form.events:
                raise PlatformError("写信表单缺少收件人、主题、合同号或发送按钮。")
        def send():
            sender.post(form, "btnSend", {"E0106": alias, "E0110": subject, "E0109": ct, "tbContent": content})
            verified = lookup()
            if not verified:
                raise PlatformError("发件箱尚未找到该结案函，已保留待核对记录。")
            return verified

        receipt = journal.run("send", send, reconcile=lookup)
        return dict(status="sent_readback", **receipt)
