import os
import shutil
import subprocess
import tempfile
import unittest
import venv
from pathlib import Path


class ScanLauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.checkout = self.root / 'checkout with spaces'
        self.checkout.mkdir()
        repo = Path(__file__).resolve().parents[1]
        for name in ('scan.sh', 'scan.cmd'):
            shutil.copy2(repo / name, self.checkout)
        shutil.copytree(repo / 'iqscan', self.checkout / 'iqscan',
                        ignore=shutil.ignore_patterns('__pycache__'))
        environment = self.checkout / '.venv'
        # A real empty environment reproduces a freshly created launcher venv.
        # Only pip is stubbed, so these tests neither download nor install packages.
        venv.EnvBuilder(with_pip=False).create(environment)
        self.python = environment / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        purelib = subprocess.check_output(
            [str(self.python), '-c', 'import sysconfig; print(sysconfig.get_path("purelib"))'], text=True)
        self.site = Path(purelib.strip())
        self.log = self.root / 'pip.log'
        (self.site / 'pip.py').write_text(r"""import os, sys
from pathlib import Path
Path(os.environ['SCAN_TEST_LOG']).write_text(' '.join(sys.argv[1:]))
if os.environ.get('SCAN_TEST_PIP_FAIL'):
    raise SystemExit(9)
Path(__file__).with_name('iqscan-test.pth').write_text(sys.argv[-1] + '\n')
""", encoding='utf-8')
        self.env = os.environ.copy()
        self.env.pop('PYTHONPATH', None)
        self.env.pop('SCAN_TEST_PIP_FAIL', None)
        self.env['SCAN_TEST_LOG'] = str(self.log)
        self.env['PYTHONDONTWRITEBYTECODE'] = '1'

    def run_launcher(self, cwd):
        if os.name == 'nt':
            command = [os.environ.get('COMSPEC', 'cmd.exe'), '/d', '/c',
                       str(self.checkout / 'scan.cmd'), '--version']
        else:
            command = ['sh', str(self.checkout / 'scan.sh'), '--version']
        return subprocess.run(command, cwd=cwd, env=self.env, text=True, capture_output=True)

    def test_missing_dependencies_install_from_checkout(self):
        result = self.run_launcher(self.checkout)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.log.read_text(), 'install -e ' + str(self.checkout))
        self.assertIn('iqscan ', result.stdout)

    def test_missing_package_installs_from_another_working_directory(self):
        self.install_dependency_stubs()
        result = self.run_launcher(self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.log.read_text(), 'install -e ' + str(self.checkout))
        self.assertIn('iqscan ', result.stdout)

    def install_dependency_stubs(self):
        for name in ('numpy', 'scipy', 'matplotlib'):
            (self.site / (name + '.py')).write_text('', encoding='utf-8')

    def test_healthy_environment_skips_install(self):
        self.install_dependency_stubs()
        result = self.run_launcher(self.checkout)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.log.exists())
        self.assertIn('iqscan ', result.stdout)

    def test_failed_dependency_install_stops_before_scanner_launch(self):
        self.env['SCAN_TEST_PIP_FAIL'] = '1'
        result = self.run_launcher(self.checkout)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.log.exists())
        self.assertNotIn('iqscan ', result.stdout)


if __name__ == '__main__':
    unittest.main()
