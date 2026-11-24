# search.name_search
"""
Ranked, fuzzy, accent-insensitive name matching over place_name.toponym,
in Postgres (pg_trgm + unaccent). Replaces the WHG Elasticsearch index for
the database search. Set up once with: python manage.py setup_name_search
"""
import logging

from django.db import connection, transaction

logger = logging.getLogger(__name__)

# pg_trgm similarity needed to count as a fuzzy match (0..1)
SIMILARITY_THRESHOLD = 0.4
# most candidate places considered before the page's other filters are applied
MAX_CANDIDATES = 5000

# Score per place = best name: exact 3, prefix 2, substring 1, plus trigram
# similarity (0..1) to order within each tier and rank the fuzzy-only matches.
# The normalised query is passed in as a literal so the planner can use the
# trigram index (place_name_toponym_norm_trgm) for both LIKE and %.
RANK_SQL = r"""
SELECT n.place_id,
       max(CASE WHEN mehdie_norm(n.toponym) = %(q)s THEN 3
                WHEN mehdie_norm(n.toponym) LIKE %(prefix)s THEN 2
                WHEN mehdie_norm(n.toponym) LIKE %(infix)s THEN 1
                ELSE 0 END
           + similarity(mehdie_norm(n.toponym), %(q)s)) AS score
FROM place_name n
WHERE mehdie_norm(n.toponym) LIKE %(where_like)s
   OR mehdie_norm(n.toponym) %% %(q)s
GROUP BY n.place_id
ORDER BY score DESC
LIMIT %(limit)s
"""


def _like_escape(s):
    return s.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')


class NameSearchUnavailable(Exception):
    """mehdie_norm() / pg_trgm missing: run `manage.py setup_name_search`."""


def rank_places(query, limit=MAX_CANDIDATES):
    """Return [(place_id, score), ...] best first for a free-text name query."""
    query = (query or '').strip()
    if not query:
        return []
    try:
        with transaction.atomic(), connection.cursor() as cur:
            cur.execute('SELECT mehdie_norm(%s)', [query])
            q = cur.fetchone()[0] or ''
            if not q:
                return []
            esc = _like_escape(q)
            # substring matching needs 3+ characters to use the trigram index;
            # shorter queries match on prefix (plus fuzzy) only
            params = {'q': q, 'prefix': esc + '%', 'infix': '%' + esc + '%',
                      'where_like': ('%' + esc + '%') if len(q) >= 3 else (esc + '%'),
                      'limit': limit}
            cur.execute('SET LOCAL pg_trgm.similarity_threshold = %s', [SIMILARITY_THRESHOLD])
            cur.execute(RANK_SQL, params)
            return cur.fetchall()
    except Exception as e:  # undefined function / extension, most likely
        if 'mehdie_norm' in str(e) or 'similarity' in str(e) or 'pg_trgm' in str(e):
            raise NameSearchUnavailable(str(e))
        raise
