# main.views
import urllib

import rdflib
from django.conf import settings
from django.core.mail import send_mail, BadHeaderError
from django.http import HttpResponse
from django.shortcuts import render, get_object_or_404, redirect  # , render_to_response
from django.urls import reverse_lazy
from django.views.generic.base import TemplateView
from collection.models import Collection
from datasets.models import Dataset
from datasets.tasks import testAdd
from places.models import Place
from bootstrap_modal_forms.generic import BSModalCreateView

from .forms import CommentModalForm, ContactForm
from elasticsearch import Elasticsearch

es = Elasticsearch([{'host': '0.0.0.0', 'port': 9200}])
from random import shuffle


# import requests

def custom_error_view(request, exception=None):
    print('error request', request.GET.__dict__)
    return render(request, "main/500.html", {'error': 'fubar'})


# experiment with MapLibre
class LibreView(TemplateView):
    template_name = 'datasets/libre.html'

    def get_context_data(self, *args, **kwargs):
        context = super(LibreView, self).get_context_data(*args, **kwargs)
        context['mbtokenkg'] = settings.MAPBOX_TOKEN_KG
        context['mbtokenmb'] = settings.MAPBOX_TOKEN_MB
        context['mbtokenwhg'] = settings.MAPBOX_TOKEN_WHG
        context['media_url'] = settings.MEDIA_URL
        return context


class Home2b(TemplateView):
    template_name = 'main/home_v2b.html'

    def get_context_data(self, *args, **kwargs):
        context = super(Home2b, self).get_context_data(*args, **kwargs)

        # deliver featured datasets and collections
        f_collections = Collection.objects.exclude(featured__isnull=True)
        f_datasets = list(Dataset.objects.exclude(featured__isnull=True))
        shuffle(f_datasets)

        # 2 collections, rotate datasets randomly
        context['featured_coll'] = f_collections.order_by('featured')[:2]
        context['featured_ds'] = f_datasets
        context['mbtokenkg'] = settings.MAPBOX_TOKEN_KG
        context['mbtokenmb'] = settings.MAPBOX_TOKEN_MB
        context['mbtokenwhg'] = settings.MAPBOX_TOKEN_WHG
        context['media_url'] = settings.MEDIA_URL
        context['base_dir'] = settings.BASE_DIR
        context['beta_or_better'] = True if self.request.user.groups.filter(
            name__in=['beta', 'admins']).exists() else False
        context['teacher'] = True if self.request.user.groups.filter(
            name__in=['teacher']).exists() else False

        return context


def statusView(request):
    context = {"status_site": "??",
               "status_database": "??",
               "status_index": "??"}

    # database
    try:
        place = get_object_or_404(Place, id=81011)
        context["status_database"] = "up" if place.title == 'Abydos' else 'error'
    except:
        context["status_database"] = "down"

    # whg index
    try:
        q = {"query": {"bool": {"must": [{"match": {"place_id": "81011"}}]}}}
        res1 = es.search(index="whg", body=q)
        context["status_index"] = "up" if (
                res1['hits']['total'] == 1 and res1['hits']['hits'][0]['_source']['title'] == 'Abydos') else "error"
    except:
        context["status_index"] = "down"

    # celery recon task
    try:
        result = testAdd.delay(8, 8)
        context["status_tasks"] = "up" if result.get() == 16 else 'error'
    except:
        context["status_tasks"] = "down"

    return render(request, "main/status.html", {"context": context})


def contactView(request):
    print('contact request.GET', request.GET)
    sending_url = request.GET.get('from')
    if request.method == 'GET':
        form = ContactForm()
    else:
        form = ContactForm(request.POST)
        if form.is_valid():
            human = True
            name = form.cleaned_data['name']
            username = form.cleaned_data['username']  # hidden input
            subject = form.cleaned_data['subject']
            from_email = form.cleaned_data['from_email']
            message = name + ' (' + username + '; ' + from_email + '), on the subject of ' + subject + ' says: \n\n' + \
                      form.cleaned_data['message']
            subject_reply = "WHG message received"
            message_reply = '\nWe received your message concerning "' + subject + '" and will respond soon.\n\n regards,\nThe WHG project team'
            try:
                send_mail(subject, message, from_email, ["mehdie.org@gmail.com"])
                send_mail(subject_reply, message_reply, 'mehdie.org@gmail.com', [from_email])
            except BadHeaderError:
                return HttpResponse('Invalid header found.')
            return redirect('/success?return=' + sending_url if sending_url else '/')
            # return redirect(sending_url)
        else:
            print('not valid, why?')

    return render(request, "main/contact.html", {'form': form, 'user': request.user})


