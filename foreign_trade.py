"""Execute normal student contract and shipment workflows using the current role's desk."""
from datetime import date, timedelta
from decimal import Decimal
from html import unescape
import json
from pathlib import Path
import re
from urllib.parse import quote, urljoin, urlsplit, parse_qs

from live_platform import PlatformError, ROLES, _menu_link
from market import import_inspection_required
from student_document import read_document, save_document
import foreign_documents as docs


class TradeReview(PlatformError):
    """The operator can reconcile the named transaction in the student portal."""


def open_desk(session, op):
    role = session.role
    if role not in ('exporter', 'importer'):
        raise TradeReview('外贸工作台需要出口商或进口商角色。')
    site_map = session.get(_menu_link(session.home, ROLES[role][2]))
    return session.get(_menu_link(site_map, 'ShowPopUp.aspx', p3=op))


def _main_contract(session, desk):
    current = session.get(_menu_link(desk, 'MainoContract.aspx'))
    m = re.search(r'<span\s+id="txtM0701"[^>]*>([^<]*)</span>', current.html, re.I)
    return unescape(m[1]).strip() if m else ''


def _find_contract(session, desk, ct):
    page = session.get(desk.links['btnCheck'])
    pages = [o.get('value', o.get('text', '')).strip() for c in page.controls
             if c.get('name') == 'ctrPages:pages' for o in c.get('options', [])]
    for index in [None] + [n for n in pages if n != page.fields.get('ctrPages:pages')]:
        if index is not None:
            page = session.post(page, 'ctrPages$pages', {'ctrPages:pages': index})
            if page.fields.get('ctrPages:pages') != index:
                raise TradeReview('无法核对合同选择列表的页码。')
        if ct in _marks(page, 'radio'):
            return page
    return None


def _visible_state(session, ct, counterparty):
    desk = open_desk(session, _role_desk(session))
    page = _find_contract(session, desk, ct)
    if page is None:
        return ''
    records = [r for r in page.rows if len(r) >= 5 and r[2] == ct
               and r[3].split('(', 1)[0].strip() == counterparty]
    if len(records) != 1:
        raise TradeReview('合同选择清单中的交易对手或合同编号不匹配。')
    return records[0][4]


def _marks(page, kind):
    return [c.get('value', '') for c in page.controls if c.get('name') == 'mark'
            and c.get('type', '').lower() == kind and 'disabled' not in c]


def _select_contract(session, desk, ct):
    if _main_contract(session, desk) == ct:
        return
    page = _find_contract(session, desk, ct)
    if page is None:
        raise TradeReview(f'此角色的合同列表中找不到 {ct}。')
    session.post(page, 'btnOk', {'mark': ct, 'flag': ct})
    if _main_contract(session, desk) != ct:
        raise TradeReview(f'平台未切换至指定合同 {ct}。')


def _actions(desk):
    regex = (r"""Go2Url\(window\.frames\['[^']+'\],window\.frames\['[^']+'\],"""
             r"""'([^']+)',([01]),'([^']+)','([01])'\)"\s*>([^<]+)</a>""")
    return [(urljoin(desk.url, unescape(path)), mode, key, need, unescape(label).strip())
            for path, mode, key, need, label in re.findall(regex, desk.html, re.I)]


def _action_url(desk, label, ct=''):
    choices = [(u, mode, key, need) for u, mode, key, need, name in _actions(desk) if name == label]
    if len(choices) != 1:
        raise TradeReview(f'当前业务菜单缺少唯一的“{label}”操作。')
    url, mode, key, need = choices[0]
    if key != 'txtM0701' or (need == '1' and not ct):
        raise TradeReview('当前菜单需要选定一份外贸合同。')
    if ct:
        url += ('&' if mode == '1' else '?') + 'id=' + quote(ct, safe='')
    return url


def open_action(session, ct, op, label):
    desk = open_desk(session, op)
    _select_contract(session, desk, ct)
    return session.get(_action_url(desk, label, ct))


