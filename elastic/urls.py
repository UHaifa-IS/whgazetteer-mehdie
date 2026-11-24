# elastic.urls

from django.urls import path, include
from django.conf.urls import url
from django.conf.urls.static import static
from django.conf import settings
from django.views.generic.base import TemplateView

# from . import views
from django.contrib.auth.decorators import user_passes_test
from elastic.es_utils import fetch, alt_parents, removeDatasetFromIndex


# index maintenance is for the site team only (these routes were open to everyone)
def _is_team(user):
    return user.is_authenticated and (
        user.is_superuser or user.is_staff
        or user.groups.filter(name__in=['whg_team', 'admins']).exists())


team_only = user_passes_test(_is_team)
#
app_name='elastic'
urlpatterns = [

    # page for performing some index grooming
    path('admin/', team_only(TemplateView.as_view(template_name='elastic/index_admin.html')), name='index-admin'),

    # given pid, gets db and index records
    path('fetch/', team_only(fetch), name='place-fetch'),

    path('alt_parents/', team_only(alt_parents), name='place-alt-parents'),

    # remove all traces of dataset from whg index
    path('remove_dataset/<int:dsid>', team_only(removeDatasetFromIndex), name='remove-dataset'),

]
if settings.DEBUG is True:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
