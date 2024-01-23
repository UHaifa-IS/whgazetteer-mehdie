from django.core.management.base import BaseCommand, CommandError
from django.db.models import F, Value
from django.db.models.functions import Concat

from datasets.models import Dataset
from places.models import Place, PlaceLink


class Command(BaseCommand):
    help = "We update incorrect identifiers for place links"

    def handle(self, *args, **options):
        links = PlaceLink.objects.select_related("place__dataset").filter(jsonb__type='same_as')
        for link in links:
            link.jsonb = {
                'type': 'same_as',
                'identifier': f"{link.place.dataset.id}:{link.place.id}"
            }
            link.save()