def _opened(page):
    links = re.findall(r"""window\.open\(\s*['"]([^'"]+)['"]""", '\n'.join(page.scripts), re.I)
    links = {urljoin(page.url, unescape(link)) for link in links}
    if len(links) != 1:
        raise TradeReview('平台未显示唯一的单据窗口。' + _receipt(page)[:160])
    return links.pop()


def _receipt(page):
    return ' '.join([page.text, *page.messages]).strip()


def _success(page, text=None):
    message = _receipt(page)
    if (text not in message if text else '成功' not in message):
        raise TradeReview('平台未返回预期回执：' + message[:280])
    return page


def _role_desk(session):
    return {'exporter':'E2','importer':'I2'}[session.role]


def _list_page(session, ct, op):
    # Bank presentation menus on the exporter's bank desk use separate document-list codes.
    desk = open_desk(session, 'E10' if op in ('E1001', 'E1002') else op)
    _select_contract(session, desk, ct)
    labels = [label for u, _, _, _, label in _actions(desk)
              if urlsplit(u).path.lower().endswith('/notelist.aspx')
              and parse_qs(urlsplit(u).query).get('op') == [op]]
    if len(labels) != 1:
        raise TradeReview(f'业务菜单 {op} 未提供对应的单据列表。')
    return session.get(_action_url(desk, labels[0], ct))


def _notes(page):
    out = {}
    # Read the radio/checkbox and the associated document link from the same table row.
    for row in re.findall(r'<tr\b[^>]*>(.*?)</tr>', page.html, re.S | re.I):
        mark = re.search(r'<input\b[^>]*\bname="mark"[^>]*\bvalue="([^"]+)"', row, re.I)
        if not mark:
            continue
        item = re.search(r"""__doPostBack\('([^']+\$lbtn)',''\)"[^>]*>([^<]+)</a>""", row)
        if not item:
            raise TradeReview('单据选择行缺少文号或打开单据的控件。')
        mark = unescape(mark[1])
        typ = mark.split(',')[1]
        if typ in out:
            raise TradeReview(f'合同存在两份 {typ}，需核对。')
        out[typ] = (mark, item[1], unescape(item[2]).strip())
    return out


def note(session, ct, op, typ, check=False):
    page = _list_page(session, ct, op)
    row = _notes(page).get(typ)
    if not row:
        raise TradeReview(f'{ct} 在当前角色缺少单据 {typ}。')
    response = session.post(page, row[1] + ('1' if check else ''))
    return response if check else _opened(response)


def add_note(session, ct, typ):
    op = _role_desk(session)
    existing = _notes(_list_page(session, ct, op)).get(typ)
    if existing:
        return existing[2]
    page = open_action(session, ct, op, '添加单据')
    if typ not in _marks(page, 'radio'):
        raise TradeReview(f'当前合同和角色无法添加单据 {typ}。')
    result = _success(session.post(page, 'btnOk', {'flag': typ, 'flag1': ct, 'mark': typ}))
    match = re.search(r'\[([^\]]+)\]', result.text)
    existing = _notes(_list_page(session, ct, op)).get(typ)
    if not existing or (match and match[1] != existing[2]):
        raise TradeReview(f'新增 {typ} 后无法在单据列表核对文号。')
    return existing[2]


def prepare_doc_submission(session, ct, op, types):
    page = _list_page(session, ct, op)
    rows = _notes(page)
    if not types or len(types) != len(set(types)) or set(types) - rows.keys():
        raise TradeReview('待提交单据清单缺失或重复。')
    marks = [rows[t][0] for t in types]
    if set(marks) - set(_marks(page, 'checkbox')):
        raise TradeReview('平台没有提供全部待提交单据的勾选项。')
    fields = {'flag': ';'.join(marks) + ';', 'mark': marks}
    return lambda: _success(session.post(page, '_ctl0:btnOk', fields))


def send_docs(session, ct, op, types):
    return prepare_doc_submission(session, ct, op, types)()


