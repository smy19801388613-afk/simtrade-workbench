"""Quote and resume a domestic purchase using exporter and factory student desks."""
from datetime import date, datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
import re
from secrets import token_hex

from domestic import run_domestic, amount
from live_platform import archive_origins
from market import catalogue_item, market_price, PRODUCTS
from pricing import factory_costs, q


def _roles(platform, buyer, factory):
    roles = {'exporter':str(buyer), 'factory':str(factory)}
    if not set(roles.values()) <= set(platform._accounts):
        raise ValueError('出口商和工厂均须属于本次登录的账号。')
    return roles


def _snapshots(platform, roles):
    snaps = {role:platform.snapshot(account,role) for role,account in roles.items()}
    if any(s['currency'] != 'RMB' or amount(s['cash']) < 0 for s in snaps.values()):
        raise ValueError('国内采购的出口商和工厂须有可核实的人民币现金余额。')
    return snaps


def _quote(platform, roles, product):
    if product not in PRODUCTS:
        raise ValueError('请从当前工作台商品目录选择货号。')
    item,_ = catalogue_item(platform.session(roles['exporter'],'exporter'),product,require_export_route=False)
    currency,cost = market_price(platform.session(roles['factory'],'factory'),product,PRODUCTS[product][1])
    per = amount(item['per_carton'])
    if currency != 'RMB' or cost <= 0 or per < 1 or per != per.to_integral_value():
        raise ValueError('工厂成本或商品装箱数量不能用于国内采购报价。')
    taxes = item['tax']
    if any(amount(taxes[k]) != 0 for k in ('consumption_pct','consumption_per_unit')):
        raise ValueError('此商品含消费税，当前国内预算尚未覆盖其计算。')
    vat = amount(taxes['VAT_pct'])/100
    if not 0 <= vat <= 1:
        raise ValueError('目录增值税税率无效。')
    return item, int(per), amount(cost), vat


def _budget(snaps, floors, n, unit_price, cost, vat_rate):
    production,vat,fee,profit = factory_costs(n,unit_price,cost,vat_rate)
    total = n*unit_price
    fc,bc = (amount(snaps[k]['cash']) for k in ('factory','exporter'))
    after_produce = fc-production
    after_release = after_produce + total-fee
    after_tax = after_release-vat
    return {
        'purchase_rmb':q(total),'production_rmb':q(production),'vat_rmb':q(vat),
        'factory_fee_rmb':q(fee),'factory_profit_rmb':q(profit),
        'exporter_inventory_at_cost_rmb':q(total),'exporter_realized_profit_rmb':'0.00',
        'factory_cash_after_production_rmb':q(after_produce),
        'factory_cash_after_release_rmb':q(after_release),'factory_cash_after_tax_rmb':q(after_tax),
        'exporter_cash_after_purchase_rmb':q(bc-total),'cash_floor_rmb':floors,
    }


def _fits(b):
    factory_min = min(amount(b[k]) for k in ('factory_cash_after_production_rmb',
                      'factory_cash_after_release_rmb','factory_cash_after_tax_rmb'))
    return (factory_min >= amount(b['cash_floor_rmb']['factory']) and
            amount(b['exporter_cash_after_purchase_rmb']) >= amount(b['cash_floor_rmb']['exporter']) and
            amount(b['factory_profit_rmb']) > 0)


