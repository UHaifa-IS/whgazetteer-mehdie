# api.views
import logging
import math
import re

from django.conf import settings
from django.db.models import Exists, OuterRef
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.models import User, Group
from django.contrib.gis.geos import Polygon, Point
# from django.contrib.postgres import search
from django.contrib.gis.measure import D
from django.contrib.gis.db.models.functions import Distance
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Count, Q
from django.http import JsonResponse, HttpResponse  # , FileResponse
from django.shortcuts import get_object_or_404
from django.views.generic import View
# from django.views.decorators.csrf import csrf_exempt
from django_filters.rest_framework import DjangoFilterBackend
from elasticsearch7 import Elasticsearch
from rest_framework import filters
from rest_framework import generics
from rest_framework import permissions
# from rest_framework import status
from rest_framework import viewsets
from rest_framework.exceptions import ParseError
from rest_framework.authentication import SessionAuthentication
from rest_framework.decorators import api_view
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.renderers import JSONRenderer
from rest_framework.reverse import reverse
from rest_framework.views import APIView
import simplejson as json
from accounts.permissions import IsOwnerOrReadOnly
from api.serializers import (UserSerializer, DatasetSerializer, PlaceSerializer, PlaceTableSerializer,
                             PlaceGeomSerializer, AreaSerializer, FeatureSerializer,
                             LPFSerializer)  # , SearchDatabaseSerializer)
from areas.models import Area
from collection.models import Collection
from datasets.access import can_view_dataset, require_dataset_access
from datasets.models import Dataset
from datasets.tasks import get_bounds_filter
from places.models import Place, PlaceGeom, PlaceLink, PlaceRelated
from search.name_search import rank_places, NameSearchUnavailable
from search.views import getGeomCollection

logger = logging.getLogger(__name__)


class StandardResultsSetPagination(PageNumberPagination):
    page_size = 10
    page_size_query_param = 'page_size'
    max_page_size = 20000


#
# External API
# 
#


#
# Query-parameter parsing for the public API. Each helper raises ParseError,
# which DRF turns into a 400 response with a JSON {"detail": ...} message.
#

def _range_text(lo, hi):
    if lo is not None and hi is not None:
        return f"between {lo} and {hi}"
    return f"at least {lo}" if lo is not None else f"at most {hi}"


def _raw_param(params, name, required):
    raw = params.get(name)
    if raw is None or raw.strip() == '':
        if required:
            raise ParseError(f"Missing required parameter '{name}'.")
        return None
    return raw.strip()


def _float_param(params, name, lo=None, hi=None, required=False):
    raw = _raw_param(params, name, required)
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError:
        raise ParseError(f"Parameter '{name}' must be a number, got {raw!r}.")
    if not math.isfinite(value) or (lo is not None and value < lo) or (hi is not None and value > hi):
        raise ParseError(f"Parameter '{name}' must be {_range_text(lo, hi)}, got {raw!r}.")
    return value


def _int_param(params, name, lo=None, hi=None, required=False):
    raw = _raw_param(params, name, required)
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        raise ParseError(f"Parameter '{name}' must be a whole number, got {raw!r}.")
    if (lo is not None and value < lo) or (hi is not None and value > hi):
        raise ParseError(f"Parameter '{name}' must be {_range_text(lo, hi)}, got {raw!r}.")
    return value


def _lonlat_param(params, name, required=False):
    """'lon,lat' -> (lon, lat), e.g. the sw/ne corners of a bbox query."""
    raw = _raw_param(params, name, required)
    if raw is None:
        return None
    parts = raw.split(',')
    try:
        if len(parts) != 2:
            raise ValueError
        lon, lat = float(parts[0]), float(parts[1])
    except ValueError:
        raise ParseError(f"Parameter '{name}' must be 'lon,lat', got {raw!r}.")
    if not (math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90):
        raise ParseError(f"Parameter '{name}' is out of range (lon -180..180, lat -90..90), got {raw!r}.")
    return lon, lat


def _fclasses_param(params):
    """Feature-class letters, comma separated or not ('P,S' or 'PS') -> ['P', 'S'].
    Read from 'fc', or from its alias 'fclass'."""
    raw = _raw_param(params, 'fc', required=False) or _raw_param(params, 'fclass', required=False)
    if raw is None:
        return None
    letters = {c.upper() for c in raw if c not in ', '}
    if not letters or not all(c.isalpha() and c.isascii() for c in letters):
        raise ParseError(f"Parameter 'fc' takes feature-class letters, e.g. 'P,S', got {raw!r}.")
    return sorted(letters)


def _pagesize_param(params, default=20, cap=200):
    value = _int_param(params, 'pagesize', lo=1)
    return default if value is None else min(value, cap)


class PrettyJsonRenderer(JSONRenderer):
    def get_indent(self, accepted_media_type, renderer_context):
        return 2


def _public_datasets():
    """The datasets the public API serves: those flagged public."""
    return Dataset.objects.filter(public=True)


"""
nearby and bbox spatial db queries
"""


