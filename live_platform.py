"""SIMtrade student web forms. Sessions and passwords remain in process memory."""
from __future__ import annotations

from datetime import datetime
from html import unescape
from html.parser import HTMLParser
from http.cookiejar import CookieJar
import re
from threading import RLock
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode, urljoin, urlsplit, parse_qs
from urllib.request import (
    HTTPCookieProcessor, HTTPRedirectHandler, ProxyHandler, Request, build_opener,
)


DEFAULT_SCHOOL_URL = "http://80.80.80.23/simtrade/"
_CAMPUS_ARCHIVE_URLS = (DEFAULT_SCHOOL_URL, "http://80.80.80.23/SIMtrade/")


def archive_origins(base):
    """Include the earlier mixed-case campus URL when reading local business records."""
    return _CAMPUS_ARCHIVE_URLS if base in _CAMPUS_ARCHIVE_URLS else (base,)


ROLES = {
    "exporter": ("1", "Export", "MapA.aspx", "CastInfo.aspx", (("foreign", "E2"), ("domestic", "E3"))),
    "importer": ("2", "Import", "MapC.aspx", "CastInfo_Importer.aspx", (("foreign", "I2"),)),
    "factory": ("3", "Factory", "MapB.aspx", "CastInfo_Factory.aspx", (("domestic", "F2"),)),
}

LOGIN_ROLES = {**ROLES,
               "export_bank": ("4", "ExBank", None, "CastInfo_Bank.aspx", ()),
               "import_bank": ("5", "ImBank", None, "CastInfo_Bank.aspx", ())}


class PlatformError(RuntimeError):
    """Navigation failed, authentication expired, or an expected UI control changed."""


class Page(HTMLParser):
    def __init__(self, url, html):
        super().__init__(convert_charrefs=True)
        self.url, self.html = url, html
        self.fields, self.controls, self.events = {}, [], set()
        self.submit_controls = {}
        self.links, self.rows, self.urls = {}, [], set()
        self.form_action, self.form_method = None, ""
        self._text, self._tables = [], []
        self._skip = 0
        self._select = self._option = self._textarea = None
        self.feed(html)
        self.close()
        self.text = " ".join(self._text)
        self.scripts = re.findall(r"<script\b[^>]*>(.*?)</script\s*>", html, re.I | re.S)
        self.messages = [unquote(x) for x in re.findall(r"""errmsg=([^'"&\s]+)""", unescape(html), re.I)]
        # The legacy menus use both literal hrefs and JavaScript popup/frame links.
        for raw in re.findall(r"""["']([^<>"'\s]+?\.(?:aspx|ashx|js|xml|xsl|html?)(?:\?[^"'<>\s]*)?)["']""",
                              unescape(html), re.I):
            self.urls.add(urljoin(url, raw))

    def handle_starttag(self, tag, attrs):
        a = {k: v or "" for k, v in attrs}
        for v in a.values():
            self.events.update(re.findall(r"""__doPostBack\(\s*['"]([^'"]+)""", v))
        if tag in ("script", "style"):
            self._skip += 1
        if tag == "form" and self.form_action is None:
            self.form_action = urljoin(self.url, a.get("action", ""))
            self.form_method = a.get("method", "get").lower()
        if tag in ("a", "frame", "iframe", "script", "xml"):
            raw = a.get("href") or a.get("src")
            if raw and not raw.lower().startswith(("#", "javascript:", "mailto:")):
                target = urljoin(self.url, raw)
                self.urls.add(target)
                if a.get("id"):
                    self.links[a["id"]] = target
        if tag == "table":
            self._tables.append({"row": None, "cell": None})
        elif self._tables:
            table = self._tables[-1]
            if tag == "tr":
                table["row"] = []
                table["cell"] = None
            elif tag in ("td", "th") and table["row"] is not None:
                table["cell"] = []

        if tag in ("input", "select", "textarea", "button"):
            self.controls.append(a)
            name = a.get("name")
            if tag == "select":
                self._select = a
                a["options"] = []
            elif tag == "textarea":
                self._textarea = a
                a.update(type="textarea", value="")
            elif name:
                typ = a.get("type", "submit" if tag == "button" else "text").lower()
                if typ in ("submit", "image") and "disabled" not in a:
                    self.submit_controls[name] = (typ, a.get("value", ""))
                    self.events.add(name)
                if typ not in ("file", "button", "submit", "reset", "image") and "disabled" not in a:
                    if typ not in ("radio", "checkbox") or "checked" in a:
                        self.fields[name] = a.get("value", "on" if typ in ("checkbox", "radio") else "")
        elif tag == "option" and self._select is not None:
            a["text"] = ""
            self._select["options"].append(a)
            self._option = a

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag == "select" and self._select is not None:
            select = self._select
            options = select["options"]
            choice = next((o for o in options if "selected" in o), options[0] if options else {})
            if select.get("name") and "disabled" not in select:
                self.fields[select["name"]] = choice.get("value", choice.get("text", ""))
            self._select = self._option = None
        elif tag == "option":
            self._option = None
        elif tag == "textarea" and self._textarea is not None:
            a = self._textarea
            if a.get("name") and "disabled" not in a:
                self.fields[a["name"]] = a["value"]
            self._textarea = None
        elif self._tables:
            table = self._tables[-1]
            if tag in ("td", "th") and table["cell"] is not None:
                table["row"].append(" ".join(table["cell"]).strip())
                table["cell"] = None
            elif tag == "tr" and table["row"] is not None:
                self.rows.append(table["row"])
                table["row"] = table["cell"] = None
            elif tag == "table":
                self._tables.pop()

    def handle_data(self, data):
        if self._option is not None:
            self._option["text"] += data
        if self._textarea is not None:
            self._textarea["value"] += data
        if not self._skip and data.strip():
            self._text.append(data.strip())
            if self._tables and self._tables[-1]["cell"] is not None:
                self._tables[-1]["cell"].append(data.strip())