def plan_supply(platform,buyer_id,factory_id,product_id,cartons,floor_percent):
    roles = _roles(platform,buyer_id,factory_id)
    count, percent = amount(cartons), amount(floor_percent)
    if not (1 <= count <= 100 and count == count.to_integral_value() and 0 <= percent <= 100):
        raise ValueError('箱数应为 1–100 的整数，资金保留百分比为 0–100。')
    item,per,cost,vat = _quote(platform,roles,product_id)
    snaps = _snapshots(platform,roles)
    floors = {k:q(amount(s['cash'])*percent/100) for k,s in snaps.items()}
    price = amount(q(cost * Decimal('1.6')))
    for boxes in range(int(count),0,-1):
        n = Decimal(boxes*per)
        budget = _budget(snaps,floors,n,price,cost,vat)
        if _fits(budget):
            break
    else:
        raise ValueError('一箱采购也无法同时覆盖生产成本、税费和双方资金保留额。')
    signed = date.today()
    contract = 'WD'+signed.strftime('%y%m%d')+token_hex(3).upper()
    description = item['name_cn'] + ' ' + item['spec_cn']
    order = dict(contract=contract,product_id=product_id,description_cn=description.strip(),
                 unit=item['unit'],quantity=int(n),cartons=boxes,unit_price_rmb=q(price),
                 expected_unit_cost_rmb=str(cost),expected_profit_rmb=budget['factory_profit_rmb'],
                 signing_city='供方所在地',delivery_date=(signed+timedelta(days=10)).isoformat(),
                 buyer_cash_floor_rmb=floors['exporter'],factory_cash_floor_rmb=floors['factory'])
    return dict(version=1,mode='domestic',contract=contract,roles=roles,order=order,opening=snaps,
                origin=platform.base_url,accounts=sorted(platform._accounts),
                created=datetime.now().astimezone().isoformat(timespec='seconds'),
                requested_cartons=int(count),floor_percent=str(percent),product=item,budget=budget)


def execute_supply(platform,plan,state_dir):
    ct = plan.get('contract','')
    if (plan.get('mode') != 'domestic' or plan.get('version') != 1 or
            plan.get('origin') not in archive_origins(platform.base_url) or set(plan.get('accounts',[])) != set(platform._accounts) or
            not re.fullmatch(r'WD\d{6}[A-F0-9]{6}',ct) or plan.get('order',{}).get('contract') != ct):
        raise ValueError('采购计划的平台、账号或合同号与本次执行不匹配。')
    roles = _roles(platform,plan['roles']['exporter'],plan['roles']['factory'])
    order = plan['order']
    if any(plan['opening'][role].get('account') != account or plan['opening'][role].get('role') != role
           for role,account in roles.items()):
        raise ValueError('采购计划的期初账户与指定角色不符。')
    # A live student-form submission might have succeeded even if the process
    # lost its response. Once there is a journal, let its reconciliation run.
    path = Path(state_dir)/('domestic-'+ct+'.json')
    submitted = path.exists() and json.loads(path.read_text(encoding='utf-8')).get('steps')
    if not submitted:
        item,per,cost,vat = _quote(platform,roles,order['product_id'])
        if (item['unit'] != order['unit'] or int(order['cartons'])*per != order['quantity'] or
                amount(order['expected_unit_cost_rmb']) != cost or
                amount(plan['product']['tax']['VAT_pct']) != vat*100 or
                order['description_cn'] != (item['name_cn']+' '+item['spec_cn']).strip()):
            raise ValueError('商品规格、生产成本或税率发生变化，请重新报价。')
        floors = {k:q(amount(s['cash'])*amount(plan['floor_percent'])/100) for k,s in plan['opening'].items()}
        if (floors != plan['budget']['cash_floor_rmb'] or order['buyer_cash_floor_rmb'] != floors['exporter']
                or order['factory_cash_floor_rmb'] != floors['factory']):
            raise ValueError('订单资金保留额与报价不符。')
        price = amount(order['unit_price_rmb'])
        fresh = _budget(_snapshots(platform,roles),floors,amount(order['quantity']),price,cost,vat)
        if not _fits(fresh) or fresh['factory_profit_rmb'] != order['expected_profit_rmb']:
            raise ValueError('最新现金或利润与本单预算不符，请重新报价。')
        if date.fromisoformat(order['delivery_date']) < date.today():
            raise ValueError('采购计划的交货期限已过，请重新报价。')
    return run_domestic(platform.session(roles['exporter'],'exporter'),platform.session(roles['factory'],'factory'),
                        order,state_dir)
