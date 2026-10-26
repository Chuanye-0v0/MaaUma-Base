"""Fetch public data-only module revisions for the isolated knowledge candidate."""
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request,urlopen
import json
import hashlib
from datetime import datetime,timezone

HERE=Path(__file__).resolve().parent
TITLES=['模块:技能数据库','模块:技能繁中数据库','模块:技能简中数据库','模块:数据库内容','模块:简中数据库内容','模块:繁中数据库内容']

def main():
    query=dict(action='query',format='json',formatversion=2,prop='revisions',rvprop='ids|timestamp|content',rvslots='main',titles='|'.join(TITLES))
    url='https://wiki.biligame.com/umamusume/api.php?'+urlencode(query)
    with urlopen(Request(url,headers={'User-Agent':'UmaKnowledgeCandidate/1 data research'}),timeout=60)as response:
        data=json.load(response)
    folder=HERE/'raw';folder.mkdir(exist_ok=True)
    pages=[]
    for index,page in enumerate(data.get('query',{}).get('pages',[])):
        row={k:page.get(k)for k in('title','pageid','missing')}
        if page.get('revisions'):
            revision=page['revisions'][0];content=revision['slots']['main']['content']
            path=folder/f'wiki-names-{index:02}.lua';path.write_text(content,'utf-8')
            row.update(revid=revision['revid'],timestamp=revision['timestamp'],path=path.name,sha256=hashlib.sha256(content.encode()).hexdigest(),bytes=len(content.encode()))
        pages.append(row)
    manifest=dict(source_api=url,fetched_at_utc=datetime.now(timezone.utc).isoformat(),pages=pages,code_executed=False)
    (folder/'wiki-names-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n','utf-8')
    print(json.dumps([{k:r.get(k)for k in('title','missing','revid','bytes','path')}for r in pages],ensure_ascii=False))

if __name__=='__main__':main()
