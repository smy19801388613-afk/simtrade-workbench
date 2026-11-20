"""Issue and advise one contract's sight credit using the four participants' normal desks."""
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urljoin, urlsplit
import re

from foreign_trade import TradeReview, open_desk, _actions, _select_contract, _action_url, _opened, _receipt, _success
from live_platform import Page, _menu_link
from student_document import read_document, save_document
from pipeline import read, write
import lc_forms

LABELS={'M31':'信用证申请书','M30':'信用证','M34':'信用证通知书'}


def _list(session,ct):
    if session.role in ('import_bank','export_bank'):
        menu=session.get(_menu_link(session.home,'Credit.aspx'))
        return session.get(_menu_link(menu,'CreditOperate.aspx',no='7' if session.role=='import_bank' else '4'))
    buyer=session.role=='importer'
    desk=open_desk(session,'I6' if buyer else 'E10')
    _select_contract(session,desk,ct)
    suffix='/bankcreditlist.aspx' if buyer else '/bankapplycredit.aspx'
    labels={label for url,_,_,_,label in _actions(desk) if urlsplit(url).path.lower().endswith(suffix)}
    if len(labels)!=1:raise TradeReview('该角色的信用证管理入口发生变化。')
    return session.get(_action_url(desk,labels.pop(),ct))


def _matching(page,ct,typ,bank):
    hits=[]
    for html in re.findall(r'<tr\b[^>]*>(?:(?!<tr\b).)*?</tr>', page.html,re.I|re.S):
        if 'mark' not in html:continue
        tr=Page(page.url,'<table>'+html+'</table>')
        if len(tr.rows)!=1:continue
        cells=tr.rows[0]
        if bank:
            if len(cells)<6 or cells[2]!=LABELS[typ] or cells[4]!=ct:continue
            number,status=cells[3],cells[5]
        else:
            if len(cells)<5 or cells[3]!=LABELS[typ]:continue
            number,status=cells[2],cells[4]
        marks=[f.get('value') for f in tr.controls if f.get('name')=='mark']
        opens=[e for e in tr.events if re.search(r'[$:]lbtn$',e)]
        checks=[e for e in tr.events if re.search(r'[$:]lbtn1$',e)]
        if len(opens)!=1 or len(marks)!=1 or not number:raise TradeReview('信用证列表行缺少单据号或打开入口。')
        hits.append(dict(page=page,mark=marks[0],open=opens[0],check=checks[0] if len(checks)==1 else '',number=number,status=status))
    if len(hits)>1:raise TradeReview(f'同一合同存在多份 {LABELS[typ]}，需核对。')
    return hits[0] if hits else None


def _find(session,ct,typ,required=True):
    page=_list(session,ct)
    first=page.fields.get('ctrPages:pages')
    pages=[op['value'] for el in page.controls if el.get('name')=='ctrPages:pages' for op in el.get('options',[]) if op.get('value')!=first]
    for index in [None]+pages:
        if index:page=session.post(page,'ctrPages$pages',{'ctrPages:pages':index})
        row=_matching(page,ct,typ,session.role.endswith('_bank'))
        if row:return row
    if required:raise TradeReview(f'{session.role} 的信用证管理页面尚未显示合同 {ct} 的{LABELS[typ]}。')
    return None


def _open(session,ct,typ):
    row=_find(session,ct,typ)
    url=_opened(session.post(row['page'],row['open']))
    root=read_document(session,url)
    node=root.find(typ)
    if node is None or node.findtext(typ+'01')!=row['number'] or node.findtext(typ+'06')!=ct:
        raise TradeReview('信用证单据编号或关联合同与所选业务不一致。')
    return url,{f.tag:f.text or '' for f in node}


def _norm(field,value):
    text=' '.join(str(value).split())
    if field in {'M3118','M3162','M3163','M3167','M3169','M3174','M3184','M3015','M3418'}:
        try:
            # The bound numeric boxes render unused quantities as zero and strip trailing cents.
            return Decimal(text or '0')
        except InvalidOperation:
            pass
    return text


def _compare(raw,expected):
    diff=[k for k,v in expected.items() if k not in raw or _norm(k,raw[k])!=_norm(k,v)]
    if diff:raise TradeReview('信用证与本合同约定不一致，请核对字段 '+', '.join(diff[:6])+'。')


def _fill(session,ct,typ,expected):
    url,raw=_open(session,ct,typ)
    if any(k not in raw or _norm(k,raw[k])!=_norm(k,v) for k,v in expected.items()):
        save_document(session,url,expected)
        _,raw=_open(session,ct,typ)
    _compare(raw,expected)
    row=_find(session,ct,typ)
    if not row['check']:raise TradeReview('单据列表未提供检查按钮。')
    response=session.post(row['page'],row['check'])
    check=re.search(r"""showModalDialog\(\s*['"]([^'"]+)""",'\n'.join(response.scripts))
    if check:response=session.get(urljoin(response.url,check[1]))
    if '100%' not in _receipt(response) and '未发现错误' not in _receipt(response):
        raise TradeReview(typ+' 检查尚未通过：'+_receipt(response)[:220])
    return raw


