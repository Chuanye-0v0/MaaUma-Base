"""Stage data-only knowledge inputs; never execute fetched Lua or read accounts."""
from pathlib import Path
from datetime import datetime,timezone
import hashlib
import json
import re
import shutil

HERE=Path(__file__).resolve().parent
TASK=HERE.parents[1]
ROOT=TASK.parents[1]
RESEARCH=ROOT/'integrations/uma-knowledge-hub-research-20261025'
FROZEN=ROOT/'dist/maauma-base-release-0.4.38-20261024/backend'
RICH=Path('C:/Users/Administrator/.codex/worktrees/077a/uma')
KACHI=RESEARCH/'repos/kachi-uma-tools'

def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def load(p):return json.loads(Path(p).read_text('utf-8-sig'))
def write(p,v):p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(v,ensure_ascii=False,separators=(',',':'))+'\n','utf-8')

def parse_tables(text):
    tables={}
    row_re=re.compile(r'\{((?:[^"{}]|"(?:\\.|[^"\\])*")*)\}')
    field_re=re.compile(r'(\w+)\s*=\s*("(?:\\.|[^"\\])*"|-?\d+(?:\.\d+)?)')
    for table in re.finditer(r'p\.(\w+)\s*=\s*\{\s*\n(.*?)\n\}',text,re.S):
        rows=[]
        for match in row_re.finditer(table[2]):
            rest=field_re.sub('',match[1])
            if rest.strip(' ,\r\n\t'):raise ValueError('Unsupported data-only Lua row: '+table[1])
            rows.append({k:json.loads(v,strict=False)if v.startswith('"')else v for k,v in field_re.findall(match[1])})
        tables[table[1]]=rows
    if not tables:raise ValueError('Missing data-only Lua tables')
    return tables

