"""SIMtrade tariff worksheet for this campus fee schedule. Returns forecasts in role currencies."""
from decimal import Decimal as D,ROUND_HALF_UP,ROUND_CEILING

class UnprofitableQuote(ValueError):
    """At least one party would lose money or break even at this price."""

def q(x):return str(D(x).quantize(D('.01'),rounding=ROUND_HALF_UP))

def factory_costs(quantity, unit_price, unit_cost, vat_rate):
    sales = quantity * unit_price
    production = quantity * unit_cost
    vat = sales / (1 + vat_rate) * vat_rate
    fee = sales * D('.05')
    return production, vat, fee, sales - production - vat - fee

def build(p,*,quantity,foreign_unit_USD,domestic_unit_RMB,payment,terms,FX_USD,FX_local,market_unit_local,freight_M,freight_W,existing_stock=False,insurance_rate='0.0088'):
 n=D(quantity);price=D(foreign_unit_USD);buy=n*D(domestic_unit_RMB);a=n*price;usd=D(FX_USD);fx=D(FX_local);rate=D(insurance_rate)
 t=p['tax'];vat=D(t['VAT_pct'])/100;refund_rate=D(t['refund_pct'])/100;duty_rate=D(t['duty_pct'])/100
 if any(D(t[k])!=0 for k in ['consumption_pct','consumption_per_unit','export_tax_pct']):raise ValueError('Current reviewed model supports zero consumption and export duties only')
 if terms not in ['FOB','CFR','CIF'] or payment not in ['T/T','D/P','D/A','L/C']:raise ValueError('Review unhandled terms')
 cartons=(n/D(p['per_carton'])).to_integral_value(rounding=ROUND_CEILING);gross=cartons*D(p['gross_per_carton']);net=cartons*D(p['net_per_carton']);vol=cartons*D(p['cbm_per_carton']);freight=max(vol*D(freight_M),gross/1000*D(freight_W))
 cif=a if terms=='CIF' else (a+(freight if terms=='FOB' else 0))/(1-D('1.1')*rate);insured=cif*D('1.1');premium=insured*rate
 xf=freight*usd if terms!='FOB' else D(0);xi=premium*usd if terms=='CIF' else D(0);reg=p['regulatory_code'];inland=vol*60;exinspect=D(200) if 'B' in reg else D(0);iminspect=D(200)/fx if 'A' in reg else D(0)
 bx=D(0) if payment=='T/T' else (D(200)+max(a*usd*D('.0013'),D(200)) if payment=='L/C' else min(D(2000),max(D(100),a*usd*D('.001'))))
 bi=min(D(1000),max(D(50),a*usd*D('.0008'))) if payment=='T/T' else (max(D(200),a*usd*D('.0015'))+max(D(200),a*usd*D('.0013')) if payment=='L/C' else min(D(2000),max(D(100),a*usd*D('.001'))))
 other=a*usd*D('.05')+200+(D(28)*usd if payment=='T/T' else 0);refund=buy/(1+vat)*refund_rate;xfob=a*usd-xf-xi;exfees=inland+exinspect+200+xf+xi+bx+other;profitx=a*usd-buy-exfees+refund
 ex={'M5007':a*usd,'M5008':buy,'M5009':xfob,'M5010':inland,'M5011':exinspect,'M5012':D(200),'M5013':xf,'M5014':xi,'M5016':bx,'M5017':other,'M5018':refund,'M5019':profitx}
 imcif=cif*usd/fx;duty=imcif*duty_rate;imvat=(imcif+duty)*vat;imfreight=freight*usd/fx if terms=='FOB' else D(0);imprem=premium*usd/fx if terms!='CIF' else D(0);comp=a*usd*D('.05')/fx
 im={'M5007':a*usd/fx,'M5008':imcif,'M5009':inland/fx,'M5010':iminspect,'M5011':D(200)/fx,'M5012':duty,'M5013':imvat,'M5014':D(0),'M5015':imfreight,'M5016':imprem,'M5017':bi/fx,'M5018':comp}
 totalim=sum(v for k,v in im.items() if k!='M5008');sales=n*D(market_unit_local);profitim=sales-totalim;prod,facvat,facfee,profitfac=factory_costs(n,D(domestic_unit_RMB),D(p['cost_rmb']),vat)
 if profitx<=0 or profitim<=0 or (not existing_stock and profitfac<=0):raise UnprofitableQuote(f'Not all parties profitable: factory={profitfac};export={profitx};import={profitim}')
 return {'quantity':int(n),'foreign_unit_USD':str(price),'amount_USD':q(a),'domestic_unit_RMB':str(domestic_unit_RMB),'existing_stock':existing_stock,'payment':payment,'terms':terms,'exporter':{k:q(v) for k,v in ex.items()},'importer':{k:q(v) for k,v in im.items()},'packing':{'quantity':int(n),'cartons':int(cartons),'gross_kg':str(gross),'net_kg':str(net),'volume_cbm':str(vol)},'freight':{'raw_USD':str(freight),'estimated_USD':q(freight),'payer':'buyer' if terms=='FOB' else 'seller'},'insurance':{'amount_usd':q(insured),'premium_usd':q(premium),'rate':str(rate),'payer':'seller' if terms=='CIF' else 'buyer','status':'not_arranged'},'economics':{'export_profit_RMB':q(profitx),'export_cash_delta_RMB':q(profitx+(buy if existing_stock else 0)),'export_new_cash_required_RMB':q(exfees+(0 if existing_stock else buy)),'inventory_cost_consumed_RMB':q(buy),'import_normal_profit_local':q(profitim),'import_sales_local':q(sales),'import_cash_required_local':q(totalim),'factory_profit_RMB':None if existing_stock else q(profitfac),'factory_production_cash_RMB':None if existing_stock else q(prod),'insured_total_loss_profit_local':q(insured*usd/fx-(a*usd/fx+imfreight+imprem+bi/fx+comp))},'rate_snapshot':{'USD_to_RMB':str(usd),'local_to_RMB':str(fx),'insurance':str(rate),'freight_M':str(freight_M),'freight_W':str(freight_W)}}
