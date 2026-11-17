"""
Set name languages for the Trismegistos dataset from the TM Geo table dump
(FIXES D7).

    python manage.py tm_sync_names                 # dry run
    python manage.py tm_sync_names --apply         # write

The dataset's places.src_id is the TM Geo id. The dump
(https://www.trismegistos.org/dataservices/tabledump/, CC BY-SA) gives per
place a Latin, Greek, Demotic and Coptic name field, variants joined by
" - ":

    latin    -> la         (TM language code 215)
    greek    -> grc        (200; ancient sources, so Ancient Greek)
    coptic   -> cop        (204)
    egyptian -> egy-Latn   (070; Demotic in transliteration)

TM's standard name (the place title, e.g. "Krokodilopolis", "Rossdorf") is a
normalised or modern form with no language, so it gets none; if it equals a
Latin variant it gets la through that variant.

Per place: matching place_name rows get jsonb.lang; TM variants that are
missing are inserted (unless --no-insert); other names are left alone and
listed in the report. The dump is cached in --cache-dir (--refresh to fetch
again). With --apply, the affected rows are first copied to
kg_backup_place_name_tm_<YYYYMMDD_HHMM>.
"""
import csv
import json
import os
import re
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from places.management.commands.kima_sync_names import clean, key

DUMP = 'https://www.trismegistos.org/dataservices/tabledump/dump.php?serve=geo'
FIELDS = {'latin': 'la', 'greek': 'grc', 'coptic': 'cop', 'egyptian': 'egy-Latn'}
SEP = re.compile(r'\s+-\s+')


class Command(BaseCommand):
    help = 'Set name languages (and add missing names) for Trismegistos from the TM Geo dump.'

    def add_arguments(self, p):
        p.add_argument('--dataset', default='trismegistos', help='dataset label; matched case-insensitively')
        p.add_argument('--apply', action='store_true')
        p.add_argument('--no-insert', action='store_true')
        p.add_argument('--cache-dir', default='/srv/backup/tm_geo_cache')
        p.add_argument('--refresh', action='store_true')
        p.add_argument('--report', default='tm_sync_names_report.csv')

    def log(self, msg):
        self.stdout.write(msg)
        self.stdout.flush()

    def dump(self, cache_dir, refresh):
        os.makedirs(cache_dir, exist_ok=True)
        path = os.path.join(cache_dir, 'tm_geo.json')
        if os.path.exists(path) and not refresh:
            with open(path, encoding='utf-8') as f:
                return json.load(f)
        body = urllib.parse.urlencode(
            [('checkbox[]', c) for c in ('id', 'name_standard', 'name_latin', 'unicode_greek',
                                         'unicode_egyptian', 'unicode_coptic')] + [('mode', 'json')]).encode()
        req = urllib.request.Request(DUMP, data=body, headers={'User-Agent': 'MEHDIE gazetteer (tools.mehdie.org)'})
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                data = json.loads(r.read().decode('utf-8'))
        except Exception as e:
            raise CommandError(f'TM dump download failed: {e}')
        if len(data.get('features', [])) < 50000:  # 65,247 places on 2026-11-17
            raise CommandError(f'TM dump has only {len(data.get("features", []))} places; not using it.')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)
        return data

    def handle(self, *a, **o):
        with connection.cursor() as c:
            c.execute('SELECT id, src_id FROM places WHERE lower(dataset) = lower(%s) ORDER BY id', [o['dataset']])
            rows = c.fetchall()
        if not rows:
            raise CommandError(f'No places in {o["dataset"]}')
        places = defaultdict(list)
        for pid, src in rows:
            s = (src or '').strip()
            if s.isdigit():
                places[s].append(pid)
        self.log(f'{len(rows)} places in {o["dataset"]}; {len(places)} TM ids')

        tm = {str(f.get('id')): f for f in self.dump(o['cache_dir'], o['refresh']).get('features', [])}
        self.log(f'TM dump: {len(tm)} places')

        pids = [pid for lst in places.values() for pid in lst]
        with connection.cursor() as c:
            c.execute('SELECT id, place_id, toponym, jsonb FROM place_name WHERE place_id = ANY(%s) ORDER BY id', [pids])
            existing = c.fetchall()
        by_place = defaultdict(list)
        for nid, pid, top, js in existing:
            by_place[pid].append((nid, top, js if isinstance(js, dict) else (json.loads(js) if js else {})))

        stats = Counter()
        updates, inserts, report, missing = [], [], [], []
        for tid, pid_list in places.items():
            f = tm.get(tid)
            if not f:
                missing.append(tid)
                continue
            want = defaultdict(list)
            for field, tag in FIELDS.items():
                for part in SEP.split((f.get('names', {}).get(field) or {}).get('name') or ''):
                    t = clean(part)
                    if t:
                        want[key(t)].append((t, tag))
            ref = f'https://www.trismegistos.org/place/{tid}'
            for pid in pid_list:
                left = {k: list(v) for k, v in want.items()}
                for nid, top, js in by_place.get(pid, []):
                    k = key(top)
                    if left.get(k):
                        _, tag = left[k].pop(0)
                        stats['matched'] += 1
                        if js.get('lang') != tag:
                            updates.append((nid, dict(js, lang=tag)))
                            stats[f'lang set: {tag}'] += 1
                            report.append(['update', pid, tid, nid, top, js.get('lang', ''), tag])
                    elif k in want:
                        stats['repeated in MEHDIE'] += 1
                    else:
                        stats['not a TM language field (standard name or other)'] += 1
                        report.append(['no TM language', pid, tid, nid, top, js.get('lang', ''), ''])
                if o['no_insert'] or pid != pid_list[0]:
                    continue
                for lst in left.values():
                    for text, tag in lst:
                        inserts.append((pid, text, {'toponym': text, 'lang': tag,
                                                    'citations': [{'label': 'Trismegistos', 'id': ref}]}))
                        stats[f'insert ({tag})'] += 1
                        report.append(['insert', pid, tid, '', text, '', tag])

        with open(o['report'], 'w', newline='', encoding='utf-8') as fh:
            w = csv.writer(fh)
            w.writerow(['action', 'place_id', 'tm_id', 'name_id', 'toponym', 'lang_before', 'lang_after'])
            w.writerows(report)
        self.log('\nSummary')
        for s, n in sorted(stats.items()):
            self.log(f'  {s}: {n}')
        if missing:
            self.log(f'  TM ids not in the dump: {len(missing)} (e.g. {missing[:10]})')
        self.log(f'  {len(updates)} updates, {len(inserts)} inserts; details in {o["report"]}')
        if not o['apply']:
            self.log('\nDry run: nothing written. Add --apply to write.')
            return

        backup = f'kg_backup_place_name_tm_{datetime.now():%Y%m%d_%H%M}'
        src = dict(rows)
        with transaction.atomic(), connection.cursor() as c:
            c.execute(f'CREATE TABLE {backup} AS SELECT * FROM place_name WHERE place_id = ANY(%s)', [pids])
            for nid, js in updates:
                c.execute('UPDATE place_name SET jsonb = %s::jsonb WHERE id = %s', [json.dumps(js, ensure_ascii=False), nid])
            for pid, text, js in inserts:
                c.execute('INSERT INTO place_name (place_id, src_id, toponym, jsonb) VALUES (%s, %s, %s, %s::jsonb)',
                          [pid, src[pid], text, json.dumps(js, ensure_ascii=False)])
        self.log(f'\nWritten. Previous rows saved in {backup}.')
