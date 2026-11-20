"""Current contract and issued document data for the three sight credit forms."""
from datetime import date, datetime
from decimal import Decimal

ORDER = ('IN DOCUMENT ITEM 2, REPLACE "MADE OUT TO ORDER AND BLANK ENDORSED" '
         'WITH "MADE OUT TO ORDER OF THE ISSUING BANK".')


def application(c, issuing, advising, on):
    terms, lc, product = c['terms'], c['lc'], c['product']
    if terms not in ('FOB','CFR','CIF') or c['payment'] != 'L/C' or lc['tenor'] != 'AT SIGHT':
        raise ValueError('本开证流程适用于即期信用证。')
    if not date.fromisoformat(on) <= date.fromisoformat(c['shipment_deadline']) <= date.fromisoformat(lc['expiry_date']):
        raise ValueError('开证日期、最迟装运日、信用证有效期需重新核对。')
    if Decimal(str(c['amount_usd'])) <= 0:
        raise ValueError('信用证金额须为正。')
    for p,keys in [(issuing,('txtB0307','txtB0309')), (advising,('txtB0307','txtB0309','txtB0312'))]:
        if any(not p.get(key,'').strip() for key in keys):
            raise ValueError('经办银行资料不完整。')
    buyer = c['buyer']['company_en'] + '\n' + c['buyer']['address_en']
    seller = c['seller']['company_en'] + '\n' + c['seller']['address_en']
    v = {f'M31{i:02}':'' for i in range(7,89)}
    checks=(9,10,11,12,20,21,22,23,28,29,30,31,33,37,38,39,40,41,42,43,44,45,46,47,48,49,
            53,54,55,58,59,60,70,71,77,78,79,80,81,82)
    enabled={12,21,22,30,33,41,42,45,48,77,78,82,{'FOB':37,'CFR':38,'CIF':39}[terms],
             53 if terms=='FOB' else 54}
    if terms=='CIF':enabled.add(44)
    for i in checks:v[f'M31{i:02}']='1' if i in enabled else '0'
    v.update(M3107=issuing['txtB0307'], M3108=on, M3113=lc['expiry_text'], M3114=buyer, M3115=seller,
             M3116=advising['txtB0307']+'\n'+advising['txtB0309'], M3117=c['currency'],
             M3118=format(Decimal(str(c['amount_usd'])),'.2f'), M3119=c['amount_words'], M3124=advising['txtB0307'],
             M3125=c['loading_port'], M3126=date.fromisoformat(c['shipment_deadline']).strftime('%y%m%d'),
             M3127=c['destination'], M3134='100',M3135='----',M3136=issuing['txtB0307'], M3150='3',
             M3152=c['contract'], M3156=buyer, M3166='3', M3172='1', M3173='ENTRY-EXIT INSPECTION AND QUARANTINE BUREAU',
             M3175='THE CERTIFICATE OF ORIGIN REQUIRED IN ITEM 7 SHALL BE FORM A IN 1 COPY.',
             M3176=(f"{product['id']} {product['name_en']} {product['spec_en']}\nQUANTITY: {product['quantity']} "
                    f"{product['unit']}\nPRICE: {c['currency']}{c['price_usd']}/{product['unit']}"),
             M3183=str(lc['presentation_days']), M3186=ORDER)
    if terms=='CIF':
        v.update(M3162=str(lc['insurance_originals']),M3163='110',M3164=c['destination'],
                 M3165='PICC '+c['insurance']['risks'],
                 M3186=ORDER+' INSURANCE IS TO BE ENDORSED TO THE BUYER.')
    else:
        v['M3186']+=(' OCEAN FREIGHT PAYABLE BY '+('BUYER' if terms=='FOB' else 'SELLER')+
                     '. CARGO INSURANCE IS ARRANGED BY THE BUYER. NO INSURANCE DOCUMENT IS REQUIRED FROM THE BENEFICIARY.')
    return v


def credit(c,v,on):
    if v['M3152']!=c['contract'] or v['M3117']!=c['currency'] or Decimal(v['M3118'])!=Decimal(str(c['amount_usd'])):
        raise ValueError('开证申请与合同的当事业务、币种或金额不一致。')
    t=c['terms']
    freight='FREIGHT TO COLLECT' if t=='FOB' else 'FREIGHT PREPAID'
    docs=[f"+SIGNED COMMERCIAL INVOICE IN {v['M3150']} COPIES INDICATING CONTRACT No. {v['M3152']}",
          f"+FULL SET OF CLEAN ON BOARD BILLS OF LADING MADE OUT TO ORDER OF THE ISSUING BANK, MARKED {freight}, NOTIFYING {v['M3156']}.",
          f"+PACKING LIST/WEIGHT MEMO IN {v['M3166']} COPIES INDICATING QUANTITY, GROSS AND NET WEIGHTS.",
          f"+CERTIFICATE OF ORIGIN IN {v['M3172']} COPIES ISSUED BY {v['M3173']}.", v['M3175']]
    if t=='CIF':
        docs.append(f"+INSURANCE POLICY IN {v['M3162']} ORIGINALS FOR {v['M3163']} PCT OF INVOICE VALUE, ENDORSED TO THE BUYER, CLAIMS PAYABLE IN {v['M3164']} IN {v['M3117']}, COVERING {v['M3165']}.")
    return dict(M3007='1/1', M3008='IRREVOCABLE', M3010=date.fromisoformat(on).strftime('%y%m%d'),
                M3011=v['M3113'], M3012=v['M3107'], M3013=v['M3114'], M3014=v['M3115'], M3015=v['M3118'],
                M3016=v['M3124']+' BY NEGOTIATION', M3017='AT SIGHT', M3018=v['M3136'],
                M3019='NOT ALLOWED',M3020='ALLOWED',M3023=v['M3126'],
                M3024=v['M3176']+'\n'+t+' '+(v['M3125'] if t=='FOB' else v['M3127']),
                M3025='\n'.join(docs),M3026=v['M3186'],
                M3027="ALL BANKING CHARGES OUTSIDE THE OPENING BANK ARE FOR BENEFICIARY'S ACCOUNT.",
                M3028=f"DOCUMENTS MUST BE PRESENTED WITHIN {v['M3183']} DAYS AFTER DATE OF ISSUANCE OF THE TRANSPORT DOCUMENTS BUT WITHIN THE VALIDITY OF THIS CREDIT.",
                M3029='WITHOUT',M3030='',M3031=v['M3117'],M3060='UCP600',M3061=v['M3125'],M3062=v['M3127'])


def notice(v,issuing,advising,on):
    if not v.get('M3009'):raise ValueError('开证行签发的信用证缺少编号。')
    expiry=datetime.strptime(v['M3011'].split()[0],'%Y%m%d').date().isoformat()
    return dict(M3407=advising['txtB0306'], M3408=advising['txtB0307'], M3409=advising['txtB0309'],
                M3410=advising.get('txtB0313',''), M3411=on, M3412=v['M3014'], M3413='',
                M3414=issuing['txtB0307']+'\n'+issuing['txtB0309'], M3415=v['M3009'], M3416=v['M3010'],
                M3417=v['M3031'], M3418=v['M3015'], M3419='CHINA', M3420=expiry,
                M3421=v['M3017'], M3422=advising['txtB0312'], M3423=advising['txtB0307'])