class SpatialAPIView(generics.ListAPIView):
    renderer_classes = [JSONRenderer]
    filter_backends = [filters.SearchFilter]

    # search_fields = ['@title']

    def get(self, format=None, *args, **kwargs):
        params = self.request.query_params
        qtype = params.get('type', None)
        if qtype not in ('nearby', 'bbox'):
            raise ParseError("Spatial queries need ?type=nearby (with lon, lat and km) "
                             "or ?type=bbox (with sw and ne).")
        fclasses = _fclasses_param(params)
        ds = params.get('dataset', None)
        pagesize = _pagesize_param(params)
        err_note = None

        # uses PlaceGeom records and PlaceGeomSerializer
        if qtype == 'nearby':
            lon = _float_param(params, 'lon', lo=-180, hi=180, required=True)
            lat = _float_param(params, 'lat', lo=-90, hi=90, required=True)
            dist = _float_param(params, 'km', lo=0, required=True)
            pnt = Point(lon, lat, srid=4326)
            # PlaceGeom records, only points
            qs = PlaceGeom.objects.extra(
                where=["geometrytype(geom) LIKE 'POINT'"]). \
                annotate(distance=Distance('geom', pnt)). \
                filter(geom__distance_lte=(pnt, D(km=dist))).order_by('distance')
            # filter on params
            qs = qs.filter(place__dataset__public=True)
            qs = qs.filter(place__dataset=ds) if ds else qs
            qs = qs.filter(place__fclasses__overlap=fclasses) if fclasses else qs

        # uses Place records and LPFSerializer
        else:
            sw = _lonlat_param(params, 'sw', required=True)
            ne = _lonlat_param(params, 'ne', required=True)
            qs = Place.objects.filter(dataset__public=True, geoms__jsonb__type='Point')
            bbox = Polygon.from_bbox((sw[0], sw[1], ne[0], ne[1]))  # [xmin, ymin, xmax, ymax]
            placeids = PlaceGeom.objects.filter(geom__within=bbox).values_list('place_id')
            qs = qs.filter(id__in=placeids)
            # filter on params
            qs = qs.filter(dataset=ds) if ds else qs
            qs = qs.filter(fclasses__overlap=fclasses) if fclasses else qs

        filtered = qs[:pagesize]
        serial = LPFSerializer if qtype == 'bbox' else PlaceGeomSerializer
        serializer = serial(filtered, many=True, context={'request': self.request})
        serialized_data = serializer.data
        result = {
            "count": qs.count(),
            "pagesize": filtered.count(),
            "parameters": params,
            "errors": err_note,
            "type": "FeatureCollection",
            "@context": "https://raw.githubusercontent.com/LinkedPasts/linked-places/master/linkedplaces-context-v1.1.jsonld",
            "features": serialized_data
        }
        # print('place result',result)
        return JsonResponse(result, safe=False, json_dumps_params={'ensure_ascii': False, 'indent': 2})


"""
  makeGeom(); called by collectionItem()
  format index locations as geojson
"""


def makeGeom(geom):
    # print('geom',geom)
    # TODO: account for non-point
    if len(geom) > 1:
        geomobj = {"type": "GeometryCollection", "geometries": []}
        for g in geom:
            geomobj['geometries'].append(g['location'])
            # {"type":g['location']['type'],
            # "coordinates":g['location']['coordinates']
    elif len(geom) == 1:
        geomobj = geom[0]['location']
    else:
        geomobj = None
    return geomobj


"""
  collectionItem(); called by collector();
  formats api search hits 
"""


def collectionItem(i, datatype, format):
    # print('collectionItem i',i)
    _id = i['_id']
    if datatype == 'place':
        # serialize as geojson
        i = i['hit']
        item = {
            "type": "Feature",
            "properties": {
                "title": i['title'],
                "index_id": _id,
                "index_role": i['relation']['name'],
                "place_id": i['place_id'],
                "child_place_ids": [int(c) for c in i['children']],
                "dataset": i['dataset'],
                "placetypes": [t['label'] for t in i['types']],
                "variants": [n for n in i['suggest']['input'] if n != i['title']],
                'links': i['links'],
                "timespans": i['timespans'],
                "minmax": i['minmax'] if 'minmax' in i.keys() else [],
                "ccodes": i['ccodes']
            },
            "geometry": makeGeom(i['geoms'])
        }
        # print('place sug item', item)
    elif datatype == 'trace':
        hit = i['hit']
        pids = [b['place_id'] for b in hit['body'] if b['place_id']]
        q_geom = {"query": {"bool": {"must": [{"terms": {"place_id": pids}}]}}}
        print('q_geom', q_geom)
        geoms = getGeomCollection('whg', 'place', q_geom)
        if format == 'geojson':
            item = {
                "type": "FeatureCollection",
                "trace_id": 'http://whgazetteer.org/traces/' + i['_id'],
                "target_id": hit['target']['id'],
                "target_title": hit['target']['title'],
                "features": geoms}
        else:  # W3C anno format
            item = {
                "@context": ["http://www.w3.org/ns/anno.jsonld",
                             {"lpo:": "http://linkedpasts.org/ontology/lpo.jsonld"}],
                "_id": 'http://whgazetteer.org/traces/' + i['_id'],
                "type": "Annotation",
                "created": hit['created'],
                "creator": hit['creator'],
                "motivation": hit['motivation'],
                "keywords": hit['tags'],
                "target": {
                    "id": hit['target']['id'],
                    "type": hit['target']['type'],
                    "title": hit['target']['title'],
                    "depiction": hit['target']['depiction'] if 'depiction' in hit['target'].keys() else '',
                    "format": "text/html",
                    "language": "en"
                },
                "bodies": hit['body']
            }
    # print('place search item:',item)
    return item


