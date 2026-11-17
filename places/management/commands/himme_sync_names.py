"""
Set name languages for the Himme dataset from HIMME's TEI records (FIXES D7).

    python manage.py himme_sync_names                 # dry run
    python manage.py himme_sync_names --apply         # write

The dataset's places.src_id is the HIMME place id; each place has a TEI file
at https://medievalmideast.org/place/<id>.xml whose names carry xml:lang:

    <placeName xml:lang="en" himme-tags="#himme-headword">Mosul</placeName>
    <placeName xml:lang="ar"><choice>
        <seg xml:lang="ar">الموصل</seg><seg xml:lang="ar-Latn">al-Mawṣil</seg>
    </choice></placeName>

Every <seg> (or a <placeName> without segs) becomes a name with that tag;
the headword gets its own xml:lang (en). If the text's script does not fit
the tag (an "ar" name written in Latin letters), the script is added
("ar-Latn"). HIMME licence: CC BY 3.0.

Per place:
  * existing place_name rows that match a HIMME name get jsonb.lang; the same
    text can occur with two tags (Diyār Bakr is ar-Latn and fa-Latn) and is
    matched row by row
  * HIMME names that are missing are inserted (unless --no-insert)
  * names HIMME does not have are left alone and listed in the report

TEI files are cached in --cache-dir (one file per place); --refresh fetches
them again. With --apply, the affected rows are first copied to
kg_backup_place_name_himme_<YYYYMMDD_HHMM>.
"""
import csv
import json
import os
import re
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from places.management.commands.kima_sync_names import clean, key, script_of

BASE = 'https://medievalmideast.org/place/'
TEI = '{http://www.tei-c.org/ns/1.0}'
XML_LANG = '{http://www.w3.org/XML/1998/namespace}lang'
TAG_OK = re.compile(r'^[a-z]{2,3}(-[A-Za-z0-9]{1,8})*$')
# default script of a language, to notice names written in another script
DEFAULT_SCRIPT = {
    'ar': 'Arab', 'fa': 'Arab', 'ota': 'Arab', 'ur': 'Arab',
    'he': 'Hebr', 'yi': 'Hebr', 'jrb': 'Hebr',
    'syr': 'Syrc', 'syc': 'Syrc', 'arc': 'Syrc',
    'hy': 'Armn', 'xcl': 'Armn', 'el': 'Grek', 'grc': 'Grek',
    'ka': 'Geor', 'cop': 'Copt',
    'en': 'Latn', 'la': 'Latn', 'fr': 'Latn', 'de': 'Latn', 'it': 'Latn', 'tr': 'Latn',
}


def tei_names(xml_text):
    """[(text, tag)] for the names of the place record."""
    root = ET.fromstring(xml_text)
    out = []
    for place in root.iter(TEI + 'place'):
        for pn in place.findall(TEI + 'placeName'):
            segs = pn.findall('.//' + TEI + 'seg')
            parts = [(s, s.get(XML_LANG) or pn.get(XML_LANG)) for s in segs] or [(pn, pn.get(XML_LANG))]
            for el, tag in parts:
                text = clean(''.join(el.itertext()))
                if text:
                    out.append((text, (tag or '').strip()))
    return out


def fit(tag, text):
    """Tag as given, with the script added when the text is in another one."""
    if not TAG_OK.match(tag or ''):
        return None
    parts = tag.split('-')
    if any(len(p) == 4 for p in parts[1:]):
        return tag  # has a script subtag already
    sc = script_of(text)
    default = DEFAULT_SCRIPT.get(parts[0])
    if sc and default and sc != default:
        return f'{parts[0]}-{sc}' + ''.join('-' + p for p in parts[1:])
    return tag