def main():
    sources=[]
    def copy(source,name,scope,url=None):
        source=Path(source);target=HERE/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,target)
        sources.append(dict(file=name,source_path=str(source),sha256=sha(target),scope=scope,source_url=url))
        return load(target)if target.suffix=='.json'else target.read_text('utf-8-sig')
    reference=copy(FROZEN/'data/skills/cn-20260916/cn_skill_master.json','skills-reference.json','CN reference catalog snapshot')
    rich=copy(RICH/'data/skills/cn_larc_end/cn_skill_master.json','skills-rich.json','CN LArc historical knowledge; master boundary 2024-02-13')
    support=copy(RICH/'data/support_knowledge/cn_larc_end/support_library.json','support-library.json','Historical card/event/skill opportunities, not observed ownership')
    roster=copy(FROZEN/'core/autonomous_training/plant_roster.json','roster.json','Existing curated CN names/outfits with original name_source')
    copy(RICH/'core/skill_conditions.py','vendor/skill_conditions.py','Existing user-project pure condition parser')
    names=copy(KACHI/'uma-skill-tools/data/skillnames.json','jp-skillnames.json','JP/English names, includes obsolete/variant IDs; no availability claim',
        'https://github.com/kachi-dev/uma-tools/blob/4401e25987d9c78707f2d3f8a9dcd02c1c56529c/uma-skill-tools/data/skillnames.json')
    umas=copy(KACHI/'umas.json','jp-umas.json','JP character/outfit references',
        'https://github.com/kachi-dev/uma-tools/blob/4401e25987d9c78707f2d3f8a9dcd02c1c56529c/umas.json')
    copy(KACHI/'uma-skill-tools/data/skill_data.json','jp-engine-skills.json','JP simulator skill mechanics reference, not CN rules',
        'https://github.com/kachi-dev/uma-tools/blob/4401e25987d9c78707f2d3f8a9dcd02c1c56529c/uma-skill-tools/data/skill_data.json')
    copy(KACHI/'skill_meta.json','jp-engine-meta.json','JP simulator metadata and step costs, not observed prices',
        'https://github.com/kachi-dev/uma-tools/blob/4401e25987d9c78707f2d3f8a9dcd02c1c56529c/skill_meta.json')
    copy(KACHI/'LICENSE','licenses/kachi-uma-tools-LICENSE','GPL-3.0-or-later license retained for the source project')
    original_manifest=load(RESEARCH/'reference-data/bwiki/manifest.json')
    text_manifest=load(HERE/'raw/wiki-names-manifest.json')
    skill_files={'cn':'05.lua','tw':'01.lua','jp':'02.lua'}
    text_files={'cn':'wiki-names-05.lua','tw':'wiki-names-04.lua','jp':'wiki-names-03.lua'}
    wiki={};text_tables={}
    for server,name in skill_files.items():
        source=RESEARCH/'reference-data/bwiki'/name
        row=next(r for r in original_manifest['pages']if Path(r['path']).name==name)
        assert sha(source)==row['sha256']
        tables=parse_tables(source.read_text('utf-8-sig'))
        text_tables[server]=parse_tables((HERE/'raw'/text_files[server]).read_text('utf-8-sig'))
        source_meta={k:row.get(k)for k in('title','revid','timestamp','sha256')}
        source_meta['url']='https://wiki.biligame.com/umamusume/index.php?title='+row['title']+'&oldid='+str(row['revid'])
        text_source=next(r for r in text_manifest['pages']if r.get('path')==text_files[server])
        wiki[server]=dict(source=source_meta,
            data={r['id']:r for r in tables.get('skill_data',[])},
            names={r['index']:r['text']for r in tables.get('text_data_47',[])},
            aliases={r['index']:[r['text_hans']]for r in tables.get('text_data_47',[])if r.get('text_hans')and r['text_hans']!=r['text']},
            descriptions={r['index']:r['text']for r in tables.get('text_data_48',[])},
            text_source={k:text_source.get(k)for k in('title','revid','timestamp','sha256')},
            costs={r['id']:int(r['need_skill_point'])for r in text_tables[server].get('single_mode_skill_need_point',[])})
        sources.append(dict(file='wiki-skills.json',source_path=str(source),sha256=sha(source),scope=server+' data-only Wiki module',source_url=source_meta['url']))
    write(HERE/'wiki-skills.json',wiki)
    # No generated translations: only ID-joined source text and existing curated names.
    by_char={};outfits={}
    def entity(cid):
        cid=str(cid);return by_char.setdefault(cid,dict(character_id=cid,names={},name_sources={},aliases=[],costumes=[]))
    relation_manifest=load(RESEARCH/'reference-data/bwiki-relations/manifest.json')
    for locale,name in [('ja','02.json'),('zh_cn','05.json'),('zh_tw','06.json')]:
        path=RESEARCH/'reference-data/bwiki-relations'/name;raw=path.read_text('utf-8-sig')
        row_meta=next(r for r in relation_manifest['pages']if Path(r['path']).name==name)
        assert sha(path)==row_meta['sha256']
        for item in json.loads(raw[raw.index('['):raw.rindex(']')+1]):
            if not item.get('id'):continue
            item_entity=entity(item['id'])
            text=item.get('日文名')if locale=='ja'else item.get('中文名')
            if text:item_entity['names'][locale]=text;item_entity['name_sources'][locale]=dict(status='wiki_reference',title=row_meta['title'],revid=row_meta['revid'])
            if item.get('日文名') and 'ja'not in item_entity['names']:
                item_entity['names']['ja']=item['日文名'];item_entity['name_sources']['ja']=dict(status='wiki_reference',title=row_meta['title'],revid=row_meta['revid'])
    for server,locale in [('cn','zh_cn'),('tw','zh_tw')]:
        for item in text_tables[server].get('text_data_6',[]):
            e=entity(item['index']);previous=e['names'].get(locale)
            if previous and previous!=item['text']:e['aliases'].append(previous)
            e['names'][locale]=item['text'];e['name_sources'][locale]=dict(status='wiki_text_reference',**wiki[server]['text_source'])
    for cid,item in umas.items():
        e=entity(cid)
        if item.get('name'):
            e['names']['ja']=item['name'][0];e['name_sources']['ja']=dict(status='external_project_reference',project='kachi-dev/uma-tools',commit='4401e25987d9c78707f2d3f8a9dcd02c1c56529c')
            if len(item['name'])>1:e['names']['en']=item['name'][1];e['name_sources']['en']=dict(e['name_sources']['ja'])
        for oid,title in item.get('outfits',{}).items():
            outfits[oid]=dict(costume_id=oid,character_id=cid,names={'ja':title},name_sources={'ja':e['name_sources']['ja']})
    for server,locale in [('cn','zh_cn'),('tw','zh_tw')]:
        for item in text_tables[server].get('text_data_5',[]):
            oid=item['index'];cid=str(int(oid)//100);e=entity(cid)
            o=outfits.setdefault(oid,dict(costume_id=oid,character_id=cid,names={},name_sources={}))
            o['names'][locale]=item['text'];o['name_sources'][locale]=dict(status='wiki_text_reference',**wiki[server]['text_source'])
    for row in roster['records']:
        cid=str(row['character_id']);e=entity(cid);source=str(row.get('name_source')or'community')
        if e['names'].get('zh_cn')and e['names']['zh_cn']!=row['name']:e['aliases'].append(e['names']['zh_cn'])
        e['names']['zh_cn']=row['name'];e['name_sources']['zh_cn']=dict(status='user_confirmed'if'user-confirmed'in source else'community'if'community'in source else'curated_source',source=source)
        e['aliases'].extend(row.get('aliases',[]))
        oid=str(row['costume_id']);o=outfits.setdefault(oid,dict(costume_id=oid,character_id=cid,names={},name_sources={}))
        if row.get('outfit'):o['names']['zh_cn']=row['outfit'];o['name_sources']['zh_cn']=dict(status='existing_roster_reference',source=source)
    for o in outfits.values():entity(o['character_id'])['costumes'].append(o)
    for e in by_char.values():e['aliases']=sorted(set(e['aliases'])-set(e['names'].values()));e['costumes'].sort(key=lambda x:x['costume_id'])
    write(HERE/'entities.json',dict(characters=sorted(by_char.values(),key=lambda x:int(x['character_id'])),
        support_names={server:{r['index']:r['text']for r in tables.get('text_data_75',[])}for server,tables in text_tables.items()},
        source='ID joins only; name sources retained, no script or simplified/traditional auto-translation'))
    manifest=dict(schema_version='uma.knowledge_inputs.v1',created_at_utc=datetime.now(timezone.utc).isoformat(),sources=sources,
        counts=dict(reference_skills=len(reference['skills']),rich_skills=len(rich['skills']),jp_skill_names=len(names),support_cards=len(support['cards']),events=len(support['events']),chains=len(support['chains']),characters=len(by_char),costumes=len(outfits)),
        wiki_counts={k:dict(mechanics=len(v['data']),names=len(v['names']),costs=len(v['costs']))for k,v in wiki.items()},
        runtime_self_contained=True,live_availability_verified=False,third_party_data_redistribution_independently_verified=False,
        original_local_paths_build_time_only=True,code_executed_from_wiki=False)
    manifest['files']=[dict(path=str(p.relative_to(HERE)),sha256=sha(p),bytes=p.stat().st_size)for p in HERE.rglob('*')if p.is_file()and p.name not in{'manifest.json'}]
    write(HERE/'manifest.json',manifest)
    print(json.dumps(dict(ok=True,counts=manifest['counts'],wiki_counts=manifest['wiki_counts']),ensure_ascii=False))

if __name__=='__main__':main()
