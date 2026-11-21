"""Build an advisory continuation preview from saved reviews or local run records."""
import argparse
import gzip
import json
from pathlib import Path
import sys
from zipfile import ZipFile

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from core.auto_trainer.larc_career_assessment import assess_career_review
from core.auto_trainer.larc_assessment_report import render_assessments


def read_review(path):
    path=Path(path)
    if path.suffix.lower()=='.zip':
        with ZipFile(path) as archive:raw=archive.read('review.json')
    else:raw=path.read_bytes()
    if raw[:2]==b'\x1f\x8b':raw=gzip.decompress(raw)
    result=json.loads(raw.decode('utf-8-sig'))
    if result.get('schema_version')=='uma.feedback.v1':result=result['review']
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--review',type=Path,action='append',default=[])
    parser.add_argument('--run-root',type=Path,action='append',default=[])
    parser.add_argument('--label',action='append',default=[])
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(argv)
    if not args.review and not args.run_root:parser.error('请提供复盘文件或已有育成目录。')
    reviews=[read_review(p) for p in args.review]
    if args.run_root:
        from core.auto_trainer.career_review_export import build_career_review
        reviews.extend(build_career_review(p) for p in args.run_root)
    if args.label and len(args.label)!=len(reviews):parser.error('标签数量应与复盘数量一致。')
    values=[assess_career_review(r) for r in reviews]
    labels=args.label or [str(v.get('run_id') or f'样本 {i+1}') for i,v in enumerate(values)]
    args.output.mkdir(parents=True,exist_ok=True)
    result=args.output/'续养评估预览.json';page=args.output/'续养评估预览.html'
    result.write_text(json.dumps(values,ensure_ascii=False,indent=2),encoding='utf8')
    page.write_text(render_assessments(list(zip(labels,values))),encoding='utf8')
    print(json.dumps({'ok':True,'html':str(page.resolve()),'json':str(result.resolve()),
                     'reviews':len(values),'device_actions':0},ensure_ascii=False))
    return 0


if __name__=='__main__':raise SystemExit(main())
