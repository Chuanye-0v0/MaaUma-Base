"""Explicit driver settings, never scan disks or read the global support deck."""
import json
from pathlib import Path

def load_driver(run_root,config_path=None):
    root=Path(run_root);local=root/'session/larc_skill_driver.json'
    default=Path(__file__).resolve().parents[2]/'config/larc_skill_driver.json'
    path=local if local.is_file() else Path(config_path or default)
    if not path.is_file():return None
    data=json.loads(path.read_text(encoding='utf-8-sig'))
    if data.get('schema_version')!='larc_skill_driver.v1':raise ValueError('skill_driver_schema_invalid')
    if local==path and data.get('run_session_id')!=root.name:raise ValueError('skill_driver_run_mismatch')
    for name in ('d1_root','external_skill_db_path'):
        if not isinstance(data.get(name),str) or not data[name]:raise ValueError('skill_driver_paths_required_together')
        if data.get('path_base')!='backend':data[name]=str(Path(data[name]).resolve())
    # Older runs retain their original D1/database settings, while the newly
    # introduced startup-only catalog can inherit the project's explicit pair.
    if path==local and 'startup_skill_catalog_dir' not in data:
        fallback=Path(config_path or default)
        if fallback.is_file():
            configured=json.loads(fallback.read_text(encoding='utf-8-sig'))
            if configured.get('schema_version')=='larc_skill_driver.v1' and 'startup_skill_catalog_dir' in configured:
                inherited=configured['startup_skill_catalog_dir']
                if configured.get('path_base')=='backend':
                    item=Path(inherited)
                    if item.is_absolute() or '..' in item.parts:raise ValueError('skill_driver_portable_path_invalid')
                    inherited=str((Path(__file__).resolve().parents[2]/item).resolve())
                data['startup_skill_catalog_dir']=inherited
    catalog=data.get("startup_skill_catalog_dir")
    if catalog is not None:
        if not isinstance(catalog,str) or not catalog:raise ValueError("startup_skill_catalog_dir_invalid")
        if data.get('path_base')!='backend':data["startup_skill_catalog_dir"]=str(Path(catalog).resolve())
    if data.get('path_base')=='backend':
        base=Path(__file__).resolve().parents[2]
        fields=('d1_root','external_skill_db_path','startup_skill_catalog_dir')
        relative=data.get('portable_paths') or {key:data[key] for key in fields if key in data}
        for key,value in relative.items():
            if key not in fields or not isinstance(value,str):raise ValueError('skill_driver_portable_path_invalid')
            item=Path(value)
            if item.is_absolute() or '..' in item.parts:raise ValueError('skill_driver_portable_path_invalid')
            resolved=(base/item).resolve()
            if not resolved.is_relative_to(base):raise ValueError('skill_driver_portable_path_outside_package')
            data[key]=str(resolved)
        data['portable_paths']=dict(relative)
    return data

def prepare_new_run(root,device,config_path=None):
    data=load_driver(root,config_path)
    if not data:return
    from core.auto_trainer.run_support_profile import save_profile,load_profile
    from scripts.ug_parent_one_turn_pipeline import _atomic_write_json
    existing=load_profile(root,Path(root).name)
    frozen=bool(existing and existing.get('declaration_frozen'))
    snapshot={key:value for key,value in data.items() if key!='support_profiles_by_device'} if frozen else data
    _atomic_write_json(Path(root)/'session/larc_skill_driver.json',{**snapshot,'run_session_id':Path(root).name})
    profile=None if frozen else data.get('support_profiles_by_device',{}).get(device)
    if profile:save_profile(root,Path(root).name,profile,source={'kind':'explicit_panel_new_run_support_selection','configuration':str(config_path or 'config/larc_skill_driver.json')})