"""
  collector(); called by IndexAPIView; 
  execute es.search, return results post-processed by suggestionItem()
"""


def collector(q, datatype, idx):
    # returns only parents
    # print('collector',doctype,q)
    es = Elasticsearch([{'host': 'localhost',
                         'port': 9200,
                         'api_key': (settings.ES_APIKEY_ID, settings.ES_APIKEY_KEY),
                         'timeout': 30,
                         'max_retries': 10,
                         'retry_on_timeout': True}])
    items = []

    if datatype == 'place':
        # print('collector/place q:',q)
        # TODO: trap errors
        res = es.search(index=idx, body=q)
        hits = res['hits']['hits']
        # print('collector()/place hits',hits)
        if len(hits) > 0:
            for h in hits:
                items.append(
                    {"_id": h['_id'],
                     "linkcount": len(h['_source']['links']),
                     "childcount": len(h['_source']['children']),
                     "hit": h['_source'],
                     }
                )
        sorteditems = sorted(items, key=lambda x: x['childcount'], reverse=True)
        # print('sorteditems from collector()',sorteditems)
        return sorteditems

    elif datatype == 'trace':
        print('collector()/trace q:', q)
        res = es.search(index='traces', doc_type='trace', body=q)
        hits = res['hits']['hits']
        # print('collector()/trace hits',hits)
        if len(hits) > 0:
            for h in hits:
                items.append({"_id": h['_id'], "hit": h['_source']})
        return items


"""
  bundler();  called by IndexAPIView, case api/index?whgid=
  execute es.search, return post-processed results 
"""


def bundler(q, whgid, idx):
    es = Elasticsearch([{'host': 'localhost',
                         'port': 9200,
                         'api_key': (settings.ES_APIKEY_ID, settings.ES_APIKEY_KEY),
                         # 'http_auth': ('elastic', settings.ES_REST_PWD), # this is superceded by api_key
                         'timeout': 30,
                         'max_retries': 10,
                         'retry_on_timeout': True}])
    res = es.search(index=idx, body=q)
    hits = res['hits']['hits']
    bundle = []
    if len(hits) > 0:
        for h in hits:
            bundle.append(
                {"_id": h['_id'],
                 "linkcount": len(h['_source']['links']),
                 "childcount": len(h['_source']['children']),
                 "hit": h['_source'],
                 }
            )
    # return bundle
    stuff = [collectionItem(i, 'place', None) for i in bundle]
    return stuff


""" 
  /api/traces?
  search trace target titles and tags
"""


class TracesAPIView(View):
    @staticmethod
    def get(request):
        params = request.GET
        print('TracesAPIView request params', params)
        qstr = params.get('q', None)
        format = params.get('format', None)

        """ 
          args q = query string
        """
        q = {
            "size": 100,
            "query": {"bool": {
                "must": [
                    {"multi_match": {
                        "query": qstr,
                        "fields": ["target.title", "tags"],
                        "type": "phrase_prefix"
                    }}]
            }}
        }

        # run query
        collection = collector(q, 'trace', 'traces')
        # format results
        collection = [collectionItem(item, 'trace', format) for item in collection]

        # result object
        result = {"trace_count": len(collection), "results": collection}

        return JsonResponse(result, safe=False, json_dumps_params={'ensure_ascii': False, 'indent': 2})


""" 
  /api/index?
  search place index (always whg) parent records
  based on search.views.SearchView(View)
"""


