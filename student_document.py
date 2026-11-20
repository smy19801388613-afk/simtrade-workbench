"""Read and fill the legacy XML-bound forms displayed by the student client."""
from copy import deepcopy
from html.parser import HTMLParser
import re
from pathlib import PurePosixPath
from urllib.parse import urljoin, urlsplit, parse_qs, quote, unquote
import xml.etree.ElementTree as ET


class DocumentReview(RuntimeError):
    pass


class _Form(HTMLParser):
    def __init__(self, page):
        super().__init__(convert_charrefs=True)
        self.inputs = []
        self.sources = {}
        self.feed(page.html)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ('input', 'select', 'textarea'):
            self.inputs.append(a)
        elif tag == 'xml' and a.get('src'):
            self.sources[a.get('id')] = a['src']


def _load(session, url):
    page = session.get(url)
    form = _Form(page)
    if 'source' not in form.sources:
        raise DocumentReview('当前单据页缺少数据源。')
    root = ET.fromstring(session.get(urljoin(page.url, form.sources['source'])).html)
    if root.tag != 'ROOT' or len(root) != 1:
        raise DocumentReview('当前单据的表头结构与前台模板不符。')
    if 'sourceChild' in form.sources:
        raw = session.get(urljoin(page.url, form.sources['sourceChild'])).html.strip()
        root.extend(ET.fromstring(raw or '<ROOT/>'))
    expected = page.fields.get('flag2') or parse_qs(urlsplit(form.sources['source']).query).get('a2', [None])[0]
    if not expected or root[0].findtext(root[0].tag + '01') != expected:
        raise DocumentReview('文书编号与已打开的表单不符。')
    return page, form, root


def read_document(session, url):
    return _load(session, url)[2]


def _editable(form, source):
    return {a['datafld'] for a in form.inputs if a.get('datafld') and
            a.get('datasrc', '').lower() == source.lower() and
            a.get('type', '').lower() != 'hidden' and 'readonly' not in a and 'disabled' not in a}


def _update(row, values, allowed):
    unknown = values.keys() - allowed
    if unknown:
        raise DocumentReview('字段在当前角色或表单不可编辑：' + ', '.join(sorted(unknown)))
    for key, value in values.items():
        node = row.find(key)
        if node is None:
            raise DocumentReview('单据字段不存在：' + key)
        node.text = str(value)


def _child_items(session, page, root, items, url):
    if not url:
        raise DocumentReview('填写货物明细需要当前表单的明细窗口链接。')
    url = urljoin(page.url, url)
    location = urlsplit(url)
    filename = PurePosixPath(location.path).name
    params = parse_qs(location.query)
    prefix = (filename + '?a0=' + params['a0'][0] + '&a1=') if 'a0' in params else filename + '?a1='
    if (location.netloc != urlsplit(page.url).netloc or
            not re.fullmatch(r'CM\d{2}[A-Z]?\.aspx', filename, re.I) or
            prefix not in page.html or params.get('a1') != [page.fields['flag2']]):
        raise DocumentReview('明细窗口与表单上的单据入口不一致。')
    child = session.get(url)
    match = re.search(r'<xml\s+id=["\']xmlSrc["\']\s*>(.*?)</xml>', child.html, re.I | re.S)
    if not match:
        raise DocumentReview('明细窗口没有商品行模板。')
    template = ET.fromstring(match[1])[0]
    tag = template.tag
    if template.findtext(tag + '01') != page.fields['flag2']:
        raise DocumentReview('明细货物行属于其他单据。')
    form = _Form(child)
    allowed = _editable(form, '#theXMLisland')
    add = child.fields.get('flag') == '0'
    if add:
        unlocked = set(re.findall(r'document\.all\["(\w+)"\]\.readOnly\s*=\s*false', child.html, re.I))
        allowed.update(a['datafld'] for a in form.inputs if a.get('datafld') and a.get('name') in unlocked
                       and a.get('type') != 'hidden' and 'disabled' not in a)
    pattern = r'"(TestChildForm\.aspx\?map=(\w+)&pfld=01&pid=)".*?"&cfld=(\d+)&cid="'
    validator = re.search(pattern, child.html)
    if add and (not validator or validator[2] != tag):
        raise DocumentReview('前台商品行核验入口不明确。')
    key_field = tag + (validator[3] if validator else '03')
    seen = set()
    for item in items:
        key = str(item.get(key_field, ''))
        if not key or key in seen:
            raise DocumentReview('货物行编号空白或重复。')
        seen.add(key)
        existing = [r for r in root.findall(tag) if r.findtext(key_field) == key]
        if len(existing) > 1:
            raise DocumentReview('单据已有重复的货物明细。')
        if existing:
            if all((existing[0].findtext(k) or '') == str(v) for k, v in item.items()):
                continue
            raise DocumentReview('已保存的货物明细与拟填内容不同，请在前台核对。')
        row = deepcopy(template)
        _update(row, item, allowed)
        if add:
            payload = ET.Element('ROOT')
            payload.append(row)
            target = validator[1] + quote(row.findtext(tag + '01'), safe='') + '&cfld=' + validator[3] + '&cid=' + quote(key,safe='')
            result = session.post_xml(child, urljoin(child.url, target), ET.tostring(payload, encoding='utf-8'))
            if result.text.strip() not in ('', 'OK'):
                raise DocumentReview('前台商品行未通过核验：' + result.text[:240])
        root.append(row)


