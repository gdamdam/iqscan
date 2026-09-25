"""Small browser regressions for the interactive report and live explorer."""
import importlib.util
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

import numpy as np

from iqscan import iq_scan
from iqscan import iq_serve


def write_recording(path, sample_rate, duration, bursts=()):
    rng = np.random.default_rng(8)
    count = int(sample_rate * duration)
    t = np.arange(count) / sample_rate
    z = .02 * (rng.normal(size=count) + 1j * rng.normal(size=count))
    for frequency, start, end in bursts:
        z += .12 * np.exp(2j * np.pi * frequency * t) * ((t >= start) & (t < end))
    np.clip(np.round(np.column_stack((z.real, z.imag)) * 128), -128, 127).astype('i1').tofile(path)


@unittest.skipUnless(importlib.util.find_spec('playwright'), 'install the optional test extra and Chromium')
class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright

        cls.temp = tempfile.TemporaryDirectory()
        base = Path(cls.temp.name)
        cls.scans = base / 'scans'
        cls.scans.mkdir()

        report_input = base / 'report_32000SPS_137900000Hz.cs8'
        write_recording(report_input, 32000, 4, ((7000, .4, 1.1), (-8000, 2.1, 3.0)))
        cls.report_dir = cls.scans / 'report-run'
        result = iq_scan.main([str(report_input), '--output', str(cls.report_dir), '--fft-size', '1024',
            '--time-bin', '.032', '--threshold', '10', '--min-duration', '.1', '--save-spectrum',
            '--clips', '0', '--bandplan', 'none', '--known-signals', 'none'])
        if result != 0:
            raise RuntimeError('could not create interactive report fixture')
        events = json.loads((cls.report_dir / 'events.json').read_text())['events']
        if len(events) < 2:
            raise RuntimeError(f'browser report fixture needs two events, got {len(events)}')

        low_input = base / 'low_800SPS_1000000Hz.cs8'
        write_recording(low_input, 800, 3)
        low_dir = cls.scans / 'low-run'
        result = iq_scan.main([str(low_input), '--output', str(low_dir), '--fft-size', '256',
            '--time-bin', '.1', '--threshold', '7', '--min-duration', '.1', '--dc-exclude', '100',
            '--save-spectrum', '--clips', '0', '--bandplan', 'none', '--known-signals', 'none'])
        if result != 0:
            raise RuntimeError('could not create low-rate explorer fixture')

        parser_defaults = iq_scan.parser().parse_args([])
        defaults = {key: getattr(parser_defaults, key) for key in iq_serve.DETECT_ARGS}
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), iq_serve.handler_for(iq_serve.Spectra(cls.scans), defaults))
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}/'
        cls.playwright = sync_playwright().start()
        try:
            launch_options = {}
            executable = os.environ.get('IQSCAN_CHROMIUM_EXECUTABLE')
            if executable: launch_options['executable_path'] = executable
            cls.browser = cls.playwright.chromium.launch(**launch_options)
        except Exception as exc:
            cls.server.shutdown()
            cls.server.server_close()
            cls.temp.cleanup()
            cls.playwright.stop()
            if os.environ.get('IQSCAN_REQUIRE_BROWSER') == '1':
                raise RuntimeError(f'Chromium is required but unavailable: {exc}') from exc
            raise unittest.SkipTest(f'Chromium is unavailable: {exc}')

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, 'browser'):
            cls.browser.close()
            cls.playwright.stop()
            cls.server.shutdown()
            cls.server.server_close()
            cls.temp.cleanup()

    def test_report_sorting_and_event_selection(self):
        page = self.browser.new_page()
        page.goto(self.report_dir.joinpath('report.html').as_uri())
        page.wait_for_selector('button.sort-column')
        rows = page.locator('.scroll').first.locator('table').first.locator('tr')
        self.assertGreaterEqual(rows.count(), 3)
        start = rows.nth(1).locator('td').nth(3).inner_text()
        button = page.locator('button.sort-column').nth(3)
        button.click()
        self.assertEqual(button.locator('xpath=..').get_attribute('aria-sort'), 'ascending')
        ascending = [rows.nth(i).locator('td').nth(3).inner_text() for i in range(1, rows.count())]
        self.assertEqual(ascending, sorted(ascending, key=lambda value: float(value.split()[0])))
        page.locator('button.event-id').first.click()
        self.assertTrue(page.locator('tr.event-selected').count())
        self.assertIn('Selected event', page.locator('.event-details').inner_text())
        self.assertTrue(start)
        page.close()

    def test_explorer_safe_errors_clear_switches_and_support_keyboard(self):
        page = self.browser.new_page()
        page.goto(self.url)
        page.wait_for_function("document.querySelectorAll('#rows tr').length > 0")
        self.assertEqual(page.locator('#threshold').input_value(), '10')
        self.assertEqual(page.locator('#vthreshold').inner_text(), '10')

        malicious = '<img src=x onerror="window.pwned=1">bad scan'
        page.route('**/api/detect?*', lambda route: route.fulfill(status=400,
            content_type='application/json', body=json.dumps({'error': malicious})))
        page.locator('#threshold').evaluate("el => { el.value='11'; el.dispatchEvent(new Event('input',{bubbles:true})); }")
        page.wait_for_function("document.querySelector('#msg').textContent.includes('bad scan')")
        self.assertEqual(page.locator('#msg').inner_text(), malicious)
        self.assertEqual(page.locator('#msg img').count(), 0)
        self.assertIsNone(page.evaluate('window.pwned'))
        page.unroute('**/api/detect?*')
        page.reload()
        page.wait_for_function("document.querySelectorAll('#rows tr').length > 0")

        page.locator('#scan').select_option('low-run')
        self.assertEqual(page.locator('#cmd').inner_text(), '')
        self.assertEqual(page.locator('#rows tr').count(), 0)
        max_dc = float(page.locator('#dc_exclude').get_attribute('max'))
        self.assertLess(max_dc, 800 * .45)
        self.assertEqual(page.locator('#vdc_exclude').inner_text(), page.locator('#dc_exclude').input_value())
        page.wait_for_function("document.querySelector('#cmd').textContent.length > 0")

        page.locator('#scan').select_option('report-run')
        page.wait_for_function("document.querySelectorAll('#rows tr').length > 0")
        row = page.locator('#rows tr').first
        row.focus()
        row.press('Enter')
        self.assertIn('sel', row.get_attribute('class'))
        page.close()


if __name__ == '__main__':
    unittest.main()
