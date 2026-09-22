import base64
import json
import unittest
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from report_interactive import panel, inject

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

    def test_reference_cannot_close_script(self):
        html = inject('', {}, [], [{'name':'</script><script>alert(1)</script>'}], None)
        payload = html.split('type="application/json">')[1].split('</script>')[0]
        self.assertNotIn('</script>', payload)
        self.assertEqual(json.loads(payload)['bands'][0]['name'], '</script><script>alert(1)</script>')

if __name__ == '__main__': unittest.main()