def save_document(session, url, values, *, items=None, child_url=None, submit=None):
    page, form, root = _load(session, url)
    main = root[0]
    tag, mode = main.tag, page.fields.get('flag')
    writable = {'M03','M05','M07','M12','M14','M17','M19','M21','M25','M31','M42','M44','M50'}
    if session.role in {'import_bank', 'export_bank'}:
        writable = {('M30' if session.role == 'import_bank' else 'M34')} if mode == '1' else set()
    elif session.role != 'importer':
        writable.discard('M31')
    if tag not in writable or mode not in {'1', '2', '3', '4'}:
        raise DocumentReview('当前角色没有可编辑的学生单据。')
    allowed = _editable(form, '#source')
    switches = {a.get('name', '') + 'h' for a in form.inputs if 'CheckIt(this,' in a.get('onclick', '') and 'disabled' not in a}
    toggles = {a['datafld'] for a in form.inputs if a.get('name') in switches and a.get('datafld')}
    allowed |= toggles
    allowed -= {'M0310','M0311'}
    if any(str(values[k]) not in {'0','1'} for k in values.keys() & toggles):
        raise DocumentReview('单据勾选项必须为 0 或 1。')
    if tag in {'M07','M12'}:
        first = {'M0732', 'M0733'} if tag == 'M07' else {f'M12{n}' for n in range(35,43)}
        second = {'M0730', 'M0731'} if tag == 'M07' else {f'M12{n}' for n in range(27,35)}
        if mode == '1':
            allowed -= second
        elif mode == '3':
            allowed -= first
        else:
            allowed &= first if mode == '2' else second
            if items:
                raise DocumentReview('签章阶段仅可填写本方资料。')
    _update(main, values, allowed)
    if items:
        _child_items(session, page, root, items, child_url)
    save = re.search(r'doClientSubmit\(\s*"POST"\s*,\s*"(PostForm\.aspx\?tb=(\w+)&uid=)"', page.html, re.I)
    if not save or save[2] != tag or not page.fields.get('flag4'):
        raise DocumentReview('当前文书没有匹配的前台保存按钮。')
    target = urljoin(page.url, save[1] + quote(page.fields['flag4'], safe=''))
    post = session.post_xml if submit is None else lambda *args: submit(session.post_xml, *args)
    response = post(page, target, ET.tostring(root, encoding='utf-8'))
    message = unquote(response.text).strip()
    if not re.match(r'^0(?:\s|$)', message):
        raise DocumentReview('单据保存缺少成功回执：' + message[:240])
    return response
