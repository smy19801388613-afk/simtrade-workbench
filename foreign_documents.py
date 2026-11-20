"""Editable student document fields for a signed ocean freight transaction."""
from datetime import date
from decimal import Decimal, Decimal as D, InvalidOperation, ROUND_HALF_UP
from urllib.parse import urlencode
import re


def text_number(value):
    return format(D(str(value)).normalize(), 'f')


def contract(case):
    s, b, g = case['seller'], case['buyer'], case['product']
    named_port = case['loading_port'] if case['terms'] == 'FOB' else case['destination']
    values = {
        'M0707': s['company_en'], 'M0708': s['address_en'],
        'M0709': b['company_en'] + '\n' + b['address_en'], 'M0711': case['date'],
        'M0712': case['terms'], 'M0713': named_port,
        'M0714': text_number(g['quantity']), 'M0715': g['unit'], 'M0717': case['currency'],
        'M0718': f"{case['amount_usd']:.2f}", 'M0719': case['amount_words'],
        'M0720': case['payment'], 'M0735': case['payment_clause'],
        'M0721': case['packing_clause'], 'M0722': case['loading_port'],
        'M0723': case['destination'], 'M0724': case['shipment_clause'],
        'M0725': case['mark'],
        'M0726': g['name_en'] + ' ' + g['spec_en'] + '. AS PER AGREED PRODUCT SPECIFICATION. ' + case['claim_clause'],
        'M0727': case['insurance_clause'], 'M0729': case['documents_clause'],
        'M0732': s['company_en'], 'M0733': s['person_en'],
    }
    return {'values': values, 'child_url': 'CM08.aspx?a1=' + case['contract'] + '&a2=',
            'items': [{'M0803': g['id'], 'M0804': g['name_en'] + ' ' + g['spec_en'],
                       'M0805': text_number(g['quantity']), 'M0806': g['unit'], 'M0807': case['currency'],
                       'M0808': f"{case['price_usd']:.2f}", 'M0809': f"{case['amount_usd']:.2f}"}]}


_SMALL = (
    "ZERO ONE TWO THREE FOUR FIVE SIX SEVEN EIGHT NINE TEN ELEVEN TWELVE "
    "THIRTEEN FOURTEEN FIFTEEN SIXTEEN SEVENTEEN EIGHTEEN NINETEEN"
).split()
_TENS = "ZERO TEN TWENTY THIRTY FORTY FIFTY SIXTY SEVENTY EIGHTY NINETY".split()
_MONTHS = (
    "", "JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY",
    "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER",
)


def _required(mapping, key):
    value = mapping.get(key)
    if value is None or not str(value).strip():
        raise ValueError(f"Missing required field: {key}")
    return str(value)


def _decimal(value, label):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Invalid number: {label}") from exc
    if not number.is_finite() or number <= 0:
        raise ValueError(f"{label} must be finite and positive")
    return number


def _plain(number):
    return format(number.normalize(), "f")


