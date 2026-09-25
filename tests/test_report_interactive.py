import base64
import json
import unittest
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from iqscan.report_interactive import panel, inject

class ReportInspectionTests(unittest.TestCase):
    def test_image_coordinates_and_level_grid(self):
        fig, ax = plt.subplots()
        values = np.arange(24).reshape(4, 6) / 10
        ax.imshow(values, extent=(137,138,20,10))
        fig.canvas.draw()
        p = panel(fig, ax, 'waterfall', values)
        self.assertEqual(p['ylim'], [20, 10])
        self.assertAlmostEqual(p['box'][1], 1-ax.get_position().y1)
        grid = np.frombuffer(base64.b64decode(p['levels']['data']), dtype='<i2').reshape(4,6)/10
        np.testing.assert_allclose(grid, values)
        plt.close(fig)

    def test_partial_bin_report_preserves_plot_and_hover_time_coordinates(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from matplotlib.axes import Axes
        from iqscan import iq_scan
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'capture_32000SPS.cs8'
            source.write_bytes(bytes(64000))
            args = iq_scan.parser().parse_args([str(source), '--time-bin', '.6', '--clips', '0'])
            meta = iq_scan.metadata(args)
            f, norm, reference, dt = iq_scan.spectrum(meta, args)
            event = dict(id=1, kind='transient', start_s=.64, end_s=1., peak_time_s=.82,
                         low_offset_hz=6990., high_offset_hz=7010., center_offset_hz=7000.,
                         bandwidth_hz=20., contrast_db=10., frequency_hz=None)
            extents = []
            original = Axes.imshow
            def capture(ax, *values, **kwargs):
                extents.append(kwargs.get('extent'))
                return original(ax, *values, **kwargs)
            out = root / 'report'
            out.mkdir()
            with patch.object(Axes, 'imshow', capture):
                iq_scan.report(meta, args, [event], f, norm, reference, dt, out)
            page = (out / 'report.html').read_text()
            data = json.loads(page.split('type="application/json">')[1].split('</script>')[0])
            for name in ('waterfall.png', 'images/event-01.png'):
                panel_data = data['plots'][name][0]
                self.assertEqual(panel_data['ylim'], [1., 0.])
                self.assertEqual(panel_data['time_origin_s'], 0.)
                self.assertEqual(panel_data['time_bin_s'], .64)
            self.assertEqual([extent[2:] for extent in extents], [(1.28, 0), (1.28, 0.)])

    def test_reference_cannot_close_script(self):
        html = inject('', {}, [], [{'name':'</script><script>alert(1)</script>'}], None)
        payload = html.split('type="application/json">')[1].split('</script>')[0]
        self.assertNotIn('</script>', payload)
        self.assertEqual(json.loads(payload)['bands'][0]['name'], '</script><script>alert(1)</script>')

if __name__ == '__main__': unittest.main()
