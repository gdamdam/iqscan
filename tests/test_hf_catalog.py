import argparse
import contextlib
import io
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch
from iqscan import hf_catalog as hf, spectrum_refs as refs, iq_scan

CSV = ('kHz:75;Time(UTC):93;Days:59;ITU:49;Station:201;Lng:49;Target:62;Remarks:135;P:35;Start:60;Stop:60;\n'
       '15000;0000-2400;;USA;WWV;E;NAm;;1;;\n'
       '6070;2200-0600;Mo-Fr;D;Test café;G;Eu;;1;0101;1231\n'
       '6070;0600-0700;Sa;D;Test café;E;Eu;;1;;\n')

class HFCatalogTests(unittest.TestCase):
    def args(self, root, *extra):
        p=argparse.ArgumentParser();refs.add_arguments(p)
        return p.parse_args(['--bandplan','none','--reference-cache',str(root),*extra])

    def response(self, text=CSV):
        response=MagicMock();response.__enter__.return_value=response
        response.read.return_value=text.encode('latin-1');response.headers.get.return_value=None
        return response

    def test_season_boundaries(self):
        for day,expected in [(date(2026,1,1),'b25'),(date(2026,3,28),'b25'),
                             (date(2026,3,29),'a26'),(date(2026,10,24),'a26'),
                             (date(2026,10,25),'b26'),(date(2027,1,1),'b26')]:
            self.assertEqual(hf.season_code(day),expected)

    def test_schedule_grouping_preserves_time_and_does_not_guess_utility_mode(self):
        rows=hf.parse_eibi(CSV.encode())
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[1]['low_hz'],6070000)
        self.assertEqual(rows[1]['name'],'Test café')
        self.assertEqual(len(rows[1]['schedules']),2)
        self.assertEqual(rows[1]['schedules'][0]['utc'],'2200-0600')
        self.assertEqual(rows[1]['schedules'][0]['start_date'],'0101')
        self.assertEqual(rows[1]['mode'],'unspecified')

    def test_rejects_html_empty_and_bad_frequencies(self):
        for data in ['<html>maintenance</html>',CSV.splitlines()[0],CSV.replace('15000;','nan;'),CSV.replace('15000;','-1;')]:
            with self.assertRaises(ValueError):hf.parse_eibi(data.encode())

    def test_offline_fresh_install_has_time_stations_without_network(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(refs,'urlopen',side_effect=AssertionError('network')):
            args=self.args(tmp,'--offline-references');catalog=refs.load(args)
            events=[dict(low_offset_hz=-100,high_offset_hz=100)]
            refs.describe(dict(center_frequency_hz=15e6,sample_rate=2000000),events,catalog,args)
            self.assertEqual({r['name'] for r in events[0]['known_signal_hints']},{'WWV — Colorado','WWVH — Hawaii'})
            self.assertFalse(any('WWVH' in r['name'] and r['low_hz'] in (20e6,25e6) for r in catalog['signals']))
            self.assertEqual(refs.load(self.args(tmp,'--known-signals','none'))['signals'],[])

    def test_update_then_automatic_offline_reuse_and_latin1(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(refs,'urlopen',return_value=self.response()) as fetch:
                catalog=refs.load(self.args(tmp,'--update-references'))
                self.assertEqual(fetch.call_count,1)
            self.assertTrue((Path(tmp)/'eibi-schedule.json').exists())
            self.assertTrue(any(r['name']=='Test café' for r in catalog['signals']))
            with patch.object(refs,'urlopen',side_effect=AssertionError('network')):
                offline=refs.load(self.args(tmp,'--offline-references'))
                online=refs.load(self.args(tmp)) # fresh cache requires no network
            self.assertEqual(offline['signals'],online['signals'])

    def test_wwv_aliases_enrich_instead_of_duplicate(self):
        rows=hf.time_signals()
        hf.merge_signals(rows,hf.parse_eibi(CSV.replace(';WWV;', ';WWV Colorado (m.ann.);').encode()))
        matches=[r for r in rows if r['low_hz']==15000000]
        self.assertEqual(len(matches),2)
        wwv=next(r for r in matches if r['name']=='WWV — Colorado')
        self.assertEqual(wwv['mode'],'AM')
        self.assertEqual(wwv['schedules'][0]['utc'],'0000-2400')

    def test_explicit_eibi_fetches_on_first_scan(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(refs,'urlopen',return_value=self.response()) as fetch:
            refs.load(self.args(tmp,'--known-signals','eibi'))
            self.assertEqual(fetch.call_count,1)

    def test_bad_refresh_keeps_good_cache_and_missing_cache_keeps_builtins(self):
        with tempfile.TemporaryDirectory() as tmp:
            args=self.args(tmp,'--update-references')
            with patch.object(refs,'urlopen',return_value=self.response()):refs.load(args)
            path=Path(tmp)/'eibi-schedule.json';before=path.read_bytes()
            with patch.object(refs,'urlopen',return_value=self.response('<html>no schedule</html>')):
                with contextlib.redirect_stderr(io.StringIO()):catalog=refs.load(args)
            self.assertEqual(before,path.read_bytes())
            self.assertTrue(catalog['warnings'])
            self.assertTrue(any(r['name']=='Test café' for r in catalog['signals']))
            path.unlink()
            with patch.object(refs,'urlopen',side_effect=OSError('offline')):
                with contextlib.redirect_stderr(io.StringIO()):catalog=refs.load(args)
            self.assertTrue(catalog['warnings'])
            self.assertTrue(any(r['name']=='WWV — Colorado' for r in catalog['signals']))

    def test_season_rollover_falls_back_offline_then_replaces_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            old=hf.EIBI_HOME+'dx/sked-b25.csv';new=hf.EIBI_HOME+'dx/sked-a26.csv'
            with patch.object(hf,'default_url',return_value=old),patch.object(refs,'urlopen',return_value=self.response()):
                refs.load(self.args(tmp,'--update-references'))
            with patch.object(hf,'default_url',return_value=new),patch.object(refs,'urlopen',side_effect=AssertionError('network')):
                with contextlib.redirect_stderr(io.StringIO()):catalog=refs.load(self.args(tmp,'--offline-references'))
            self.assertTrue(catalog['warnings'])
            self.assertTrue(any(r['name']=='Test café' for r in catalog['signals']))
            with patch.object(hf,'default_url',return_value=new),patch.object(refs,'urlopen',return_value=self.response()):
                refs.load(self.args(tmp,'--update-references'))
            self.assertEqual(json.loads((Path(tmp)/'eibi-schedule.json').read_text())['source_url'],new)

    def test_update_cli_reports_failure_without_iq_file(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(refs,'urlopen',side_effect=OSError('offline')):
            with contextlib.redirect_stderr(io.StringIO()),contextlib.redirect_stdout(io.StringIO()):
                code=iq_scan.main(['--update-references','--bandplan','none','--reference-cache',tmp])
            self.assertEqual(code,2)

if __name__=='__main__':unittest.main()