class IndexAPIView(View):
    # @staticmethod
    def get(self, request):
        params = request.GET
        print('IndexAPIView request params', params)
        """
          args in params: whgid, pid, name, name_startswith, fclass, dataset, ccode, year, area
    
        """
        whgid = request.GET.get('whgid')
        pid = request.GET.get('pid')
        name = request.GET.get('name')
        name_startswith = request.GET.get('name_startswith')
        fc = params.get('fclass', None)
        fclasses = [x.upper() for x in fc.split(',')] if fc else None
        dataset = request.GET.get('dataset')
        cc = request.GET.get('ccode')
        ccodes = [x.upper() for x in cc.split(',')] if cc else None
        year = request.GET.get('year')
        pagesize = params.get('pagesize', None)
        area = request.GET.get('area')
        idx = 'whg'

        if all(v is None for v in [name, name_startswith, whgid, pid]):
            # TODO: format better
            return HttpResponse(
                content='<h3>Query requires either name, name_startswith, pid, or whgid</h3>' + '<p><a href="/usingapi/">API instructions</a>')
        else:
            if whgid and whgid != '':
                print('fetching whg_id', whgid)
                q = {"query": {"bool": {"should": [
                    {"parent_id": {"type": "child", "id": whgid}},
                    {"match": {"_id": whgid}}
                ]}}}
                bundle = bundler(q, whgid, idx)
                # print('bundler', bundle)
                print('bundler q', q)
                # result=[b for b in bundle]
                result = {"index_id": whgid,
                          "note": str(len(bundle)) + " records asserted as skos:closeMatch",
                          "type": "FeatureCollection",
                          "features": [b for b in bundle]}
            else:
                q = {
                    "size": 100,
                    "query": {"bool": {
                        "must": [
                            {"exists": {"field": "whg_id"}},
                            {"multi_match": {
                                "query": name if name else name_startswith,
                                "fields": ["title", "names.toponym", "searchy"],
                                "type": "phrase" if name else "phrase_prefix"
                            }}]
                    }}
                }
                if fc:
                    q['query']['bool']['must'].append({"terms": {"fclasses": fclasses}})
                if dataset:
                    q['query']['bool']['must'].append({"match": {"dataset": dataset}})
                if ccodes:
                    q['query']['bool']['must'].append({"terms": {"ccodes": ccodes}})
                if year:
                    q['query']['bool']['must'].append({"term": {"timespans": {"value": year}}})
                # if area:
                # TODO:
                if area:
                    a = get_object_or_404(Area, pk=area)
                    bounds = {"id": [str(a.id)], "type": [a.type]}  # nec. b/c some are polygons, some are multipolygons
                    q['query']['bool']["filter"] = get_bounds_filter(bounds, 'whg')

                # print('the api query was:',q)

                # run query
                collection = collector(q, 'place', 'whg')
                # format hits
                collection = [collectionItem(s, 'place', None) for s in collection]

                # result object
                # result = {'type':'FeatureCollection','count': len(collection), 'features':collection}
                result = {'type': 'FeatureCollection',
                          'count': len(collection),
                          'pagesize': pagesize,
                          'features': collection[:int(pagesize)] if pagesize else collection}

        # to client
        return JsonResponse(result, safe=False, json_dumps_params={'ensure_ascii': False, 'indent': 2})


""" 
  /api/db?
  SearchAPIView()
  return lpf results from database search 
"""


class SearchAPIView(generics.ListAPIView):
    """GET /api/db/ : search the places of public datasets.

    name           ranked, accent-insensitive name match (same as the site's search)
    name_contains  substring of the place title
    id             a single place by its MEHDIE place id
    dataset, fc (alias fclass), year, ccode, pagesize: filters
    """
    renderer_classes = [JSONRenderer]

    def get(self, request, format=None, *args, **kwargs):
        params = request.query_params
        id_ = _int_param(params, 'id', lo=1)
        name = (params.get('name') or '').strip()
        name_contains = (params.get('name_contains') or '').strip()
        ds = (params.get('dataset') or '').strip()
        cc = [c.strip().upper() for c in params.get('ccode').split(',') if c.strip()] \
            if params.get('ccode') else None
        fclasses = _fclasses_param(params)
        year = _int_param(params, 'year', lo=-9999, hi=9999)
        pagesize = _pagesize_param(params)
        err_note = None

        if not (id_ or name or name_contains):
            raise ParseError('Needs a "name", "name_contains" or "id" parameter at minimum '
                             '(e.g. ?name=jerusalem, ?name_contains=forest or ?id=44942).')

        qs = Place.objects.filter(dataset__public=True)
        scores = None
        if id_:
            qs = qs.filter(id=id_)
            err_note = 'id given, other parameters ignored' if len(params) > 1 else None
        else:
            qs = qs.filter(dataset=ds) if ds else qs
            qs = qs.filter(ccodes__overlap=cc) if cc else qs
            qs = qs.filter(fclasses__overlap=fclasses) if fclasses else qs
            qs = qs.filter(minmax__0__lte=year, minmax__1__gte=year) if year is not None else qs
            if name_contains:
                qs = qs.filter(title__icontains=name_contains)
            else:
                try:
                    scores = dict(rank_places(name))
                    qs = qs.filter(id__in=list(scores))
                except NameSearchUnavailable as e:
                    # setup_name_search not run: fall back to a case-insensitive prefix match
                    logger.warning('name search unavailable, falling back to prefix match: %s', e)
                    qs = qs.filter(names__toponym__istartswith=name).distinct()

        if scores is not None:
            # best match first; at most MAX_CANDIDATES ids, so ranking in Python is cheap
            ids = sorted(qs.values_list('id', flat=True), key=lambda i: -scores[i])
            count = len(ids)
            page_ids = ids[:pagesize]
            places = {p.id: p for p in Place.objects.filter(id__in=page_ids).select_related('dataset')}
            filtered = [places[i] for i in page_ids if i in places]
        else:
            qs = qs.order_by('id')
            count = qs.count()
            filtered = list(qs.select_related('dataset')[:pagesize])

        features = LPFSerializer(filtered, many=True, context={'request': request}).data
        result = {"count": count,
                  "pagesize": len(filtered),
                  "parameters": params,
                  "note": err_note,
                  "type": "FeatureCollection",
                  "features": features}
        return JsonResponse(result, safe=False, json_dumps_params={'ensure_ascii': False, 'indent': 2})