def _decode(raw, headers):
    try:
        return raw.decode("utf-8-sig").replace("\0", "")
    except UnicodeDecodeError:
        match = re.search(br"""charset\s*=\s*["']?([\w-]+)""", raw[:6000], re.I)
        encoding = match[1].decode("ascii") if match else headers.get_content_charset()
        if not encoding or encoding.lower() in ("gb2312", "gbk"):
            encoding = "gb18030"
        return raw.decode(encoding, errors="replace").replace("\0", "")


class _StudentRedirect(HTTPRedirectHandler):
    def __init__(self, validate):
        self.validate = validate

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.validate(urljoin(req.full_url, newurl))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class StudentSession:
    """One authenticated role. Relative URLs resolve from the SIMtrade base directory."""

    def __init__(self, base_url, account, password, role):
        self.base_url, self.account, self.role = base_url, account, role
        self._base = urlsplit(base_url)
        self.lock = RLock()
        self._opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()),
                                    _StudentRedirect(self._validate))
        self._owner = object()
        self._paths = set()
        self._last = None
        self._authenticated = False
        login = self._request(urljoin(base_url, "login.aspx"))
        homepage = self.post(login, "btnLogin", {"txtUsrName": account, "txtUsrPwd": password,
                                                "cblUsrCls": LOGIN_ROLES[role][0]})
        expected = f"/{LOGIN_ROLES[role][1]}/home.aspx".lower()
        if not urlsplit(homepage.url).path.lower().endswith(expected):
            raise PlatformError("登录未进入所选角色首页，请检查账号、密码和角色。")
        self._authenticated = True
        self.home = homepage

    def _validate(self, url):
        u = urlsplit(url)
        path = unquote(u.path).lower()
        if ((u.scheme, u.netloc.lower()) != (self._base.scheme, self._base.netloc.lower())
                or not path.startswith(self._base.path.lower()) or "\\" in path
                or any(x in path.split("/") for x in ("..", "teacher", "admin", "manager"))):
            raise PlatformError("该地址不属于当前学生平台。")

    def _observed(self, url):
        self._validate(url)
        if urlsplit(url).path.lower() not in self._paths:
            raise PlatformError(f"当前角色尚未打开通向此页面的菜单：{urlsplit(url).path}")

    def _request(self, url, data=None, content_type=None):
        self._validate(url)
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Trident/7.0; rv:11.0) like Gecko"}
        if self._last:
            headers["Referer"] = self._last
        if content_type:
            headers["Content-Type"] = content_type
        try:
            with self._opener.open(Request(url, data=data, headers=headers), timeout=25) as res:
                self._validate(res.url)
                page = Page(res.url, _decode(res.read(), res.headers))
        except HTTPError as error:
            raise PlatformError(f"学生平台返回 HTTP {error.code}（{urlsplit(url).path}）。") from None
        except (URLError, OSError) as error:
            raise PlatformError(f"无法连接学生平台（{type(error).__name__}），请检查校园网或 VPN。") from None
        if self._authenticated and urlsplit(page.url).path.lower().endswith("/login.aspx"):
            raise PlatformError("角色会话已过期，请重新登录后核对当前状态。")
        self._last = page.url
        page._owner = self._owner
        self._paths.update(urlsplit(link).path.lower() for link in page.urls | {page.url})
        return page

    def get(self, relative_url):
        with self.lock:
            url = urljoin(self.base_url, relative_url)
            self._observed(url)
            return self._request(url)

    def _owns(self, page):
        if getattr(page, "_owner", None) is not self._owner:
            raise PlatformError("页面不属于当前角色会话。")

    def post(self, page, event, fields=None, argument=""):
        with self.lock:
            self._owns(page)
            if not page.form_action or page.form_method != "post":
                raise PlatformError("当前页面没有可提交的 POST 表单。")
            fields = fields or {}
            if event and event not in page.events:
                raise PlatformError(f"页面未提供操作控件 {event}。")
            names = {a.get("name") for a in page.controls if "disabled" not in a}
            if set(fields) - names:
                raise PlatformError("提交内容中存在页面未提供的控件。")
            data = dict(page.fields)
            for key, value in fields.items():
                if value is None:
                    data.pop(key, None)
                else:
                    data[key] = value
            if event in page.submit_controls:
                typ, value = page.submit_controls[event]
                if typ == "image":
                    data[event + ".x"] = "1"
                    data[event + ".y"] = "1"
                else:
                    data[event] = value
            elif event:
                data["__EVENTTARGET"] = event.replace("$", ":")
                data["__EVENTARGUMENT"] = argument
            self._last = page.url
            return self._request(page.form_action, urlencode(data, doseq=True).encode("ascii"),
                                 "application/x-www-form-urlencoded")

    def post_xml(self, page, target_relative_url, data):
        with self.lock:
            self._owns(page)
            target = urljoin(page.url, target_relative_url)
            self._observed(target)
            if urlsplit(target).path.lower() not in {urlsplit(u).path.lower() for u in page.urls}:
                raise PlatformError("此单据页面没有提供该保存或校验控件。")
            if not isinstance(data, bytes):
                raise TypeError("单据内容须为 XML 字节。")
            self._last = page.url
            return self._request(target, data, "text/xml; charset=utf-8")


