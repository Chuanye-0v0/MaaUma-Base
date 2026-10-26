# Reference simulation source attribution

This engine is built from [kachi-dev/uma-tools](https://github.com/kachi-dev/uma-tools) commit `4401e25987d9c78707f2d3f8a9dcd02c1c56529c`, a fork of alpha123/uma-tools and its race-solver work by pecan. The selected upstream repository declares GPL-3.0-or-later. Its GPL text is retained in `LICENSE` and the original source/README/package metadata in `vendor/kachi-uma-tools/`.

The integration TypeScript in this `engine/` directory is provided under GPL-3.0-or-later. This notice does not claim that running a component across a process/HTTP boundary automatically removes distribution obligations. The surrounding MaaUma/QQ application and third-party data/image rights must be considered separately when distributing a product.

Two source changes are explicitly recorded in `source-manifest.json`:

1. Remove an unused import of absent `EnhancedHpPolicy.ts`. No HP model was replaced; the actual `GameHpPolicy` remains used.
2. Resolve `originWisdom` by skill ID and perspective instead of indexing an input array after synthetic post-1200 effects have been added. The original crash is reproduced in `checks/simulation/preimage-result.json`.

The compiled artifacts contain the upstream source and Prando 6.0.1 (MIT). Its license is in `licenses/prando-MIT.txt`. The build uses esbuild 0.19.12 (MIT), with its license in `licenses/esbuild-MIT.txt`. Package versions/integrities are locked in `package-lock.json`. Dependencies were installed with `--ignore-scripts`; no dependency installation scripts ran. Only the reviewed local `build.mjs` was manually executed.

Numeric game data and names are pinned to the upstream fork snapshots. They are not represented as independently verified current-CN or current-TW game data; no game images, database extraction or user account information are used by this adapter.