class _Record:
    def __init__(self, state_dir, case):
        self.case = case
        ct = case['contract']
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', ct):
            raise TradeReview('请输入平台接受的合同编号。')
        self.path = Path(state_dir) / ('foreign-' + ct + '.json')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        identity = [ct, case['seller_account'], case['buyer_account'], case['product']['id'],
                    str(case['product']['quantity']), str(case['amount_usd']), case['destination']]
        self.data = json.loads(self.path.read_text('utf-8')) if self.path.exists() else {
            'identity': identity, 'terms':case['terms'], 'payment':case['payment'], 'steps': {}, 'saved': {}}
        if (self.data['identity'] != identity or self.data.get('terms', 'CIF') != case['terms']
                or self.data.get('payment', 'T/T') != case['payment']):
            raise TradeReview('交易参数与已有合同执行记录不一致。')
        case.setdefault('documents', {}).update(self.data['saved'].get('documents', {}))
        for k in ('ship', 'insurance'):
            case.setdefault(k, {}).update(self.data['saved'].get(k, {}))

    def persist(self):
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        tmp.replace(self.path)

    def put(self, key, value):
        self.case.setdefault(key, {}).update(value)
        self.data['saved'].setdefault(key, {}).update(value)
        self.persist()

    def run(self, key, callback, recover=None):
        if key in self.data['steps']:
            return self.data['steps'][key]
        pending = self.data.get('pending')
        if pending:
            if pending != key or recover is None or not (result := recover()):
                raise TradeReview(f'步骤 {pending} 的回执未写入，请先核对平台记录再继续。')
        else:
            self.data['pending'] = key
            self.persist()
            result = callback()
        if hasattr(result, 'text'):
            result = _receipt(result)[:220]
        self.data['steps'][key] = result
        self.data.pop('pending', None)
        self.persist()
        return result


def _budget(session, case, budget, typ):
    op = _role_desk(session)
    ct = case['contract']
    fields = budget['exporter' if typ == 'M50A' else 'importer']
    if not fields or any(not re.fullmatch(r'M50\d{2}', key) for key in fields):
        raise TradeReview('缺少角色预算表的测算字段。')
    save_document(session, note(session, ct, op, typ), fields)
    check = note(session, ct, op, typ, check=True)
    if '100%' not in check.text and '请等合同完成时再来查看得分' not in check.text:
        raise TradeReview(typ + ' 预算表核验：' + check.text[:220])


def _match_contract(root, draft, case):
    main = root.find('M07')
    rows = root.findall('M08')
    if main is None or main.findtext('M0701') != case['contract'] or len(rows) != 1:
        raise TradeReview('买方看到的合同编号或货物行数不一致。')
    numeric = {'M0714','M0718','M0805','M0808','M0809'}
    for row, expected in ((main, draft['values']), (rows[0], draft['items'][0])):
        for key, value in expected.items():
            actual = (row.findtext(key) or '').replace('\r\n', '\n')
            same = Decimal(actual) == Decimal(str(value)) if key in numeric and actual else actual == str(value)
            if not same:
                raise TradeReview('买卖双方合同草稿字段不一致：' + key)


def _validate(case):
    import_inspection_required(case['product'])
    if (case['terms'] not in ('FOB', 'CFR', 'CIF') or case['payment'] not in ('T/T','L/C','D/P','D/A')
            or case['currency'] != 'USD'
            or case['loading_port'] != 'SHANGHAI' or case['product']['packing_unit'] != 'CARTON'):
        raise TradeReview('请核对合同的贸易术语、结算方式、美元计价、上海起运、纸箱包装和商品监管条件。')
    qty = Decimal(str(case['product']['quantity']))
    price = Decimal(str(case['price_usd']))
    if qty <= 0 or price <= 0 or qty * price != Decimal(str(case['amount_usd'])):
        raise TradeReview('数量、单价或合同金额不一致。')


