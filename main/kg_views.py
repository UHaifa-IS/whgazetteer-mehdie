# main.kg_views
# Resolve the knowledge graph's identifiers to pages of this site.
#
# The RDF graph (mehdie-knowledge-graph) names things
#   https://tools.mehdie.org/place/<places.id>
#   https://tools.mehdie.org/person/<places.id>
#   https://tools.mehdie.org/dataset/<datasets.id>
#   https://tools.mehdie.org/place-name/<place_name.id>[/<n>]
#   https://tools.mehdie.org/geometry/<place_geom.id>
# plus sub-resources such as place/<id>/appellation/primary and
# place/<id>/timespan. None of these were pages; this view sends each of them
# (303 See Other) to the page about the record: the place detail page or the
# public dataset page.
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse

from datasets.models import Dataset
from places.models import Place, PlaceName, PlaceGeom


def _see_other(url):
    response = redirect(url)
    response.status_code = 303
    return response


def _visible(request, dataset):
    # records of non-public datasets resolve only for signed-in users
    return bool(dataset.public) or request.user.is_authenticated


def kg_entity_redirect(request, kind, id, rest=None):
    if kind == 'dataset':
        dataset = get_object_or_404(Dataset, id=id)
        if not _visible(request, dataset):
            raise Http404
        return _see_other(reverse('datasets:ds_meta', args=[dataset.id]))

    if kind == 'place-name':
        place = get_object_or_404(PlaceName, id=id).place
    elif kind == 'geometry':
        place = get_object_or_404(PlaceGeom, id=id).place
    else:  # place, person
        place = get_object_or_404(Place, id=id)

    if not _visible(request, place.dataset):
        raise Http404
    return _see_other(reverse('places:place-detail', args=[place.id]))