def _menu_link(page, suffix, **query):
    matches = []
    for link in page.urls:
        u = urlsplit(link)
        q = parse_qs(u.query)
        if u.path.lower().endswith("/" + suffix.lower()) and all(q.get(k) == [v] for k, v in query.items()):
            matches.append(link)
    if len(matches) != 1:
        raise PlatformError(f"角色菜单未提供唯一的 {suffix} 入口。")
    return matches[0]


def _money(value):
    m = re.fullmatch(r"([A-Z]{3})\s+(-?[\d,]+(?:\.\d+)?)", value.strip())
    if not m:
        raise PlatformError("平台返回的财务字段格式无法识别。")
    return m[1], float(m[2].replace(",", ""))


def _contract_rows(page, kind):
    if "合同号" not in page.text or "目前状况" not in page.text:
        raise PlatformError("合同列表表头未能识别。")
    marks = [c for c in page.controls if c.get("name") == "mark" and c.get("type") == "radio"]
    numbers = {c.get("value") for c in marks}
    out = []
    for row in page.rows:
        # An unsigned draft can have an empty date. The row's selectable contract
        # number is the identifier on both the domestic and foreign desks.
        if len(row) >= 5 and row[2] in numbers:
            out.append(dict(kind=kind, date=row[1], number=row[2], counterparty=row[3], status=row[4]))
    if sorted(c.get("value") for c in marks) != sorted(r["number"] for r in out):
        raise PlatformError("合同表格行与平台选择控件不一致。")
    return out