def ensure_credit(platform,case,budget,state_dir):
    ct=case['contract']
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,40}',ct) or case['payment']!='L/C':
        raise TradeReview('信用证流程需要当前已签订的 L/C 合同。')
    identity=[ct,case['terms'],case['amount_usd'],case['seller_account'],case['buyer_account'],case['import_bank'],case['export_bank']]
    path=Path(state_dir)/('lc-'+ct+'.json')
    journal=read(path) if path.exists() else dict(identity=identity,dates={},steps={})
    if journal.get('identity')!=identity:raise TradeReview('本地信用证存档与本次合同不符。')
    if 'result' in journal:
        case['lc'].update(journal['result']);case.setdefault('documents',{}).update(journal['documents']);case['status']='lc_accepted';return case
    b=platform.session(case['buyer_account'],'importer')
    e=platform.session(case['seller_account'],'exporter')
    ib=platform.session(case['import_bank'],'import_bank')
    eb=platform.session(case['export_bank'],'export_bank')
    ip=platform.profile(case['import_bank'],'import_bank');ep=platform.profile(case['export_bank'],'export_bank')
    def today(key):
        if key not in journal['dates']:
            journal['dates'][key]=date.today().isoformat();write(path,journal)
        return journal['dates'][key]
    def found(session,typ,statuses=None):
        row=_find(session,ct,typ,False)
        return bool(row and (statuses is None or row['status'] in statuses))
    def action(key,session,typ,event,proof):
        if key in journal['steps']:return
        if proof():msg='已核实对应角色的合同单据及进度'
        else:
            if journal.get('pending'):raise TradeReview('请核实信用证步骤 '+journal['pending']+' 的回执后续办。')
            if typ:
                row=_find(session,ct,typ);page=row['page'];fields={'mark':row['mark'],'flag':row['mark']}
            else:page=_list(session,ct);fields={}
            if event not in page.events:raise TradeReview('当前信用证页面未提供操作 '+event+'。')
            journal['pending']=key;write(path,journal)
            response=session.post(page,event,fields)
            msg=_receipt(response)
            # Creating a new draft may only change the grid; the row itself is the receipt.
            if '成功' not in msg and not proof():
                raise TradeReview('信用证步骤 '+key+' 需核对回执：'+msg[:240])
            msg='学生端已接受此操作' if '成功' in msg else '已读回对应单据'
        journal['steps'][key]=msg;journal.pop('pending',None);write(path,journal)
    def complete_field(key,session,typ,fields):
        if key in journal['steps']:return
        _fill(session,ct,typ,fields)
        journal['steps'][key]='已回读所填字段，并通过平台检查';write(path,journal)
    app=lc_forms.application(case,ip,ep,today('application'))
    action('创建开证申请',b,None,'btnAddCredit',lambda:found(b,'M31'))
    complete_field('填写开证申请',b,'M31',app)
    if '递交开证申请' not in journal['steps'] and not journal.get('pending'):
        from settlement import _guard
        _guard(b,case,case['buyer']['currency'],budget['importer']['M5017'])
    action('递交开证申请',b,'M31','btnSend',lambda:found(ib,'M31'))
    _,read_app=_open(ib,ct,'M31');_compare(read_app,app)
    draft=lc_forms.credit(case,read_app,today('issue'))
    action('开立信用证',ib,'M31','btn1',lambda:found(ib,'M30'))
    complete_field('填写信用证',ib,'M30',draft)
    action('发送进口商确认',ib,'M30','btn2',lambda:found(b,'M30'))
    _,buyer_credit=_open(b,ct,'M30');_compare(buyer_credit,draft)
    action('进口商确认',b,'M30','btnYes',lambda:found(ib,'M30',{'已确认','已到出口地银行'}))
    action('开证行转通知行',ib,'M30','btn3',lambda:found(eb,'M30'))
    _,issued=_open(eb,ct,'M30');_compare(issued,draft)
    if not issued.get('M3009') or issued['M3009']!=buyer_credit['M3009']:
        raise TradeReview('开证行签发编号与买方收到的信用证编号不符。')
    notice=lc_forms.notice(issued,ip,ep,today('notice'))
    action('建立信用证通知书',eb,'M30','btnFF',lambda:found(eb,'M34'))
    complete_field('填写通知书',eb,'M34',notice)
    action('通知出口商',eb,'M34','btnJJ',lambda:found(e,'M34'))
    _,review_credit=_open(e,ct,'M30');_compare(review_credit,{**draft,'M3009':issued['M3009']})
    _,review_notice=_open(e,ct,'M34');_compare(review_notice,notice)
    action('出口商接受通知',e,'M34','btnOk',lambda:found(e,'M34',{'已接受','已经接受','接受'}))
    journal['result']=dict(number=issued['M3009'],issuing_bank=ip['txtB0307'],advising_bank=ep['txtB0307'],
                           issue_date=datetime.strptime(issued['M3010'],'%y%m%d').date().isoformat(),
                           issue_date_raw=issued['M3010'],expiry_date=notice['M3420'],tenor='AT SIGHT',
                           goods_description=app['M3176'],accepted=True)
    journal['documents']={'M31':read_app['M3101'],'M30':issued['M3001'],'M34':review_notice['M3401']}
    write(path,journal);case['lc'].update(journal['result']);case.setdefault('documents',{}).update(journal['documents'])
    case['status']='lc_accepted'
    return case

