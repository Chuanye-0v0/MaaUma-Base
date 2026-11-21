"""One ordinary-event title resolver, shared by manual and automatic choices.

Resolve against the whole applicable catalog before looking at saved rules or
available effect adapters. Fuzzy identity never supplies discard evidence.
"""
from functools import lru_cache

from core.event_titles import title_key, compare_title
from core.event_text_match import option_key


@lru_cache(maxsize=32768)
def _keys(titles):
    return frozenset(title_key(title) for title in titles if title)


def _owner(row):
    from core.support_card_knowledge import default_library
    if row['scope_kind']=='support':
        card=default_library().cards.get(str(row.get('support_card_id') or ''),{})
        return str(row.get('character_id') or card.get('character_id') or '')
    return str(row.get('character_id') or '')


def _ordered_options(lines,row,title_index):
    previous=title_index
    end=next((i for i in range(title_index+1,len(lines)) if lines[i] in {'选项效果','選項效果'}),len(lines))
    for option in sorted(row['options'],key=lambda item:item['index']):
        key=option_key(option.get('text') or '')
        if not key:return False
        positions=[i for i in range(title_index+1,end) if option_key(lines[i])==key]
        if len(positions)!=1 or positions[0]<=previous:return False
        previous=positions[0]
    return True


def _fuzzy_scope(row,context,visible,options_verified,*,scope=None):
    from core.support_card_knowledge import default_library
    variant=str(context.get('trainee_variant_id') or '')
    if row['scope_kind']=='character':
        if row.get('trainee_variant_id')==variant and variant:
            return 'current_costume_and_option_count'
        # A reviewed base-character event also needs its full ordered options.
        if row['source']=='reviewed' and options_verified and _owner(row) in {
                str(context.get('trainee_id') or ''),variant[:4]}- {''}:
            return 'current_character_and_ordered_options'
        return None
    if row['scope_kind']!='support':return None
    from core.auto_trainer.larc_event_library import deck_support_scope
    library=default_library();owner=_owner(row)
    scope=scope or deck_support_scope('\n'.join(visible),context,library=library)
    cards=scope['card_set']
    if row.get('support_card_id') in cards or (not row.get('support_card_id') and owner
            and owner in scope['deck_chars']):
        return 'current_support_card_and_option_count'
    if owner and owner in scope['excluded_chars']:return None
    names={title_key(card.get('character_name_zh_cn') or '') for card in library.cards.values()
           if owner and str(card.get('character_id'))==owner} - {''}
    if names & visible:return 'visible_support_owner_and_option_count'
    return None


def _groups(matches):
    """Same story copied across sources/cards is one identity, not a new event.

    Effects may still differ by card; the support scorer checks those separately.
    An observed row without a story ID can join only one compatible known story.
    """
    groups=set()
    for hit in matches:
        row=hit['row'];owner=(row['scope_kind'],_owner(row));story=row.get('story_id')
        if not story:
            alternatives={str(other['row']['story_id']) for other in matches
                if other['row'].get('story_id') and (other['row']['scope_kind'],_owner(other['row']))==owner
                and _keys(tuple(row['titles'])) & _keys(tuple(other['row']['titles']))}
            if len(alternatives)>1:return None
            story=next(iter(alternatives),None)
        groups.add((*owner,str(story) if story else title_key(row['title'])))
    return groups


def catalog_title_match(text,count,context,*,allow_wild_support=False):
    from core.auto_trainer.larc_event_catalog import event_catalog,_scope_matches
    from core.auto_trainer.larc_event_library import _card_confirmed_wild,deck_support_scope
    from core.support_card_knowledge import default_library
    library=default_library()
    scope=deck_support_scope(text,context,library=library)
    lines=scope['lines_stripped']
    visible={title_key(line) for line in lines}
    scoped_context=context if 'screen_text' in context else {**context,'screen_text':text}
    rows=[]
    for row in event_catalog().values():
        if _scope_matches(row,scoped_context,require_costume=True):
            rows.append(row)
        elif allow_wild_support and row['scope_kind']=='support' and _card_confirmed_wild(row,scope,library=library):
            if scope['wild_speaker'] is None or _owner(row)==scope['wild_speaker']:
                rows.append(row)
    exact=[]
    for row in rows:
        keys=_keys(tuple(row['titles']))
        for index,line in enumerate(lines):
            if title_key(line) not in keys:continue
            match=next(compare_title(line,title) for title in row['titles'] if title_key(title)==title_key(line))
            exact.append({'row':row,'line_index':index,'title_match':match,'scope_basis':'exact_scoped_title'})
    if exact:
        # A recognized title with a different option count must not fall through
        # to a nearby spelling of another event with the requested count.
        hits=[hit for hit in exact if hit['row']['option_count']==count]
        if len(hits)!=len(exact):return {'status':'option_count_mismatch','matches':exact}
    else:
        hits=[]
        for row in rows:
            if row['option_count']!=count:continue
            keys=_keys(tuple(row['titles']))
            for index,line in enumerate(lines):
                key=title_key(line)
                if not any(abs(len(key)-len(title))<=1 and min(len(key),len(title))>=6 for title in keys):continue
                options_verified=_ordered_options(lines,row,index)
                matches=[value for title in row['titles'] if (value:=compare_title(line,title,
                    allow_typo=True,options_verified=options_verified)) is not None]
                if not matches:continue
                hits.append({'row':row,'line_index':index,'title_match':matches[0],
                    'scope_basis':_fuzzy_scope(row,context,visible,options_verified,scope=scope),
                    'ordered_options_verified':options_verified})
    if not hits:return {'status':'unmatched','matches':[]}
    if len({hit['line_index'] for hit in hits})!=1 or len(_groups(hits) or ())!=1:
        return {'status':'ambiguous','matches':hits}
    if not any(hit['scope_basis'] for hit in hits):return {'status':'scope_unverified','matches':hits}
    return {'status':'matched','matches':[hit for hit in hits if hit['scope_basis']]}


def title_resolution(text,count,context):
    cached=context.get('_event_title_resolution')
    if cached is not None and cached[:2]==(text,count):return cached[2]
    return catalog_title_match(text,count,context)


def title_diagnostic(resolution):
    hits=resolution.get('matches',[])
    return {'status':resolution['status'],'candidate_count':len(hits),
        'candidates':[{'event_id':hit['row']['key'],'story_id':hit['row'].get('story_id'),
            **hit['title_match'],'scope_basis':hit['scope_basis'],
            'option_count':hit['row']['option_count'],
            'ordered_options_verified':hit.get('ordered_options_verified')}
            for hit in hits[:6]]}