""" *** """
""" TODO: the next two attempt the same and are WAY TOO SLOW """
""" 
    api/places/<str:dslabel>/[?q={string}]
    Paged list of places in dataset. 
"""


class PlaceAPIView(generics.ListAPIView):
    serializer_class = PlaceSerializer
    pagination_class = StandardResultsSetPagination

    def get_queryset(self, format=None, *args, **kwargs):
        print('kwargs', self.kwargs)
        print('self.request.GET', self.request.GET)
        dslabel = self.kwargs['dslabel']
        qs = Place.objects.all().filter(dataset=dslabel).order_by('title')
        query = self.request.GET.get('q')
        if query is not None:
            qs = qs.filter(title__icontains=query)
        return qs

    permission_classes = [permissions.IsAuthenticatedOrReadOnly, IsOwnerOrReadOnly]


""" 
    api/dataset/<str:dslabel>/lpf/
    all places in a dataset, for download
"""


class DownloadDatasetAPIView(generics.ListAPIView):
    """  Dataset as LPF FeatureCollection  """

    # serializer_class = PlaceSerializer
    # pagination_class = StandardResultsSetPagination

    def get(self, format=None):
        print('self.request.GET', self.request.GET)
        dslabel = self.request.GET.get('dataset')
        ds = get_object_or_404(Dataset, label=dslabel)
        features = []
        qs = ds.places.all()
        for p in qs:
            rec = {"type": "Feature",
                   "properties": {"id": p.id, "src_id": p.src_id, "title": p.title, "ccodes": p.ccodes},
                   "geometry": {"type": "GeometryCollection",
                                "features": [g.jsonb for g in p.geoms.all()]},
                   "names": [n.jsonb for n in p.names.all()],
                   "types": [t.jsonb for t in p.types.all()],
                   "links": [l.jsonb for l in p.links.all()],
                   "whens": [w.jsonb for w in p.whens.all()],
                   }
            # print('rec',rec)
            features.append(rec)

        result = {"type": "FeatureCollection", "features": features}
        print('result', result)
        return JsonResponse(result, safe=False, json_dumps_params={'ensure_ascii': False, 'indent': 2})

    # permission_classes = [permissions.IsAuthenticatedOrReadOnly,IsOwnerOrReadOnly]


"""
  /api/datasets? > query public datasets by id, label, term
"""


def _dataset_record(ds):
    """Public metadata of a dataset; `ds` must be annotated with place_count."""
    return {
        'id': ds.id,
        'label': ds.label,
        'title': ds.title,
        'description': ds.description,
        'datatype': ds.datatype,
        'place_count': ds.place_count,
        'creator': ds.creator,
        'contributors': ds.contributors,
        'source': ds.source,
        'citation': ds.citation,
        'license': ds.license,
        'webpage': ds.webpage,
        'owner': ds.owner.username,
        'page': f'/datasets/{ds.id}',
        'places': reverse('ds-places', kwargs={'label': ds.label}),
    }


class DatasetAPIView(APIView):
    """GET /api/datasets/ : the public datasets, optionally filtered
    by id, label, q (in title or description) or datatype (place|people)."""
    renderer_classes = [PrettyJsonRenderer]

    def get(self, request, format=None):
        params = request.query_params
        id_ = _int_param(params, 'id', lo=1)
        label = (params.get('label') or '').strip()
        query = (params.get('q') or '').strip()
        datatype = (params.get('datatype') or '').strip().lower()
        if datatype and datatype not in ('place', 'people'):
            raise ParseError(f"Parameter 'datatype' must be 'place' or 'people', got {datatype!r}.")

        qs = _public_datasets().select_related('owner') \
            .annotate(place_count=Count('places')).order_by('label')
        qs = qs.filter(id=id_) if id_ else qs
        qs = qs.filter(label=label) if label else qs
        qs = qs.filter(Q(title__icontains=query) | Q(description__icontains=query)) if query else qs
        qs = qs.filter(datatype=datatype) if datatype else qs

        results = [_dataset_record(ds) for ds in qs]
        return Response({'count': len(results), 'results': results})


class DatasetDetailAPIView(APIView):
    """GET /api/datasets/<label>/ : metadata of one public dataset."""
    renderer_classes = [PrettyJsonRenderer]

    def get(self, request, label, format=None):
        qs = _public_datasets().select_related('owner').annotate(place_count=Count('places'))
        return Response(_dataset_record(get_object_or_404(qs, label=label)))


class DatasetPlacesAPIView(APIView):
    """GET /api/datasets/<label>/places/ : id, src_id and title of every place
    in one public dataset, ordered by id. Full records: /api/place/<id>/."""
    renderer_classes = [JSONRenderer]

    def get(self, request, label, format=None):
        ds = get_object_or_404(_public_datasets(), label=label)
        places = list(Place.objects.filter(dataset=ds.label).order_by('id')
                      .values('id', 'src_id', 'title'))
        return Response({'dataset': ds.label, 'count': len(places), 'places': places})


"""
  /api/area_features
"""


