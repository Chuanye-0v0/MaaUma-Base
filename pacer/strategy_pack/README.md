# MaaUma L'Arc Local Strategy Script Slot

Open-source builds run on the public L'Arc baseline (`2.0 ~ 2.3`) with zero online activation or license gating.

To load local custom strategy scripts without modifying core source:

1. Create a directory containing `manifest.json` and an entry module (`pack.py`):
   - `pacer/strategy_pack/` (direct root), or
   - `pacer/strategy_pack/<pack_name>/`, or
   - `pacer/strategy_packs/<pack_name>/`, or
   - any path listed in the `MAAUMA_STRATEGY_PACK_DIR` environment variable.
2. Required `manifest.json` fields:
   - `"schema_version": "maauma.strategy_pack.v1"`
   - `"pack_id"`: unique identifier
   - `"display_name"`: human-readable name shown in summaries
   - `"entry"`: relative Python file implementing `register_strategy_pack(registry, *, manifest=None, pack_dir=None)`
   - `"default_revision"`: default revision selected on fresh forms when this pack is active
   - `"ui_revisions"`: list of UI descriptor objects (`revision`, `label`, `notice`, `default_failure_limit`, `attribute_weights`, `after_target_credit`, optional `aliases`, `family`, `auto_routed`) read by `MFAAvalonia.Helper.StrategyPackCatalog`.
3. Set `MAAUMA_STRATEGY_PACK_DIR=__NONE__` to disable automatic on-disk discovery during isolated baseline tests.
