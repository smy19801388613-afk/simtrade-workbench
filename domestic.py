"""Run a single exporter–factory domestic order through student-visible forms."""
from contextlib import contextmanager
from datetime import date, timedelta, datetime
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import re
from urllib.parse import urljoin, urlsplit, parse_qs

from live_platform import ROLES, _menu_link, _contract_rows
from student_document import read_document, save_document
from wording import cn_money


class DomesticReview(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise DomesticReview(message)


def amount(value):
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise DomesticReview('金额或数量格式不符。') from None
    require(result.is_finite(), '金额或数量必须为有限数。')
    return result


def cash_value(value):
    match = re.fullmatch(r'RMB\s+(-?[\d,]+(?:\.\d+)?)', value.strip())
    require(match, '当前财务或库存单价格式、币种无法核实。')
    return amount(match[1].replace(',', ''))


def _cash(session):
    code = ROLES[session.role][0]
    page = session.get(_menu_link(session.home, 'Finance.aspx', cls=code))
    return cash_value(page.fields.get('B0342', ''))


def _pages(session, page):
    selector = next((a for a in page.controls if a.get('name') == 'ctrPages:pages'), {})
    numbers = [o.get('value', o.get('text', '')).strip() for o in selector.get('options', [])]
    active = page.fields.get('ctrPages:pages', '')
    yield page
    for n in numbers:
        if n != active:
            page = session.post(page, 'ctrPages$pages', {'ctrPages:pages': n})
            require(page.fields.get('ctrPages:pages') == n, '列表未切换至指定页。')
            yield page


def _stock(session, product, unit):
    code = ROLES[session.role][0]
    initial = session.get(_menu_link(session.home, 'Stock.aspx', cls=code))
    entries = []
    for page in _pages(session, initial):
        require('库存货品列表' in page.text and '库存成本' in page.text, '库存页面表头无法识别。')
        entries += [row for row in page.rows if len(row) == 4 and row[0] == product]
    require(len(entries) <= 1, '同一商品出现多条库存记录，请核对。')
    if not entries:
        return Decimal(0), Decimal(0)
    q = re.fullmatch(r'([\d,]+(?:\.\d+)?)\s+' + re.escape(unit), entries[0][2], re.I)
    require(q, '库存计量单位与订单不符。')
    return amount(q[1].replace(',', '')), cash_value(entries[0][3])


def _desk(session, id_):
    site = session.get(_menu_link(session.home, ROLES[session.role][2]))
    return session.get(_menu_link(site, 'ShowPopUp.aspx', p3=id_))


def _market(factory, order):
    desk = _desk(factory, 'F3')
    for page in _pages(factory, factory.get(_menu_link(desk, 'MarketList.aspx'))):
        require('市场货品列表' in page.text, '工厂市场页无法识别。')
        for row in page.rows:
            if len(row) >= 5 and row[1] == order['product_id']:
                require(row[-2].upper() == order['unit'].upper(), '市场计量单位与计划不同。')
                require(cash_value(row[-1]) == amount(order['expected_unit_cost_rmb']), '市场生产成本与预算不同。')
                return page
    raise DomesticReview('工厂市场没有当前订单商品。')


def _profile(session):
    code, _, _, name, _ = ROLES[session.role]
    profile = session.get(_menu_link(session.home, name, cls=code)).fields
    require(profile.get('txtB0301') == session.account, '档案页账号与操作角色不符。')
    tax_code = 'txtB0303' if session.role == 'exporter' else 'txtB0328'
    required = ('txtB0306','txtB0308','txtB0305','txtB0312',tax_code,'txtB0330','txtB0311')
    require(all(profile.get(k, '').strip() for k in required), '公司资料缺少签章所需信息。')
    return [profile[k].strip() for k in required]


def _contract(session, ct, partner, select=False):
    desk = _desk(session, 'E3' if session.role == 'exporter' else 'F2')
    page = session.get(desk.links['btnCheck'])
    for page in _pages(session, page):
        for row in _contract_rows(page, 'domestic'):
            if row['number'] == ct:
                require(row['counterparty'].split('(')[0].strip() == partner, '合同号已关联另一交易方。')
                if select:
                    session.post(page,'btnOk',{'flag':ct,'mark':ct})
                    current = session.get(_menu_link(desk,'MainiContract.aspx'))
                    require(re.search(r'合同编号\s*[：:]\s*' + re.escape(ct) + r'(?:\s|$)',current.text),
                            '当前主合同与已选合同不符。')
                return row['status']
    return None


def _action(session, ct, desk_id, code, submit=None):
    desk = _desk(session, desk_id)
    name = 'ExportDoPost.aspx' if session.role == 'exporter' else 'FactoryDoPost.aspx'
    url = _menu_link(desk, name, cls=code) + '&id=' + ct
    return session.get(url) if submit is None else submit(session.get,url)


def _docurl(page, ct):
    choices = [u for u in page.urls if urlsplit(u).path.lower().endswith('/doc/m12.aspx') and
               parse_qs(urlsplit(u).query).get('did') == [ct]]
    require(len(choices) == 1, '未找到与本单匹配的国内合同窗口。')
    return choices[0]


def _draft(order, buyer_profile, factory_profile, signed):
    qty = str(order['quantity'])
    total = amount(qty) * amount(order['unit_price_rmb'])
    end = order.get('delivery_date') or (date.fromisoformat(signed) + timedelta(days=15)).isoformat()
    require(date.fromisoformat(end) >= date.fromisoformat(signed), '交货日早于签约日。')
    values = {
        'M1207':factory_profile[0], 'M1208':buyer_profile[0], 'M1209':signed,
        'M1210':order['signing_city'], 'M1211':order['unit'], 'M1212':qty, 'M1214':str(total),
        'M1216':f'{end}前交付{qty} {order["unit"]}。', 'M1217':cn_money(total),
        'M1218':'本合同为含税价格，供方交付货物清单及相应票据。',
        'M1219':'货物应符合所列规格，包装完好。因制造质量产生的问题由供方承担。',
        'M1220':factory_profile[1] + '，供方仓库交货，买方验收提货。',
        'M1221':'买方安排提货并承担后续运输费用。',
        'M1222':f'出口纸箱包装，共{order["cartons"]}箱。包装费用计入合同价。',
        'M1223':'买方按合同规格、数量和包装验收，有异议应在交货后三十日内书面通知供方。',
        'M1224':f'交货时买方一次付清人民币{total:.2f}元。',
        'M1225':'违约方承担因违约造成的直接损失，双方应及时协商补救。',
        'M1226':'双方先行协商，协商不成可向有管辖权的人民法院起诉。',
    }
    values.update(zip(('M1235','M1236','M1237','M1238','M1239','M1241','M1242'),buyer_profile))
    item = {'M1303':order['product_id'], 'M1304':order['description_cn'], 'M1305':order['unit'],
            'M1306':qty, 'M1307':f'{amount(order["unit_price_rmb"]):.2f}', 'M1308':f'{total:.2f}'}
    sign = dict(zip(('M1227','M1228','M1229','M1230','M1231','M1233','M1234'),factory_profile))
    return values, item, sign


def _matches(doc, ct, buyer, factory, values, item):
    fields = {'M1201':ct,'M1243':buyer.account+'1','M1245':factory.account+'3',**values}
    if doc.find('M12') is None or len(doc.findall('M13')) != 1:
        return False
    numerical = {'M1212','M1214','M1306','M1307','M1308'}
    for row, entries in ((doc.find('M12'), fields),(doc.find('M13'), {**item, 'M1309':'RMB'})):
        for key, expected in entries.items():
            actual = row.findtext(key) or ''
            if key in numerical:
                if not actual or amount(actual) != amount(expected):
                    return False
            elif actual.strip() != str(expected).strip():
                return False
    return True


def _ledger(session, ct):
    code = ROLES[session.role][0]
    finance = session.get(_menu_link(session.home, 'Finance.aspx', cls=code))
    ledger = session.get(_menu_link(finance,'Finance_Dialog.aspx'))
    require('项目描述' in ledger.text and '合同号' in ledger.text, '日记账表头无法识别。')
    # Newly submitted orders are normally found on the first page. The final
    # per-contract calculation uses all entries rather than unrelated transactions.
    return [row for page in _pages(session, ledger) for row in page.rows if len(row)==5 and row[3]==ct]


@contextmanager
def _lock(path):
    # OS releases this advisory lock even when the process crashes.
    f = path.open('a+b')
    try:
        f.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        raise DomesticReview('该合同已有运行中的工作台进程。') from None
    try:
        yield
    finally:
        f.close()


class _Journal:
    def __init__(self, path, identity):
        self.path = path
        self.data = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {
            'identity':identity,'signed':date.today().isoformat(),'steps':{}}
        require(self.data['identity'] == identity, '同号订单与已保存的角色或交易条件不同。')

    def save(self):
        temp = self.path.with_suffix('.tmp')
        with temp.open('w',encoding='utf-8') as f:
            json.dump(self.data,f,ensure_ascii=False,indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp,self.path)

    def step(self, name, action, verify, *, accept_existing=False):
        steps = self.data['steps']
        if steps.get(name,{}).get('status') == 'done':
            return
        if name not in steps and accept_existing and verify():
            steps[name] = {'status':'done','observed_at':datetime.now().astimezone().isoformat()}
            self.save()
            return
        if name not in steps:
            def dispatch(method,*args):
                steps[name] = {'status':'pending','at':datetime.now().astimezone().isoformat()}
                self.save()
                return method(*args)
            result = action(dispatch)
            require(name in steps,'操作未进入提交阶段。')
            if result is not None:
                steps[name]['receipt'] = result.text[:350]
                self.save()
        # Even if the response was lost, never send a production, release or tax
        # request again. Attribute the result independently through the UI.
        require(verify(), '步骤「' + name + '」已请求，但前台回读尚未确认。请核对对应合同和日记账。')
        steps[name]['status'] = 'done'
        self.save()


def run_domestic(buyer, factory, order, state_dir):
    require((buyer.role,factory.role) == ('exporter','factory') and buyer.base_url == factory.base_url,
            '本单需要同一学生平台的出口商和工厂会话。')
    keys = ('contract','product_id','description_cn','unit','quantity','cartons','unit_price_rmb',
            'expected_unit_cost_rmb','expected_profit_rmb','signing_city')
    require(all(k in order and str(order[k]).strip() for k in keys), '国内订单缺少合同、货物、价格或预算字段。')
    optional = ('delivery_date','available_factory_stock','buyer_cash_floor_rmb','factory_cash_floor_rmb')
    order = {k:str(order[k]).strip() for k in (*keys,*optional) if k in order}
    ct = order['contract']
    require(re.fullmatch(r'[A-Z0-9]{1,40}',ct) and re.fullmatch(r'\d{5}',order['product_id']),
            '合同号须为 1–40 位大写字母或数字，商品号须为五位数字。')
    require(all(re.fullmatch(r'[1-9]\d*',order[k]) for k in ('quantity','cartons')), '数量与箱数须为正整数。')
    n, price, cost, profit = (amount(order[k]) for k in ('quantity','unit_price_rmb','expected_unit_cost_rmb','expected_profit_rmb'))
    require(min(price,cost,profit) > 0 and price == price.quantize(Decimal('.01')) and profit <= n*(price-cost),
            '国内订单预估利润、生产成本或成交单价有误。')
    root = Path(state_dir)
    root.mkdir(parents=True, exist_ok=True)
    with _lock(root/('domestic-' + ct + '.lock')), buyer.lock, factory.lock:
        journal = _Journal(root/('domestic-' + ct + '.json'),
                           dict(platform=buyer.base_url,buyer=buyer.account,factory=factory.account,order=order))
        return _run(buyer,factory,order,journal,n,price,cost,profit)


def _run(buyer,factory,order,journal,n,price,cost,profit):
    data = journal.data
    if 'result' in data:
        return data['result']
    ct, product, unit = (order[k] for k in ('contract','product_id','unit'))
    total = n*price
    if 'before' not in data or not data['steps']:
        data['signed'] = date.today().isoformat()
        require(_contract(buyer,ct,factory.account) is None and _contract(factory,ct,buyer.account) is None,
                '合同号已存在，工作台不会接管未由本单创建的合同。')
        _market(factory,order)
        opening, book = _stock(factory,product,unit)
        usable = amount(order.get('available_factory_stock','0'))
        require(0 <= usable <= min(n,opening) and usable == usable.to_integral_value(), '可用于本单的现货数量超出范围。')
        require(not usable or book == cost, '现货账面单价与预算成本不同。')
        fc, bc = _cash(factory), _cash(buyer)
        bf = amount(order.get('buyer_cash_floor_rmb','0'))
        ff = amount(order.get('factory_cash_floor_rmb','0'))
        require(min(bf,ff) >= 0 and fc-(n-usable)*cost >= ff and bc-total >= bf,
                '采购或生产后的余额将低于资金保留额。')
        data['before'] = dict(buyer_cash=str(bc),factory_cash=str(fc),factory_stock=str(opening),
                              buyer_stock=str(_stock(buyer,product,unit)[0]),produce=str(n-usable))
        journal.save()
    before = {k:amount(v) for k,v in data['before'].items()}
    values, item, signature = _draft(order, _profile(buyer), _profile(factory),data['signed'])
    def bstatus():
        return _contract(buyer,ct,factory.account)
    def fstatus():
        return _contract(factory,ct,buyer.account)
    def create(submit):
        require(bstatus() is None and fstatus() is None, '同号合同已出现在平台，请核对订单来源。')
        desk = _desk(buyer,'E3')
        page = buyer.get(_menu_link(desk,'E3_Contract_add.aspx'))
        return submit(buyer.post,page,'btnOk',{'txtM1201':ct,'txtM1245':factory.account,'chkMain':'on'})
    def edit(s):
        export = s is buyer
        _contract(s,ct,factory.account if export else buyer.account,select=True)
        return _docurl(_action(s,ct,'E3' if export else 'F2','E301' if export else 'F201'),ct)
    def save_buyer(submit):
        url = edit(buyer)
        return save_document(buyer,url,values,items=[item],child_url=urljoin(url,'CM13.aspx?a1='+ct+'&a2='),submit=submit)
    def checked(s, desk, check, code, submit):
        p = _action(s,ct,desk,check)
        require('未发现错误' in p.text, '平台合同检查未通过：' + p.text[:250])
        return _action(s,ct,desk,code,submit)
    def signed_doc():
        return _matches(read_document(factory,edit(factory)),ct,buyer,factory,{**values,**signature},item)
    def sign_factory(submit):
        url = edit(factory)
        require(_matches(read_document(factory,url),ct,buyer,factory,values,item),
                '工厂收到的合同与本单货物、金额或买方资料不符。')
        return save_document(factory,url,signature,submit=submit)
    journal.step('create',create,lambda:bstatus() == '起草合同')
    journal.step('draft',save_buyer,lambda:_matches(read_document(buyer,edit(buyer)),ct,buyer,factory,values,item))
    journal.step('send',lambda s:checked(buyer,'E3','E302','E304',s),lambda:fstatus() == '等待确认')
    journal.step('signature',sign_factory,signed_doc)
    journal.step('confirm',lambda s:checked(factory,'F2','F202','F205',s),lambda:fstatus() == '正在进行')
    def produce(submit):
        market = _market(factory,order)
        url = _menu_link(market,'F3_Produce.aspx') + product
        form = factory.get(url)
        require(form.fields.get('txtB0401') == product, '生产页货号不同。')
        require(_stock(factory,product,unit)[0] == before['factory_stock'] and
                _cash(factory) == before['factory_cash'], '工厂库存或资金已变化，生产预算需要复核。')
        return submit(factory.post,form,'btnOk',{'txtINumber':str(before['produce'])})
    def produced():
        return (_stock(factory,product,unit)[0] == before['factory_stock'] + before['produce']
                and _cash(factory) == before['factory_cash'] - before['produce']*cost)
    if before['produce']:
        journal.step('produce',produce,produced)
    def release(submit):
        require(produced() and _stock(buyer,product,unit)[0] == before['buyer_stock'] and
                _cash(buyer) == before['buyer_cash'], '放货前双方库存或资金已变化。')
        _contract(factory,ct,buyer.account,select=True)
        return _action(factory,ct,'F2','F206',submit)
    def released():
        if (_stock(buyer,product,unit)[0] != before['buyer_stock'] + n or
                _stock(factory,product,unit)[0] != before['factory_stock'] + before['produce'] - n or
                before['buyer_cash'] - _cash(buyer) != total):
            return False
        incoming = [cash_value(r[1]) for r in _ledger(factory,ct) if r[4] == '货款' and r[1]]
        outgoing = [cash_value(r[2]) for r in _ledger(buyer,ct) if r[4] == '货款' and r[2]]
        return incoming == [total] and outgoing == [total]
    journal.step('release',release,released,accept_existing=True)
    def tax(submit):
        _contract(factory,ct,buyer.account,select=True)
        return _action(factory,ct,'F4','F401',submit)
    journal.step('tax',tax,lambda:fstatus() == '已经完成',accept_existing=True)
    ledger = _ledger(factory,ct)
    income = sum((cash_value(r[1]) for r in ledger if r[1]), Decimal(0))
    expenses = sum((cash_value(r[2]) for r in ledger if r[2]), Decimal(0))
    actual = income-expenses-n*cost
    final_cash = _cash(factory)
    require(income == total and actual > 0 and abs(actual-profit) <= Decimal('.02') and
            abs(final_cash-before['factory_cash']-(income-expenses-before['produce']*cost)) <= Decimal('.02'),
            '平台合同已完成，流水利润与预算或现金变动有差异。请复核该单，切勿重新提交。')
    result = {'contract':ct,'status':'completed','actual_profit_rmb':f'{actual:.2f}',
              'factory_cash_before':str(before['factory_cash']),'factory_cash_after':str(final_cash),
              'buyer_cash_spent_rmb':f'{total:.2f}','buyer_stock_received':int(n),
              'factory_ledger':ledger,'evidence':str(journal.path.resolve())}
    data['result'] = result
    journal.save()
    return result