# geojson feature for api
class AreaFeaturesView(generics.ListAPIView):
    # @staticmethod

    def get(self, format=None, *args, **kwargs):
        params = self.request.query_params
        user = self.request.user
        print('params', params)
        print('api/areas request', self.request)

        id_ = params.get('id', None)
        query = params.get('q', None)
        filter = params.get('filter', None)

        areas = []
        qs = Area.objects.all().filter((Q(type='predefined'))).values('id', 'title', 'type', 'description', 'geojson')

        # filter for parameters
        if id_:
            qs = qs.filter(id=id_)
        if query:
            qs = qs.filter(title__icontains=query)
        if filter and filter == 'un':
            qs = qs.filter(description="UN Statistical Division Sub-Region")

        for a in qs:
            feat = {
                "type": "Feature",
                "properties": {"id": a['id'], "title": a['title'], "type": a['type'], "description": a['description']},
                "geometry": a['geojson']
            }
            areas.append(feat)

        return JsonResponse(areas, safe=False)


class UserList(generics.ListAPIView):
    queryset = User.objects.all()
    serializer_class = UserSerializer


class UserDetail(generics.RetrieveAPIView):
    queryset = User.objects.all()
    serializer_class = UserSerializer


"""
  API 'home page' (not implemented)
"""


@api_view(['GET'])
def api_root(request, format=None):
    return Response({
        # 'datasets': reverse('dataset-list', request=request, format=format)
        'datasets': reverse('api:ds-list', request=request, format=format)
    })


#

# IN USE May 2020

#
"""
    place/<int:pk>/
    uses: ds_browse.html; place_collection_browse.html
    "published record by place_id"
"""


class PlaceDetailAPIView(generics.RetrieveAPIView):
    """  returns single serialized database place record by id  """
    queryset = Place.objects.all()
    serializer_class = PlaceSerializer
    renderer_classes = [PrettyJsonRenderer]

    permission_classes = [permissions.IsAuthenticatedOrReadOnly, IsOwnerOrReadOnly]
    authentication_classes = [SessionAuthentication]

    def get_object(self):
        place = super().get_object()
        require_dataset_access(self.request, place.dataset)  # FIXES O15
        return place

    def get(self, request, *args, **kwargs):
        response = super().get(request, *args, **kwargs)  # Get the original response
        place_title = response.data['title']
        this_place_id = response.data['id']
        this_place = Place.objects.get(id=this_place_id)
        response.data['name_parts'] = this_place.name_parts
        response.data['datatype'] = this_place.dataset.datatype
        this_dataset_id = this_place.dataset.id

        # Initialize nodes with the title of the current place
        nodes = [place_title]

        # Initialize urls with dataset:place_id of the current place
        urls = [f"{this_dataset_id}:{this_place_id}"]

        # Initialize an empty list for edges
        edges = []

        def get_related_places(source_place_id):
            related_info = []
            try:
                # Find the dataset label of the current place
                this_dataset_label = this_place.dataset.label

                # Retrieve related PlaceRelated objects
                related_objects = PlaceRelated.objects.filter(place=this_place)

                # Extract label and relationType from each related object's jsonb field
                for related_record in related_objects:
                    jsonb_data = related_record.jsonb
                    label = jsonb_data.get('label') if jsonb_data else None
                    relation_type = jsonb_data.get('relationType') if jsonb_data else None

                    # Look up Place by title using the label
                    try:
                        related_place = Place.objects.filter(title=label, dataset__label=this_dataset_label).first()
                        if related_place is None:
                            logging.warning(f"Could not find related place with title {label} "
                                            f"in dataset {this_dataset_label}")
                            continue

                        # Only add to related_info if related Place is found
                        related_info.append({
                            'title': related_place.title,
                            'id': related_place.id,
                            'relationType': relation_type
                        })
                    except Place.DoesNotExist:
                        # If no matching Place is found, do not add to related_info
                        pass

            except Place.DoesNotExist:
                # If no matching Place is found, do not add to related_info
                pass

            return related_info

        def process_link(from_node, link_type, link_identifier, reverse=False):
            # records of datasets the visitor may not read are left out of the graph (FIXES O15)
            if isinstance(link_identifier, str) and re.match(r'^\d+:\d+$', link_identifier):
                linked_ds = Dataset.objects.filter(id=link_identifier.split(':')[0]).first()
                if linked_ds is not None and not can_view_dataset(request.user, linked_ds):
                    return None
            # Check if link type is not 'closeMatch' and identifier has the correct format
            if link_type != "closeMatch":
                # check if the link_identifier is a string
                if not isinstance(link_identifier, str):
                    logging.warning(f"Link identifier {link_identifier} from node {from_node} is not a string")
                    node_lbl = link_identifier
                else:
                    if ':' in link_identifier:
                        try:
                            dataset_id, place_id = link_identifier.split(':')
                            # Fetching labels from Dataset and Place models
                            dataset_label = Dataset.objects.get(id=dataset_id).label
                            place_label = Place.objects.get(id=place_id).title
                            node_lbl = f"{dataset_label}:{place_label}"
                        except (ValueError, ObjectDoesNotExist):
                            # If parsing fails or objects don't exist, use the original identifier
                            logging.warning(f"Could not parse link identifier {link_identifier}")
                            node_lbl = link_identifier
            else:
                node_lbl = link_identifier

            # Ensure the node is unique
            if node_lbl not in nodes:
                nodes.append(node_lbl)
                urls.append(link_identifier)
                # Add an edge from the current place to the linked place
                if not reverse:
                    edges.append({
                        "from": from_node,
                        "relation": link_type,
                        "to": node_lbl
                    })
                else:
                    edges.append({
                        "from": node_lbl,
                        "relation": link_type,
                        "to": from_node
                    })

            return node_lbl

        # Iterate over the links and populate nodes and edges
        for link in response.data.get('links', []):
            link_type = link.get('type')
            if link_type == 'different':
                continue

            link_identifier = link.get('identifier')
            node_label = process_link(place_title, link_type, link_identifier)
            if node_label is None:
                continue

            # If this link leads to a place, retrieve and process its links (second-order)
            if link_type != "closeMatch" and ':' in link_identifier:  # Assuming this indicates a place
                dataset_id, place_id = link_identifier.split(':')
                try:
                    linked_place_links = PlaceLink.objects.filter(place_id=place_id)
                    for place_link in linked_place_links:
                        second_order_link_data = place_link.jsonb
                        second_order_type = second_order_link_data.get('type')
                        second_order_identifier: str = second_order_link_data.get('identifier')
                        if not second_order_identifier.endswith(str(this_place_id)) and second_order_type != 'different':
                            process_link(node_label, second_order_type, second_order_identifier)
                except Place.DoesNotExist:
                    # Handle cases where the place does not exist
                    pass

        # Iterate over reverse links and populate nodes and edges
        # Concatenate this_dataset_id and this_place_id with a colon separator
        exact_pattern = f"{this_dataset_id}:{this_place_id}"

        # Perform an exact match search
        reverse_links = PlaceLink.objects.filter(jsonb__identifier__exact=exact_pattern)

        for reverse_link in reverse_links:
            reverse_link_data = reverse_link.jsonb
            reverse_link_type = reverse_link_data.get('type')
            if reverse_link_type == 'different':
                continue

            reverse_place = reverse_link.place
            reverse_link_identifier = f"{reverse_place.dataset.id}:{reverse_place.id}"
            process_link(place_title, reverse_link_type, reverse_link_identifier, reverse=True)

        # Add parent links
        related = get_related_places(this_place_id)
        if len(related) > 0:
            place = Place.objects.get(id=this_place_id)
            dataset_id = place.dataset.id
            for r in related:
                process_link(place_title, r['relationType'], f"{dataset_id}:{r['id']}")

        # Create graph data
        graph_data = {
            "nodes": nodes,
            "edges": edges,
            "urls": urls
        }

        # Append the graph data to the response
        response.data['graph'] = graph_data
        return response


