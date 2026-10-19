"""Run with -I: exercise the installed wheel outside repository imports."""
import json
from pathlib import Path
import sys

package = Path(sys.argv[1]).resolve()
workspace = Path(sys.argv[2]).resolve()
sys.path.insert(0, str(package))
from lightnovel_translation import (  # noqa: E402
    TranslationConfig, TranslationProject, __version__, plan_reference_set,
)

assert __version__ == '0.4.0'
workspace.mkdir(parents=True, exist_ok=True)
source = workspace / 'source.txt'
source.write_text('第1話 帰還\n\nノアは村に帰った。\n\n皆は旅の話を聞いた。', encoding='utf-8')
refs = []
for volume in (9, 7, 8):
    path = workspace / f'Novel {volume}.txt'
    path.write_text(f'第{volume}章 归来\n\n诺亚回村了。\n\n众人听取旅途见闻。', encoding='utf-8')
    refs.append(path)
plan = plan_reference_set(refs, edition_id='print')
with TranslationProject(workspace / 'project', TranslationConfig()) as project:
    ja = project.import_book(source, edition_id='web', language='ja')
    zh = project.import_edition(plan.to_input_spec())
    index = project.index([ja, zh])
    continuation = index.plan_continuation(source=ja, references=[zh], reference_set=plan)
    assert continuation['reference_endpoint']['volume_ordinal'] == 9
    assert continuation['reference_set']['availability']['scope'] == 'window'
    assert callable(index.locate_continuation)
print(json.dumps({'installed_version': __version__, 'imported_from_wheel': Path(sys.modules['lightnovel_translation'].__file__).is_relative_to(package),
                  'scope': 'window', 'terminal_volume': 9, 'model_requests': 0}))
