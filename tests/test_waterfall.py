import unittest

from iqscan import iq_scan
from iqscan.waterfall import DEFAULT_WATERFALL_STYLE, WATERFALL_STYLES, YOUSSEF_STOPS, waterfall_cmap


class WaterfallStyleTests(unittest.TestCase):
    def test_youssef_is_the_satdump_classic_palette_and_uses_2000_lut_entries(self):
        cmap = waterfall_cmap('youssef')
        self.assertEqual(cmap.name, 'youssef')
        self.assertEqual(cmap.N, 2000)
        self.assertEqual(tuple(round(channel * 255) for channel in cmap(0)[:3]), (0, 0, 32))
        # Float 1.0 addresses the normalized colormap endpoint; integer 1 is
        # interpreted by Matplotlib as the second LUT index.
        self.assertEqual(tuple(round(channel * 255) for channel in cmap(1.0)[:3]), (74, 0, 0))
        self.assertEqual(len(YOUSSEF_STOPS), 29)

    def test_magma_is_default_and_style_argument_is_selectable(self):
        self.assertEqual(DEFAULT_WATERFALL_STYLE, 'magma')
        self.assertEqual(waterfall_cmap(), 'magma')
        self.assertEqual(WATERFALL_STYLES, ('magma', 'youssef'))
        args = iq_scan.parser().parse_args(['--waterfall-style', 'youssef'])
        self.assertEqual(args.waterfall_style, 'youssef')
        with self.assertRaises(SystemExit):
            iq_scan.parser().parse_args(['--waterfall-style', 'unknown'])


if __name__ == '__main__':
    unittest.main()