"""
    place/<str:dslabel>/<str:src_id>/
    published record by dataset label and src_id
"""


class PlaceDetailSourceAPIView(generics.RetrieveAPIView):
    """  single database place record by src_id  """
    queryset = Place.objects.all()
    serializer_class = PlaceSerializer
    renderer_classes = [PrettyJsonRenderer]

    lookup_field = 'src_id'
    permission_classes = [permissions.IsAuthenticatedOrReadOnly, IsOwnerOrReadOnly]
    authentication_classes = [SessionAuthentication]

    def get_queryset(self):
        ds = get_object_or_404(Dataset, label=self.kwargs.get('dslabel'))
        require_dataset_access(self.request, ds)  # FIXES O15
        return Place.objects.filter(dataset=ds.label)


""" 
    /api/geoms?ds={{ ds.label }}} 
    /api/geoms?coll={{ coll.id }}} 
    in ds_browse and ds_places for all geoms if < 15k
    TODO: this needs refactor (make collection.geometries @property?)
"""


class GeomViewSet(viewsets.ModelViewSet):
    queryset = PlaceGeom.objects.all()
    serializer_class = PlaceGeomSerializer
    # pagination_class = None
    permission_classes = (permissions.IsAuthenticatedOrReadOnly,)

    def get_queryset(self):
        # PlaceGeom objects do not have dataset id or label :^(
        if 'ds' in self.request.GET:
            dslabel = self.request.GET.get('ds')
            require_dataset_access(self.request, get_object_or_404(Dataset, label=dslabel))  # FIXES O15
            dsPlaceIds = Place.objects.values('id').filter(dataset=dslabel)
            qs = PlaceGeom.objects.filter(place_id__in=dsPlaceIds)
        elif 'coll' in self.request.GET:
            cid = self.request.GET.get('coll')
            coll = get_object_or_404(Collection, id=cid)
            collPlaceIds = [p.id for p in coll.places.all()]
            # leaves out polygons and linestrings
            qs = PlaceGeom.objects.filter(
                place_id__in=collPlaceIds,
                jsonb__type__icontains='Point')
        return qs


""" 
    /api/geojson/{{ ds.id }}
"""


