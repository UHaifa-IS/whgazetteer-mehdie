"""
Sync place names of Kima-derived datasets with the Kima API (FIXES D7).

    python manage.py kima_sync_names --dataset KIMA-IL            # dry run
    python manage.py kima_sync_names --dataset KIMA-IL --apply    # write

The datasets' places.src_id is the Kima place id. For each place the command
builds Kima's name list:

  * primary_heb_full  -> language he   (trailing " (qualifier)" removed)
  * primary_arab_full -> language ar   (same)
  * primary_rom_full  -> no language of its own (romanised headings mix
                         languages); gets one only through a variant
  * every variant     -> the majority language of its attestations
                         (variant -> attestation -> language)

An attestation language is used only if it fits the script of the name:
NLI/BHB record the language of the *book*, so a Hebrew-script imprint in a
German book is not German. Codes not in LANG below are ignored (reported).

Then, per place:
  * existing place_name rows that match a Kima name get jsonb.lang set
  * Kima names that are missing are inserted (unless --no-insert)
  * NLI's non-filing markers are removed from names: "<<ال>>قدس" -> "القدس"
  * names Kima does not have are left alone and listed in the report

Names without a usable language keep no lang, so the KG mapping still gives
them their script tag (und-Hebr, ...).

The API key goes in whg/local_settings.py as KIMA_API_KEY (or the
environment variable KIMA_API_KEY). Without it the API returns at most 50
objects per list and the download fails.

API responses are cached in --cache-dir; use --refresh to download again.
With --apply, the affected place_name rows are first copied to a backup
table kg_backup_place_name_kima_<YYYYMMDD>.
"""
import csv
import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import date

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

API = 'https://data.geo-kima.org/api/'

# Kima/MARC language code -> (BCP 47 tag, scripts the language is written in)
LANG = {
    # Hebrew script
    'heb': ('he', {'Hebr'}), 'yid': ('yi', {'Hebr'}), 'arc': ('arc', {'Hebr', 'Syrc'}),
    'jrb': ('jrb', {'Hebr'}), 'jpr': ('jpr', {'Hebr'}),
    'lad': ('lad', {'Hebr', 'Latn'}),
    # Arabic script
    'ara': ('ar', {'Arab'}), 'per': ('fa', {'Arab'}), 'fas': ('fa', {'Arab'}),
    'urd': ('ur', {'Arab'}), 'ota': ('ota', {'Arab'}),
    # Other scripts
    'gre': ('el', {'Grek'}), 'ell': ('el', {'Grek'}), 'grc': ('grc', {'Grek'}),
    'arm': ('hy', {'Armn'}), 'hye': ('hy', {'Armn'}),
    'syr': ('syr', {'Syrc'}), 'syc': ('syc', {'Syrc'}),
    'rus': ('ru', {'Cyrl'}), 'ukr': ('uk', {'Cyrl'}), 'bul': ('bg', {'Cyrl'}),
    'bel': ('be', {'Cyrl'}), 'mac': ('mk', {'Cyrl'}), 'srp': ('sr', {'Cyrl', 'Latn'}),
    # Latin script
    'eng': ('en', {'Latn'}), 'ger': ('de', {'Latn'}), 'deu': ('de', {'Latn'}),
    'fre': ('fr', {'Latn'}), 'fra': ('fr', {'Latn'}), 'ita': ('it', {'Latn'}),
    'spa': ('es', {'Latn'}), 'por': ('pt', {'Latn'}), 'dut': ('nl', {'Latn'}),
    'nld': ('nl', {'Latn'}), 'lat': ('la', {'Latn'}), 'pol': ('pl', {'Latn'}),
    'cze': ('cs', {'Latn'}), 'ces': ('cs', {'Latn'}), 'slo': ('sk', {'Latn'}),
    'hun': ('hu', {'Latn'}), 'rum': ('ro', {'Latn'}), 'ron': ('ro', {'Latn'}),
    'swe': ('sv', {'Latn'}), 'dan': ('da', {'Latn'}), 'nor': ('no', {'Latn'}),
    'fin': ('fi', {'Latn'}), 'tur': ('tr', {'Latn'}), 'hrv': ('hr', {'Latn'}),
    'slv': ('sl', {'Latn'}), 'cat': ('ca', {'Latn'}), 'alb': ('sq', {'Latn'}),
    'baq': ('eu', {'Latn'}), 'ice': ('is', {'Latn'}), 'est': ('et', {'Latn'}),
    'lav': ('lv', {'Latn'}), 'lit': ('lt', {'Latn'}), 'mlt': ('mt', {'Latn'}),
    'afr': ('af', {'Latn'}), 'epo': ('eo', {'Latn'}), 'wel': ('cy', {'Latn'}),
    'gle': ('ga', {'Latn'}), 'ind': ('id', {'Latn'}), 'may': ('ms', {'Latn'}),
}
# languages whose script is ambiguous get an explicit script subtag
SCRIPT_SUBTAG = {'lad', 'sr', 'arc'}

