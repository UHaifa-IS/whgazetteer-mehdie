# datasets.access
# Who may read the records of a dataset (FIXES O15).
#
# One rule for every page, API endpoint and download that serves a dataset's
# records, so it can be changed in one place:
#   - public and core datasets: everyone
#   - all other datasets: signed-in users only (the same rule as the dataset
#     browse page, which requires a login)
# To restrict private datasets to their owners, collaborators and the team,
# tighten can_view_dataset() below (see the commented lines).
from django.http import Http404


def can_view_dataset(user, dataset):
    if dataset is None:
        return False
    if dataset.public or getattr(dataset, 'core', False):
        return True
    if user is None or not user.is_authenticated:
        return False
    return True
    # stricter alternative:
    # return (user.is_superuser or dataset.owner_id == user.id
    #         or dataset.collaborators.filter(id=user.id).exists())


def require_dataset_access(request, dataset):
    """For API endpoints and downloads: 404 unless the visitor may read the dataset."""
    if not can_view_dataset(getattr(request, 'user', None), dataset):
        raise Http404('Not found.')


class DatasetAccessMixin:
    """For DetailViews of a Dataset, or of a record that has a .dataset (a Place):
    visitors who may not read the dataset are sent to the login page."""

    def get_access_dataset(self, obj):
        from datasets.models import Dataset
        return obj if isinstance(obj, Dataset) else obj.dataset

    def get(self, request, *args, **kwargs):
        dataset = self.get_access_dataset(self.get_object())
        if not can_view_dataset(request.user, dataset):
            if request.user.is_authenticated:
                raise Http404('Not found.')
            from django.contrib.auth.views import redirect_to_login
            return redirect_to_login(request.get_full_path())
        return super().get(request, *args, **kwargs)
