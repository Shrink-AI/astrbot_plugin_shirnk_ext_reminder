import json
import re
from pathlib import Path

_defaults = json.loads(Path(__file__).with_name('prompt_defaults.json').read_text('utf-8'))
_overrides = {}


def load(root):
    path = root / 'prompts.json'
    _overrides.clear()
    if path.exists():
        _overrides.update(json.loads(path.read_text('utf-8')))


def text(key):
    return _overrides.get(key, _defaults[key])


def render(key, **values):
    return re.sub(r'\{\{(\w+)\}\}', lambda m: str(values[m.group(1)]), text(key))
