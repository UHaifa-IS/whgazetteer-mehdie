"""
Delete places whose Kima id no longer exists in Kima (FIXES D7).

    python manage.py kima_prune_places                 # dry run: list them
    python manage.py kima_prune_places --apply         # delete

Uses the Kima place list cached by `kima_sync_names` (places.json in
--cache-dir) and the datasets of record group `kima` (minus --exclude;
AustriaKimaDE/HE by default, whose ids are not Kima's). Sinai (Kima)
confirmed on 2026-11-17 that places removed from Kima can go.

Deletion goes through the Django ORM, so everything that points to a place
(names, geometries, links, whens, types, reconciliation hits, …) is removed
with it. Before deleting, all those objects are written to a JSON fixture in
--backup-dir; `python manage.py loaddata <file>` restores them.
Dataset numrows are updated. Places that are in the Elasticsearch index
(places.indexed) are listed; remove them from the index separately.
"""
import json
import os
from collections import Counter
from datetime import datetime

from django.core import serializers
from django.core.management.base import BaseCommand, CommandError
from django.db import DEFAULT_DB_ALIAS, connection, transaction
from django.db.models.deletion import Collector

from places.models import Place


class Command(BaseCommand):
    help = 'Delete places of the Kima record group whose Kima id is gone from Kima.'

    def add_arguments(self, p):
        p.add_argument('--record-group', default='kima')
        p.add_argument('--exclude', action='append', default=None,
                       help='dataset label to leave out (default: AustriaKimaDE, AustriaKimaHE)')
        p.add_argument('--cache-dir', default='/srv/backup/kima_api_cache')
        p.add_argument('--backup-dir', default='/srv/backup')
        p.add_argument('--apply', action='store_true')

    def log(self, msg):
        self.stdout.write(msg)
        self.stdout.flush()

    def handle(self, *a, **o):
        exclude = o['exclude'] if o['exclude'] is not None else ['AustriaKimaDE', 'AustriaKimaHE']
        path = os.path.join(o['cache_dir'], 'places.json')
        if not os.path.exists(path):
            raise CommandError(f'{path} not found; run kima_sync_names first.')
        with open(path, encoding='utf-8') as f:
            kplaces = json.load(f)
        kima_ids = {int(p.get('Id', p.get('id'))) for p in kplaces}
        if len(kima_ids) < 40000:  # the API had 48,124 places on 2026-11-17
            raise CommandError(f'Only {len(kima_ids)} Kima places in the cache; refusing to prune from it.')

        with connection.cursor() as c:
            c.execute('SELECT label FROM datasets WHERE lower(btrim(record_group)) = lower(btrim(%s)) ORDER BY label',
                      [o['record_group']])
            labels = [r[0] for r in c.fetchall() if r[0] not in exclude]
        if not labels:
            raise CommandError(f'No datasets in record group {o["record_group"]}')
        self.log(f'Datasets: {", ".join(labels)}')

        gone = []
        for p in Place.objects.filter(dataset_id__in=labels).only('id', 'src_id', 'title', 'dataset_id', 'indexed'):
            s = (p.src_id or '').strip()
            if s.isdigit() and int(s) not in kima_ids:
                gone.append(p)
        gone.sort(key=lambda p: (int(p.src_id), p.dataset_id))
        self.log(f'\n{len(gone)} places with {len({p.src_id for p in gone})} Kima ids no longer in Kima:')
        for p in gone:
            self.log(f'  Kima {p.src_id:>6}  {p.dataset_id:22s} place {p.id:>7}  {p.title}'
                     + ('  [indexed]' if p.indexed else ''))
        if not gone:
            return

        collector = Collector(using=DEFAULT_DB_ALIAS)
        collector.collect(list(Place.objects.filter(id__in=[p.id for p in gone])))  # full rows for the backup
        counts = Counter()
        objs = []
        for model, instances in collector.data.items():
            counts[model._meta.label] += len(instances)
            objs.extend(instances)
        for qs in collector.fast_deletes:
            counts[qs.model._meta.label + ' (bulk)'] += qs.count()
            objs.extend(qs)
        self.log('\nWould delete:' if not o['apply'] else '\nDeleting:')
        for label, n in sorted(counts.items()):
            self.log(f'  {label}: {n}')
        indexed = [p.id for p in gone if p.indexed]
        if indexed:
            self.log(f'  {len(indexed)} of them are in the Elasticsearch index: {indexed[:20]}')

        if not o['apply']:
            self.log('\nDry run: nothing deleted. Add --apply to delete.')
            return

        backup = os.path.join(o['backup_dir'], f'kima_pruned_places_{datetime.now():%Y%m%d_%H%M}.json')
        with open(backup, 'w', encoding='utf-8') as f:
            f.write(serializers.serialize('json', objs, indent=1))
        self.log(f'\nBackup: {backup} (restore with: python manage.py loaddata {backup})')

        with transaction.atomic():
            Place.objects.filter(id__in=[p.id for p in gone]).delete()
            with connection.cursor() as c:
                c.execute('UPDATE datasets d SET numrows = (SELECT count(*) FROM places p WHERE p.dataset = d.label) '
                          'WHERE d.label = ANY(%s)', [sorted({p.dataset_id for p in gone})])
        self.log(f'Deleted {len(gone)} places.')
