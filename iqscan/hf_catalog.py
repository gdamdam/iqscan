"""Offline time stations and cached EiBi schedules.

Season selection and CSV fields follow msdr.catalog.parsers.eibi (GPL-3.0).
EiBi data remains a separately downloaded, attributed non-commercial source.
"""
import calendar
import csv
import io
import math
import re
from datetime import date

EIBI_HOME = 'http://www.eibispace.de/'
NIST_SOURCE = 'https://www.nist.gov/pml/time-and-frequency-division/what-time-it-faqs'


def time_signals():
    rows = []
    for station, frequencies in [('WWV — Colorado', (2.5, 5, 10, 15, 20)),
                                  ('WWVH — Hawaii', (2.5, 5, 10, 15))]:
        for mhz in frequencies:
            rows.append(dict(name=station, low_hz=mhz*1e6, high_hz=mhz*1e6,
                             mode='AM', status='frequency reference only', source_url=NIST_SOURCE))
    rows.append(dict(name='WWV — Colorado (experimental)', low_hz=25e6, high_hz=25e6,
                     mode='AM', status='experimental; reception not guaranteed',
                     source_url='https://www.nist.gov/pml/time-and-frequency-division/time-distribution/radio-station-wwv'))
    return rows


def season_code(day=None):
    day = day or date.today()
    def last_sunday(month):
        last = date(day.year, month, calendar.monthrange(day.year, month)[1])
        return last.day - (last.weekday() + 1) % 7
    if (day.month, day.day) >= (10, last_sunday(10)):
        return f'b{day.year % 100:02d}'
    if (day.month, day.day) >= (3, last_sunday(3)):
        return f'a{day.year % 100:02d}'
    return f'b{(day.year-1) % 100:02d}'


def default_url():
    return EIBI_HOME + f'dx/sked-{season_code()}.csv'


def parse_eibi(data):
    """Group schedule rows by station/frequency; keep UTC/day/date metadata.

    Matching is frequency-only: a recording's filename is not a trustworthy
    UTC timestamp. Utility entries are not all AM, so do not infer modulation.
    The download layer normalizes upstream Latin-1 into UTF-8 for the cache.
    """
    text = data.decode('utf-8-sig')
    reader = csv.reader(io.StringIO(text), delimiter=';')
    header = next(reader, [])
    if len(header) < 5 or header[0].split(':')[0].strip().lower() != 'khz' or header[4].split(':')[0].strip().lower() != 'station':
        raise ValueError('Invalid EiBi CSV header (expected kHz;Time;Days;ITU;Station)')
    grouped = {}
    for index, fields in enumerate(reader, 2):
        if not fields or not any(s.strip() for s in fields): continue
        if len(fields) < 8: raise ValueError(f'Invalid EiBi CSV row {index}: expected at least 8 fields')
        fields = [s.strip() for s in fields] + [''] * max(0, 11-len(fields))
        try: hz = float(fields[0]) * 1000
        except ValueError as exc: raise ValueError(f'Invalid EiBi frequency at row {index}') from exc
        if not math.isfinite(hz) or hz <= 0 or not fields[4]:
            raise ValueError(f'Invalid EiBi station/frequency at row {index}')
        key = (hz, fields[4], fields[3])
        if key not in grouped:
            grouped[key] = dict(name=fields[4], low_hz=hz, high_hz=hz, country=fields[3],
                                mode='unspecified', status='schedule reference; time not checked', schedules=[])
        schedule = dict(utc=fields[1], days=fields[2], language=fields[5], target=fields[6],
                        remarks=fields[7], start_date=fields[9], stop_date=fields[10])
        if schedule not in grouped[key]['schedules']: grouped[key]['schedules'].append(schedule)
    if not grouped: raise ValueError('Empty EiBi schedule')
    return list(grouped.values())


def merge_signals(builtins, schedules):
    """Enrich WWV/WWVH entries instead of consuming match slots with aliases."""
    def key(row):
        match = re.match(r'^(WWVH|WWV)\b', row['name'])
        return (match[1], row['low_hz']) if match else None
    known = {key(row): row for row in builtins if key(row)}
    for row in schedules:
        existing = known.get(key(row))
        if existing is None:
            builtins.append(row)
        else:
            existing['schedules'] = row['schedules']
            existing['schedule_source_url'] = row.get('source_url', EIBI_HOME)