class LivePlatform:
    def __init__(self, base_url, accounts):
        self.base_url = base_url.rstrip("/") + "/"
        u = urlsplit(self.base_url)
        if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password or u.query or u.fragment:
            raise ValueError("请填写学生平台根地址，例如 https://campus.example/SIMtrade/。")
        self._accounts = {str(k): v for k, v in accounts.items()}
        if not 1 <= len(accounts) <= 10 or any(not a.strip() or not isinstance(v, str)
                                              for a, v in self._accounts.items()):
            raise ValueError("请提供 1 至 10 个学生账号及对应密码。")
        self._sessions = {}
        self._lock = RLock()

    def session(self, account, role):
        account = str(account)
        if role not in LOGIN_ROLES or account not in self._accounts:
            raise ValueError("请选择当前配置的账号和学生角色。")
        with self._lock:
            key = account, role
            if key not in self._sessions:
                self._sessions[key] = StudentSession(self.base_url, account, self._accounts[account], role)
            return self._sessions[key]

    def profile(self, account, role):
        s = self.session(account, role)
        code, _, _, filename, _ = LOGIN_ROLES[role]
        page = s.get(_menu_link(s.home, filename, cls=code))
        if page.fields.get("txtB0301") != s.account:
            raise PlatformError("资料页账号不匹配。")
        return {c["name"]: page.fields[c["name"]] for c in page.controls
                if re.fullmatch(r"txtB\d+", c.get("name", "")) and c["name"] in page.fields
                and c.get("type", "text").lower() in ("text", "textarea")}

    def snapshot(self, account, role):
        s = self.session(account, role)
        code, _, map_page, _, desks = ROLES[role]
        with s.lock:
            home = s.get(_menu_link(s.home, "Main.aspx"))
            level = re.search(r"等级\s*[：:]\s*(\d+)", home.html)
            if not level:
                raise PlatformError("首页等级未能识别。")
            finance = s.get(_menu_link(s.home, "Finance.aspx", cls=code))
            values = {k: _money(finance.fields.get(f, "")) for k, f in (
                ("cash", "B0342"), ("stock", "B0335"), ("loan", "B0343"))}
            currencies = {v[0] for v in values.values()}
            if len(currencies) != 1:
                raise PlatformError("角色财务字段币种不一致。")
            todo = s.get(_menu_link(home, "ToDoList.aspx", UserNo=s.account + code, cls=code))
            hints = [m[1].strip() for t in todo._text if (m := re.fullmatch(r"\d+\s*[、.]\s*(.+)", t))]
            site_map = s.get(_menu_link(s.home, map_page))
            contracts = []
            for kind, desk_id in desks:
                desk = s.get(_menu_link(site_map, "ShowPopUp.aspx", p3=desk_id))
                if "btnCheck" not in desk.links:
                    raise PlatformError("业务对话框缺少合同选择入口。")
                page = s.get(desk.links["btnCheck"])
                contracts.extend(_contract_rows(page, kind))
                selector = next((c for c in page.controls if c.get("name") == "ctrPages:pages"), {})
                for opt in selector.get("options", []):
                    n = opt.get("value", opt.get("text", "")).strip()
                    if n != "1":
                        page = s.post(page, "ctrPages$pages", {"ctrPages:pages": n})
                        if page.fields.get("ctrPages:pages") != n:
                            raise PlatformError("合同列表翻页结果无法核对。")
                        contracts.extend(_contract_rows(page, kind))
            return dict(account=s.account, role=role, as_of=datetime.now().astimezone().isoformat(timespec="seconds"),
                        currency=currencies.pop(), level=int(level[1]), contracts=contracts, todo=hints,
                        **{k: v[1] for k, v in values.items()})
