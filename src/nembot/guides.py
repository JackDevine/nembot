"""Guides: reference material an agent reads only when a task needs it.

Each guide is a folder in ``guide_files/`` with a ``GUIDE.md`` (front matter: ``title`` and a one-line
``summary``) and optional ``sections/<NAME>.md``. The system prompt lists only the summaries
(``prompt_index``); the agent calls ``read_guide(name)`` for the page and ``read_guide(name, section)``
for a section, so users who never need a topic don't pay for it in every request.
"""
from functools import cache
from pathlib import Path
import re

ROOT = Path(__file__).parent / 'guide_files'


@cache
def available() -> dict[str, dict]:
    """name -> {'title', 'summary', 'body', 'sections': {NAME: text}}."""
    guides = {}
    for folder in sorted(p for p in ROOT.iterdir() if (p / 'GUIDE.md').exists()):
        text = (folder / 'GUIDE.md').read_text()
        meta, body = {}, text
        if match := re.match(r'---\n(.*?)\n---\n', text, re.S):
            meta = dict(line.split(': ', 1) for line in match.group(1).splitlines() if ': ' in line)
            body = text[match.end():]
        sections = {p.stem.upper(): p.read_text() for p in sorted((folder / 'sections').glob('*.md'))}
        guides[folder.name] = {'title': meta.get('title', folder.name), 'summary': meta.get('summary', ''),
                               'body': body.strip(), 'sections': sections}
    return guides


def prompt_index() -> str:
    lines = [f'  - `{name}`: {guide["summary"]}' for name, guide in available().items()]
    return ('- `read_guide(name, section?)` returns a guide. Read the relevant one BEFORE working on its topic:\n'
            + '\n'.join(lines) + '\n') if lines else ''


def read(name: str, section: str | None = None) -> tuple[str, bool]:
    """(text, is_error)."""
    guide = available().get(str(name).strip().lower())
    if guide is None:
        return f'No guide named {name!r}. Guides: {", ".join(available())}.', True
    if not section:
        extra = f'\n\nSections: {", ".join(guide["sections"])}' if guide['sections'] else ''
        return guide['body'] + extra, False
    text = guide['sections'].get(str(section).strip().upper().removesuffix('.MD'))
    if text is None:
        return f'No section {section!r} in the {name} guide. Sections: {", ".join(guide["sections"])}.', True
    return text, False


TOOL = {
    'name': 'read_guide',
    'description': ('Read a guide (reference material on a topic, listed in the system prompt), or one of its '
                    'sections. Read the relevant guide before working on its topic.'),
    'input_schema': {
        'type': 'object',
        'properties': {
            'name': {'type': 'string', 'description': 'Guide name, e.g. "marimo".'},
            'section': {'type': 'string', 'description': 'Optional section name listed at the end of the guide.'},
        },
        'required': ['name'],
    },
}