def foreign_contract(platform, case, budget, state_dir):
    _validate(case)
    rec = _Record(state_dir, case)
    ct = case['contract']
    s = platform.session(case['seller_account'], 'exporter')
    b = platform.session(case['buyer_account'], 'importer')
    if case['export_bank'] not in platform._accounts or case['import_bank'] not in platform._accounts:
        raise TradeReview('外销合同所指定的银行编号必须属于工作台已配置的账号。')
    draft = docs.contract(case)
    if 'created' not in rec.data['steps']:
        def recovered_creation():
            if _visible_state(s, ct, b.account) != '起草合同':
                return None
            url = _opened(open_action(s, ct, 'E2', '修改合同'))
            main = read_document(s, url).find('M07')
            expected = {'M0701': ct, 'M0736': s.account + '1', 'M0739': b.account + '2',
                        'M0742': case['export_bank'] + '4'}
            if main is None or any(main.findtext(k) != v for k, v in expected.items()):
                return None
            return url
        if rec.data.get('pending') == 'created':
            rec.run('created', None, recover=recovered_creation)
        else:
            desk = open_desk(s, 'E2')
            if _find_contract(s, desk, ct):
                raise TradeReview('合同号已在平台存在，请选择新编号或恢复原执行记录。')
            page = s.get(_action_url(desk, '起草合同'))
            fields = {'txtM0701': ct, 'txtM0739': b.account, 'txtM0742': case['export_bank'], 'chkMain': 'on'}
            rec.run('created', lambda: _opened(s.post(page, 'btnOk', fields)))
    if rec.data.get('pending') == 'sent_to_buyer':
        rec.run('sent_to_buyer', None, recover=lambda: (
            '进口商合同列表已显示该合同等待确认。' if _visible_state(b, ct, s.account) == '等待确认' else None))
    if rec.data.get('pending') == 'confirmed':
        rec.run('confirmed', None, recover=lambda: (
            '双方合同列表均已进入履约。' if
            _visible_state(s, ct, b.account) in ('正在进行', '已经完成') and
            _visible_state(b, ct, s.account) in ('正在进行', '已经完成') else None))
    if 'sent_to_buyer' not in rec.data['steps']:
        # Open the current named contract from the menu on each fresh role session.
        edit = _opened(open_action(s, ct, 'E2', '修改合同'))
        root = read_document(s, edit)
        if root.findall('M08'):
            _match_contract(root, draft, case)
        else:
            save_document(s, edit, **draft)
        _success(open_action(s, ct, 'E2', '检查合同'), '未发现错误')
        rec.put('documents', {'M50A': add_note(s, ct, 'M50A')})
        _budget(s, case, budget, 'M50A')
        rec.run('sent_to_buyer', lambda: _success(open_action(s, ct, 'E2', '合同送进口商'), '成功发送'))
    if 'confirmed' not in rec.data['steps']:
        edit = _opened(open_action(b, ct, 'I2', '修改合同'))
        _match_contract(read_document(b, edit), draft, case)
        save_document(b, edit, {'M0730':case['buyer']['company_en'], 'M0731':case['buyer']['person_en']})
        rec.put('documents', {'M50B': add_note(b, ct, 'M50B')})
        _budget(b, case, budget, 'M50B')
        _success(open_action(b, ct, 'I2', '检查合同'), '未发现错误')
        page = open_action(b, ct, 'I2', '确认合同')
        if page.fields.get('txtM0701') not in ('', ct):
            raise TradeReview('确认窗口的合同编号不一致。')
        rec.run('confirmed', lambda: _success(b.post(page, 'btnOk', {
            'txtM0701': ct, 'txtBankName': case['import_bank']}), '合同生效'))
    case['status'] = 'confirmed'
    return case


def _single_option(page, name, text):
    options = [o['value'] for c in page.controls if c.get('name') == name and 'disabled' not in c
               for o in c.get('options', []) if o.get('text', '').strip() == text and 'disabled' not in o]
    if len(options) != 1:
        raise TradeReview('平台未提供唯一的表单选项：' + text)
    return options[0]


def _issued(session, ct, typ):
    root = read_document(session, note(session, ct, _role_desk(session), typ))
    row = root.find(typ)
    if row is None or row.findtext(typ + '06') != ct or not row.findtext(typ + '01'):
        raise TradeReview(f'{typ} 签发文书与选定合同不一致。')
    return row


def _checked(session, rec, typ, draft):
    key = 'checked-' + typ
    if key in rec.data['steps']:
        return
    ct = rec.case['contract']
    desk = _role_desk(session)
    save_document(session, note(session, ct, desk, typ), **draft)
    result = note(session, ct, desk, typ, check=True)
    if '100%' not in result.text:
        raise TradeReview(typ + ' 单据检查未通过，请核对平台提示：' + result.text[:480])
    rec.data['steps'][key] = '100%'
    rec.persist()