def contactSuccessView(request, *args, **kwargs):
    returnurl = request.GET.get('return')
    return HttpResponse(
        '<div style="font-family:sans-serif;margin-top:3rem; width:50%; margin-left:auto; margin-right:auto;"><h4>Thank you for your message! We will reply soon.</h4><p><a href="' + returnurl + '">Return</a><p></div>')


class CommentCreateView(BSModalCreateView):
    template_name = 'main/create_comment.html'
    form_class = CommentModalForm
    success_message = 'Success: Comment was created.'
    success_url = reverse_lazy('')

    def form_valid(self, form, **kwargs):
        form.instance.user = self.request.user
        place = get_object_or_404(Place, id=self.kwargs['rec_id'])
        form.instance.place_id = place
        return super().form_valid(form)

    def get_context_data(self, *args, **kwargs):
        context = super().get_context_data(**kwargs)
        context['place_id'] = self.kwargs['rec_id']
        context['user'] = self.request.user
        return context

    # ** ADDED for referrer redirect
    def get_form_kwargs(self, **kwargs):
        kwargs = super().get_form_kwargs()
        redirect_ = self.request.GET.get('next')
        if redirect_ is not None:
            self.success_url = redirect_
        else:
            self.success_url = '/mydata'
        if redirect_:
            if 'initial' in kwargs.keys():
                kwargs['initial'].update({'next': redirect_})
            else:
                kwargs['initial'] = {'next': redirect_}
        return kwargs
    # ** END


def format_uri(node, namespace_manager):
    """
    Custom function to format RDF node URIs using namespace prefixes.
    """
    uri = str(node)
    for prefix, ns in namespace_manager.namespaces():
        ns = str(ns)
        if uri.startswith(ns):
            return f"{prefix}:{uri[len(ns):]}"
    return node.n3(namespace_manager)  # Fallback to default n3 serialization


class GraphView(TemplateView):
    template_name = "datasets/graph.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        selected_classes = self.request.GET.get('classes', '').split(',')
        # selected_classes = [urllib.parse.unquote(cls) for cls in selected_classes]
        # print("Selected classes:", selected_classes)

        # Path to your Turtle file
        file_path = 'knowledge_graph/output.ttl'

        # Create a graph
        g = rdflib.Graph()

        # Parse the Turtle file
        g.parse(file_path, format='turtle')

        # Prepare the data for JavaScript
        # Assuming 'g' is your rdflib.Graph instance
        triples = []
        classes = set([obj for subj, pred, obj in g if str(pred) == 'http://www.w3.org/1999/02/22-rdf'
                                                                                 '-syntax-ns#type'])
        #print("Classes:", classes)

        if not selected_classes:
            # print("No classes selected, using all classes")
            selected_classes = classes

        # print("Selected classes:", selected_classes)
        classes_str = ", ".join(f"<{cls}>" for cls in selected_classes)
        # print("Classes string:", classes_str)

        # filter the graph for the selected classes
        # print("Graph size before filtering:", len(g))
        query = f'''
        SELECT ?s ?p ?o WHERE {{
            ?s ?p ?o .
            ?s rdf:type ?type .
            FILTER(?type IN ({classes_str}))
        }}'''
        qres = g.query(query)
        # print("Graph size after filtering:", len(g))

        limit = 500
        for subj, pred, obj in qres:
            if limit == 0:
                break

            limit -= 1
            # if the predicate is  rdf:type, skip it
            if str(pred) == 'http://www.w3.org/1999/02/22-rdf-syntax-ns#type':
                continue

            triples.append({
                "subject": format_uri(subj, g.namespace_manager),
                "predicate": format_uri(pred, g.namespace_manager),
                "object": format_uri(obj, g.namespace_manager)
            })

        # Add triples to the context
        context['triples'] = triples
        context['classes'] = sorted([str(cls) for cls in classes])
        context['selected_classes'] = [str(cls) for cls in selected_classes]
        return context
