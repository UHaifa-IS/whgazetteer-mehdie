# main.search_index
"""
MEHDIE does not run Elasticsearch. The WHG "union index" features inherited
from upstream (index search, place portal, /api/index/, WHG accessioning,
local Wikidata/TGN reconciliation) are retired.

- index_retired: view that the retired routes point to (HTTP 410 Gone).
- SearchIndexUnavailableMiddleware: safety net for any remaining code path
  that still calls Elasticsearch. It turns the connection error into a 503
  and logs a warning, so admins do not get an error email for every hit.
"""
import logging

from django.http import HttpResponse, JsonResponse
from elasticsearch7.exceptions import ConnectionError as ESConnectionError
from elasticsearch7.exceptions import NotFoundError as ESNotFoundError

logger = logging.getLogger(__name__)

RETIRED_MSG = ("This feature relied on the World Historical Gazetteer search index, "
               "which MEHDIE does not use. Please use the database search at /search/.")


def _wants_json(request):
    return (request.path.startswith('/api/')
            or request.headers.get('x-requested-with') == 'XMLHttpRequest'
            or 'application/json' in request.headers.get('accept', ''))


def index_retired(request, *args, **kwargs):
    if _wants_json(request):
        return JsonResponse({"error": "gone", "detail": RETIRED_MSG}, status=410)
    return HttpResponse(RETIRED_MSG, status=410, content_type='text/plain; charset=utf-8')


class SearchIndexUnavailableMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_exception(self, request, exception):
        if not isinstance(exception, (ESConnectionError, ESNotFoundError)):
            return None
        logger.warning("Elasticsearch call on %s %s: %s",
                       request.method, request.get_full_path(), exception)
        msg = "The search index is not available on this site."
        if _wants_json(request):
            return JsonResponse({"error": "unavailable", "detail": msg}, status=503)
        return HttpResponse(msg, status=503, content_type='text/plain; charset=utf-8')
