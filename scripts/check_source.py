"""Check the public source boundary without importing game or device modules."""
import ast
import json
import os
from pathlib import Path
import re
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[1]
SKIP={'.git','.build','bin','obj','__pycache__','.venv','.pytest_cache'}
FORBIDDEN_NAMES={'admin.json','worker-secrets.json','private-service-auth.json',
    'distribution-receipt.json','mfa-accounts.json','credentials.json',
    'authorization.json','control_token.json','device-key-v1.bin'}
FORBIDDEN_PATHS=('pacer/','backend/core/auto_trainer/','backend/desktop/mfa_batch.py',
    'backend/desktop/mfa_accounts.py','runtime/','dotnet/','ocr/','local_models/',
    'backend/data/','backend/assets/','resource/portraits/')
FORBIDDEN_BINARY={'.exe','.dll','.pyd','.pdb','.onnx','.traineddata','.pt','.pth','.zip','.nupkg','.ttf','.otf','.woff','.woff2',
    '.png','.jpg','.jpeg','.gif','.webp','.bmp','.ico','.icns','.wav','.mp3','.ogg'}
TOKEN=re.compile(r'\b(?:ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|AKIA[0-9A-Z]{16})\b|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----')

def check(root=ROOT):
    root=Path(root).resolve();issues=[];count=0;python_count=0;projects=0
    paths=[]
    for directory,folders,names in os.walk(root,followlinks=False):
        for folder in folders:
            path=Path(directory)/folder
            if folder not in SKIP and path.is_symlink():
                issues.append({'path':path.relative_to(root).as_posix(),'kind':'linked_source'})
        folders[:]=[folder for folder in folders if folder not in SKIP and not (Path(directory)/folder).is_symlink()]
        paths.extend(Path(directory)/name for name in names)
    for path in paths:
        relative=path.relative_to(root)
        if any(part in SKIP for part in relative.parts):continue
        if path.is_symlink():
            issues.append({'path':relative.as_posix(),'kind':'linked_source'});continue
        if not path.is_file():continue
        name=relative.as_posix();count+=1
        if path.name.casefold() in FORBIDDEN_NAMES or name.startswith(FORBIDDEN_PATHS):
            issues.append({'path':name,'kind':'private_or_runtime_path'})
        if path.suffix.lower() in FORBIDDEN_BINARY:
            issues.append({'path':name,'kind':'runtime_or_model_binary'})
        if path.name.startswith('.env') and path.name!='.env.example':
            issues.append({'path':name,'kind':'environment_credentials_file'})
        try:text=path.read_text(encoding='utf-8-sig')
        except (UnicodeError,OSError):continue
        if TOKEN.search(text):issues.append({'path':name,'kind':'credential_signature'})
        if path.suffix=='.py':
            python_count+=1
            try:ast.parse(text,filename=name)
            except SyntaxError:issues.append({'path':name,'kind':'python_syntax'})
        if path.suffix=='.csproj':
            projects+=1
            for node in ET.fromstring(text).iter():
                tag=node.tag.rsplit('}',1)[-1]
                if tag=='HintPath' and re.match(r'\s*(?:[A-Za-z]:|/)',node.text or ''):
                    issues.append({'path':name,'kind':'machine_local_hintpath'})
                if tag!='ProjectReference':continue
                value=node.get('Include','').replace('\\','/')
                if re.match(r'^(?:[A-Za-z]:|/)',value) or '$(' in value:
                    issues.append({'path':name,'kind':'nonportable_project_reference'});continue
                target=(path.parent/value).resolve()
                if not target.is_relative_to(root) or not target.is_file():
                    issues.append({'path':name,'kind':'missing_project_reference'})
    for required in ('README.md','LICENSE','NOTICE.md','SOURCE-ORIGIN.json',
        'gui/MFAAvalonia.Desktop/MFAAvalonia.Desktop.csproj',
        'tools/MaaUmaIdentity/MaaUmaIdentity.csproj','tools/UmaPublicCheck/UmaPublicCheck.csproj'):
        if not (root/required).is_file():issues.append({'path':required,'kind':'required_source_missing'})
    return {'ok':not issues,'files':count,'python_files':python_count,'projects':projects,
        'issues':issues,'device_actions':0,'old_history_audited':False,
        'scope':'source paths, project references, syntax and common credential signatures'}

if __name__=='__main__':
    result=check();print(json.dumps(result,ensure_ascii=False,indent=2));raise SystemExit(0 if result['ok'] else 1)
