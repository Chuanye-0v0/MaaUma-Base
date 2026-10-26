# Source-built reference simulation

This is a local candidate component. It does not alter the published MaaUma 0.4.38 program, connect QQ, launch an emulator or buy anything.

## What is actually implemented

- Search course geometry by venue/distance/surface/direction, including `京都 3000`, `京都3000米`, `京都 3km`.
- `菊花赏` / `菊花賞` is an explicit geometry-only alias for Kyoto turf 3000m (`10810`). It does not establish an event edition, weather or current competition conditions.
- Search the engine's skill records by stable DB ID or JP/EN name. Native and inherited uniques retain different IDs, even when their names match.
- Simulate the same build with and without one chosen skill. Return mean/median/min/max model distance gain, time saving, both finish times, remaining model HP, full-spurt and positive-HP finish ratios, and actual selected-skill activation examples.
- Return the actual effective five-stat transformation, activation-check switch, fixed seed, sample count, model/data commit and assumptions.
- Run the same source through Node and a browser Worker, using the same JSON request.

The reference does not calculate a true field win probability. `finish_viability.win_probability` is explicitly null. Current unique-skill-level increments are not additionally modeled: `inputs.unique_level_requested` is echoed, `unique_level_modeled=false`, and native uniques include this limitation in assumptions. Native strong/weak conditional alternatives are separately modeled from their actual data conditions; that distinction is not substituted for a level increment.

## Versions and data scope

The source and data are from kachi-dev/uma-tools `4401e25987d9c78707f2d3f8a9dcd02c1c56529c`. JP and Global are built separately with the upstream `CC_GLOBAL` setting and their corresponding JSON datasets.

CN/TW requests remain useful but are explicitly labeled `reference_for_requested_server`, using the JP reference model by default. `cn_calibrated` and `tw_calibrated` remain false. This is not a silent assertion that Global or JP equals the current Chinese server. The source snapshot itself is not advertised as the current full JP database.

Missing stats use visibly labeled reference values (speed/stamina 1200, power/wisdom 1000, guts800); missing mood is neutral, missing aptitudes A, and missing weather/surface condition are sunny/good. These are reference assumptions, not a player's graduation panel. An unknown skill is not fabricated as zero gain. Course/skill information can still be queried without completing a race form.

## API contract

Python `hub/simulation.py` exports `dispatch(request)`, `courses(...)`, `skills(...)`, and `run(request)`.

Actions:

```json
{"action":"simulation.courses","query":"京都3000米","server":"cn","ruleset":"jp"}
{"action":"simulation.skills","ids":["101151","901151"],"ruleset":"jp"}
{"action":"simulation.run","server":"cn","ruleset":"jp","course_id":"10810","strategy":"追","stats":{"speed":1200,"stamina":1200,"power":1000,"guts":1000,"wisdom":1000},"compare_skill_id":"101151","samples":64,"seed":20261025,"ground":"good","weather":"sunny","season":"autumn","mood":0}
```

Python returns business fields at top level: `{ok, courses/skills/baseline/comparison/skill_delta, metadata, ...}`. Raw Node CLI returns `{ok,action,result,metadata}`. Browser Worker input `{id,request:<same request JSON>}` returns `{id,result:<same Node envelope>}`.

`skill_delta.bashin` and `time_saved_seconds` each have `{mean,median,min,max}`. Positive means the added skill is faster in this model. A bashin is 2.5m here. HP is an internal model pool, not the visible stamina stat. Activation-example speed modifiers are m/s, positions metres and durations seconds.

Important parameters include `distance_aptitude`, `surface_aptitude`, `strategy_aptitude`, `ground`, `weather`, `season`, `time`, `mood`, `field_size`, `order_min/max`, `skill_activation_checks`, `rushed`, `post_1200_mechanics`, and `position_keep` (`none` or `approximate`). Chinese moods and the common 良/晴/秋 labels are normalized. Samples above1000 are labeled and reduced to1000 to keep a local request bounded.

## Build and runtime

Runtime was validated with Node24.15.0. The Python adapter invokes the installed `node` or `MAAUMA_SIMULATION_NODE`. The browser bundle is self-contained and does not require Node.

```text
npm ci --ignore-scripts --no-audit --no-fund --cache .npm-cache
node build.mjs
```

These four actual source-built artifacts are produced:

- `dist/simulation-jp.cjs`
- `dist/simulation-global.cjs`
- `dist/simulation-jp.worker.js`
- `dist/simulation-global.worker.js`

Only the two `.worker.js` files need a browser static route. Do not expose the whole engine, source or node_modules folder through a static server. Source code, licenses, patches, dependency lock and the build manifest accompany this component for maintainability and license handling.

## Evidence

`checks/simulation/verification.json` contains 29 meaningful checks, including real Python→Node calls, both browser workers, browser/Node identical results, exact fixed-seed repeatability, actual Orfevre strong/weak/inherited effects and distance-scaled durations, wisdom activation changes, HP sensitivity, labeled defaults, and a no-fabricated-win-rate result. `preimage-result.json` reproduces the original synthetic-post1200 crash and verifies the corrected source.

For one fixed reference panel on Kyoto3000, the native strong record actually activates at current-speed+0.35 for18s; the weak branch is+0.25, and the inherited record is+0.05 for10.8s. The numerical gain varies with input/rng/HP and is not a fixed CN recommendation or validation of the group chat's “2.7 lengths”.
