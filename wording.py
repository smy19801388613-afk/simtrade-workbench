from decimal import Decimal as D

_SMALL = (
    "ZERO ONE TWO THREE FOUR FIVE SIX SEVEN EIGHT NINE TEN ELEVEN TWELVE "
    "THIRTEEN FOURTEEN FIFTEEN SIXTEEN SEVENTEEN EIGHTEEN NINETEEN"
).split()
_TENS = "ZERO TEN TWENTY THIRTY FORTY FIFTY SIXTY SEVENTY EIGHTY NINETY".split()
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

def words_usd(x):
 d=D(str(x));n=int(d);c=int((d-n)*100)
 return 'U.S.DOLLARS '+_words(n)+((' AND '+_words(c)+' CENTS') if c else '')+' ONLY'

def cn_money(x):
 digits='零壹贰叁肆伍陆柒捌玖';units=['','拾','佰','仟'];groups=['','万','亿']
 def chunk(n):
  z=False;o=''
  for i in range(3,-1,-1):
   k=n//10**i%10
   if k:
    if z and o:o+='零'
    o+=digits[k]+units[i];z=False
   elif o:z=True
  return o
 d=D(str(x));n=int(d);out='';parts=[]
 while n:parts.append(n%10000);n//=10000
 for i in reversed(range(len(parts))):
  if parts[i]:
   if out and parts[i]<1000:out+='零'
   out+=chunk(parts[i])+groups[i]
 out=(out or '零')+'元';c=int((d-int(d))*100)
 return out+('整' if not c else ((digits[c//10]+'角') if c//10 else '零')+((digits[c%10]+'分') if c%10 else ''))
