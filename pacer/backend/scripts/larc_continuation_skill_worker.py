"""Isolated read-only D1 inventory capture. Purchase APIs are never called."""
import argparse
import importlib.util
import json
from pathlib import Path


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def run(args, *, loader=None):
    report = {'ok': False, 'reason': 'dry_run', 'purchase_executed': False,
              'selected_skill_count': 0, 'endpoint': args.endpoint}
    if not args.execute_readonly:
        return report
    root, output, d1 = (Path(p).resolve() for p in (args.run_root, args.output_dir, args.d1_root))
    progress = read(root / 'progress.json')
    if progress.get('run_session_id') != root.name or progress.get('device') != args.endpoint:
        raise ValueError('continuation_inventory_run_device_mismatch')
    if output == root or not output.is_relative_to(root) or output.is_relative_to(d1):
        raise ValueError('continuation_inventory_output_outside_run')
    output.mkdir(parents=True, exist_ok=False)
    device = None
    try:
        spec = importlib.util.spec_from_file_location('continuation_d1_adapter',
            Path(__file__).with_name('larc_d1_prerace_skill_worker.py'))
        adapter = importlib.util.module_from_spec(spec); spec.loader.exec_module(adapter)
        api = loader(d1) if loader is not None else adapter._load_d1(d1,endpoint=args.endpoint)
        adapter.bind_startup_transport(args.endpoint)

        class ReadOnlyDevice(api.Device):
            def step(self, action_id, stop_event=None):
                if action_id not in adapter.SCROLL_ACTIONS:
                    raise RuntimeError('continuation_inventory_forbidden_action')
                return super().step(action_id, stop_event)

            def close_completed(self, *args, **kwargs):
                raise RuntimeError('continuation_inventory_forbidden_close')

        device = ReadOnlyDevice(args.endpoint, None, output / 'capture', expected_endpoint=args.endpoint)
        device.connect()
        before = device.capture()
        if (before.get('page') != 'skills' or before.get('endpoint') != args.endpoint
                or before.get('skill_cart_disabled') is not True):
            raise ValueError('continuation_inventory_empty_skill_page_required')
        catalogs = Path(args.catalog_dir) if args.catalog_dir else d1 / 'data/skills/cn-20260916'
        context = {'turn_id': f'{root.name}_continuation_{progress["next_turn"]}',
            'target_mode': 'parent', 'race_profile': {'distance_type': 'any', 'style': 'any'},
            'policy': {'max_purchase_count': 1, 'allow_gold_without_lower': True},
            'resolver_catalog': str(catalogs / 'cn_skill_resolver_catalog.json'),
            'planner_catalog': str(catalogs / 'cn_skill_planner_catalog.json'),
            'external_skill_db_path': str(Path(args.external_skill_db_path).resolve())}
        with api.ocr_session():
            prepared = api.prepare(device, output / 'prepare', context, expected_endpoint=args.endpoint)
        if prepared.get('status') != 'ready':
            report['reason'] = prepared.get('reason', 'continuation_inventory_prepare_failed')
        else:
            inventory = read(prepared['inventory_path'])
            report.update(ok=True, reason='inventory_read_only', inventory_path=prepared['inventory_path'],
                catalog_path=context['planner_catalog'],
                complete=(not bool(inventory.get('skipped_skills')) and not bool(inventory.get('abstentions'))),
                capture_manifest_path=prepared.get('capture_manifest_path'))
    except Exception as error:
        report.update(reason=type(error).__name__, detail=str(error))
    finally:
        if device is not None and callable(getattr(device, 'disconnect', None)):
            device.disconnect()
        (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('run-root', 'output-dir', 'd1-root', 'external-skill-db-path'):
        parser.add_argument('--' + name, required=True, type=Path)
    parser.add_argument('--catalog-dir', default='')
    parser.add_argument('--endpoint', required=True)
    parser.add_argument('--execute-readonly', action='store_true')
    report = run(parser.parse_args())
    print(json.dumps({'ok': report['ok'], 'reason': report['reason'], 'purchase_executed': False}))
    return 0 if report['ok'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
