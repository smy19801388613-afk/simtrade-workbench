"""Import customs declaration prepared from the signed contract and issued delivery order."""
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re
from urllib.parse import urlencode


def _text(obj, key):
    value = obj.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'Missing nonempty text: {key}')
    return value.strip()


def _decimal(value, label, positive=True):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f'Invalid number: {label}') from exc
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise ValueError(f'Invalid nonnegative/positive number: {label}')
    return result


def _plain(value, label):
    return format(_decimal(value, label).normalize(), 'f')


def _money(value, label, positive=True):
    return format(_decimal(value, label, positive).quantize(
        Decimal('.01'), rounding=ROUND_HALF_UP), 'f')


def _date(value, label):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError(f'{label} must be an actual ISO date YYYY-MM-DD')
    return date.fromisoformat(value)


def _operation_date(case, override):
    return _date(case.get(override) or case.get('operation_date') or
                 case.get('date'), override)


def _doc(case, kind):
    value = case.get('documents', {}).get(kind)
    if isinstance(value, dict):
        value = value.get('id') or value.get('document_id')
    prefix = {'M25': 'STINS', 'M19': 'STINV', 'M21': 'STPLT',
              'M01': 'STBLN', 'M41': 'STBLO', 'M17B': 'STIPC',
              'M05B': 'STIAC'}[kind]
    if not isinstance(value, str) or not re.fullmatch(prefix + r'\d+', value):
        raise ValueError(f'Missing actual issued documents.{kind}')
    return value


def _arrival(case, operation):
    _doc(case, 'M41')
    actual = _date(case['ship'].get('actual_arrival'), 'ship.actual_arrival from M41 M4119')
    if actual < _date(case['ship'].get('date'), 'ship.date'):
        raise ValueError('Actual arrival predates shipment')
    if operation < actual:
        raise ValueError('Document application/declaration predates actual arrival')
    return actual


def _draft(values, child_url=None, items=None):
    return {'values': values, 'child_url': child_url, 'items': items or []}


def customs(case):
    """Import M05B/CM06. Disclose real freight/premium even under CFR/CIF.

    Disclosure is not a second payment or extra budget charge. Declaration
    signer is only the actual importer's person; no bank/customs signature.
    """
    seller, buyer, product, ship = (case[k] for k in ('seller', 'buyer', 'product', 'ship'))
    form_id = _doc(case, 'M05B')
    bill = _doc(case, 'M01')
    declaration = _operation_date(case, 'import_declaration_date')
    actual = _arrival(case, declaration)
    if case['insurance'].get('status') != 'issued':
        raise ValueError('Use issued insurance facts and premium before import declaration')
    freight = _money(ship.get('freight_usd'), 'actual booked freight', positive=False)
    premium = _money(case['insurance'].get('premium_usd'), 'issued premium', positive=False)
    identity = _text(buyer, 'company_en') + '\n' + _text(buyer, 'credit_code')
    values = {
        'M0513': identity, 'M0509': case['destination_cn'] + '海关',
        'M0511': actual.isoformat(), 'M0512': declaration.isoformat(), 'M0510': '',
        'M0548': _text(seller, 'company_en'), 'M0514': '江海运输',
        'M0515': ship['vessel'] + '/' + ship['voyage'], 'M0516': bill,
        'M0547': '', 'M0517': identity, 'M0518': '一般贸易', 'M0519': '一般征税',
        'M0521': '', 'M0546': case['loading_port_cn'], 'M0533': case['contract'],
        'M0562': '中国', 'M0522': '中国', 'M0523': case['loading_port_cn'],
        'M0524': case['destination_cn'], 'M0535': product['packing_unit'],
        'M0534': _plain(product['cartons'], 'cartons'),
        'M0536': _plain(product['gross_kg'], 'gross'),
        'M0537': _plain(product['net_kg'], 'net'), 'M0526': case['terms'],
        'M0527': case['currency'], 'M0528': freight, 'M0529': case['currency'],
        'M0530': premium, 'M0531': '', 'M0532': '', 'M0539': '',
        'M0541': _text(case, 'mark'), 'M0549': _text(buyer, 'person_en'),
        'M0525': identity,
    }
    item = {
        'M0602': '1', 'M0603': _text(product, 'id'), 'M0613': _text(product, 'hs'),
        'M0604': _text(product, 'name_cn'), 'M0605': _text(product, 'spec_cn'),
        'M0606': _plain(product['quantity'], 'quantity'), 'M0607': _text(product, 'unit'),
        'M0614': '中国', 'M0608': case['destination_country_cn'],
        'M0615': case['destination_cn'], 'M0612': '照章征税',
        'M0611': case['currency'], 'M0609': _money(case['price_usd'], 'unit price'),
        'M0610': _money(case['amount_usd'], 'contract amount'),
    }
    return _draft(values, 'CM06.aspx?' +
                  urlencode({'a0': '2', 'a1': form_id, 'a2': ''}), [item])
