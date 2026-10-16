"""Build path guards only; no compiler, network, identity, or device calls."""
import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('public_build', ROOT/'scripts/build.py')
build = importlib.util.module_from_spec(spec); spec.loader.exec_module(build)


class BuildPaths(unittest.TestCase):
    def test_ordinary_source_version_matches_build_and_resource_contract(self):
        for name, key in (('interface.json', 'version'),
                          ('maauma-suite.json', 'base_version'),
                          ('resources/required-inputs.json', 'version')):
            with self.subTest(path=name):
                value = json.loads((ROOT/name).read_text(encoding='utf-8-sig'))
                self.assertEqual(value[key], build.VERSION)

    def test_public_project_graph_is_self_contained(self):
        self.assertTrue(build.check()['ok'])

    def test_manifest_rejects_escape_and_absolute_names(self):
        for name in ('../outside', '/outside', 'C:/outside', 'folder\\outside'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                build.member(ROOT, name)

    def test_runtime_version_and_private_tree_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manifest = {'product_id':'umaa-public', 'version':'0.4.4', 'files':[]}
            (root/'public-package-manifest.json').write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, 'same_version'):
                build.runtime_manifest(root)
            manifest['version'] = build.VERSION
            (root/'public-package-manifest.json').write_text(json.dumps(manifest))
            (root/'pacer').mkdir()
            with self.assertRaisesRegex(ValueError, 'same_version'):
                build.runtime_manifest(root)

    def test_original_mark_has_native_icon_frames(self):
        data = build.icon_bytes()
        self.assertEqual(struct.unpack('<HHH', data[:6]), (0,1,9))
        for index in range(9):
            width,height,_,_,_,depth,length,offset = struct.unpack('<BBBBHHII', data[6+index*16:22+index*16])
            self.assertEqual(depth, 32)
            self.assertEqual(data[offset:offset+8], b'\x89PNG\r\n\x1a\n')
            self.assertLessEqual(offset+length, len(data))
            self.assertEqual(width, height)


if __name__ == '__main__':
    unittest.main()
