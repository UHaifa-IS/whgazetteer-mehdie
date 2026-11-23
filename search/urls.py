# search/urls.py
from django.urls import path #, include

from search.views import (
  SearchView, SearchPageView, FeatureContextView, TraceGeomView,
  SearchDatabaseView, CollectionGeomView
)

from main.search_index import index_retired

#app_name = "search"

urlpatterns = [
  path('', SearchPageView.as_view(), name='search-page'), # former home page
  # ES-backed endpoints retired (MEHDIE has no Elasticsearch); names kept for reverse()
  path('index/', index_retired, name='search'), # executes index search
  path('db/', SearchDatabaseView.as_view(), name='search-db'), # executes database search
  path('context/', index_retired, name='feature_context'), # place portal context
  path('tracegeom/', index_retired, name='trace_geom'), # trace features <- search & place portal
  path('collgeom/', index_retired, name='collection_geom'), # collection features <- search & place portal

]