MARKS = re.compile('[֑-ׇً-ٰٟ̀-ͯܰ-݊]')
SCRIPTS = [
    ('Arab', re.compile('[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]')),
    ('Hebr', re.compile('[֐-׿יִ-ﭏ]')),
    ('Syrc', re.compile('[܀-ݏ]')),
    ('Armn', re.compile('[԰-֏ﬓ-ﬗ]')),
    ('Grek', re.compile('[Ͱ-Ͽἀ-῿]')),
    ('Cyrl', re.compile('[Ѐ-ԯ]')),
    ('Latn', re.compile('[A-Za-zÀ-ɏḀ-ỿ]')),
]
# Yiddish spelling: double vav/yod ligatures, pointed alef, or ayin as a vowel
# inside a word (יערושאלאים). Hebrew names without these are not Yiddish.
YIDDISH = re.compile('[\u05F0-\u05F2]|\u05D0[\u05B7\u05B8]|[\u05D0-\u05EA]\u05E2')
QUALIFIER = re.compile(r'\s*\([^()]*\)\s*$')


def script_of(text):
    """Single script of the text (as in the KG mapping), or None if mixed/none."""
    t = MARKS.sub('', text or '')
    found = [name for name, rx in SCRIPTS if rx.search(t)]
    return found[0] if len(found) == 1 else None


def clean(text):
    """Name as it should be stored: NFC, NLI non-filing markers removed."""
    t = unicodedata.normalize('NFC', text or '')
    t = t.replace('<<', '').replace('>>', '')
    return re.sub(r'\s+', ' ', t).strip()


def key(text):
    """Comparison key: cleaned, Hebrew geresh/gershayim unified."""
    t = clean(text).replace('״', '"').replace('׳', "'")
    return t.casefold()


def lang_tag(code, text):
    """BCP 47 tag for an attestation language, or None if unknown/unfit."""
    entry = LANG.get((code or '').strip().lower())
    if not entry:
        return None
    tag, scripts = entry
    sc = script_of(text)
    if sc not in scripts:
        return None
    if tag == 'yi' and not YIDDISH.search(text):
        return None  # Hebrew name cited in a Yiddish book (e.g. "קיבוץ אלונים")
    return f'{tag}-{sc}' if tag in SCRIPT_SUBTAG else tag


class Kima:
    def __init__(self, api_key, cache_dir, page_size, refresh, log):
        self.api_key = api_key
        self.cache_dir = cache_dir
        self.page_size = page_size
        self.refresh = refresh
        self.log = log
        os.makedirs(cache_dir, exist_ok=True)

    def get(self, path):
        url = API + urllib.parse.quote(path, safe='/|')
        req = urllib.request.Request(url, headers={'code': self.api_key, 'Accept': 'application/json'})
        for attempt in range(6):
            try:
                with urllib.request.urlopen(req, timeout=180) as r:
                    return json.loads(r.read().decode('utf-8'))
            except Exception as e:  # network hiccups, 5xx
                if attempt == 5:
                    raise CommandError(f'Kima API failed for {url}: {e}')
                time.sleep(2 ** attempt)

    def dump(self, kind, sorts):
        """All objects of a list endpoint, cached. kind: Places|Variants|Attestations."""
        path = os.path.join(self.cache_dir, f'{kind.lower()}.json')
        if os.path.exists(path) and not self.refresh:
            with open(path, encoding='utf-8') as f:
                return json.load(f)
        objs, total = {}, None
        for sort in sorts:  # paging over a non-unique sort can skip rows; a second sort fills gaps
            page = 1
            while True:
                res = self.get(f'{kind}/{page}/{self.page_size}/0/{sort}/none')
                ents = res.get('entities') or []
                total = res.get('count', total)
                for e in ents:
                    objs[e.get('Id', e.get('id'))] = e
                self.log(f'  {kind} {sort} page {page}: {len(objs)}/{total}')
                if len(ents) < self.page_size:
                    break
                page += 1
            if total is not None and len(objs) >= total:
                break
        if total is not None and len(objs) < total:
            self.log(f'  WARNING: {kind}: got {len(objs)} of {total}')
        if total and len(objs) <= 50 < total:
            raise CommandError(f'{kind}: only {len(objs)} objects returned; is the API key set?')
        data = list(objs.values())
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)
        return data