def _words(number):
    if not isinstance(number, int) or not 0 <= number < 1_000_000_000:
        raise ValueError("English amount/package wording supports integer values below 1 billion")
    if number < 20:
        return _SMALL[number]
    if number < 100:
        return _TENS[number // 10] + (" " + _words(number % 10) if number % 10 else "")
    for size, label in ((1_000_000, "MILLION"), (1_000, "THOUSAND"), (100, "HUNDRED")):
        if number >= size:
            return _words(number // size) + " " + label + (
                " " + _words(number % size) if number % size else ""
            )
    raise AssertionError("Unreachable")


def _document_id(documents, form, prefix):
    value = documents.get(form)
    if isinstance(value, dict):
        value = value.get("id") or value.get("document_id") or value.get("document_no")
    if not isinstance(value, str) or not re.fullmatch(prefix + r"\d+", value):
        raise ValueError(f"case.documents.{form} must contain its actual {prefix}... document ID")
    return value


def invoice_packing(case):
    """Return {'invoice': {values, child_url, items}, 'packing': {...}}.

    This function does not mutate case. All parties and product text are copied
    from case. Use the carton, weight, and volume totals established by the current quote.
    Optional case.amount_words can carry the exact checked contract wording.
    """
    seller, buyer, product = case['seller'], case['buyer'], case['product']
    documents = case.get('documents', {})
    invoice_id = _document_id(documents, 'M19', 'STINV')
    packing_id = _document_id(documents, 'M21', 'STPLT')
    ship = case.get('ship')
    if not isinstance(ship, dict):
        raise ValueError('case.ship must contain the confirmed vessel, voyage and date')
    vessel, voyage = _required(ship, 'vessel'), _required(ship, 'voyage')
    sailing_date = date.fromisoformat(_required(ship, 'date'))
    issue_date = date.fromisoformat(case.get('operation_date') or _required(case, 'date')).isoformat()
    quantity = _decimal(product.get('quantity'), 'quantity')
    cartons = _decimal(product.get('cartons'), 'cartons')
    price = _decimal(case.get('price_usd'), 'price_usd')
    amount = _decimal(case.get('amount_usd'), 'amount_usd')
    gross = _decimal(product.get('gross_kg'), 'gross_kg')
    net = _decimal(product.get('net_kg'), 'net_kg')
    volume = _decimal(product.get('volume_cbm'), 'volume_cbm')
    if quantity * price != amount:
        raise ValueError('quantity multiplied by price_usd must equal amount_usd')
    if cartons != cartons.to_integral_value() or (amount != amount.to_integral_value() and not case.get('amount_words')):
        raise ValueError('This case requires integer cartons and a whole-dollar amount')
    if net > gross:
        raise ValueError('Net weight must not exceed gross weight')
    if _required(product, 'packing_unit') != 'CARTON':
        raise ValueError('Use the verified B2B packing unit CARTON')
    if _required(case, 'payment') not in {'T/T','D/P','D/A','L/C'} or _required(case, 'terms') not in {'FOB','CFR','CIF'}:
        raise ValueError('Review payment and freight fields for other settlement/terms')
    seller_name = _required(seller, 'company_en')
    seller_address = _required(seller, 'address_en')
    buyer_name = _required(buyer, 'company_en')
    buyer_address = _required(buyer, 'address_en')
    signer = _required(seller, 'person_en')
    product_id = _required(product, 'id')
    description = _required(product, 'name_en') + ' ' + _required(product, 'spec_en')
    unit = _required(product, 'unit')
    mark = _required(case, 'mark')
    loading = _required(case, 'loading_port')
    destination = _required(case, 'destination')
    transport = (
        f'FROM {loading} TO {destination} ON {_MONTHS[sailing_date.month]} '
        f'{sailing_date.day}, {sailing_date.year} BY VESSEL {vessel} / {voyage}.'
    )
    amount_words = case.get('amount_words') or f'U.S.DOLLARS {_words(int(amount))} ONLY'
    if not isinstance(amount_words, str):
        raise ValueError('amount_words must be a string')
    carton_words = f'{_words(int(cartons))} CARTONS ONLY'
    quantity_text, carton_text = _plain(quantity), _plain(cartons)
    gross_text, net_text, volume_text = _plain(gross), _plain(net), format(volume.normalize(), 'f')
    amount_text, price_text = format(amount, '.2f'), format(price, '.2f')
    issuer, consignee = seller_name + '\n' + seller_address, buyer_name + '\n' + buyer_address
    invoice = {
        'values': {
            'M1907': issuer, 'M1908': consignee, 'M1909': issue_date,
            'M1910': _required(case, 'contract'), 'M1911': _required(case['lc'], 'number') if case['payment']=='L/C' else '', 'M1912': case['payment'],
            'M1913': transport, 'M1914': case['terms'] + ' ' + (loading if case['terms']=='FOB' else destination),
            'M1915': quantity_text, 'M1916': unit, 'M1917': 'USD',
            'M1918': amount_text, 'M1919': amount_words, 'M1920': '',
            'M1921': seller_name, 'M1922': signer,
        },
        'child_url': 'CM20.aspx?' + urlencode({'a1': invoice_id, 'a2': ''}),
        'items': [{
            'M2003': product_id, 'M2004': mark, 'M2005': description,
            'M2006': quantity_text, 'M2007': unit, 'M2008': 'USD',
            'M2009': price_text, 'M2010': amount_text,
        }],
    }
    packing = {
        'values': {
            'M2107': issuer, 'M2108': consignee, 'M2109': invoice_id,
            'M2110': issue_date, 'M2111': carton_text, 'M2112': 'CARTONS',
            'M2113': gross_text, 'M2114': 'KGS', 'M2115': net_text, 'M2116': 'KGS',
            'M2117': volume_text, 'M2118': 'CBM', 'M2119': carton_words,
            'M2120': f'TOTAL {quantity_text} {unit} IN {carton_text} CARTONS.',
            'M2121': seller_name, 'M2122': signer,
        },
        'child_url': 'CM22.aspx?' + urlencode({'a1': packing_id, 'a2': ''}),
        'items': [{
            'M2203': product_id, 'M2204': mark, 'M2205': description,
            'M2206': carton_text, 'M2207': 'CARTON',
            'M2208': gross_text, 'M2209': 'KGS', 'M2210': net_text,
            'M2211': volume_text, 'M2212': 'CBM',
        }],
    }
    # No system IDs, child row identifiers, hidden flags or other-party signatures.
    return {'invoice': invoice, 'packing': packing}


def shipping_request(a):
    s, b, g, ds = (a["seller"], a["buyer"], a["product"], a["documents"])
    term = a['terms']
    prepaid = term in ('CFR', 'CIF')
    freight = D(str(a['freight']['raw_USD'])) if prepaid else D(0)
    premium = D(str(a['insurance']['premium_usd'])) if term == 'CIF' else D(0)
    buyer = b['company_en'] + '\n' + b['address_en']
    values = {
        'M1407': a.get('operation_date',a['date']), 'M1408': a['contract'], 'M1409': ds['M19'],
        'M1413': s['company_cn'] + '\n' + s['address_cn'], 'M1414': a['payment'],
        'M1415': '一般贸易', 'M1416': a['destination_country_cn'], 'M1417': buyer,
        'M1418': '海运', 'M1419': a['destination_country_cn'], 'M1420': a['shipment_deadline'],
        'M1421': a['loading_port'], 'M1422': buyer, 'M1424': a['destination'],
        'M1425': 'YES', 'M1426': 'NO', 'M1427': 'YES' if prepaid else 'NO',
        'M1428': 'NO' if prepaid else 'YES', 'M1429': str(g['cartons']), 'M1430': g['packing_unit'],
        'M1431': str(g['quantity']), 'M1432': g['unit'], 'M1433': text_number(g['gross_kg']),
        'M1434': 'KGS', 'M1435': text_number(g['net_kg']), 'M1436': 'KGS',
        'M1437': f"{a['amount_usd']:.2f}", 'M1438': a['currency'],
        'M1439': '请妥善装载，防潮防碰。运费' + ('由卖方预付。' if prepaid else '到付。') + '保险由' + ('卖方' if term == 'CIF' else '买方') + '办理。',
        'M1440': f"{D(str(a['amount_usd']))-freight-premium:.2f}", 'M1441': a['currency'],
        'M1442': text_number(g['volume_cbm']), 'M1443': 'CBM', 'M1448': s['credit_code'],
        'M1449': s['person_cn'], 'M1456': s['company_cn'], 'M1457': s['phone'],
        'M1458': s['fax'], 'M1459': s['person_cn'],
    }
    if a['payment'] == 'L/C':
        lc = a['lc']
        values.update(M1411=lc['number'], M1412=lc['issuing_bank'],
                      M1417='TO ORDER OF ' + lc['issuing_bank'], M1423=lc['expiry_date'])
    if term == 'CIF':
        values.update(M1444='PICC ALL RISKS, WAR RISKS AND STRIKE', M1445=a['currency'],
                      M1446=a['insurance']['amount_usd'], M1447=a['destination'])
    return {'values': values, 'child_url': 'CM15.aspx?a1=' + ds['M14'] + '&a2=',
            'items': [{'M1503': g['id'], 'M1504': a['mark'], 'M1505': g['name_en'] + ' ' + g['spec_en'],
                       'M1506': str(g['cartons']), 'M1507': g['packing_unit'],
                       'M1508': str(g['quantity']), 'M1509': g['unit'], 'M1510': text_number(g['gross_kg']),
                       'M1511': 'KGS', 'M1512': text_number(g['net_kg']), 'M1513': a['currency'],
                       'M1514': f"{a['price_usd']:.2f}", 'M1515': f"{a['amount_usd']:.2f}"}]}


def customs(a):
    s, b, g, ds = (a["seller"], a["buyer"], a["product"], a["documents"])
    sh = a['ship']; identity = s['company_cn'] + '\n' + s['credit_code']
    v = {'M0513': identity, 'M0509': '上海海关', 'M0511': sh['date'],
         'M0512': a.get('operation_date', a['date']), 'M0510': '', 'M0548': b['company_en'],
         'M0514': '江海运输', 'M0515': sh['vessel'] + '/' + sh['voyage'], 'M0516': '',
         'M0517': identity, 'M0518': '一般贸易', 'M0519': '一般征税', 'M0521': '',
         'M0533': a['contract'], 'M0562': a['destination_country_cn'],
         'M0522': a['destination_country_cn'], 'M0523': a['destination_cn'], 'M0524': a['loading_port_cn'],
         'M0535': g['packing_unit'], 'M0534': str(g['cartons']), 'M0536': text_number(g['gross_kg']),
         'M0537': text_number(g['net_kg']), 'M0526': a['terms'], 'M0527': '', 'M0528': '',
         'M0529': '', 'M0530': '', 'M0531': '', 'M0532': '', 'M0539': '',
         'M0541': a['mark'], 'M0549': s['person_cn'], 'M0525': identity}
    if a['terms'] in ('CFR', 'CIF'):
        v.update(M0527=a['currency'], M0528=sh['freight_usd'])
    if a['terms'] == 'CIF':
        v.update(M0529=a['currency'], M0530=a['insurance']['premium_usd'])
    return {'values': v, 'child_url': 'CM06.aspx?a0=1&a1=' + ds['M05A'] + '&a2=',
            'items': [{'M0602': '1', 'M0603': g['id'], 'M0613': g['hs'], 'M0604': g['name_cn'],
                       'M0605': g['spec_cn'], 'M0606': str(g['quantity']), 'M0607': g['unit'],
                       'M0614': '中国', 'M0608': a['destination_country_cn'], 'M0615': a['loading_port_cn'],
                       'M0612': '照章征税', 'M0611': a['currency'], 'M0609': f"{a['price_usd']:.2f}",
                       'M0610': f"{a['amount_usd']:.2f}"}]}


def origin(a):
    s, b, g, ds = (a["seller"], a["buyer"], a["product"], a["documents"])
    sh = a['ship']; issued = date.fromisoformat(a.get('operation_date',a['date'])).strftime('%B %d, %Y').upper()
    sailing = date.fromisoformat(sh['date']).strftime('%B %d, %Y').upper()
    pack = 'CARTON' if g['cartons'] == 1 else 'CARTONS'
    wording = _words(g['cartons']) + ' ' + pack + ' ONLY'
    return {'values': {'M0307': s['company_en']+'\n'+s['address_en']+('' if 'CHINA' in s['address_en'].upper() else '\nCHINA'),
                       'M0308': b['company_en']+'\n'+b['address_en']+('' if a['destination_country_en'] in b['address_en'].upper() else '\n'+a['destination_country_en']),
                       'M0309': f"FROM {a['loading_port']} TO {a['destination']} ON {sailing} BY VESSEL {sh['vessel']} / {sh['voyage']}.",
                       'M0313': a['destination_country_en'], 'M0315': wording, 'M0321': '',
                       'M0314': s['declaration_city_en']+', CHINA '+issued+'\n'+s['person_en']},
            'child_url': 'CM04A.aspx?a1='+ds['M03A']+'&a2=',
            'items': [{'M0403': g['id'], 'M0404': a['mark'],
                       'M0405': f"{g['cartons']} {pack} ({wording}) OF {g['name_en']}\n{g['spec_en']}\n****",
                       'M0406': g['hs'], 'M0407': str(g['quantity']), 'M0408': g['unit'],
                       'M0409': ds['M19']+'\n'+issued}]}


def bank_documents(case):
    """Prepare Form A and the actual sight/time draft for bank presentation."""
    seller, buyer = case['seller'], case['buyer']
    g, ship, forms = case['product'], case['ship'], case['documents']
    payment = case['payment']
    if payment not in ('D/P', 'D/A', 'L/C') or case['currency'] != 'USD':
        raise ValueError('银行交单需有美元托收或信用证合同。')
    number = _document_id(forms, 'M03B', 'STGSP')
    _document_id(forms, 'M42', 'STDFT')
    inv = _document_id(forms, 'M19', 'STINV')
    issue = date.fromisoformat(case['operation_date'])
    sailed = date.fromisoformat(ship['date'])
    inv_date = date.fromisoformat(case.get('invoice_date') or case['operation_date'])
    def eng(d):
        return f'{_MONTHS[d.month]} {d.day}, {d.year}'
    country = case['destination_country_en']
    signer = seller['person_en']
    declaration = f"{seller['declaration_city_en']} CHINA {issue.isoformat()} {signer}"
    if len(declaration) > 40:
        raise ValueError('FORM A 的地点、日期与签署人超过表单 40 字符，请核对公司登记。')
    n = g['cartons']
    pack = 'CARTON' if n == 1 else 'CARTONS'
    desc = f"{n} {pack} ({_words(n)} {pack} ONLY) OF {g['name_en']}\n{g['spec_en']}\n****"
    form_a = dict(values={
        'M0307':seller['company_en']+'\n'+seller['address_en']+('' if 'CHINA' in seller['address_en'].upper() else '\nCHINA'),
        'M0308':buyer['company_en']+'\n'+buyer['address_en']+('' if country in buyer['address_en'].upper() else '\n'+country),
        'M0309':f"FROM {case['loading_port']} TO {case['destination']} ON {eng(sailed)} BY VESSEL {ship['vessel']} / {ship['voyage']}.",
        'M0312':country,'M0322':declaration},
        child_url='CM04B.aspx?'+urlencode({'a1':number,'a2':''}),
        items=[{'M0402':'1','M0403':g['id'],'M0404':case['mark'],'M0405':desc,'M0406':'"P"',
                'M0407':text_number(g['gross_kg']),'M0408':'KGS','M0409':inv+'\n'+eng(inv_date)}])
    tenor = case['payment_tenor']
    if tenor != ('30 days after B/L date' if payment == 'D/A' else '----'):
        raise ValueError('汇票期限须与本合同约定及平台选项相符。')
    draft = {'M4207':issue.isoformat(),'M4208':'USD','M4209':f"{case['amount_usd']:.2f}",'M4210':tenor,
             'M4211':seller['company_en'],'M4212':case['amount_words'],'M4213':'','M4214':'',
             'M4215':buyer['company_en'],'M4216':buyer['company_en']+'\n'+buyer['address_en'],'M4217':seller['company_en']}
    if payment == 'L/C':
        lc = case['lc']
        if lc.get('tenor') != 'AT SIGHT':
            raise ValueError('信用证须为合同约定的即期议付。')
        draft.update(M4211=_required(lc,'advising_bank'), M4213=_required(lc,'number'),
                     M4214=_required(lc,'issue_date'), M4215=_required(lc,'issuing_bank'), M4216=lc['issuing_bank'])
    return {'M03B':form_a, 'M42':{'values':draft}}


def advice(a):
    s, b, g, ds = (a["seller"], a["buyer"], a["product"], a["documents"]); sh = a['ship']
    v = {'M4407': b['company_en']+'\n'+b['address_en'], 'M4408': ds['M19'],
         'M4409': a.get('operation_date', a['date']), 'M4410': a['lc']['number'] if a['payment']=='L/C' else '',
         'M4411': a['contract'], 'M4412': sh['vessel']+' '+sh['voyage'],
         'M4413': a['loading_port'], 'M4414': a['destination'], 'M4415': sh['date'], 'M4416': sh['eta'],
         'M4417': g['name_en']+' '+g['spec_en'], 'M4418': str(g['quantity']), 'M4419': g['unit'],
         'M4420': a['currency'], 'M4421': f"{a['amount_usd']:.2f}",
         'M4422': '1 ORIGINAL AND 2 COPIES', 'M4423': '1 ORIGINAL AND 2 COPIES',
         'M4424': '3 ORIGINALS', 'M4425': str(a['insurance'].get('originals') or 5)+' ORIGINALS' if a['terms']=='CIF' else '',
         'M4426': ('CERTIFICATE OF ORIGIN' if a['payment']=='T/T' else 'FORM A')+': 1 ORIGINAL. '+('INSURANCE POLICY ENDORSED TO BUYER.' if a['terms']=='CIF' else 'CARGO INSURANCE ARRANGED BY BUYER.'),
         'M4427': s['company_en'], 'M4428': s['person_en'], 'M4429': 'LCL',
         'M4430': a['currency'], 'M4431': sh['freight_usd']}
    return {'values': v}


def insurance(case, payer='buyer'):
    """M25; seller/CIF uses own Chinese name and endorses to the real buyer.

    Buyer/FOB or CFR uses own English company name, without transfer. Only the
    agreed PICC ALL RISKS + WAR + STRIKE combination is supported. No policy
    number/date or insurer state is fabricated; M2545/M2546 remain untouched.
    """
    seller, buyer, product, ship = (case['seller'], case['buyer'], case['product'], case['ship'])
    if payer not in {'buyer', 'seller'}:
        raise ValueError('payer must be buyer or seller')
    expected = 'seller' if case['terms'] == 'CIF' else 'buyer'
    plan = case['insurance']
    if payer != expected or plan.get('payer') != payer:
        raise ValueError('Payer must match the actual term and case insurance agreement')
    clause = _required(case, 'insurance_clause').upper()
    if not all(x in clause for x in ('PICC', 'ALL RISKS', 'WAR RISKS', 'STRIKE')):
        raise ValueError('Review changed insurance promises; do not silently change risks')
    _document_id(case['documents'], 'M25', 'STINS')
    invoice = _document_id(case['documents'], 'M19', 'STINV')
    application = date.fromisoformat(case['operation_date'])
    if application > date.fromisoformat(ship['date']):
        raise ValueError('Insurance application must not postdate loading; never backdate')
    applicant = _required(seller, 'company_cn') if payer == 'seller' else _required(buyer, 'company_en')
    value = format(_decimal(plan.get('amount_usd'), 'insurance value').quantize(Decimal('0.01'), rounding=ROUND_HALF_UP), 'f')
    if payer == 'seller' and Decimal(value) != (
            _decimal(case['amount_usd'], 'CIF amount') * Decimal('1.10')).quantize(
                Decimal('.01'), rounding=ROUND_HALF_UP):
        raise ValueError('This CIF agreement requires exactly110% invoice coverage')
    claim_currency = plan.get('claim_currency') or case['currency']
    if claim_currency != case['currency']:
        raise ValueError('Review separately agreed claims currency and amount conversion')
    copies = plan.get('copies', 5)
    if not str(copies).isdigit() or int(copies) < 1:
        raise ValueError('Policy copies must be a positive integer')
    values = {
        'M2507': applicant, 'M2508': application.isoformat(), 'M2509': invoice,
        'M2510': applicant, 'M2511': _required(buyer, 'company_en') if payer == 'seller' else '',
        'M2512': case['currency'], 'M2513': value,
        'M2514': case['loading_port'], 'M2515': case['destination'],
        'M2516': plan.get('inland_destination', ''), 'M2517': ship['date'],
        'M2518': ship['vessel'] + '/' + ship['voyage'],
        'M2519': plan.get('claims_payable_at') or case['destination'],
        'M2520': claim_currency, 'M2521': str(copies),
        'M2522': plan.get('special_conditions', ''),
        'M2523': '1', 'M2524': '0', 'M2525': '1', 'M2526': '0',
        'M2527': '0', 'M2528': '1', 'M2529': '0', 'M2530': '1',
        'M2531': '0', 'M2532': '0', 'M2533': '0', 'M2540': '0',
        'M2542': '0', 'M2543': '0',
    }
    return {'values': values}