# class GeoJSONViewSet(viewsets.ModelViewSet):
class GeoJSONAPIView(generics.ListAPIView):
    # use: api/geojson
    # queryset = PlaceGeom.objects.all()
    # serializer_class = GeoJsonSerializer
    serializer_class = FeatureSerializer
    permission_classes = (permissions.IsAuthenticatedOrReadOnly,)

    def get_queryset(self, format=None, *args, **kwargs):
        if 'id' in self.request.GET:
            dsid = self.request.GET.get('id')
            ds = get_object_or_404(Dataset, pk=dsid)
            require_dataset_access(self.request, ds)  # FIXES O15
            dslabel = ds.label
            dsPlaceIds = Place.objects.values('id').filter(dataset=dslabel)
            qs = PlaceGeom.objects.filter(place_id__in=dsPlaceIds)
        elif 'coll' in self.request.GET:
            cid = self.request.GET.get('coll')
            coll = get_object_or_404(Collection, id=cid)
            collPlaceIds = [p.id for p in coll.places.all()]
            qs = PlaceGeom.objects.filter(place_id__in=collPlaceIds, jsonb__type='Point')
        return qs


"""
    populates drf table in ds_browse.html
"""


class PlaceTableViewSet(viewsets.ModelViewSet):
    # queryset = Place.objects.all()
    serializer_class = PlaceTableSerializer
    permission_classes = (permissions.IsAuthenticatedOrReadOnly)

    """
      q: query string
      ds: dataset
    """

    def get_queryset(self):
        ds = get_object_or_404(Dataset, label=self.request.GET.get('ds'))
        require_dataset_access(self.request, ds)  # FIXES O15
        # qs = ds.places.all().order_by('place_id')
        qs = ds.places.all().order_by('id')
        # qs = ds.places.all()
        query = self.request.GET.get('q')
        if query is not None:
            qs = qs.filter(title__istartswith=query)

        # Apply 'Matched' or 'Unmatched' filter based on 'filter' parameter
        filter_val = self.request.GET.get('filter')
        if filter_val == '1':  # 'Matched' places
            # Filter for places with at least one corresponding record in PlaceLink
            qs = qs.annotate(has_link=Exists(PlaceLink.objects.filter(place_id=OuterRef('id')))).filter(
                has_link=True)
        elif filter_val == '0':  # 'Unmatched' places
            # Filter for places with no corresponding records in PlaceLink
            qs = qs.annotate(has_link=Exists(PlaceLink.objects.filter(place_id=OuterRef('id')))).filter(
                has_link=False)

        return qs

    def get_permissions(self):
        """
        Instantiates and returns the list of permissions that this view requires.
        """
        if self.action in ['list', 'retrieve']:
            print(self.action)
            permission_classes = [permissions.AllowAny]
        else:
            permission_classes = [permissions.IsAdminUser]
        return [permission() for permission in permission_classes]


"""
    populates drf table in collection.collection_places.html
"""


class PlaceTableCollViewSet(viewsets.ModelViewSet):
    # queryset = Place.objects.all()
    serializer_class = PlaceTableSerializer
    permission_classes = (permissions.IsAuthenticatedOrReadOnly)

    """
      q: query string
      coll: collection
    """

    def get_queryset(self):
        coll = get_object_or_404(Collection, id=self.request.GET.get('id'))
        qs = coll.places_all.order_by('title')
        query = self.request.GET.get('q')
        # print('queryset', qs)
        if query is not None:
            qs = qs.filter(title__istartswith=query)
        return qs

    def get_permissions(self):
        """
        Instantiates and returns the list of permissions that this view requires.
        """
        if self.action in ['list', 'retrieve']:
            print(self.action)
            permission_classes = [permissions.AllowAny]
        else:
            permission_classes = [permissions.IsAdminUser]
        return [permission() for permission in permission_classes]


"""
  areas/

"""


# simple objects for dropdown
class AreaListView(View):
    @staticmethod
    def get(request):
        print('area_list() request.user', request.user, type(request.user))
        print('area_list() request.user', str(request.user))
        userstr = str(request.user)
        if userstr == 'AnonymousUser':
            qs = Area.objects.all().filter(Q(type__in=('predefined', 'country'))).values('id', 'title', 'type')
        else:
            user = request.user
            qs = Area.objects.all().filter(Q(type__in=('predefined', 'country')) | Q(owner=user)).values('id', 'title',
                                                                                                         'type')
        area_list = []
        for a in qs:
            area = {"id": a['id'], "title": a['title'], "type": a['type']}
            area_list.append(area)

        return JsonResponse(area_list, safe=False)


"""
  areas/

"""


# simple objects for dropdown
class AreaListAllView(View):
    @staticmethod
    def get(request):
        area_list = []
        qs = Area.objects.all().filter(
            Q(type__in=('predefined', 'country')) | Q(owner=request.user)
        ).values('id', 'title', 'type')
        for a in qs:
            area = {"id": a['id'], "title": a['title'], "type": a['type']}
            area_list.append(area)
        return JsonResponse(area_list, safe=False)


"""
    area/<int:pk>/
    in dataset.html#addtask
"""


class AreaViewSet(viewsets.ModelViewSet):
    queryset = Area.objects.all().order_by('title')
    serializer_class = AreaSerializer


"""
    regions/
    in dataset.html#addtask
"""


class RegionViewSet(View):
    queryset = Area.objects.filter(
        description='UN Statistical Division Sub-Region').order_by('title')
    serializer_class = AreaSerializer