def g(obj, *names):
    """Field by any of its spellings (the API mixes Id/id, SourceId/sourceId)."""
    for n in names:
        if n in obj:
            return obj[n]
        for k in obj:
            if k.lower() == n.lower():
                return obj[k]
    return None


class Command(BaseCommand):
    help = 'Set name languages (and add missing names) for Kima-derived datasets from the Kima API.'

    def add_arguments(self, p):
        p.add_argument('--dataset', action='append', required=True,
                       help='dataset label whose places.src_id are Kima ids (repeatable)')
        p.add_argument('--apply', action='store_true', help='write to the database (default: dry run)')
        p.add_argument('--no-insert', action='store_true', help='only set languages, add no names')
        p.add_argument('--cache-dir', default='/srv/backup/kima_api_cache')
        p.add_argument('--refresh', action='store_true', help='download from the API again')
        p.add_argument('--page-size', type=int, default=1000)
        p.add_argument('--report', default='kima_sync_names_report.csv')

    def log(self, msg):
        self.stdout.write(msg)
        self.stdout.flush()

    def handle(self, *a, **o):
        api_key = getattr(settings, 'KIMA_API_KEY', None) or os.environ.get('KIMA_API_KEY')
        cached = all(os.path.exists(os.path.join(o['cache_dir'], f'{k}.json'))
                     for k in ('places', 'variants', 'attestations'))
        if not api_key and (o['refresh'] or not cached):
            raise CommandError('Set KIMA_API_KEY in whg/local_settings.py (or the environment).')

        # ---- places of the datasets
        with connection.cursor() as c:
            c.execute('SELECT id, src_id, dataset FROM places WHERE dataset = ANY(%s)', [o['dataset']])
            rows = c.fetchall()
        if not rows:
            raise CommandError(f'No places in {o["dataset"]}')
        places = defaultdict(list)  # kima id -> [mehdie place ids]
        bad_src = 0
        for pid, src, ds in rows:
            s = (src or '').strip()
            if s.isdigit():
                places[int(s)].append(pid)
            else:
                bad_src += 1
        self.log(f'{len(rows)} places in {", ".join(o["dataset"])}; {len(places)} Kima ids; '
                 f'{bad_src} without a numeric src_id')

        # ---- Kima data
        k = Kima(api_key, o['cache_dir'], o['page_size'], o['refresh'], self.log)
        kplaces = {g(x, 'Id'): x for x in k.dump('Places', ['hebasc', 'romasc'])}
        variants = k.dump('Variants', ['hebasc', 'romasc'])
        atts = k.dump('Attestations', ['attasc', 'dteasc'])
        self.log(f'Kima: {len(kplaces)} places, {len(variants)} variants, {len(atts)} attestations')

        votes = defaultdict(Counter)  # variant id -> Counter(tag)
        unknown_codes, unfit = Counter(), Counter()
        vtext = {g(v, 'Id'): g(v, 'variant') or '' for v in variants}
        for at in atts:
            vid = g(at, 'VariantId')
            code = (g(at, 'language') or '').strip().lower()
            if vid not in vtext or not code:
                continue
            tag = lang_tag(code, vtext[vid])
            if tag:
                votes[vid][tag] += 1
            elif code not in LANG:
                unknown_codes[code] += 1
            else:
                unfit[f'{code}/{script_of(vtext[vid])}'] += 1

        names = defaultdict(dict)  # kima place id -> key -> {text, votes, source, ref}
        def add(kpid, text, tag_votes, source, ref):
            t = clean(text)
            if not t or kpid not in places:
                return
            e = names[kpid].setdefault(key(t), {'text': t, 'votes': Counter(), 'source': source, 'ref': ref})
            e['votes'].update(tag_votes)

        for v in variants:
            add(g(v, 'PlaceId'), g(v, 'variant'), votes.get(g(v, 'Id'), Counter()),
                f'Kima variant (source {g(v, "SourceId")})', f'{API}Variants/{g(v, "Id")}')
        for kpid in places:
            kp = kplaces.get(kpid)
            if not kp:
                continue
            ref = f'{API}Places/{kpid}'
            heb = QUALIFIER.sub('', g(kp, 'primary_heb_full') or '')
            ara = QUALIFIER.sub('', g(kp, 'primary_arab_full') or '')
            rom = QUALIFIER.sub('', g(kp, 'primary_rom_full') or '')
            if script_of(heb) == 'Hebr':
                add(kpid, heb, Counter({'he': 10 ** 6}), 'Kima primary Hebrew name', ref)
            if script_of(ara) == 'Arab':
                add(kpid, ara, Counter({'ar': 10 ** 6}), 'Kima primary Arabic name', ref)
            add(kpid, rom, Counter(), 'Kima primary romanised name', ref)

        missing_kima = [kp for kp in places if kp not in kplaces]

        # ---- existing names
        pids = [pid for lst in places.values() for pid in lst]
        with connection.cursor() as c:
            c.execute('SELECT id, place_id, toponym, jsonb FROM place_name WHERE place_id = ANY(%s)', [pids])
            existing = c.fetchall()
        by_place = defaultdict(list)
        for nid, pid, top, js in existing:
            by_place[pid].append((nid, top, js if isinstance(js, dict) else (json.loads(js) if js else {})))

        updates, inserts, report = [], [], []
        stats = Counter()
        for kpid, pid_list in places.items():
            kn = names.get(kpid, {})
            for pid in pid_list:
                seen = set()
                for nid, top, js in by_place.get(pid, []):
                    kk = key(top)
                    e = kn.get(kk)
                    new_top = clean(top)
                    new_js = dict(js)
                    if e:
                        seen.add(kk)
                        stats['matched'] += 1
                        if e['votes']:
                            tag, n = e['votes'].most_common(1)[0]
                            if len(e['votes']) > 1:
                                stats['conflicting votes'] += 1
                            if js.get('lang') != tag:
                                new_js['lang'] = tag
                                stats[f'lang set: {tag}'] += 1
                        else:
                            stats['matched, no usable language'] += 1
                    else:
                        stats['not in Kima'] += 1
                        report.append(['not in Kima', pid, kpid, nid, top, js.get('lang', ''), ''])
                    if new_top != top:
                        new_js['toponym'] = new_top
                        stats['markup removed'] += 1
                    if new_top != top or new_js != js:
                        updates.append((nid, new_top, new_js))
                        report.append(['update', pid, kpid, nid, f'{top} -> {new_top}' if new_top != top else top,
                                       js.get('lang', ''), new_js.get('lang', '')])
                if o['no_insert']:
                    continue
                for kk, e in kn.items():
                    if kk in seen:
                        continue
                    tag = e['votes'].most_common(1)[0][0] if e['votes'] else None
                    js = {'toponym': e['text'], 'citations': [{'label': e['source'], 'id': e['ref']}]}
                    if tag:
                        js['lang'] = tag
                    inserts.append((pid, kpid, e['text'], js))
                    stats['insert' + (f' ({tag})' if tag else ' (no language)')] += 1
                    report.append(['insert', pid, kpid, '', e['text'], '', tag or ''])

        # ---- report
        with open(o['report'], 'w', newline='', encoding='utf-8') as f:
            w = csv.writer(f)
            w.writerow(['action', 'place_id', 'kima_id', 'name_id', 'toponym', 'lang_before', 'lang_after'])
            w.writerows(report)
        self.log('\nSummary')
        for s, n in sorted(stats.items()):
            self.log(f'  {s}: {n}')
        if missing_kima:
            self.log(f'  Kima ids not in the API: {len(missing_kima)} (e.g. {missing_kima[:10]})')
        if unknown_codes:
            self.log(f'  unknown language codes (ignored): {dict(unknown_codes.most_common(30))}')
        if unfit:
            self.log(f'  language/script mismatches (ignored): {dict(unfit.most_common(30))}')
        self.log(f'  {len(updates)} updates, {len(inserts)} inserts; details in {o["report"]}')

        if not o['apply']:
            self.log('\nDry run: nothing written. Add --apply to write.')
            return

        backup = f'kg_backup_place_name_kima_{date.today():%Y%m%d}'
        with transaction.atomic(), connection.cursor() as c:
            c.execute(f'CREATE TABLE IF NOT EXISTS {backup} AS SELECT * FROM place_name WHERE false')
            c.execute(f'INSERT INTO {backup} SELECT * FROM place_name WHERE place_id = ANY(%s)', [pids])
            for nid, top, js in updates:
                c.execute('UPDATE place_name SET toponym = %s, jsonb = %s::jsonb WHERE id = %s',
                          [top, json.dumps(js, ensure_ascii=False), nid])
            src = {pid: s for pid, s, _ in rows}
            for pid, kpid, text, js in inserts:
                c.execute('INSERT INTO place_name (place_id, src_id, toponym, jsonb) VALUES (%s, %s, %s, %s::jsonb)',
                          [pid, src[pid], text, json.dumps(js, ensure_ascii=False)])
        self.log(f'\nWritten. Previous rows saved in {backup}.')