def _insure(session, rec, budget):
    case = rec.case
    ct = case['contract']
    is_seller = session.role == 'exporter'
    if 'M25' not in case['documents']:
        rec.put('documents', {'M25': add_note(session, ct, 'M25')})
    if 'checked-M25' not in rec.data['steps']:
        _checked(session, rec, 'M25', docs.insurance(case, 'seller' if is_seller else 'buyer'))
    desk, forms = ('E7', ['M19', 'M25']) if is_seller else ('I8', ['M25'])
    if not is_seller and 'policy' not in rec.data['steps'] and rec.data.get('pending') != 'policy':
        from settlement import _guard
        _guard(session, case, case['buyer']['currency'], Decimal(budget['importer']['M5016']))
    rec.run('policy', lambda: send_docs(session, ct, desk, forms),
            recover=lambda: _notes(_list_page(session, ct, _role_desk(session))).get('M26', ('','',''))[2])
    if not case['documents'].get('M26'):
        policy = _issued(session, ct, 'M26')
        if (policy.findtext('M2610') != case['buyer']['company_en'] or
                policy.findtext('M2607') != case['documents']['M19'] or
                policy.findtext('M2618') != case['destination']):
            raise TradeReview('已签发保单的被保险人、发票号或目的港与合同不符。')
        rec.put('insurance', {'status':'issued'})
        rec.put('documents', {'M26':policy.findtext('M2601')})


