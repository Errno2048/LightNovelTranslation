"""Quantity candidates in aligned paragraph groups, not whole-chapter counters."""
from collections import Counter
from decimal import Decimal, InvalidOperation
import re
import unicodedata

from ..storage import digest

VERSION = 'p6-quantities-v1'
DIGITS = {c: i for i, c in enumerate('零一二三四五六七八九')}
DIGITS.update({'〇': 0, '两': 2, '兩': 2})
UNITS = {'歳':'age', '才':'age', '岁':'age', '歲':'age', '人':'person',
         '個':'item', '个':'item', '名':'person', '位':'person',
         '分間':'minute', '分钟':'minute', '分鐘':'minute', '分':'minute',
         '秒':'second', '秒間':'second', '時間':'hour', '小时':'hour', '小時':'hour',
         '日間':'day', '日':'day', '天':'day', '年':'year', '年間':'year',
         '級':'level', '级':'level', 'レベル':'level', '倍':'times',
         '%':'percent', 'パーセント':'percent', '円':'JPY', '日元':'JPY',
         '元':'yuan', '章':'chapter', '点':'point', '點':'point', '回':'occurrence', '次':'occurrence',
         '本':'counter:本', '枚':'counter:枚', '匹':'counter:匹', '隻':'counter:隻', '只':'counter:只'}
NUM = r'(?:\d+(?:\.\d+)?|[零〇一二三四五六七八九十百千万萬亿億兆两兩]+(?:[点點][零〇一二三四五六七八九]+)?)'
PATTERN = re.compile(r'(?P<approx>約|约|大約|大约|およそ|約莫|大概)?(?P<num>'+NUM+
                     r')\s*(?P<unit>'+ '|'.join(re.escape(u) for u in sorted(UNITS, key=len, reverse=True)) + r')?')
PROTECTED = re.compile(r'精一杯|一生懸命|万が一|萬が一|十分(?:抱歉|感谢|感謝|高兴|高興|重要|必要|满意|滿意)|这一点|這一點|一点(?:也|都|不)|一點(?:也|都|不)')


def canonical_number(value):
    value = unicodedata.normalize('NFKC', value).replace('萬', '万').replace('億', '亿').replace('點', '点')
    if re.fullmatch(r'\d+(?:\.\d+)?', value):
        number = Decimal(value)
    else:
        parts = value.split('点')
        def integer(s):
            if not s:
                return 0
            for char, scale in [('兆', 10**12), ('亿', 10**8), ('万', 10**4)]:
                if char in s:
                    if s.count(char) != 1:
                        raise ValueError('ambiguous large unit')
                    a, b = s.split(char)
                    return (integer(a) if a else 1)*scale + integer(b)
            if all(c in DIGITS for c in s):
                return int(''.join(str(DIGITS[c]) for c in s))
            total, section, current = 0, 0, 0
            for c in s:
                if c in DIGITS:
                    current = DIGITS[c]
                elif c in '十百千':
                    section += (current or 1)*{'十':10, '百':100, '千':1000}[c]
                    current = 0
                elif c in '万亿兆':
                    section += current
                    unit = {'万':10**4, '亿':10**8, '兆':10**12}[c]
                    if unit >= 10**8:
                        total = (total+section)*unit
                    else:
                        total += section*unit
                    section, current = 0, 0
                else:
                    raise ValueError('unsupported number')
            return total+section+current
        number = Decimal(integer(parts[0]))
        if len(parts) == 2:
            number += Decimal('0.'+''.join(str(DIGITS[c]) for c in parts[1]))
    return format(number.normalize(), 'f')


def quantities(text, language):
    # NFKC can expand one original character: retain an explicit offset map.
    normalized, offsets = '', []
    for i, char in enumerate(text):
        part = unicodedata.normalize('NFKC', char)
        normalized += part
        offsets.extend([i]*len(part))
    protected = [m.span() for m in PROTECTED.finditer(normalized)]
    rows = []
    for m in PATTERN.finditer(normalized):
        if any(m.start() < b and m.end() > a for a,b in protected):
            continue
        n, unit = m['num'], m['unit']
        left, right = normalized[max(0,m.start()-8):m.start()], normalized[m.end():m.end()+8]
        arabic = n[0].isdigit()
        if not unit and not arabic:
            continue  # Internal kanji numerals are not independent quantities.
        if not unit and re.search(r'[A-Za-z]$', left) or not unit and re.match(r'[A-Za-z]', right):
            if not left.lower().endswith(('lv', 'level', 'rank')):
                continue
            unit = '級'
        if unit == '分' and language == 'ja' and n == '十' and right.startswith(('に','な','だ','です','である','だった')):
            continue  # じゅうぶん; ambiguous cases remain semantic-review duties.
        try:
            value = canonical_number(n)
        except (ValueError, KeyError, InvalidOperation):
            continue
        confidence = 'explicit'
        if n == '十' and unit == '分' and not right.startswith(('間', '钟', '鐘')):
            confidence = 'ambiguous'
        if value == '1' and unit in {'个','個','名','位','点','點'}:
            confidence = 'ambiguous'
        start, end = offsets[m.start()], offsets[m.end()-1]+1
        nearby = normalized[max(0,m.start()-3):min(len(normalized),m.end()+3)]
        qualified = bool(m['approx']) or any(x in nearby for x in ('以上','以下','未満','未满','余','餘','前後','左右','約','约','〜','～','~','/','分の','分之'))
        rows.append({'raw':text[start:end], 'start':start, 'end':end, 'value':value,
                     'unit':UNITS.get(unit, 'number'), 'confidence':confidence,
                     'qualified':qualified, 'context':text[max(0,start-12):min(len(text),end+12)]})
    return rows


def aligned_number_signals(version, coverage):
    source = {r['id']: r for r in version['source_segments']}
    target = {r['id']: r for r in version['segments']}
    signals, diagnostics = [], []
    for group in coverage['groups']:
        ss = [source[i] for i in group['source_paragraph_ids']]
        tt = [target[i] for i in group['target_paragraph_ids']]
        def collect(rows, language):
            return [{**q, 'paragraph_id':r['id'], 'alias':r['alias']} for r in rows for q in quantities(r['text'], language)]
        sq, tq = collect(ss, 'ja'), collect(tt, 'zh')
        ambiguous = [q for q in sq+tq if q['confidence'] == 'ambiguous']
        if ambiguous:
            diagnostics.append({'kind':'ambiguous_quantity', 'candidates':ambiguous})
        key = lambda q: (q['value'],q['unit'],q['qualified'])
        explicit_s = [q for q in sq if q['confidence'] == 'explicit']
        # Match ambiguous Chinese "一个" only when the source supplies that quantity.
        explicit_t = [q for q in tq if q['confidence'] == 'explicit' or key(q) in {key(x) for x in explicit_s}]
        mismatch = Counter(map(key, explicit_s)) != Counter(map(key, explicit_t))
        multiple = len(explicit_s) > 1 and len(explicit_t) > 1
        if mismatch or multiple or any(q['qualified'] for q in explicit_s+explicit_t):
            data = {'kind':'numbers_or_units', 'reason':'quantity_difference' if mismatch else 'verify_assignment_or_qualifier',
                    'source_refs':[r['alias'] for r in ss], 'target_refs':[r['alias'] for r in tt],
                    'source':sq, 'target':tq, 'source_text':[r['text'] for r in ss], 'target_text':[r['text'] for r in tt]}
            data['id'] = 'number:'+digest({'version':version['text_hash'], 'check':VERSION, **data})[:20]
            signals.append(data)
    return signals, diagnostics
