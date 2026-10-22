"""Source-bound translation output. Structure is a declaration, not a verdict."""
from .protocol import parse_records


PROFILE = 'source-records'
RULES = '''【译文输出协议 source-records】
整章连贯翻译，但逐个保留全部非空原文段落，每个源段只输出一条记录。
复制该段给出的完整 C章节.J段落ID，不自行计数、改号或合并。即使一句话跨段，也逐段保留引语前后的叙述，不把后一段内容移到前一段。
只输出如下记录，最后独占一行@@END；text为该源段的完整中文，不输出标题、分析或代码块。
以下为虚构格式示例，不是本章内容：
源文：[Cdemo.J001] 彼は、 [Cdemo.J002] 「はい」 [Cdemo.J003] とだけ答えた。
@@TRANSLATION Cdemo.J001
text: 他
@@TRANSLATION Cdemo.J002
text: 「是。」
@@TRANSLATION Cdemo.J003
text: 只回答了这一句。
@@END
实际输出必须换成本章全部实际ID和内容。text中的换行仅作记录内排版，程序会组装成一段；不要借换行遗漏或重复源段。'''


def protocol_messages(messages, aliases):
    """Replace plain-body instructions only for a newly frozen output profile."""
    result = [dict(m) for m in messages]
    result[0]['content'] = result[0]['content'].replace(
        '仅输出本章译文正文，不输出标题、分析、标签或代码块。',
        '按本次源段记录协议输出译文，不输出标题、分析或代码块。')
    result[0]['content'] += '\n' + RULES
    result[-1]['content'] = result[-1]['content'].replace(
        '只翻译本章。只输出译文正文，不输出段落定位ID。',
        '只翻译本章。按source-records逐段输出，不输出纯正文。')
    result[-1]['content'] += '\n【本次合法源段ID，按此顺序各一次】\n' + ','.join(aliases)
    return result


def parse_output(text, source_rows):
    parsed = parse_records(text, {'TRANSLATION'}, field_schema={'TRANSLATION': {'text'}},
        syntax_profile='tolerant-records', implicit_fields={'TRANSLATION': 'text'},
        allow_intermediate_end=False)
    expected = [r['alias'] for r in source_rows]
    actual = [r['id'] for r in parsed['records']]
    errors = list(parsed['errors'])
    unknown = [ident for ident in actual if ident not in expected]
    missing = [ident for ident in expected if ident not in actual]
    if unknown: errors.append('unknown_source_ids:' + ','.join(unknown))
    if missing: errors.append('missing_source_ids:' + ','.join(missing))
    if actual != expected: errors.append('source_ids_must_be_an_ordered_partition')
    paragraphs = []
    for row in parsed['records']:
        body = row['fields'].get('text', '')
        if not body.strip(): errors.append('empty_translation:' + row['id'])
        paragraphs.append(' '.join(line.strip() for line in body.splitlines() if line.strip()))
    return {'profile': PROFILE, 'complete': not errors, 'errors': errors,
        'source_aliases': actual, 'text': '\n\n'.join(paragraphs),
        'syntax_notes': parsed.get('notes', []), 'semantic_status': 'not_verified'}


def render_output(source_rows, text):
    """Render a validated final version as history in the same output protocol."""
    paragraphs = [line for line in text.splitlines() if line.strip()]
    if len(paragraphs) != len(source_rows):
        raise ValueError('cannot render source-bound history with different paragraph counts')
    return '\n'.join(f"@@TRANSLATION {row['alias']}\ntext: {paragraph}"
                     for row, paragraph in zip(source_rows, paragraphs))+'\n@@END'


def recovery_feedback(parsed, aliases):
    return ('上次输出未通过源段布局校验。依据同一原文重新输出本章全部TRANSLATION记录，不能只补一段。'
        '不补空行，不为凑数重复内容；保留每段原意以及跨段引语后的叙述。\n'
        '具体错误：' + str(parsed['errors']) + '\n合法ID（各一次，严格按此顺序）：' + ','.join(aliases) +
        '\n最后独占一行@@END。只输出修复后的完整记录。')