def foreign_ship(platform, case, budget, state_dir):
    """Book and dispatch goods, then verify the agreed party's signed policy."""
    _validate(case)
    rec = _Record(state_dir, case)
    if 'confirmed' not in rec.data['steps'] or case.get('domestic', {}).get('status') != 'completed':
        raise TradeReview('外销合同须经买方确认，且本轮采购货物已交付。')
    ct = case['contract']
    s = platform.session(case['seller_account'], 'exporter')
    case['operation_date'] = date.today().isoformat()
    if case['payment'] == 'L/C':
        credit = case.get('lc') or {}
        if (case.get('status') != 'lc_accepted' or not case['documents'].get('M34')
                or credit.get('tenor') != 'AT SIGHT' or not all(credit.get(key) for key in
                ('number', 'issuing_bank', 'advising_bank', 'issue_date', 'expiry_date'))):
            raise TradeReview('出口商须先核对并接受本合同的即期信用证通知书。')
        if 'booking' not in rec.data['steps'] and (date.today() > date.fromisoformat(case['shipment_deadline'])
                or date.today() > date.fromisoformat(credit['expiry_date'])):
            raise TradeReview('装运期限或信用证有效期已过，需先核对改证及合同安排。')
    if 'documents_sent' in rec.data['steps']:
        if case['terms'] != 'CIF' and not case['documents'].get('M26'):
            _insure(platform.session(case['buyer_account'], 'importer'), rec, budget)
        case['status'] = 'shipped_documents_sent'
        return case
    bank_trade = case['payment'] != 'T/T'
    origin_type = 'M03B' if bank_trade else 'M03A'
    forms = ['M14','M19','M21',origin_type,'M05A','M44']
    if bank_trade:
        forms.append('M42')
    for typ in forms:
        if typ not in rec.data['saved'].get('documents', {}):
            rec.put('documents', {typ: add_note(s, ct, typ)})
    if 'carrier' not in rec.data['steps']:
        operator, desk = (platform.session(case['buyer_account'],'importer'), 'I3') if case['terms']=='FOB' else (s, 'E4')
        page = open_action(operator, ct, desk, '指定船公司')
        carriers = _marks(page, 'radio')
        carrier = case.get('carrier') or (carriers[0] if len(carriers) == 1 else None)
        if carrier not in carriers:
            raise TradeReview('请从当前平台列出的船公司中指定一家承运人。')
        rec.run('carrier', lambda: _success(operator.post(page, 'btnOk', {'flag': carrier, 'mark': carrier})))
    _checked(s, rec, 'M14', docs.shipping_request(case))
    if 'booking' not in rec.data['steps']:
        page = open_action(s, ct, 'E4', '洽订舱位')
        option = _single_option(page, 'txtContype', '拼箱')
        rec.run('booking', lambda: _success(s.post(page, 'btnOk', {
            'txtContype': option, 'txtSdate': case['operation_date']}), '成功洽订舱位'),
            recover=lambda: _notes(_list_page(s, ct, 'E2')).get('M16', ('','',''))[2])
    if not case['ship'].get('allocation'):
        row = _issued(s, ct, 'M16')
        sailing = date.fromisoformat(row.findtext('M1608'))
        freight = re.fullmatch(r'USD\s+([0-9,.]+)', row.findtext('M1615', ''))
        if (not freight or row.findtext('M1609') != case['destination']
                or not row.findtext('M1612') or not row.findtext('M1613')):
            raise TradeReview('订舱回执的目的港、船名、航次或运费需要核对。')
        rec.put('ship', dict(allocation=row.findtext('M1601'), date=sailing.isoformat(),
                vessel=row.findtext('M1612'), voyage=row.findtext('M1613'),
                freight_usd=freight[1].replace(',',''), eta=(sailing + timedelta(days=10)).isoformat(),
                eta_basis='装船通知使用预计十天航程，实际到港以提货单为准。'))
    pair = docs.invoice_packing(case)
    drafts = {'M19':pair['invoice'], 'M21':pair['packing'], 'M05A':docs.customs(case)}
    if bank_trade:
        drafts.update(docs.bank_documents(case))
    else:
        drafts['M03A'] = docs.origin(case)
    for typ, draft in drafts.items():
        _checked(s, rec, typ, draft)
    if case['terms'] == 'CIF':
        _insure(s, rec, budget)
    elif bank_trade:
        # The buyer takes out cover on the agreed voyage before bank presentation.
        _insure(platform.session(case['buyer_account'], 'importer'), rec, budget)
    if 'origin' not in rec.data['steps']:
        page = open_action(s, ct, 'E5', '申请产地证')
        choices = [o.get('value') for c in page.controls if c.get('name') == 'txtContype'
                   for o in c.get('options', []) if 'disabled' not in o]
        if origin_type not in choices:
            raise TradeReview('签发窗口没有合同要求的原产地证明书。')
        rec.run('origin', lambda: _success(s.post(page, 'btnOk', {'txtContype':origin_type}), '成功申请'))
    rec.run('customs_delivery', lambda: _success(open_action(s, ct, 'E9', '送货'), '送货成功'))
    rec.run('customs_clearance', lambda: send_docs(s, ct, 'E9', ['M19','M21','M05A']))
    rec.run('bill_of_lading', lambda: _success(open_action(s, ct, 'E4', '取回提单'), '成功取回'),
            recover=lambda: _notes(_list_page(s, ct, 'E2')).get('M01', ('','',''))[2])
    if not case['documents'].get('M01'):
        bill = _issued(s, ct, 'M01')
        if bill.findtext('M0113') != case['loading_port'] or bill.findtext('M0114') != case['destination']:
            raise TradeReview('提单起运港或目的港与合同不一致。')
        rec.put('documents', {'M01':bill.findtext('M0101')})
    _checked(s, rec, 'M44', docs.advice(case))
    rec.run('notice', lambda: _success(open_action(s, ct, 'E4', '发送装船通知'), '成功发送装船通知'))
    packet = ['M19','M21',origin_type,'M01']
    if bank_trade:
        packet.append('M42')
    if case['terms'] == 'CIF':
        packet.append('M26')
    if bank_trade:
        from collection_workflow import present_bank_documents
        case['bank_packet'] = packet
        present_bank_documents(platform, rec)
        case['status'] = 'shipped_for_bank'
        return case
    rec.run('documents_sent', lambda: send_docs(s, ct, 'E2', packet))
    if case['terms'] != 'CIF':
        _insure(platform.session(case['buyer_account'], 'importer'), rec, budget)
    case['status'] = 'shipped_documents_sent'
    return case
