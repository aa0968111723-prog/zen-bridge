"""Fast translation checks. Suspicions are review flags, not accuracy scores."""
import re
import unicodedata

_NUMBER=re.compile(r'(?<![0-9A-Za-z.])[+-]?\d+(?:[.,]\d+)*(?:%|％)?')


def check_translation(source, target, direction, glossary=None):
    warnings=[]
    if not isinstance(target,str) or not target.strip():
        return ['empty_translation']
    from app.mt_backend import validate_caption
    lang={'zh-en':'en','zh-ja':'ja'}.get(direction)
    if lang and validate_caption(target,lang,zh=source,glossary=glossary) is None:
        warnings.append('invalid_target_language_or_format')
    def numbers(text):
        return set(_NUMBER.findall(unicodedata.normalize('NFKC',text)))
    missing=numbers(source)-numbers(target)
    if missing:
        # Spelled-out/converted numbers can be legitimate: flag for review only.
        warnings.append('numeric_value_needs_review')
    after=bool(re.search(r'\d+\s*(?:分鐘|秒|小時|天|日|週|月|年)後',source))
    within=bool(re.search(r'\d+\s*(?:分鐘|秒|小時|天|日|週|月|年)內',source))
    if direction=='zh-en' and after and not within and re.search(r'\bwithin\b',target,re.I):
        warnings.append('temporal_relation_needs_review')
    if direction=='zh-ja' and after and not within and '以内' in target:
        warnings.append('temporal_relation_needs_review')
    if glossary and lang:
        from app.glossary import matched_terms,is_locked
        for term in matched_terms(source,glossary):
            required=term.get(lang)
            if is_locked(term) and required and required.casefold() not in target.casefold():
                warnings.append('locked_term_needs_review')
                break
    return warnings
