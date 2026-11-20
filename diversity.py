"""Read verified contract terms for the workbench's automatic route selection."""
from hashlib import sha256
from html.parser import HTMLParser
from pathlib import Path

from live_platform import PlatformError, ROLES, _menu_link
from pipeline import read, write
from trade_modes import PAYMENTS, TERMS


ROLES_FOREIGN = ("exporter", "importer")


class _SummaryFields(HTMLParser):
    """The student desk exposes the active contract in three labelled spans."""
    def __init__(self, page):
        super().__init__(convert_charrefs=True)
        self.values = {}
        self.current = None
        self.feed(page.html)
        self.close()

    def handle_starttag(self, tag, attrs):
        if tag == "span":
            key = dict(attrs).get("id")
            self.current = key if key in {"txtM0701", "txtM0720", "txtY"} else None
            if self.current:
                self.values.setdefault(self.current, [])

    def handle_endtag(self, tag):
        if tag == "span":
            self.current = None

    def handle_data(self, text):
        if self.current:
            self.values[self.current].append(text)

    def text(self, key):
        return " ".join(" ".join(self.values.get(key, ())).split())


def _read_active_contract(platform, snapshot):
    """Enrich a completed active contract by reading its desk. Never change the selection."""
    role = snapshot.get("role")
    contracts = snapshot.get("contracts", [])
    if role not in ROLES_FOREIGN or not any(
        r.get("kind") == "foreign" and r.get("status") == "已经完成" for r in contracts
    ):
        return snapshot
    try:
        session = platform.session(snapshot["account"], role)
        with session.lock:
            site_map = session.get(_menu_link(session.home, ROLES[role][2]))
            desk_code = dict(ROLES[role][4])["foreign"]
            desk = session.get(_menu_link(site_map, "ShowPopUp.aspx", p3=desk_code))
            page = session.get(_menu_link(desk, "MainoContract.aspx"))
            info = _SummaryFields(page)
    except (PlatformError, OSError):
        return dict(snapshot, method_note="当前主合同条款暂时无法读取。")
    number = info.text("txtM0701")
    pair = info.text("txtM0720").upper().split()
    if (info.text("txtY") != "已经完成" or len(pair) != 2 or pair[0] not in PAYMENTS
            or pair[1] not in TERMS or not number):
        return snapshot
    matches = [r for r in contracts if r.get("number") == number
               and r.get("kind") == "foreign" and r.get("status") == "已经完成"]
    if len(matches) != 1:
        return snapshot
    rows = [dict(r, terms=pair[1], payment=pair[0], method_source="主合同信息")
            if r is matches[0] else r for r in contracts]
    return dict(snapshot, contracts=rows)


def read_current_method(platform, snapshot, history_dir=None):
    """Optionally retain verified terms per school, account, role and completed contract."""
    result = _read_active_contract(platform, snapshot)
    if history_dir is None or result.get("role") not in ROLES_FOREIGN:
        return result
    scope = "\0".join((platform.base_url, str(result["account"]), result["role"]))
    directory = Path(history_dir) / ".method-history" / sha256(scope.encode()).hexdigest()
    rows = []
    note = result.get("method_note", "")
    for row in result.get("contracts", []):
        rows.append(row)
        if row.get("kind") != "foreign" or row.get("status") != "已经完成" or not row.get("number"):
            continue
        ct = row["number"]
        path = directory / (sha256(ct.encode()).hexdigest() + ".json")
        try:
            if row.get("method_source") == "主合同信息":
                observed = {"number": ct, "terms": row["terms"], "payment": row["payment"]}
                write(path, observed)
            elif path.exists():
                saved = read(path)
                if (isinstance(saved, dict) and saved.get("number") == ct
                        and saved.get("terms") in TERMS and saved.get("payment") in PAYMENTS):
                    rows[-1] = dict(row, terms=saved["terms"], payment=saved["payment"],
                                    method_source="此前由主合同信息核实")
        except (OSError, ValueError):
            note = "部分本地条款记录暂时无法读写。"
    return dict(result, contracts=rows, method_note=note)
