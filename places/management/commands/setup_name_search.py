"""
Set up fuzzy, accent-insensitive name search for the database search page.

    python manage.py setup_name_search          # create (idempotent)
    python manage.py setup_name_search --drop   # remove index and function

Creates the pg_trgm and unaccent extensions, an IMMUTABLE normalising
function mehdie_norm(text), and a trigram GIN index on
mehdie_norm(place_name.toponym). search/name_search.py relies on these;
without them the search page falls back to the old prefix match.

mehdie_norm lower-cases, strips Latin diacritics (unaccent), Hebrew points
and cantillation (U+0591-U+05C7), Arabic harakat and tatweel, and folds
Arabic alef variants to bare alef.

CREATE EXTENSION needs the database owner on PostgreSQL 13+ (both are
"trusted" extensions) or a superuser on older versions. If it fails, run
once as postgres:
    sudo -u postgres psql mehdie -c "CREATE EXTENSION IF NOT EXISTS pg_trgm; CREATE EXTENSION IF NOT EXISTS unaccent;"
and then this command again.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

INDEX = 'place_name_toponym_norm_trgm'

FUNCTION_SQL = r"""
CREATE OR REPLACE FUNCTION mehdie_norm(t text) RETURNS text
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
  SELECT translate(
           regexp_replace(
             lower(public.unaccent('public.unaccent'::regdictionary, t)),
             '[֑-ׇؐ-ًؚ-ٰٟـ]', '', 'g'),
           'أإآٱ', 'اااا')
$$;
"""


class Command(BaseCommand):
    help = "Create pg_trgm/unaccent, mehdie_norm() and the place_name trigram index."

    def add_arguments(self, parser):
        parser.add_argument('--drop', action='store_true', help='drop the index and function')

    def handle(self, *args, **opts):
        with connection.cursor() as cur:
            if opts['drop']:
                cur.execute(f'DROP INDEX CONCURRENTLY IF EXISTS {INDEX}')
                cur.execute('DROP FUNCTION IF EXISTS mehdie_norm(text)')
                self.stdout.write(self.style.SUCCESS('Dropped index and function (extensions left in place).'))
                return

            for ext in ('pg_trgm', 'unaccent'):
                try:
                    cur.execute(f'CREATE EXTENSION IF NOT EXISTS {ext}')
                except Exception as e:
                    raise CommandError(
                        f'Could not create extension {ext}: {e}\n'
                        'Run as postgres: sudo -u postgres psql mehdie -c '
                        '"CREATE EXTENSION IF NOT EXISTS pg_trgm; CREATE EXTENSION IF NOT EXISTS unaccent;"\n'
                        'then run this command again.')

            cur.execute(FUNCTION_SQL)
            self.stdout.write('mehdie_norm() created.')

            # CONCURRENTLY: no write lock on place_name while building
            # (needs autocommit, which is Django's default outside atomic blocks)
            cur.execute(f'CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX} '
                        f'ON place_name USING gin (mehdie_norm(toponym) gin_trgm_ops)')
            cur.execute('ANALYZE place_name')

            cur.execute("SELECT mehdie_norm('Ḥalab'), mehdie_norm('יְרוּשָׁלַיִם'), mehdie_norm('إسْكَنْدَرِيَّة')")
            self.stdout.write('Self-test: %s | %s | %s' % cur.fetchone())
            cur.execute('SELECT count(*) FROM place_name')
            self.stdout.write(self.style.SUCCESS(f'Index {INDEX} ready over {cur.fetchone()[0]} names.'))
