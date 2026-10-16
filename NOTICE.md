# Copyright and attribution notices

Copyright (C) 2026 MaaUma contributors, for original additions and modifications.

MaaUma Base is based on the ordinary MaaUma 0.4.5 client and the upstream projects listed below. Original copyright and license notices in source files remain in effect.

The MaaUma contributors' original additions and modifications are made available under version 3 of the GNU General Public License, as authorized for this source publication. The MFAAvalonia-derived GUI is distributed under the same GPLv3 framework. This statement does not replace the independent licenses of third-party components or grant rights in external game resources, datasets, fonts or model files.

## Upstream source projects

| Source in this repository | Origin and attribution | License copy |
| --- | --- | --- |
| `gui/MFAAvalonia`, `gui/LazyStaticGenerator` | [MaaXYZ/MFAAvalonia](https://github.com/MaaXYZ/MFAAvalonia) and its contributors; preserve individual source notices | [GPLv3](licenses/MFAAvalonia-GPL-3.0.txt), also the root `LICENSE` |
| `gui/SukiUI` | [kikipoulet/SukiUI](https://github.com/kikipoulet/SukiUI), Copyright (c) 2022 kikipoulet | [MIT](licenses/SukiUI-MIT.txt) |
| `gui/Markdown.Avalonia`, `gui/Markdown.Avalonia.Tight` and the Markdown extension/ColorDocument/ColorTextBlock projects | [whistyun/Markdown.Avalonia](https://github.com/whistyun/Markdown.Avalonia), including the attributions below | [MIT](licenses/Markdown.Avalonia-MIT.txt) |

The copied Markdown project metadata preserves these additional attributions:

- Markdown.Avalonia / Tight: Copyright (c) 2010 Bevan Arps, 2020 whistyun; contributor metadata also names Fox.
- Markdown.Avalonia.Html: © Simon Baynes 2013; whistyun 2023.
- Markdown.Avalonia.Svg: Copyright (c) 2023 grifsun, whistyun.
- Markdown.Avalonia.SyntaxHigh: Copyright (c) 2021 whistyun.
- ColorDocument.Avalonia: Copyright (c) 2024 whistyun; contributor metadata names Bevan Arps (original) and whistyun.
- ColorTextBlock.Avalonia: Copyright (c) 2020 whistyun.

## MaaUma changes

Source export and adaptation date: **2026-10-16**. The ordinary 0.4.5 source was reorganized into an independent build layout. Changes include ordinary-client projection and update/activation UI, parent-library/planner integration, code-based branding, system-font use, relative project references, source/build tools and removal of private or user-specific inputs. The Suki side-menu footer collection isolation and GUI icon rendering adaptations are retained. See [SOURCE-ORIGIN.json](SOURCE-ORIGIN.json) and [docs/PROVENANCE.md](docs/PROVENANCE.md).

## Restored dependencies and external payloads

MaaFramework and its bindings are dependencies governed by LGPLv3; a reference copy is in [licenses/MaaFramework-LGPL-3.0.md](licenses/MaaFramework-LGPL-3.0.md). Other dependencies restored from NuGet or Python packages retain their own licenses and notices. Their declarations are in the project files and `requirements.txt`; they are not relicensed merely by being used here.

Prebuilt Python/.NET/OCR runtimes, DirectML, game resources, OCR weights and datasets are not part of this source export. A separately assembled binary distribution must preserve the terms and notices applying to every included dependency. In particular, MaaFramework's LGPL does not cover Microsoft's independently distributed DirectML binary.

Official license downloads and checksums are recorded in [licenses/ORIGINS.json](licenses/ORIGINS.json).
