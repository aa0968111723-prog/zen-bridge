"""Small authored zh/en/ja references; suggestions, never automatic translations."""
from functools import lru_cache
import csv
import os
from pathlib import Path
from app.glossary import _match_key

PACK = Path(__file__).parent / 'resources' / 'live_reference.tsv'
MAX_REFERENCES = 8


@lru_cache(maxsize=1)
def entries():
    with PACK.open(encoding='utf-8', newline='') as source:
        rows = list(csv.DictReader(source, delimiter='\t'))
    seen = set()
    for row in rows:
        if set(row) != {'zh', 'en', 'ja'} or any(not isinstance(v, str) or not v.strip() or
                len(v) > 240 or any(ord(c) < 32 for c in v) for v in row.values()):
            raise ValueError('Invalid bundled translation reference')
        key = _match_key(row['zh'])
        if key in seen:
            raise ValueError('Duplicate bundled translation reference')
        seen.add(key)
    return tuple(rows)


def references(text, lang='en', *, limit=MAX_REFERENCES, glossary=None):
    if os.getenv('ZEN_REFERENCE_PACK_ENABLED', '1').lower() in ('0', 'false', 'off'):
        return []
    if lang not in ('en', 'ja') or not isinstance(text, str) or not text:
        return []
    limit = max(0, min(MAX_REFERENCES, int(limit)))
    if not limit:
        return []
    current = _match_key(text)
    blocked = {_match_key(t['zh']) for t in (glossary or []) if isinstance(t, dict) and isinstance(t.get('zh'), str)}
    hits = []
    for row in entries():
        key = _match_key(row['zh'])
        start = current.find(key)
        if start >= 0 and key not in blocked:
            hits.append((start, -len(key), row))
    # Prefer the longest expression at an occurrence, and never insert both a
    # whole phrase and its individual words into the already limited context.
    hits.sort(key=lambda hit: (hit[1], hit[0], hit[2]['zh']))
    selected = []
    spans = []
    for start, neg_length, row in hits:
        end = start - neg_length
        if any(start < b and end > a for a, b in spans):
            continue
        selected.append({'zh': row['zh'], 'target': row[lang]})
        spans.append((start, end))
        if len(selected) == limit:
            break
    return selected


def pack_status():
    rows = entries()
    return {'enabled': os.getenv('ZEN_REFERENCE_PACK_ENABLED', '1').lower() not in ('0', 'false', 'off'),
            'entries': len(rows), 'language_pairs': len(rows) * 2, 'max_references': MAX_REFERENCES}
