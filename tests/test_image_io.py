"""Synthetic image-path regression; no game resources or device operations."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('public_image_io', ROOT/'backend/core/image_io.py')
image_io = importlib.util.module_from_spec(spec)
spec.loader.exec_module(image_io)


class ImagePathIO(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='maauma-image-io-')
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)/'中文目录'/ '技能图片'
        self.folder.mkdir(parents=True)
        self.pixels = np.arange(16*20*3, dtype=np.uint8).reshape(16,20,3)

    def test_unicode_png_roundtrip_preserves_pixels(self):
        path = self.folder/'图像.png'
        self.assertIs(image_io.write_image(path, self.pixels), True)
        np.testing.assert_array_equal(image_io.read_image(path), self.pixels)

    def test_read_flags_keep_color_grayscale_and_alpha_semantics(self):
        path = self.folder/'透明.png'
        alpha = np.full((16,20,1), 173, dtype=np.uint8)
        bgra = np.concatenate((self.pixels, alpha), axis=2)
        self.assertTrue(image_io.write_image(path, bgra))
        np.testing.assert_array_equal(image_io.read_image(path), self.pixels)
        np.testing.assert_array_equal(image_io.read_image(path, cv2.IMREAD_UNCHANGED), bgra)
        self.assertEqual(image_io.read_image(path, cv2.IMREAD_GRAYSCALE).shape, (16,20))

    def test_missing_empty_and_corrupt_images_still_return_none(self):
        empty, corrupt = self.folder/'空.png', self.folder/'坏.png'
        empty.write_bytes(b'')
        corrupt.write_bytes(b'synthetic invalid image')
        for path in (self.folder/'不存在.png', empty, corrupt):
            with self.subTest(case=path.name):
                self.assertIsNone(image_io.read_image(path))

    def test_write_failure_remains_false(self):
        for path in (self.folder/'missing-parent'/'图.png', self.folder/'图.unsupported'):
            with self.subTest(case=path.suffix):
                self.assertIs(image_io.write_image(path, self.pixels), False)
                self.assertFalse(path.exists())

    def test_encoder_parameters_are_retained(self):
        path = self.folder/'压缩.png'
        self.assertTrue(image_io.write_image(path, self.pixels, [cv2.IMWRITE_PNG_COMPRESSION, 1]))
        np.testing.assert_array_equal(image_io.read_image(path), self.pixels)


if __name__ == '__main__':
    unittest.main()
