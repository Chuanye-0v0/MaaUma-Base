"""Shared title normalization and bounded OCR comparison (no substring guesses)."""
import unicodedata
import re


def title_key(value):
    text=unicodedata.normalize('NFKC',str(value)).casefold()
    return ''.join(c for c in text if not c.isspace()
                   and unicodedata.category(c)[0] not in {'P','S','Z'}
                   and unicodedata.category(c)!='Cf')


def one_edit_apart(left,right):
    """Compatibility wrapper for the shared long-title OCR rule."""
    result=compare_title(left,right,allow_typo=True)
    return result is not None and result['mode']=='one_edit'


def compare_title(observed, reference, *, allow_typo=False, options_verified=False):
    left,right=title_key(observed),title_key(reference)
    if not left or not right:return None
    if left==right:
        return {'mode':'exact' if observed==reference else 'normalized',
            'observed_title':observed,'catalog_title':reference,'edits':0}
    if not allow_typo:return None
    # Preserve numeric time/stage labels. Do not treat every Chinese numeral
    # as a number: the observed 干 -> 千 typo is inside an ordinary phrase.
    if ''.join(c for c in left if c.isdigit())!=''.join(c for c in right if c.isdigit()):return None
    stage=r'第[一二三四五六七八九十百]+|[一二三四五六七八九十]+(?:段目|章|话|階段)'
    if re.findall(stage,left)!=re.findall(stage,right):return None
    minimum=6 if options_verified else 8
    if min(len(left),len(right))<minimum or abs(len(left)-len(right))>1:return None
    if len(left)==len(right):
        if sum(a!=b for a,b in zip(left,right))!=1:return None
        edit='substitution'
    else:
        short,long=sorted((left,right),key=len)
        index=next((i for i,(a,b) in enumerate(zip(short,long)) if a!=b),len(short))
        # An extra prefix/suffix can name another stage, not a damaged title.
        if not 0<index<len(short) or short[index:]!=long[index+1:]:return None
        edit='missing_character' if len(left)<len(right) else 'extra_character'
    return {'mode':'one_edit','observed_title':observed,'catalog_title':reference,
        'edits':1,'edit_kind':edit}