class Command(BaseCommand):
    help = 'Set name languages (and add missing names) for Himme from the HIMME TEI records.'

    def add_arguments(self, p):
        p.add_argument('--dataset', default='himme', help='dataset label (not title); matched case-insensitively')
        p.add_argument('--apply', action='store_true')
        p.add_argument('--no-insert', action='store_true')
        p.add_argument('--cache-dir', default='/srv/backup/himme_tei_cache')
        p.add_argument('--refresh', action='store_true')
        p.add_argument('--workers', type=int, default=4)
        p.add_argument('--report', default='himme_sync_names_report.csv')

    def log(self, msg):
        self.stdout.write(msg)
        self.stdout.flush()

    def fetch(self, hid):
        path = os.path.join(self.cache_dir, f'{hid}.xml')
        miss = path + '.404'
        if not self.refresh:
            if os.path.exists(path):
                with open(path, encoding='utf-8') as f:
                    return f.read()
            if os.path.exists(miss):
                return None
        req = urllib.request.Request(f'{BASE}{hid}.xml', headers={'User-Agent': 'MEHDIE gazetteer (tools.mehdie.org)'})
        for attempt in range(5):
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    text = r.read().decode('utf-8')
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(text)
                return text
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    open(miss, 'w').close()
                    return None
                err = e
            except Exception as e:
                err = e
            time.sleep(2 ** attempt)
        raise CommandError(f'HIMME fetch failed for {hid}: {err}')

    def handle(self, *a, **o):
        self.cache_dir, self.refresh = o['cache_dir'], o['refresh']
        os.makedirs(self.cache_dir, exist_ok=True)

        with connection.cursor() as c:
            c.execute('SELECT id, src_id FROM places WHERE lower(dataset) = lower(%s) ORDER BY id', [o['dataset']])
            rows = c.fetchall()
        if not rows:
            raise CommandError(f'No places in {o["dataset"]}')
        places = defaultdict(list)
        for pid, src in rows:
            s = (src or '').strip()
            if s.isdigit():
                places[int(s)].append(pid)
        self.log(f'{len(rows)} places in {o["dataset"]}; {len(places)} HIMME ids; '
                 f'{len(rows) - sum(map(len, places.values()))} without a numeric src_id')

        # ---- TEI
        tei, missing, bad = {}, [], []
        ids = sorted(places)
        done = 0
        with ThreadPoolExecutor(max_workers=o['workers']) as ex:
            for hid, text in zip(ids, ex.map(self.fetch, ids)):
                done += 1
                if done % 1000 == 0:
                    self.log(f'  TEI {done}/{len(ids)}')
                if text is None:
                    missing.append(hid)
                    continue
                try:
                    tei[hid] = tei_names(text)
                except ET.ParseError:
                    bad.append(hid)

        stats, tags_seen = Counter(), Counter()
        pids = [pid for lst in places.values() for pid in lst]
        with connection.cursor() as c:
            c.execute('SELECT id, place_id, toponym, jsonb FROM place_name WHERE place_id = ANY(%s) ORDER BY id', [pids])
            existing = c.fetchall()
        by_place = defaultdict(list)
        for nid, pid, top, js in existing:
            by_place[pid].append((nid, top, js if isinstance(js, dict) else (json.loads(js) if js else {})))

        updates, inserts, report = [], [], []
        for hid, pid_list in places.items():
            if hid not in tei:
                continue
            want = defaultdict(list)  # key -> [(text, tag)]
            for text, tag in tei[hid]:
                t = fit(tag, text)
                if tag and not t:
                    stats[f'invalid tag ignored: {tag}'] += 1
                if t and t != tag:
                    stats[f'script added: {tag} -> {t}'] += 1
                tags_seen[t or '(none)'] += 1
                want[key(text)].append((text, t))
            ref = f'{BASE}{hid}'
            for pid in pid_list:
                left = {k: list(v) for k, v in want.items()}
                for nid, top, js in by_place.get(pid, []):
                    k = key(top)
                    if left.get(k):
                        _, tag = left[k].pop(0)
                        stats['matched'] += 1
                        if tag and js.get('lang') != tag:
                            new_js = dict(js, lang=tag)
                            updates.append((nid, new_js))
                            stats[f'lang set: {tag}'] += 1
                            report.append(['update', pid, hid, nid, top, js.get('lang', ''), tag])
                    elif k in want:
                        stats['repeated in MEHDIE'] += 1
                    else:
                        stats['not in HIMME'] += 1
                        report.append(['not in HIMME', pid, hid, nid, top, js.get('lang', ''), ''])
                if o['no_insert'] or pid != pid_list[0]:
                    continue
                for k, lst in left.items():
                    for text, tag in lst:
                        js = {'toponym': text, 'citations': [{'label': 'HIMME', 'id': ref}]}
                        if tag:
                            js['lang'] = tag
                        inserts.append((pid, text, js))
                        stats[f'insert ({tag or "no language"})'] += 1
                        report.append(['insert', pid, hid, '', text, '', tag or ''])

        with open(o['report'], 'w', newline='', encoding='utf-8') as f:
            w = csv.writer(f)
            w.writerow(['action', 'place_id', 'himme_id', 'name_id', 'toponym', 'lang_before', 'lang_after'])
            w.writerows(report)
        self.log('\nSummary')
        for s, n in sorted(stats.items()):
            self.log(f'  {s}: {n}')
        self.log(f'  tags in HIMME names: {dict(tags_seen.most_common())}')
        if missing:
            self.log(f'  HIMME ids without a TEI record: {len(missing)} (e.g. {missing[:10]})')
        if bad:
            self.log(f'  unreadable TEI: {len(bad)} (e.g. {bad[:10]})')
        self.log(f'  {len(updates)} updates, {len(inserts)} inserts; details in {o["report"]}')

        if not o['apply']:
            self.log('\nDry run: nothing written. Add --apply to write.')
            return
        backup = f'kg_backup_place_name_himme_{datetime.now():%Y%m%d_%H%M}'
        src = dict(rows)
        with transaction.atomic(), connection.cursor() as c:
            c.execute(f'CREATE TABLE {backup} AS SELECT * FROM place_name WHERE place_id = ANY(%s)', [pids])
            for nid, js in updates:
                c.execute('UPDATE place_name SET jsonb = %s::jsonb WHERE id = %s',
                          [json.dumps(js, ensure_ascii=False), nid])
            for pid, text, js in inserts:
                c.execute('INSERT INTO place_name (place_id, src_id, toponym, jsonb) VALUES (%s, %s, %s, %s::jsonb)',
                          [pid, src[pid], text, json.dumps(js, ensure_ascii=False)])
        self.log(f'\nWritten. Previous rows saved in {backup}.')
