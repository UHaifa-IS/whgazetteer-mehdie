import math
import shutil
import tempfile
from pathlib import Path
from shutil import copyfile

import pandas as pd
import requests
from celery import current_app as celapp
from celery.result import AsyncResult
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.mail import send_mail
from django.core.paginator import Paginator
from django.db import transaction
from django.db.utils import DataError
from django.forms import modelformset_factory
from django.http import HttpResponseRedirect
from django.shortcuts import redirect
from django.urls import reverse
from django.views.generic import (CreateView, ListView, UpdateView, DeleteView, DetailView)
from django_celery_results.models import TaskResult
from elasticsearch7 import Elasticsearch

from areas.models import Area
from collection.models import Collection
from datasets.forms import HitModelForm, DatasetDetailModelForm, DatasetCreateModelForm
from datasets.models import DatasetFile
from datasets.static.hashes import mimetypes_plus as mthash_plus
from datasets.static.hashes.parents import ccodes as cchash
from datasets.tasks import maxID, align_match_data, run_mehdi_er, align_wdlocal, align_tgn, align_idx
from datasets.update import deleteFromIndex
from datasets.utils import *
from elastic.es_utils import makeDoc, removePlacesFromIndex, replaceInIndex
from main.choices import AUTHORITY_BASEURI
from main.models import Log, Comment
from places.models import *
from resources.models import Resource

# es = Elasticsearch([{'host': 'localhost',
#                      'port': 9200,
#                      'api_key': (settings.ES_APIKEY_ID, settings.ES_APIKEY_KEY),
#                      'timeout': 30,
#                      'max_retries': 10,
#                      'retry_on_timeout': True
#                      }])

es = Elasticsearch([{'host': '0.0.0.0',
                     'port': 9200,
                     'api_key': (settings.ES_APIKEY_ID, settings.ES_APIKEY_KEY),
                     'timeout': 30,
                     'max_retries': 10,
                     'retry_on_timeout': True}])

"""
  email various, incl. Celery down notice
  to ['whgazetteer@gmail.com','mehdie.org@gmail.com'],
"""


def emailer(subj, msg, from_addr, to_addr):
    send_mail(
        subj, msg, from_addr, to_addr,
        fail_silently=False,
    )


""" check Celery process is running before initiating reconciliation task """


def celeryUp():
    response = celapp.control.ping(timeout=1.0)
    return len(response) > 0


""" append src_id to base_uri"""


def link_uri(auth, id):
    # if auth = 'align_match_data' look up the dataset_id in the Place record using the id which is actually a place_id
    if auth == 'align_match_data':
        place = Place.objects.get(id=id)
        ds_id = place.dataset.label
        src_id = place.src_id
        uri = ds_id + ":" + src_id
    else:
        baseuri = AUTHORITY_BASEURI[auth]
        uri = baseuri + str(id)

    print("[DEBUG] link_uri generated this link: %s" % uri)
    return uri


"""
  from datasets.views.review()
  indexes a db record upon a single hit match in align_idx review
  new record becomes child in the matched hit group 
"""


def indexMatch(pid, hit_pid=None):
    from elasticsearch7 import Elasticsearch
    es = Elasticsearch([{'host': '0.0.0.0',
                         'port': 9200,
                         'api_key': (settings.ES_APIKEY_ID, settings.ES_APIKEY_KEY),
                         'timeout': 30,
                         'max_retries': 10,
                         'retry_on_timeout': True}])

    idx = 'whg'
    place = get_object_or_404(Place, id=pid)

    # is this place already indexed (e.g. by pass0 automatch)?
    q_place = {"query": {"bool": {"must": [{"match": {"place_id": pid}}]}}}
    res = es.search(index=idx, body=q_place)
    if res['hits']['total']['value'] == 0:
        # not indexed, make a new doc
        new_obj = makeDoc(place)
        p_hits = None
    else:
        # it's indexed, get parent
        p_hits = res['hits']['hits']
        place_parent = p_hits[0]['_source']['relation']['parent']

    if hit_pid == None and not p_hits:
        # there was no match and place is not already indexed
        new_obj['relation'] = {"name": "parent"}

        # increment whg_id
        whg_id = maxID(es, idx) + 1
        # parents get an incremented _id & whg_id
        new_obj['whg_id'] = whg_id
        # sys.exit()

        # add its own names to the suggest field
        for n in new_obj['names']:
            new_obj['suggest']['input'].append(n['toponym'])
        # add its title
        if place.title not in new_obj['suggest']['input']:
            new_obj['suggest']['input'].append(place.title)
        # index it
        try:
            res = es.index(index=idx, id=str(whg_id), body=json.dumps(new_obj))
            place.indexed = True
            place.save()
        except:
            pass
    else:
        # get hit record in index
        q_hit = {"query": {"bool": {"must": [{"match": {"place_id": hit_pid}}]}}}
        res = es.search(index=idx, body=q_hit)
        hit = res['hits']['hits'][0]

        # see if new place (pid) is already indexed (i.e. due to prior automatch)
        q_place = {"query": {"bool": {"must": [{"match": {"place_id": pid}}]}}}
        res = es.search(index=idx, body=q_place)
        if len(res['hits']['hits']) > 0:
            # it's already in, (almost) certainly a child...of what?
            place_hit = res['hits']['hits'][0]

        # if hit is a child, get _id of its parent; this will be a sibling
        # if hit is a parent, get its _id, this will be a child
        if hit['_source']['relation']['name'] == 'child':
            parent_whgid = hit['_source']['relation']['parent']
        else:
            parent_whgid = hit['_id']

        # mine new place for its names, make an index doc
        match_names = [p.toponym for p in place.names.all()]
        new_obj['relation'] = {"name": "child", "parent": parent_whgid}

        # all or nothing; pass if error
        try:
            # index child
            es.index(index=idx, id=place.id, routing=1, body=json.dumps(new_obj))
            # count_kids +=1

            # add child's names to parent's searchy & suggest.input[] fields
            q_update = {"script": {
                "source": "ctx._source.suggest.input.addAll(params.names); ctx._source.children.add(params.id); ctx._source.searchy.addAll(params.names)",
                "lang": "painless",
                "params": {"names": match_names, "id": str(place.id)}
            },
                "query": {"match": {"_id": parent_whgid}}}
            es.update_by_query(index=idx, body=q_update, conflicts='proceed')
        except Exception as e:
            capture_exception(e)
            raise ValueError(e)


"""
  from datasets.views.review()
  indexes a db record given multiple hit matches in align_idx review
  a LOT has to happen (see _notes/accession-psudocode.txt): 
    - pick a single 'winner' among the matched hits (max score)
    - make new record its child
    - demote all non-winners in index from parent to child
      - whg_id and children[] ids (if any) added to winner
      - name variants added to winner's searchy[] and suggest.item[] lists
"""


def indexMultiMatch(pid, matchlist):
    from elasticsearch7 import Elasticsearch, RequestError
    es = Elasticsearch([{'host': '0.0.0.0',
                         'port': 9200,
                         'api_key': (settings.ES_APIKEY_ID, settings.ES_APIKEY_KEY),
                         'timeout': 30,
                         'max_retries': 10,
                         'retry_on_timeout': True}])

    idx = 'whg'
    place = Place.objects.get(id=pid)
    from elastic.es_utils import makeDoc
    new_obj = makeDoc(place)

    # bins for new values going to winner
    addnames = []
    addkids = [str(pid)]  # pid will also be the new record's _id

    # max score is winner
    winner = max(matchlist, key=lambda x: x['score'])  # 14158663
    # this is multimatch so there is at least one demoted (list of whg_ids)
    demoted = [str(i['whg_id']) for i in matchlist if not (i['whg_id'] == winner['whg_id'])]  # ['14090523']

    # complete doc for new record
    new_obj['relation'] = {"name": "child", "parent": winner['whg_id']}
    # copy its toponyms into addnames[] for adding to winner later
    for n in new_obj['names']:
        addnames.append(n['toponym'])
    if place.title not in addnames:
        addnames.append(place.title)

    # generate script used to update winner w/kids and names
    # from new record and any kids of 'other' matched parents
    def q_updatewinner(addkids, addnames):
        return {"script": {
            "source": """ctx._source.children.addAll(params.newkids);
      ctx._source.suggest.input.addAll(params.names);
      ctx._source.searchy.addAll(params.names);
      """,
            "lang": "painless",
            "params": {
                "newkids": addkids,
                "names": addnames}
        }}

    # index the new record as child of winner
    try:
        es.index(index=idx, id=str(pid), routing=1, body=json.dumps(new_obj))
    except Exception as e:
        capture_exception(e)
        raise ValueError(e)

    # demote others
    for _id in demoted:
        # get index record stuff, to be altered then re-indexed
        # ES won't allow altering parent/child relations directly
        q_demote = {"query": {"bool": {"must": [{"match": {"whg_id": _id}}]}}}
        res = es.search(body=q_demote, index=idx)
        srcd = res['hits']['hits'][0]['_source']
        # add names in suggest to names[]
        sugs = srcd['suggest']['input']
        for sug in sugs:
            addnames.append(sug)
        addnames = list(set(addnames))
        # _id of demoted (a whg_id) belongs in winner's children[]
        addkids.append(str(srcd['whg_id']))

        haskids = len(srcd['children']) > 0
        # if demoted record has kids, add to addkids[] list
        # for 'adoption' by topdog later
        if haskids:
            morekids = srcd['children']
            for kid in morekids:
                addkids.append(str(kid))

        # update the 'winner' parent
        q = q_updatewinner(list(set(addkids)), list(set(addnames)))  # ensure only unique
        try:
            es.update(idx, winner['whg_id'], body=q)
        except RequestError as rq:
            raise ValueError(rq)

        from copy import deepcopy
        newsrcd = deepcopy(srcd)
        # update it to reflect demotion
        newsrcd['relation'] = {"name": "child", "parent": winner['whg_id']}
        newsrcd['children'] = []
        if 'whg_id' in newsrcd:
            newsrcd.pop('whg_id')

        # zap the demoted, reindex with same _id and modified doc (newsrcd)
        try:
            es.delete('whg', _id)
            es.index(index='whg', id=_id, body=newsrcd, routing=1)
        except RequestError as e:
            raise ValueError(e)

        # re-assign parent for kids of all/any demoted parents
        if len(addkids) > 0:
            for kid in addkids:
                q_adopt = {"script": {
                    "source": "ctx._source.relation.parent = params.new_parent; ",
                    "lang": "painless",
                    "params": {"new_parent": winner['whg_id']}
                },
                    "query": {"match": {"place_id": kid}}}
                es.update_by_query(index=idx, body=q_adopt, conflicts='proceed')


""" 
  GET   returns review.html for Wikidata, or accession.html for accessioning
  POST  for each record that got hits, process user matching decisions 
"""


def review(request, pk, tid, passnum):
    pid = None
    if 'pid' in request.GET:
        pid = request.GET['pid']
    ds = get_object_or_404(Dataset, id=pk)
    task = get_object_or_404(TaskResult, task_id=tid)
    auth = task.task_name[6:].replace('local', '')
    authname = 'Wikidata' if auth == 'wd' else 'Getty TGN' \
        if auth == 'tgn' else 'WHG'
    kwargs = json.loads(task.task_kwargs.replace("'", '"'))
    beta = 'beta' in list(request.user.groups.all().values_list('name', flat=True))
    # filter place records by passnum for those with unreviewed hits on this task
    # if request passnum is complete, increment
    cnt_pass = Hit.objects.values('place_id').filter(task_id=tid, reviewed=False, query_pass=passnum).count()
    # TODO: refactor this awful mess; controls whether PASS appears in review dropdown
    cnt_pass0 = Hit.objects.values('place_id').filter(
        task_id=tid, reviewed=False, query_pass='pass0').count()
    cnt_pass1 = Hit.objects.values('place_id').filter(
        task_id=tid, reviewed=False, query_pass='pass1').count()
    cnt_pass2 = Hit.objects.values('place_id').filter(
        task_id=tid, reviewed=False, query_pass='pass2').count()
    cnt_pass3 = Hit.objects.values('place_id').filter(
        task_id=tid, reviewed=False, query_pass='pass3').count()

    # calling link passnum may be 'pass*', 'def', or '0and1' (for idx)
    # if 'pass*', just get place_ids for that pass
    if passnum.startswith('pass'):
        pass_int = int(passnum[4])
        # if no unreviewed left, go to next pass
        passnum = passnum if cnt_pass > 0 else 'pass' + str(pass_int + 1)
        hitplaces = Hit.objects.values_list('place_id', flat=True).filter(
            task_id=tid,
            reviewed=False,
            query_pass=passnum
        )
    else:
        # all unreviewed
        hitplaces = Hit.objects.values_list('place_id', flat=True).filter(task_id=tid, reviewed=False)

    # set review page returned
    if auth in ['whg', 'idx']:
        review_page = 'accession.html'
    else:
        review_page = 'review.html'

    #
    # review_field = 'review_whg' if auth in ['whg', 'idx'] else \
    #     'review_wd' if auth.startswith('wd') else 'review_tgn'

    review_field = 'review_whg' if auth in ['whg', 'idx'] else \
        'review_wd' if auth.startswith('wd') else \
            'review_md' if auth.startswith('match_data') else 'review_tgn'

    lookup = '__'.join([review_field, 'in'])
    """
    2 = deferred; 1 = reviewed, 0 = unreviewed; NULL = no hits
    status = [2] if passnum == 'def' else [0,2]
    by default, don't return deferred
  """
    status = [2] if passnum == 'def' else [0]

    # unreviewed place objects from place_ids (a single pass or all)
    # record_list = ds.places.order_by('id').filter(**{lookup: status}, pk__in=hitplaces)
    record_list = ds.places.order_by('id').filter(pk__in=hitplaces)

    # no records left for pass (or in deferred queue)
    if len(record_list) == 0:
        context = {
            "nohits": True,
            'ds_id': pk,
            'task_id': tid,
            'passnum': passnum,
        }
        return render(request, 'datasets/' + review_page, context=context)

    # manage pagination & urls
    # gets next place record as records[0]
    # TODO: manage concurrent reviewers; i.e. 2 people have same page 1
    paginator = Paginator(record_list, 1)

    # handle request for singleton (e.g. deferred from browse table)
    # if 'pid' in request.GET, bypass per-pass sequential loading
    if pid:
        # get its index and add 1 to get page
        page = (*record_list,).index(Place.objects.get(id=pid)) + 1
    else:
        # default action, sequence of all pages for the pass
        page = 1 if not request.GET.get('page') else \
            request.GET.get('page')
    records = paginator.get_page(page)

    # get hits for this record
    placeid = records[0].id
    place = get_object_or_404(Place, id=placeid)
    if passnum.startswith('pass') and auth not in ['whg', 'idx']:
        # this is wikidata review, list only for this pass
        raw_hits = Hit.objects.filter(place_id=placeid, task_id=tid, query_pass=passnum).order_by('-score')
    else:
        # accessioning -> get all regardless of pass
        raw_hits = Hit.objects.filter(place_id=placeid, task_id=tid).order_by('-score')

    # ??why? get pass contents for all of a place's hits
    passes = list(set([item for sublist in [[s['pass'] for s in h.json['sources']] for h in raw_hits]
                       for item in sublist])) if auth in ['whg', 'idx'] else None

    # convert ccodes to names
    countries = []
    for r in place.ccodes:
        try:
            countries.append(cchash[0][r.upper()]['gnlabel'] + ' (' + cchash[0][r.upper()]['tgnlabel'] + ')')
        except Exception as e:
            raise ValueError(e)

    # prep some context
    context = {
        'ds_id': pk, 'ds_label': ds.label, 'task_id': tid,
        'hit_list': raw_hits,
        'passes': passes,
        'authority': task.task_name[6:8] if auth == 'wdlocal' else task.task_name[6:],
        'records': records,
        'countries': countries,
        'passnum': passnum,
        'page': page if request.method == 'GET' else str(int(page) - 1),
        'aug_geom': json.loads(task.task_kwargs.replace("'", '"'))['aug_geom'],
        'mbtokenmb': settings.MAPBOX_TOKEN_MB,
        'count_pass0': cnt_pass0,
        'count_pass1': cnt_pass1,
        'count_pass2': cnt_pass2,
        'count_pass3': cnt_pass3,
        'deferred': True if passnum == 'def' else False,
    }

    # build formset from hits, add to context
    HitFormset = modelformset_factory(
        Hit,
        fields=('id', 'authority', 'authrecord_id', 'query_pass', 'score', 'json', 'relation_type', 'explanations'),
        form=HitModelForm, extra=0)
    formset = HitFormset(request.POST or None, queryset=raw_hits)
    context['formset'] = formset
    method = request.method

    if method == 'POST':
        # process match/no match choices made by save in review or accession page
        # NB very different cases.
        #   For wikidata review, act on each hit considered (new place_geom and place_link records if matched)
        #   For accession, act on index 'clusters'
        place_post = get_object_or_404(Place, pk=request.POST['place_id'])
        review_status = getattr(place_post, review_field)
        # proceed with POST only if place is unreviewed or deferred; else return to a GET (and next place)
        print("[DEBUG] entered POST for review page, place_id: " + str(place_post.id) + ", review_status: " + str(review_status))
        # NB. other reviewer(s) *not* notified
        if review_status == 1:
            context["already"] = True
            return redirect('/datasets/' + str(pk) + '/review/' + task.task_id + '/' + passnum)
        elif formset.is_valid():
            print("[DEBUG] reading formset: {}".format(str(formset.cleaned_data)))
            hits = formset.cleaned_data
            matches = 0
            matched_for_idx = []  # for accession
            # are any of the listed hits matches?
            for x in range(len(hits)):
                hit = hits[x]['id']
                # is this hit a match?
                print("[DEBUG] reading hit: {} task is {}".format(str(hits[x]), task.task_name))
                if hits[x]['relation_type'] not in ['none']:
                    matches += 1
                    # if wd or tgn, write place_geom, place_link record(s) now
                    # IF someone didn't just review it!
                    if task.task_name[6:] in ['wdlocal', 'wd', 'tgn', 'match_data']:
                        print("[DEBUG] writing place_geom and place_link records")
                        hasGeom = 'geoms' in hits[x]['json'] and len(hits[x]['json']['geoms']) > 0
                        # create place_geom records if 'accept geometries' was checked
                        if kwargs['aug_geom'] == 'on' and hasGeom \
                                and tid not in place_post.geoms.all().values_list('task_id', flat=True):
                            gtype = hits[x]['json']['geoms'][0]['type']
                            coords = hits[x]['json']['geoms'][0]['coordinates']
                            # TODO: build real postgis geom values
                            gobj = GEOSGeometry(json.dumps({"type": gtype, "coordinates": coords}))
                            PlaceGeom.objects.create(
                                place=place_post,
                                task_id=tid,
                                src_id=place.src_id,
                                geom=gobj,
                                jsonb={
                                    "type": gtype,
                                    "citation": {"id": auth + ':' + hits[x]['authrecord_id'], "label": authname},
                                    "coordinates": coords
                                }
                            )

                        # create single PlaceLink for matched authority record
                        # TODO: this if: condition handled already?
                        if tid not in place_post.links.all().values_list('task_id', flat=True):
                            print("[DEBUG] writing place_link record")
                            PlaceLink.objects.create(
                                place=place_post,
                                task_id=tid,
                                src_id=place.src_id,
                                jsonb={
                                    "type": hits[x]['relation_type'],
                                    "identifier": link_uri(task.task_name, hits[x]['authrecord_id'] \
                                        if hits[x]['authority'] not in ['whg', 'md'] else hits[x]['json']['place_id'])
                                    }
                            )

                        # create multiple PlaceLink records (e.g. Wikidata)
                        # TODO: filter duplicates
                        if 'links' in hits[x]['json']:
                            for l in hits[x]['json']['links']:
                                authid = re.search("\: ?(.*?)$", l).group(1)
                                if authid not in place.authids:
                                    PlaceLink.objects.create(
                                        place=place,
                                        task_id=tid,
                                        src_id=place.src_id,
                                        jsonb={
                                            "type": hits[x]['relation_type'],
                                            "identifier": l.strip()
                                        }
                                    )
                                    # update totals
                                    ds.numlinked = ds.numlinked + 1 if ds.numlinked else 1
                                    ds.total_links = ds.total_links + 1
                                    ds.save()
                    # this is accessioning to whg index, add to matched[]
                    elif task.task_name == 'align_idx':
                        if 'links' in hits[x]['json']:
                            links_count = len(hits[x]['json'])
                        matched_for_idx.append({'whg_id': hits[x]['json']['whg_id'],
                                                'pid': hits[x]['json']['pid'],
                                                'score': hits[x]['json']['score'],
                                                'links': links_count})
                    # TODO: informational lookup on whg index?
                # in any case, flag hit as reviewed...
                hitobj = get_object_or_404(Hit, id=hit.id)
                hitobj.reviewed = True
                hitobj.relation_type = hits[x]['relation_type']
                hitobj.save()

            # handle accessioning match results
            if len(matched_for_idx) == 0 and task.task_name == 'align_idx':
                # no matches during accession, index as seed (parent
                indexMatch(str(place_post.id))
                place_post.indexed = True
                place_post.save()
            elif len(matched_for_idx) == 1:
                indexMatch(str(place_post.id), matched_for_idx[0]['pid'])
                place_post.indexed = True
                place_post.save()
            elif len(matched_for_idx) > 1:
                indexMultiMatch(place_post.id, matched_for_idx)
                place_post.indexed = True
                place_post.save()

            if ds.unindexed == 0:
                setattr(ds, 'ds_status', 'indexed')
                ds.save()

            # if none are left for this task, change status & email staff
            if auth in ['wd'] and ds.recon_status['wdlocal'] == 0:
                ds.ds_status = 'wd-complete'
                ds.save()
                status_emailer(ds, 'wd')
            elif auth == 'idx' and ds.recon_status['idx'] == 0:
                ds.ds_status = 'indexed'
                ds.save()
                status_emailer(ds, 'idx')

            place_post.save()

            return redirect('/datasets/' + str(pk) + '/review/' + tid + '/' + passnum + '?page=' + str(int(page)))
    return render(request, 'datasets/' + review_page, context=context)


"""
  write_wd_pass0(taskid)
  called from dataset_detail>reconciliation tab
  accepts all pass0 wikidata matches, writes geoms and links
"""


def write_wd_pass0(request, tid):
    task = get_object_or_404(TaskResult, task_id=tid)
    kwargs = json.loads(task.task_kwargs.replace("'", '"'))
    referer = request.META.get('HTTP_REFERER') + '#reconciliation'
    auth = task.task_name[6:].replace('local', '')
    ds = get_object_or_404(Dataset, pk=kwargs['ds'])
    authname = 'Wikidata'

    # get unreviewed pass0 hits
    hits = Hit.objects.filter(
        task_id=tid,
        query_pass='pass0',
        reviewed=False
    )
    for h in hits:
        hasGeom = 'geoms' in h.json and len(h.json['geoms']) > 0
        hasLinks = 'links' in h.json and len(h.json['links']) > 0
        place = h.place  # object
        # existing for the place
        authids = place.links.all().values_list('jsonb__identifier', flat=True)
        # GEOMS
        # confirm another user hasn't just done this...
        if hasGeom and kwargs['aug_geom'] == 'on' \
                and tid not in place.geoms.all().values_list('task_id', flat=True):
            for g in h.json['geoms']:
                PlaceGeom.objects.create(
                    place=place,
                    task_id=tid,
                    src_id=place.src_id,
                    geom=GEOSGeometry(json.dumps({"type": g['type'], "coordinates": g['coordinates']})),
                    jsonb={
                        "type": g['type'],
                        "citation": {"id": auth + ':' + h.authrecord_id, "label": authname},
                        "coordinates": g['coordinates']
                    }
                )

        # LINKS
        link_counter = 0
        # add PlaceLink record for wikidata hit if not already there
        if 'wd:' + h.authrecord_id not in authids:
            link_counter += 1
            PlaceLink.objects.create(
                place=place,
                task_id=tid,
                src_id=place.src_id,
                jsonb={
                    "type": "closeMatch",
                    "identifier": link_uri(task.task_name, h.authrecord_id)
                }
            )

        # create link for each wikidata concordance, if any
        if hasLinks:
            # authids=place.links.all().values_list(
            # 'jsonb__identifier',flat=True)
            for l in h.json['links']:
                link_counter += 1
                authid = re.search("\:?(.*?)$", l).group(1)
                # TODO: same no-dupe logic in review()
                # don't write duplicates
                if authid not in authids:
                    PlaceLink.objects.create(
                        place=place,
                        task_id=tid,
                        src_id=place.src_id,
                        jsonb={
                            "type": "closeMatch",
                            "identifier": authid
                        }
                    )

        # update dataset totals for metadata page
        ds.numlinked = len(set(PlaceLink.objects.filter(place_id__in=ds.placeids).values_list('place_id', flat=True)))
        ds.total_links += link_counter
        ds.save()

        # flag hit as reviewed
        h.reviewed = True
        h.save()

        # flag place as reviewed
        place.review_wd = 1
        place.save()

    return HttpResponseRedirect(referer)


"""
  ds_recon(pk)
  initiates & monitors Celery tasks against Elasticsearch indexes
  i.e. align_[wdlocal | idx | tgn ] in tasks.py
  url: datasets/{ds.id}/reconcile ('ds_reconcile'; from ds_addtask.html)
  params: pk (dataset id), auth, region, userarea, geom, scope
  each align_{auth} task runs matching es_lookup_{auth}() and writes Hit instances
"""


def process_er(url):
    c = pd.read_csv(url)


def ds_recon(request, pk):
    if 'task_id' in request.session:
        print("[DEBUG] task_id is set to {}".format(request.session['task_id']))
        # Retrieve the task.
        task_id = request.session['task_id']
        task = AsyncResult(task_id)

        # Check the task status and get the result if it's ready.
        if task.ready():
            print("[DEBUG] task is ready")
            if isinstance(task.result, Exception):
                print("[DEBUG] task has failed with exception {}".format(str(task.result)))
                messages.add_message(request, messages.INFO,
                                     "<span class='text-success'>Your ER reconciliation task has failed due to an "
                                     "error.</span><br/> {}".format(str(task.result)))
            else:
                print("[DEBUG] task has succeeded with result {}".format(str(task.result)))
                csv_url, status_code = task.result
                if status_code > 200 and status_code != 400:
                    return HttpResponse('Error with Datasets, check again')
                if status_code == 400:
                    m_dataset = request.session['d1']
                    p_dataset = request.session['d2']
                    try:
                        d1 = DatasetFile.objects.get(dataset_id=m_dataset)
                        d2 = DatasetFile.objects.get(dataset_id=p_dataset)
                    except Exception as e:
                        print("Exception in ds_recon when trying to fetch dataset files for {} and {}".format(m_dataset, p_dataset))
                        raise e
                    if csv_url['detail']['dataset1']:
                        return HttpResponse("The dataset matching service expects the dataset to contain several fields."
                                            f" {d1.dataset_id.title}"
                                            f" is missing the fields {', '.join([field for field in csv_url['detail']['dataset1']])}."
                                            f" If possible, we will proceed with the given fields."
                                            )
                    if csv_url['detail']['dataset2']:
                        return HttpResponse("The dataset matching service expects the dataset to contain several fields."
                                            f" {d2.dataset_id.title}"
                                            f" is missing the fields {', '.join([field for field in csv_url['detail']['dataset2']])}."
                                            f" If possible, we will proceed with the given fields."
                                            )
                # process_er(csv_url)
                messages.add_message(request, messages.INFO,
                                     "<span class='text-success'>Your ER reconciliation task has been "
                                     "processsed.</span><br/>Download the csv file using the link below, results will "
                                     "appear below (you may have to refresh screen). <br/> <a href='{}'>Download "
                                     "Match File</a>".format(
                                         csv_url))
            del request.session['task_id']  # Clear the task ID from the session.
        else:
            messages.add_message(request, messages.INFO,"<span class='text-success'>Your ER reconciliation task is "
                                                        "showing the following status: {}".format(str(task.status)))



    ds = get_object_or_404(Dataset, id=pk)
    # TODO: handle multipolygons from "#area_load" and "#area_draw"
    user = request.user

    if request.method == 'POST' and request.POST:
        auth = request.POST['recon']
        aug_geom = request.POST['geom'] if 'geom' in request.POST else ''
        language = request.LANGUAGE_CODE
        print(auth)
        if auth == 'match_data':

            m_dataset = request.POST['m_dataset']
            p_dataset = request.POST['p_dataset']

            if m_dataset == '0' and p_dataset == '0':
                return HttpResponse('You have not selected a dataset')

            if m_dataset != '0' and p_dataset != '0':
                return HttpResponse('You must select one dataset')

            dt_1 = pk
            dt_2 = m_dataset if m_dataset != '0' else p_dataset

            match_config_str = request.POST.get('match_config', '{}')
            try:
                print("Received match_config_str:", match_config_str)
                match_config = json.loads(match_config_str)
            except json.JSONDecodeError:
                return HttpResponse('Invalid match_config format, received: {}, expected valid JSON'.format(match_config_str))

            # try:
            print("[DEBUG] running run_mehdi_er.delay({},{},{},{},{},{},{},{})".format(dt_1, dt_2, ds.id, aug_geom, language, user.id, match_config))
            task = run_mehdi_er.delay(dt_1, dt_2, ds.id, aug_geom, language, user.id, match_config)
            request.session['task_id'] = task.id
            request.session['d1'] = m_dataset
            request.session['d2'] = p_dataset

            messages.add_message(request, messages.INFO,
                                 "Your dataset matching task is being processed. Please wait a few minutes and refresh this page.")
            return redirect('/datasets/' + str(ds.id) + '/reconcile')

        if auth == 'idx' and ds.public == False:
            messages.add_message(request, messages.ERROR, """Dataset must be public before indexing!""")
            return redirect('/datasets/' + str(ds.id) + '/addtask')
        # previous successful task of this type?
        #   wdlocal? archive previous, scope = unreviewed
        #   idx? scope = unindexed
        previous = ds.tasks.filter(task_name__startswith='align_' + auth, status='SUCCESS')
        prior = request.POST['prior'] if 'prior' in request.POST else 'na'
        if previous.count() > 0:
            if auth == 'idx':
                scope = "unindexed"
            else:
                # get its id and archive it
                tid = previous.first().task_id
                task_archive(tid, prior)
                scope = 'unreviewed'
        else:
            # no existing task, submit all rows
            scope = 'all'

        # which task? wdlocal, tgn, idx, whg (future)
        func = eval('align_' + auth)

        # TODO: let this vary per task?
        region = request.POST['region']  # pre-defined UN regions
        userarea = request.POST['userarea']  # from ccodes, or drawn
        # on == write geom if matched
        bounds = {
            "type": ["region" if region != "0" else "userarea"],
            "id": [region if region != "0" else userarea]}

        # check Celery service
        if not celeryUp():
            emailer('Celery is down :^(',
                    'if not celeryUp() -- look into it, bub!',
                    'whg@kgeographer.org',
                    ['mehdie.org@gmail.com'])
            messages.add_message(request, messages.INFO, """Sorry! WHG reconciliation services appears to be down. 
        The system administrator has been notified.""")
            return redirect('/datasets/' + str(ds.id) + '/reconcile')

        # sys.exit()
        # initiate celery/redis task
        # NB 'func' resolves to align_wdlocal() or align_idx() or align_tgn()
        try:
            func.delay(
                ds.id,
                ds=ds.id,
                dslabel=ds.label,
                owner=ds.owner.id,
                user=user.id,
                bounds=bounds,
                aug_geom=aug_geom,
                scope=scope,
                lang=language,
            )
            messages.add_message(request, messages.INFO,
                                 "<span class='text-danger'>Your reconciliation task is under way.</span><br/>When complete, you will receive an email and if successful, results will appear below (you may have to refresh screen). <br/>In the meantime, you can navigate elsewhere.")
            return redirect('/datasets/' + str(ds.id) + '/reconcile')
        except Exception as e:
            capture_exception(e)
            messages.add_message(request, messages.INFO,
                                 "Sorry! Reconciliation services appear to be down. The system administrator has been notified.<br/>" + str(
                                     sys.exc_info()))
            emailer('WHG recon task failed',
                    'a reconciliation task has failed for dataset #' + ds.id + ', w/error: \n' + str(
                        sys.exc_info()) + '\n\n',
                    'whg@kgeographer.org',
                    'mehdie.org@gmail.com')

            return redirect('/datasets/' + str(ds.id) + '/reconcile')


"""
  task_delete(tid, scope)
  delete results of a reconciliation task:
  hits + any geoms and links added by review
  reset Place.review_{auth} to null
"""


def task_delete(request, tid, scope="foo"):
    hits = Hit.objects.all().filter(task_id=tid)
    tr = get_object_or_404(TaskResult, task_id=tid)
    dsid = tr.task_args[1:-1]
    auth = tr.task_name[6:]  # wdlocal, idx
    # only the places that had hit(s) in this task
    places = Place.objects.filter(id__in=[h.place_id for h in hits])
    # links and geometry added by a task have the task_id
    placelinks = PlaceLink.objects.all().filter(task_id=tid)
    placegeoms = PlaceGeom.objects.all().filter(task_id=tid)

    # reset Place.review_{auth} to null
    for p in places:
        if auth in ['whg', 'idx']:
            p.review_whg = None
        elif auth.startswith('wd'):
            p.review_wd = None
        else:
            p.review_tgn = None
        p.defer_comments.delete()
        p.save()

    # zap task record & its hits
    # or only geoms if that was the choice
    if scope == 'task':
        tr.delete()
        hits.delete()
        placelinks.delete()
        placegeoms.delete()
    elif scope == 'geoms':
        placegeoms.delete()

    # delete dataset from index
    # undoes any acceessioning work
    # set status back to reconciling

    if ',' in dsid:
        dsid = dsid[:-1]
    ds = Dataset.objects.get(id=dsid)

    ds.ds_status = 'reconciling'
    ds.save()

    return redirect('/datasets/' + dsid + '/reconcile')


"""
  task_archive(tid, scope, prior)
  delete hits
  if prior = 'zap: delete geoms and links added by review
  reset Place.review_{auth} to null
  set task status to 'ARCHIVED'
"""


def task_archive(tid, prior):
    hits = Hit.objects.all().filter(task_id=tid)
    tr = get_object_or_404(TaskResult, task_id=tid)
    dsid = tr.task_args[1:-1]
    auth = tr.task_name[6:]
    places = Place.objects.filter(id__in=[h.place_id for h in hits])

    # reset Place.review_{auth} to null
    for p in places:
        p.defer_comments.delete()
        if auth in ['whg', 'idx'] and p.review_whg != 1:
            p.review_whg = None
        elif auth.startswith('wd') and p.review_wd != 1:
            p.review_wd = None
        elif auth == 'tgn' and p.review_tgn != 1:
            p.review_tgn = None
        p.save()

    # zap hits
    hits.delete()
    if prior == 'na':
        tr.delete()
    else:
        # flag task as ARCHIVED
        tr.status = 'ARCHIVED'
        tr.save()
        # zap prior links/geoms if requested
        if prior == 'zap':
            PlaceLink.objects.all().filter(task_id=tid).delete()
            PlaceGeom.objects.all().filter(task_id=tid).delete()


"""
  add collaborator to dataset in role
"""


def collab_add(request, dsid, v):
    try:
        uid = get_object_or_404(User, username=request.POST['username']).id
        role = request.POST['role']
    except:
        # TODO: raise error to screen
        messages.add_message(
            request, messages.INFO, "Please check username, we don't have '" + request.POST['username'] + "'")
        if not v:
            return redirect('/datasets/' + str(dsid) + '/collab')
        else:
            return HttpResponseRedirect(request.META.get('HTTP_REFERER'))
    DatasetUser.objects.create(user_id_id=uid, dataset_id_id=dsid, role=role)
    if v == '1':
        return redirect('/datasets/' + str(dsid) + '/collab')
    else:
        return HttpResponseRedirect(request.META.get('HTTP_REFERER'))


"""
  collab_delete(uid, dsid)
  remove collaborator from dataset
"""


def collab_delete(request, uid, dsid, v):
    get_object_or_404(DatasetUser, user_id_id=uid, dataset_id_id=dsid).delete()
    if v == '1':
        return redirect('/datasets/' + str(dsid) + '/collab')
    else:
        return HttpResponseRedirect(request.META.get('HTTP_REFERER'))


"""
  dataset_file_delete(ds)
  delete all uploaded files for a dataset
"""


def dataset_file_delete(ds):
    dsf_list = ds.files.all()
    for f in dsf_list:
        ffn = 'media/' + f.file.name
        if os.path.exists(ffn):
            os.remove(ffn)


"""
  update_rels_tsv(pobj, row)
  updates objects related to a Place (pobj)
  make new child objects of pobj: names, types, whens, related, descriptions
  for geoms and links, add from row if not there
  row is a pandas dict
"""


def update_rels_tsv(pobj, row):
    """
    update related objects of a Place
    """
    print("DEBUG: update_rels_tsv()")
    header = list(row.keys())
    src_id = row['id']
    title = row['title']
    # for PlaceName insertion, strip anything in parens
    title = re.sub('\(.*?\)', '', title)
    title_source = row['title_source']
    title_uri = row['title_uri'] if 'title_uri' in header else ''
    variants = [x.strip() for x in row['variants'].split(';')] \
        if 'variants' in header else []
    types = [x.strip() for x in row['types'].split(';')] \
        if 'types' in header and str(row['types']) not in ('nan', '') else []
    aat_types = [x.strip() for x in row['aat_types'].split(';')] \
        if 'aat_types' in header and str(row['aat_types']) not in ('nan', '') else []
    parent_name = row['parent_name'] if 'parent_name' in header else ''
    parent_id = row['parent_id'] if 'parent_id' in header else ''
    coords = makeCoords(row['lon'], row['lat']) \
        if 'lon' in header and 'lat' in header and not math.isnan(row['lon']) else []
    matches = [x.strip() for x in row['matches'].split(';')] \
        if 'matches' in header and row['matches'] != '' else []
    description = row['description'] \
        if 'description' in header else ''

    # build associated objects and add to arrays
    objs = {"PlaceName": [], "PlaceType": [], "PlaceGeom": [], "PlaceWhen": [],
            "PlaceLink": [], "PlaceRelated": [], "PlaceDescription": [],
            "PlaceDepiction": []}

    # title as a PlaceName
    objs['PlaceName'].append(
        PlaceName(
            place=pobj,
            src_id=src_id,
            toponym=title,
            jsonb={"toponym": title, "citation": {"id": title_uri, "label": title_source}}
        ))

    # add variants as PlaceNames, if any
    if len(variants) > 0:
        for v in variants:
            haslang = re.search("@(.*)$", v.strip())
            new_name = PlaceName(
                place=pobj,
                src_id=src_id,
                toponym=v.strip(),
                jsonb={"toponym": v.strip(), "citation": {"id": "", "label": title_source}}
            )
            if haslang:
                new_name.jsonb['lang'] = haslang.group(1)
            objs['PlaceName'].append(new_name)

    # PlaceType()
    # TODO: parse t
    if len(types) > 0:
        for i, t in enumerate(types):
            # i always 0 in tsv
            aatnum = 'aat:' + aat_types[i] if len(aat_types) >= len(types) else ''
            objs['PlaceType'].append(
                PlaceType(
                    place=pobj,
                    src_id=src_id,
                    jsonb={"identifier": aatnum,
                           "sourceLabel": t,
                           "label": aat_lookup(int(aatnum[4:])) if aatnum != 'aat:' else ''
                           }
                ))

    # PlaceGeom()
    # TODO: test geometry type or force geojson
    if len(coords) > 0:
        geom = {"type": "Point",
                "coordinates": coords,
                "geowkt": 'POINT(' + str(coords[0]) + ' ' + str(coords[1]) + ')'}
    elif 'geowkt' in header and row['geowkt'] not in ['', None]:  # some rows no geom
        geom = parse_wkt(row['geowkt'])

    def trunc4(val):
        return round(val, 4)

    new_coords = list(map(trunc4, list(geom['coordinates'])))
    # only add new geometry
    if len(pobj.geoms.all()) > 0:
        for g in pobj.geoms.all():
            if list(map(trunc4, g.jsonb['coordinates'])) != new_coords:
                objs['PlaceGeom'].append(
                    PlaceGeom(
                        place=pobj,
                        src_id=src_id,
                        jsonb=geom
                    ))

    #
    # PlaceLink() - all are closeMatch
    if len(matches) > 0:
        # any existing? only add new
        exist_links = list(pobj.links.all().values_list('jsonb__identifier', flat=True))
        if set(matches) - set(exist_links) > 0:
            # one or more new matches; add 'em
            for m in matches:
                objs['PlaceLink'].append(
                    PlaceLink(
                        place=pobj,
                        src_id=src_id,
                        jsonb={"type": "closeMatch", "identifier": m}
                    ))

    # PlaceRelated()
    if parent_name != '':
        objs['PlaceRelated'].append(
            PlaceRelated(
                place=pobj,
                src_id=src_id,
                jsonb={
                    "relationType": "gvp:broaderPartitive",
                    "relationTo": parent_id,
                    "label": parent_name}
            ))

    # PlaceWhen()
    # timespans[{start{}, end{}}], periods[{name,id}], label, duration
    objs['PlaceWhen'].append(
        PlaceWhen(
            place=pobj,
            src_id=src_id,
            jsonb={
                "timespans": [{
                    "start": {"earliest": pobj.minmax[0]},
                    "end": {"latest": pobj.minmax[1]}}]
            }
        ))

    # PlaceDescription()
    # @id, value, lang
    if description != '':
        objs['PlaceDescription'].append(
            PlaceDescription(
                place=pobj,
                src_id=src_id,
                jsonb={
                    "@id": "", "value": description, "lang": ""
                }
            ))

    # TODO: update place.fclasses, place.minmax, place.timespans

    # bulk_create(Class, batch_size=n) for each
    PlaceName.objects.bulk_create(objs['PlaceName'], batch_size=10000)
    PlaceType.objects.bulk_create(objs['PlaceType'], batch_size=10000)
    PlaceGeom.objects.bulk_create(objs['PlaceGeom'], batch_size=10000)
    PlaceLink.objects.bulk_create(objs['PlaceLink'], batch_size=10000)
    PlaceRelated.objects.bulk_create(objs['PlaceRelated'], batch_size=10000)
    PlaceWhen.objects.bulk_create(objs['PlaceWhen'], batch_size=10000)
    PlaceDescription.objects.bulk_create(objs['PlaceDescription'], batch_size=10000)


"""
  ds_update()
  perform updates to database and index
  given new datafile
"""


# TODO: test this
def ds_update(request):
    if request.method == 'POST':
        dsid = request.POST['dsid']
        ds = get_object_or_404(Dataset, id=dsid)
        file_format = request.POST['format']

        # compare_data {'compare_result':{}}
        compare_data = json.loads(request.POST['compare_data'])
        compare_result = compare_data['compare_result']

        # tempfn has .tsv or .jsonld extension from validation step
        tempfn = compare_data['tempfn']
        filename_new = compare_data['filename_new']
        dsfobj_cur = ds.files.all().order_by('-rev')[0]
        rev_cur = dsfobj_cur.rev

        # rename file if already exists in user area
        if Path('media/' + filename_new).exists():
            fn = os.path.splitext(filename_new)
            filename_new = fn[0] + '_' + tempfn[-11:-4] + fn[1]

        # user said go...copy tempfn to media/{user} folder
        filepath = 'media/' + filename_new
        copyfile(tempfn, filepath)

        # and create new DatasetFile instance
        DatasetFile.objects.create(
            dataset_id=ds,
            file=filename_new,
            rev=rev_cur + 1,
            format=file_format,
            # TODO: accept csv, track delimiter
            upload_date=datetime.date.today(),
            header=compare_result['header_new'],
            numrows=compare_result['count_new']
        )

        # (re-)open files as panda dataframes; a = current, b = new
        # test files
        # cur: user_whgadmin/diamonds135.tsv
        # new: user_whgadmin/diamonds135_rev2.tsv
        if file_format == 'delimited':
            print('DEBUG: in ds_update: delimited')
            adf = pd.read_csv('media/' + compare_data['filename_cur'], delimiter='\t',
                              dtype={'id': 'str', 'ccodes': 'str'})
            bdf = pd.read_csv(filepath, delimiter='\t')
            bdf = bdf.astype({"id": str, "ccodes": str})
            ids_a = adf['id'].tolist()
            ids_b = bdf['id'].tolist()
            delete_srcids = [str(x) for x in (set(ids_a) - set(ids_b))]
            replace_srcids = set.intersection(set(ids_b), set(ids_a))

            # CURRENT
            places = Place.objects.filter(dataset=ds.label)
            # Place.id lists
            rows_delete = list(places.filter(src_id__in=delete_srcids).values_list('id', flat=True))
            rows_replace = list(places.filter(src_id__in=replace_srcids).values_list('id', flat=True))
            # rows_add = list(places.filter(src_id__in=compare_result['rows_add']).values_list('id',flat=True))

            # delete places with ids missing in new data (CASCADE includes links & geoms)
            places.filter(id__in=rows_delete).delete()

            # delete related instances for the rest (except links and geoms)
            PlaceName.objects.filter(place_id__in=places).delete()
            PlaceType.objects.filter(place_id__in=places).delete()
            PlaceWhen.objects.filter(place_id__in=places).delete()
            PlaceRelated.objects.filter(place_id__in=places).delete()
            PlaceDescription.objects.filter(place_id__in=places).delete()
            PlaceDepiction.objects.filter(place_id__in=places).delete()

            count_updated, count_new = [0, 0]
            # update remaining place instances w/data from new file
            # AND add new
            place_fields = {'id', 'title', 'ccodes', 'start', 'end'}
            for index, row in bdf.iterrows():
                # make 3 dicts: all; for Places; for PlaceXxxxs
                rd = row.to_dict()
                # rdp = {key:rd[key][0] for key in place_fields}
                rdp = {key: rd[key] for key in place_fields}
                p = places.filter(src_id=rdp['id']).first()
                start = int(rdp['start']) if 'start' in rdp else None
                end = int(rdp['end']) if 'end' in rdp and str(rdp['end']) != 'nan' else start
                minmax_new = [start, end] if start else [None]
                if p != None:
                    # place exists, update it
                    count_updated += 1
                    p.title = rdp['title']
                    p.ccodes = [] if str(rdp['ccodes']) == 'nan' else rdp['ccodes'].replace(' ', '').split(';')
                    p.minmax = minmax_new
                    p.timespans = [minmax_new]
                    p.save()
                    pobj = p
                else:
                    # entirely new place + related records
                    count_new += 1
                    newpl = Place.objects.create(
                        src_id=rdp['id'],
                        title=re.sub('\(.*?\)', '', rdp['title']),
                        ccodes=[] if str(rdp['ccodes']) == 'nan' else rdp['ccodes'].replace(' ', '').split(';'),
                        dataset=ds,
                        minmax=minmax_new,
                        timespans=[minmax_new]
                    )
                    newpl.save()
                    pobj = newpl

                # TODO: needs to update, not add
                # create related records (place_name, etc)
                # pobj is either a current (now updated) place or entirely new
                # rd is row dict
                update_rels_tsv(pobj, rd)

            # update numrows
            ds.numrows = ds.places.count()
            ds.save()

            # initiate a result object
            result = {"status": "updated", "update_count": count_updated,
                      "new_count": count_new, "del_count": len(rows_delete), "newfile": filepath,
                      "format": file_format}
            #
            # if dataset is indexed, update it there too
            # TODO: if new records, new recon task & accessioning tasks needed
            if compare_data['count_indexed'] > 0:
                from elasticsearch7 import Elasticsearch
                es = Elasticsearch([{'host': '0.0.0.0',
                                     'port': 9200,
                                     'api_key': (settings.ES_APIKEY_ID, settings.ES_APIKEY_KEY),
                                     'timeout': 30,
                                     'max_retries': 10,
                                     'retry_on_timeout': True}])

                idx = 'whg'

                result["indexed"] = True

                # surgically remove as req.
                if len(rows_delete) > 0:
                    deleteFromIndex(es, idx, rows_delete)

                # update others
                if len(rows_replace) > 0:
                    replaceInIndex(es, idx, rows_replace)

            # write log entry
            Log.objects.create(
                # category, logtype, "timestamp", subtype, note, dataset_id, user_id
                category='dataset',
                logtype='ds_update',
                note=json.dumps(compare_result),
                dataset_id=dsid,
                user_id=request.user.id
            )

            return JsonResponse(result, safe=False)
        elif file_format == 'lpf':
            ...


"""
ds_compare()
validates dataset update file & compares w/existing
called by ajax function from modal button
returns json result object
"""


def ds_compare(request):
    if request.method == 'POST':
        dsid = request.POST['dsid']  # 586 for diamonds
        user = request.user.username
        format = request.POST['format']
        ds = get_object_or_404(Dataset, id=dsid)

        # {idxcount, submissions[{task_id,date}]}
        ds_status = ds.status_idx

        # how many exist, whether from recon or original?
        count_geoms = PlaceGeom.objects.filter(place_id__in=ds.placeids).count()
        count_links = PlaceLink.objects.filter(place_id__in=ds.placeids).count()

        # wrangling names
        # current (previous) file
        file_cur = ds.files.all().order_by('-rev')[0].file
        filename_cur = file_cur.name

        # new file
        file_new = request.FILES['file']
        tempf, tempfn = tempfile.mkstemp()

        # write new file as temporary to /var/folders/../...
        try:
            for chunk in file_new.chunks():
                os.write(tempf, chunk)
        except:
            raise Exception("Problem with the input file %s" % request.FILES['file'])
        finally:
            os.close(tempf)

        # format validation
        if format == 'delimited':
            # goodtable wants filename only
            # returns [x['message'] for x in errors]
            vresult = validate_tsv(tempfn, 'coll')
        elif format == 'lpf':
            # TODO: feed tempfn only?
            # TODO: accept json-lines; only FeatureCollections ('coll') now
            vresult = validate_lpf(tempfn, 'coll')

        # if errors, parse & return to modal
        # which expects {validation_result{errors['','']}}
        if len(vresult['errors']) > 0:
            errormsg = {"failed": {
                "errors": vresult['errors']
            }}
            return JsonResponse(errormsg, safe=False)

        # give new file a path
        filename_new = 'user_' + user + '/' + file_new.name
        # temp files were given extensions in validation functions
        tempfn_new = tempfn + '.tsv' if format == 'delimited' else tempfn + '.jsonld'

        # begin report
        comparison = {
            "id": dsid,
            "filename_cur": filename_cur,
            "filename_new": filename_new,
            "format": format,
            "validation_result": vresult,
            "tempfn": tempfn_new,
            "count_links": count_links,
            "count_geoms": count_geoms,
            "count_indexed": ds_status['idxcount'],
        }
        # perform comparison
        fn_a = 'media/' + filename_cur
        fn_b = tempfn_new
        if format == 'delimited':
            adf = pd.read_csv(fn_a, delimiter='\t')
            bdf = pd.read_csv(fn_b, delimiter='\t')
            ids_a = adf['id'].tolist()
            ids_b = bdf['id'].tolist()
            # new or removed columns?
            cols_del = list(set(adf.columns) - set(bdf.columns))
            cols_add = list(set(bdf.columns) - set(adf.columns))

            comparison['compare_result'] = {
                "count_new": len(ids_b),
                'count_diff': len(ids_b) - len(ids_a),
                'count_replace': len(set.intersection(set(ids_b), set(ids_a))),
                'cols_del': cols_del,
                'cols_add': cols_add,
                'header_new': vresult['columns'],
                'rows_add': [str(x) for x in (set(ids_b) - set(ids_a))],
                'rows_del': [str(x) for x in (set(ids_a) - set(ids_b))]
            }
        # TODO: process LP format, collections + json-lines
        elif format == 'lpf':
            comparison['compare_result'] = "it's lpf...tougher row to hoe"

        # back to calling modal
        return JsonResponse(comparison, safe=False)


""" 
  ds_insert_lpf
  insert LPF into database
"""


def ds_insert_lpf(request, pk):
    import json
    [countrows, countlinked, total_links] = [0, 0, 0]
    ds = get_object_or_404(Dataset, id=pk)
    user = request.user
    # latest file
    dsf = ds.files.all().order_by('-rev')[0]
    uribase = ds.uri_base

    # TODO: lpf can get big; support json-lines

    # insert only if empty
    dbcount = Place.objects.filter(dataset=ds.label).count()

    if dbcount == 0:
        errors = []
        try:
            infile = dsf.file.open(mode="r")
            with infile:
                jdata = json.loads(infile.read())

                for feat in jdata['features']:
                    # create Place, save to get id, then build associated records for each
                    objs = {"PlaceNames": [], "PlaceTypes": [], "PlaceGeoms": [], "PlaceWhens": [],
                            "PlaceLinks": [], "PlaceRelated": [], "PlaceDescriptions": [],
                            "PlaceDepictions": []}
                    countrows += 1

                    # build attributes for new Place instance
                    title = re.sub('\(.*?\)', '', feat['properties']['title'])

                    # geometry
                    geojson = feat['geometry'] if 'geometry' in feat.keys() else None

                    # ccodes
                    if 'ccodes' not in feat['properties'].keys():
                        if geojson:
                            # a GeometryCollection
                            ccodes = ccodesFromGeom(geojson)
                        else:
                            ccodes = []
                    else:
                        ccodes = feat['properties']['ccodes']

                    # temporal
                    # send entire feat for time summary
                    # (minmax and intervals[])
                    datesobj = parsedates_lpf(feat)

                    # TODO: compute fclasses
                    try:
                        newpl = Place(
                            # strip uribase from @id
                            src_id=feat['@id'] if uribase in ['', None] else feat['@id'].replace(uribase, ''),
                            dataset=ds,
                            title=title,
                            ccodes=ccodes,
                            minmax=datesobj['minmax'],
                            timespans=datesobj['intervals']
                        )
                        newpl.save()
                    except Exception as e:
                        capture_exception(e)
                        raise ValueError(e)

                    # PlaceName: place,src_id,toponym,task_id,
                    # jsonb:{toponym, lang, citation[{label, year, @id}], when{timespans, ...}}
                    # TODO: adjust for 'ethnic', 'demonym'
                    for n in feat['names']:
                        if 'toponym' in n.keys():
                            # if comma-separated listed, get first
                            objs['PlaceNames'].append(PlaceName(
                                place=newpl,
                                src_id=newpl.src_id,
                                toponym=n['toponym'].split(', ')[0],
                                jsonb=n
                            ))

                    # PlaceType: place,src_id,task_id,jsonb:{identifier,label,src_label}
                    if 'types' in feat.keys():
                        fclass_list = []
                        for t in feat['types']:
                            if 'identifier' in t.keys() and t['identifier'][:4] == 'aat:' \
                                    and int(t['identifier'][4:]) in Type.objects.values_list('aat_id', flat=True):
                                fc = get_object_or_404(Type, aat_id=int(t['identifier'][4:])).fclass \
                                    if t['identifier'][:4] == 'aat:' else None
                                fclass_list.append(fc)
                            else:
                                fc = FEATURE_CLASSES[3][0]
                            objs['PlaceTypes'].append(PlaceType(
                                place=newpl,
                                src_id=newpl.src_id,
                                jsonb=t,
                                fclass=fc
                            ))
                        newpl.fclasses = fclass_list
                        newpl.save()

                    # PlaceWhen: place,src_id,task_id,minmax,jsonb:{timespans[],periods[],label,duration}
                    if 'when' in feat.keys() and feat['when'] != {}:
                        objs['PlaceWhens'].append(PlaceWhen(
                            place=newpl,
                            src_id=newpl.src_id,
                            jsonb=feat['when'],
                            minmax=newpl.minmax
                        ))

                    # PlaceGeom: place,src_id,task_id,jsonb:{type,coordinates[],when{},geo_wkt,src}
                    # if 'geometry' in feat.keys() and feat['geometry']['type']=='GeometryCollection':
                    if geojson and geojson['type'] == 'GeometryCollection':
                        # for g in feat['geometry']['geometries']:
                        for g in geojson['geometries']:
                            objs['PlaceGeoms'].append(PlaceGeom(
                                place=newpl,
                                src_id=newpl.src_id,
                                jsonb=g
                                , geom=GEOSGeometry(json.dumps(g))
                            ))
                    elif geojson:
                        objs['PlaceGeoms'].append(PlaceGeom(
                            place=newpl,
                            src_id=newpl.src_id,
                            jsonb=geojson
                            , geom=GEOSGeometry(json.dumps(geojson))
                        ))

                    # PlaceLink: place,src_id,task_id,jsonb:{type,identifier}
                    if 'links' in feat.keys() and len(feat['links']) > 0:
                        countlinked += 1  # record has *any* links
                        for l in feat['links']:
                            total_links += 1  # record has n links
                            objs['PlaceLinks'].append(PlaceLink(
                                place=newpl,
                                src_id=newpl.src_id,
                                # alias uri base for known authorities
                                jsonb={"type": l['type'], "identifier": aliasIt(l['identifier'].rstrip('/'))}
                            ))

                    # PlaceRelated: place,src_id,task_id,jsonb{relationType,relationTo,label,when{}}
                    if 'relations' in feat.keys():
                        for r in feat['relations']:
                            objs['PlaceRelated'].append(PlaceRelated(
                                place=newpl, src_id=newpl.src_id, jsonb=r))

                    # PlaceDescription: place,src_id,task_id,jsonb{@id,value,lang}
                    if 'descriptions' in feat.keys():
                        for des in feat['descriptions']:
                            objs['PlaceDescriptions'].append(PlaceDescription(
                                place=newpl, src_id=newpl.src_id, jsonb=des))

                    # PlaceDepiction: place,src_id,task_id,jsonb{@id,title,license}
                    if 'depictions' in feat.keys():
                        for dep in feat['depictions']:
                            objs['PlaceDepictions'].append(PlaceDepiction(
                                place=newpl, src_id=newpl.src_id, jsonb=dep))

                    # throw errors into user message
                    def raiser(model, e):
                        errors.append({"field": model, "error": e})
                        raise DataError

                    # create related objects
                    try:
                        PlaceName.objects.bulk_create(objs['PlaceNames'])
                    except DataError as e:
                        raiser('Name', e)

                    try:
                        PlaceType.objects.bulk_create(objs['PlaceTypes'])
                    except DataError as e:
                        raiser('Type', e)

                    try:
                        PlaceWhen.objects.bulk_create(objs['PlaceWhens'])
                    except DataError as e:
                        raiser('When', e)

                    try:
                        PlaceGeom.objects.bulk_create(objs['PlaceGeoms'])
                    except DataError as e:
                        raiser('Geom', e)

                    try:
                        PlaceLink.objects.bulk_create(objs['PlaceLinks'])
                    except DataError as e:
                        raiser('Link', e)

                    try:
                        PlaceRelated.objects.bulk_create(objs['PlaceRelated'])
                    except DataError as e:
                        raiser('Related', e)

                    try:
                        PlaceDescription.objects.bulk_create(objs['PlaceDescriptions'])
                    except DataError as e:
                        raiser('Description', e)

                    try:
                        PlaceDepiction.objects.bulk_create(objs['PlaceDepictions'])
                    except DataError as e:
                        raiser('Depiction', e)

                    # TODO: compute newpl.ccodes (if geom), newpl.fclasses, newpl.minmax
                    # something failed in *any* Place creation; delete dataset

                infile.close()

            return ({"numrows": countrows,
                     "numlinked": countlinked,
                     "total_links": total_links})
        except Exception as e:
            capture_exception(e)
            # drop the (empty) database
            # ds.delete()
            # email to user, admin
            subj = 'World Historical Gazetteer error followup'
            msg = 'Hello ' + user.username + ', \n\nWe see your recent upload for the ' + ds.label + \
                  ' dataset failed, very sorry about that!' + \
                  '\nThe likely cause was: ' + str(errors) + '\n\n' + \
                  "If you can, fix the cause. If not, please respond to this email and we will get back to you soon.\n\nRegards,\nThe WHG Team"
            emailer(subj, msg, 'whg@kgeographer.org', [user.email, 'whgadmin@kgeographer.com'])

            # return message to 500.html
            # messages.error(request, "Database insert failed, but we don't know why. The WHG team has been notified and will follow up by email to <b>"+user.username+'</b> ('+user.email+')')
            # return redirect(request.GET.get('from'))
            raise ValueError(e)

    else:
        messages.add_message(request, messages.INFO, 'data is uploaded, but problem displaying dataset page')
        return redirect('/mydata')


"""
  ds_insert_tsv(pk)
  insert tsv into database
  file is validated, dataset exists
  if insert fails anywhere, delete dataset + any related objects
"""


def ds_insert_tsv(request, pk):
    import csv, re
    csv.field_size_limit(300000)
    ds = get_object_or_404(Dataset, id=pk)
    user = request.user
    # retrieve just-added file
    dsf = ds.files.all().order_by('-rev')[0]

    # insert only if empty
    dbcount = Place.objects.filter(dataset=ds.label).count()

    if dbcount == 0:
        try:
            infile = dsf.file.open(mode="r")
            reader = csv.reader(infile, delimiter=dsf.delimiter)

            infile.seek(0)
            header = next(reader, None)
            header = [col.lower().strip() for col in header]

            # strip BOM character if exists
            header[0] = header[0][1:] if '\ufeff' in header[0] else header[0]

            objs = {"PlaceName": [], "PlaceType": [], "PlaceGeom": [], "PlaceWhen": [],
                    "PlaceLink": [], "PlaceRelated": [], "PlaceDescription": []}

            # TODO: what if simultaneous inserts?
            countrows = 0
            countlinked = 0
            total_links = 0
            for r in reader:
                # build attributes for new Place instance
                src_id = r[header.index('id')]
                title = r[header.index('title')].replace("' ", "'")  # why?
                # strip anything in parens for title only
                title = re.sub('\(.*?\)', '', title)
                title_source = r[header.index('title_source')]
                title_uri = r[header.index('title_uri')] if 'title_uri' in header else ''
                ccodes = r[header.index('ccodes')] if 'ccodes' in header else []
                variants = [x.strip() for x in r[header.index('variants')].split(';')] \
                    if 'variants' in header and r[header.index('variants')] != '' else []
                types = [x.strip() for x in r[header.index('types')].split(';')] \
                    if 'types' in header else []
                aat_types = [x.strip() for x in r[header.index('aat_types')].split(';')] \
                    if 'aat_types' in header else []
                parent_name = r[header.index('parent_name')] if 'parent_name' in header else ''
                parent_id = r[header.index('parent_id')] if 'parent_id' in header else ''
                coords = makeCoords(r[header.index('lon')], r[header.index('lat')]) \
                    if 'lon' in header and 'lat' in header else None
                geowkt = r[header.index('geowkt')] if 'geowkt' in header else None
                geojson = None  # zero it out

                # make Point geometry from lon/lat if there
                if coords and len(coords) == 2:
                    geojson = {"type": "Point", "coordinates": coords,
                               "geowkt": 'POINT(' + str(coords[0]) + ' ' + str(coords[1]) + ')'}
                # else make geometry (any) w/Shapely if geowkt
                if geowkt and geowkt not in ['', None]:
                    geojson = parse_wkt(r[header.index('geowkt')])

                # ccodes; compute if missing and there is geometry
                if len(ccodes) == 0:
                    if geojson:
                        ccodes = ccodesFromGeom(geojson)
                    else:
                        ccodes = []
                else:
                    ccodes = [x.strip().upper() for x in r[header.index('ccodes')].split(';')]
                # TODO: assign aliases if wd, tgn, pl, bnf, gn, viaf
                matches = [aliasIt(x.strip()) for x in r[header.index('matches')].split(';')] \
                    if 'matches' in header and r[header.index('matches')] != '' else []

                start = r[header.index('start')] if 'start' in header else None
                # validate_tsv() ensures there is always a start
                has_end = 'end' in header and r[header.index('end')] != ''
                end = r[header.index('end')] if has_end else start

                datesobj = parsedates_tsv(start, end)
                # returns {timespans:[{}],minmax[]}

                if 'description' in header:
                    description = r[header.index('description')]
                    print("[DEBUG] description: " + description)
                else:
                    description = ''

                # create new Place object
                # TODO: generate fclasses
                newpl = Place(
                    src_id=src_id,
                    dataset=ds,
                    title=title,
                    ccodes=ccodes,
                    minmax=datesobj['minmax'],
                    timespans=[datesobj['minmax']]  # list of lists
                )
                newpl.save()
                countrows += 1
                objs['PlaceName'].append(
                    PlaceName(
                        place=newpl,
                        src_id=src_id,
                        toponym=title,
                        jsonb={"toponym": title, "citations": [{"id": title_uri, "label": title_source}]}
                    ))

                # variants if any; assume same source as title toponym
                if len(variants) > 0:
                    for v in variants:
                        try:
                            haslang = re.search("@(.*)$", v.strip())
                            if len(v.strip()) > 200:
                                pass
                            else:
                                new_name = PlaceName(
                                    place=newpl,
                                    src_id=src_id,
                                    toponym=v.strip(),
                                    jsonb={"toponym": v.strip(), "citations": [{"id": "", "label": title_source}]}
                                )
                                if haslang:
                                    new_name.jsonb['lang'] = haslang.group(1)

                                objs['PlaceName'].append(new_name)
                        except Exception as e:
                            raise ValueError(e)

                #
                # PlaceType()
                #
                if len(types) > 0:
                    fclass_list = []
                    for i, t in enumerate(types):
                        aatnum = 'aat:' + aat_types[i] if len(aat_types) >= len(types) and aat_types[i] != '' else None
                        if aatnum:
                            fclass_list.append(get_object_or_404(Type, aat_id=int(aatnum[4:])).fclass)
                        objs['PlaceType'].append(
                            PlaceType(
                                place=newpl,
                                src_id=src_id,
                                jsonb={"identifier": aatnum if aatnum else '',
                                       "sourceLabel": t,
                                       "label": aat_lookup(int(aatnum[4:])) if aatnum else ''
                                       }
                            ))
                    newpl.fclasses = fclass_list
                    newpl.save()

                if geojson:
                    objs['PlaceGeom'].append(
                        PlaceGeom(
                            place=newpl,
                            src_id=src_id,
                            jsonb=geojson
                            , geom=GEOSGeometry(json.dumps(geojson))
                        ))

                # PlaceWhen()
                # via parsedates_tsv(): {"timespans":[{start{}, end{}}]}
                if start != '':
                    objs['PlaceWhen'].append(
                        PlaceWhen(
                            place=newpl,
                            src_id=src_id,
                            # jsonb=datesobj['timespans']
                            jsonb=datesobj
                        ))

                # PlaceLink() - all are closeMatch
                if len(matches) > 0:
                    countlinked += 1
                    for m in matches:
                        total_links += 1
                        objs['PlaceLink'].append(
                            PlaceLink(
                                place=newpl,
                                src_id=src_id,
                                jsonb={"type": "closeMatch", "identifier": m}
                            ))

                # PlaceRelated()
                if parent_name != '':
                    objs['PlaceRelated'].append(
                        PlaceRelated(
                            place=newpl,
                            src_id=src_id,
                            jsonb={
                                "relationType": "gvp:broaderPartitive",
                                "relationTo": parent_id,
                                "label": parent_name}
                        ))

                # PlaceDescription()
                # @id, value, lang
                if description != '':
                    print("[DEBUG] description appended to PlaceDescription: " + description)
                    objs['PlaceDescription'].append(
                        PlaceDescription(
                            place=newpl,
                            src_id=src_id,
                            jsonb={
                                "value": description
                            }
                        ))

            PlaceName.objects.bulk_create(objs['PlaceName'], batch_size=10000)

            PlaceType.objects.bulk_create(objs['PlaceType'], batch_size=10000)

            PlaceGeom.objects.bulk_create(objs['PlaceGeom'], batch_size=10000)

            PlaceLink.objects.bulk_create(objs['PlaceLink'], batch_size=10000)

            PlaceRelated.objects.bulk_create(objs['PlaceRelated'], batch_size=10000)

            PlaceDescription.objects.bulk_create(objs['PlaceDescription'], batch_size=10000)

            infile.close()

        except Exception as e:
            # drop the (empty) dataset if insert wasn't complete
            capture_exception(e)
            ds.delete()
            # email to user, admin
            subj = 'World Historical Gazetteer error followup'
            msg = 'Hello ' + user.username + ', \n\nWe see your recent upload for the ' + ds.label + ' dataset failed, very sorry about that! We will look into why and get back to you within a day.\n\nRegards,\nThe WHG Team'
            emailer(subj, msg, 'whg@kgeographer.org', [user.email, 'mehdie.org@gmail.com'])

            # return message to 500.html
            messages.error(request,
                           "Database insert failed, but we don't know why. The WHG team has been notified and will follow up by email to <b>" + user.username + '</b> (' + user.email + ')')
            raise ValueError(e)
    else:
        messages.add_message(request, messages.INFO, 'data is uploaded, but problem displaying dataset page')
        return redirect('/mydata')

    return ({"numrows": countrows,
             "numlinked": countlinked,
             "total_links": total_links})


"""
  DataListsView()
  Returns lists for various data types
"""


class DataListsView(LoginRequiredMixin, ListView):
    login_url = '/accounts/login/'
    redirect_field_name = 'redirect_to'

    # templates per list type
    template_d = 'datasets/data_datasets.html'
    template_c = 'datasets/data_collections.html'
    template_a = 'datasets/data_areas.html'
    template_r = 'datasets/data_resources.html'

    # which template to use?
    def get_template_names(self, *args, **kwargs):
        if self.request.path == reverse('data-datasets'):
            return [self.template_d]
        elif self.request.path == reverse('data-collections'):
            return [self.template_c]
        elif self.request.path == reverse('data-areas'):
            return [self.template_a]
        else:
            return [self.template_r]

    def get_queryset(self, **kwargs):
        me = self.request.user
        whgteam = me.is_superuser or 'whg_team' in [g.name for g in me.groups.all()]
        teaching = 'teaching' in [g.name for g in me.groups.all()]

        if self.request.path == reverse('data-datasets'):
            idlist = [obj.id for obj in Dataset.objects.all() if me in obj.owners or
                      me in obj.collaborators or me.is_superuser]
            dslist = Dataset.objects.filter(id__in=idlist).order_by('-create_date')
            return dslist
        elif self.request.path == reverse('data-collections'):
            list = Collection.objects.all().order_by('created') if whgteam \
                else Collection.objects.filter(owner=me).order_by('created')
            return list
        elif self.request.path == reverse('data-areas'):
            study_areas = ['ccodes', 'copied', 'drawn']  # only user study areas
            list = Area.objects.all().filter(type__in=study_areas).order_by('-id') if whgteam else \
                Area.objects.all().filter(type__in=study_areas, owner=me).order_by('-id')
            return list
        else:
            list = Resource.objects.all().order_by('create_date') if whgteam or teaching \
                else Resource.objects.all().filter(owner=me).order_by('created')
            return list

    def get_context_data(self, *args, **kwargs):
        me = self.request.user
        context = super(DataListsView, self).get_context_data(*args, **kwargs)

        context['viewable'] = ['uploaded', 'inserted', 'reconciling', 'review_hits', 'reviewed', 'review_whg',
                               'indexed']
        context['beta_or_better'] = True if me.groups.filter(name__in=['beta', 'admins', 'whg_team']).exists() \
            else False
        context['whgteam'] = True if me.groups.filter(name__in=['admins', 'whg_team']).exists() else False
        # TODO: assign users to 'teacher' group
        context['teacher'] = True if self.request.user.groups.filter(name__in=['teacher']).exists() else False
        return context


"""
  PublicListView()
  list public datasets and collections
"""


class PublicListsView(ListView):
    redirect_field_name = 'redirect_to'

    context_object_name = 'dataset_list'
    template_name = 'datasets/public_list.html'
    model = Dataset

    def get_queryset(self):
        # original qs
        qs = super().get_queryset()
        return qs.filter(public=True).order_by('core', 'title')

    def get_context_data(self, *args, **kwargs):
        context = super(PublicListsView, self).get_context_data(*args, **kwargs)

        context['coll_list'] = Collection.objects.filter(public=True).order_by('created')
        context['viewable'] = ['uploaded', 'inserted', 'reconciling', 'review_hits', 'reviewed', 'review_whg',
                               'indexed']

        context['beta_or_better'] = True if self.request.user.groups.filter(
            name__in=['beta', 'admins']).exists() else False
        return context


def failed_upload_notification(user, tempfn):
    subj = 'World Historical Gazetteer error followup'
    msg = 'Hello ' + user.username + \
          ', \n\nWe see your recent upload failed -- very sorry about that!' + \
          'We will look into why and get back to you within a day.\n\nRegards,\nThe WHG Team\n\n\n[' + tempfn + ']'
    emailer(subj, msg, 'whg@kgeographer.org',
            [user.email, 'mehdie.org@gmail.com'])


"""
  DatasetCreateView()
  initial create
  upload file, validate format, create DatasetFile instance,
  redirect to dataset.html for db insert if context['format_ok']
"""


class DatasetCreateView(LoginRequiredMixin, CreateView):
    login_url = '/accounts/login/'
    redirect_field_name = 'redirect_to'

    form_class = DatasetCreateModelForm
    template_name = 'datasets/dataset_create.html'
    success_message = 'dataset created'

    def form_invalid(self, form):
        context = {'form': form}
        return self.render_to_response(context=context)

    @transaction.atomic()
    def form_valid(self, form):
        data = form.cleaned_data
        context = {"format": data['format']}
        user = self.request.user
        file = self.request.FILES['file']
        filename = file.name
        mimetype = file.content_type

        newfn, newtempfn = ['', '']

        # open & write tempf to a temp location;
        # call it tempfn for reference
        tempf, tempfn = tempfile.mkstemp()
        try:
            for chunk in data['file'].chunks():
                os.write(tempf, chunk)
        except Exception as e:
            capture_exception(e)
            raise Exception("Problem with the input file %s" % self.request.FILES['file'])
        finally:
            os.close(tempf)

        valid_mime = mimetype in mthash_plus.mimetypes

        if not valid_mime:
            context['errors'] = "Not a valid file type; must be one of [.csv, .tsv, .xlsx, .ods, .json]"
            return self.render_to_response(self.get_context_data(form=form, context=context))

        ext = mthash_plus.mimetypes[mimetype]
        fail_msg = "A database insert failed and we aren't sure why. The WHG team has been notified " + \
                   "and will follow up by email to <b>" + user.username + "</b> (" + user.email + ")"

        # this validates per row and always gets a result, even if errors
        if ext == 'json':
            try:
                result = validate_lpf(tempfn, 'coll')
            except Exception as e:
                capture_exception(e)
                failed_upload_notification(user, tempfn)
                messages.error(self.request, fail_msg)
                # return HttpResponseServerError()
                raise e

        elif ext in ['csv', 'tsv']:
            try:
                newfn = tempfn + '.' + ext
                os.rename(tempfn, newfn)
                result = validate_tsv(newfn, ext)
            except Exception as e:
                capture_exception(e)
                failed_upload_notification(user, tempfn)
                messages.error(self.request, fail_msg)
                # return HttpResponseServerError()
                raise e
        elif ext in ['xlsx', 'ods']:
            try:
                import pandas as pd

                # open new file for tsv write
                newfn = tempfn + '.tsv'
                with codecs.open(newfn, 'w', encoding='utf8') as file_out:
                    # add ext to tempfn (pandas need this)
                    newtempfn = tempfn + '.' + ext
                    os.rename(tempfn, newtempfn)

                    # dataframe from spreadsheet
                    df = pd.read_excel(newtempfn, converters={
                        'id': str, 'start': str, 'end': str,
                        'aat_types': str, 'lon': float, 'lat': float})

                    # write it as tsv
                    table = df.to_csv(sep='\t', index=False).replace('\nan', '')
                    file_out.write(table)

                result = validate_tsv(newfn, 'tsv')
            except Exception as e:
                capture_exception(e)
                failed_upload_notification(user, newfn)
                messages.error(self.request, "Database insert failed and we aren't sure why. " +
                               "The WHG team has been notified and will follow up by email to <b>" +
                               user.username + '</b> (' + user.email + ')')
                raise e

        if len(result['errors']) == 0:
            context['status'] = 'format_ok'

            # new Dataset record ('owner','id','label','title','description')
            dsobj = form.save(commit=False)
            dsobj.ds_status = 'format_ok'
            dsobj.numrows = result['count']
            if not form.cleaned_data['uri_base']:
                dsobj.uri_base = 'https://whgazetteer.org/api/db/?id='

            # links will be counted later on insert
            dsobj.numlinked = 0
            dsobj.total_links = 0
            try:
                dsobj.save()
            except Exception as e:
                capture_exception(e)
                return render(self.request, 'datasets/dataset_create.html', self.args)

            # create user directory if necessary
            user_dir = r'media/user_' + user.username + '/'
            if not Path(user_dir).exists():
                os.makedirs(user_dir)

            # build path, and rename file if already exists in user area
            file_exists = Path(user_dir + filename).exists()
            if not file_exists:
                filepath = user_dir + filename
            else:
                splitty = filename.split('.')
                filename = splitty[0] + '_' + tempfn[-7:] + '.' + splitty[1]
                filepath = user_dir + filename

            # write log entry
            Log.objects.create(
                category='dataset',
                logtype='ds_create',
                subtype=data['datatype'],
                dataset_id=dsobj.id,
                user_id=user.id
            )

            # write request obj file to user directory
            if ext in ['csv', 'tsv', 'json']:
                with codecs.open(filepath, 'w', 'utf8') as file_out:
                    try:
                        for chunk in file.chunks():
                            file_out.write(chunk.decode("utf-8"))
                    except Exception as e:
                        capture_exception(e)
                        raise e

            # if spreadsheet, copy newfn (tsv conversion)
            if ext in ['xlsx', 'ods']:
                shutil.copy(newfn, filepath + '.tsv')

            # create initial DatasetFile record
            DatasetFile.objects.create(
                dataset_id=dsobj,
                # uploaded valid file as is
                file=filepath[6:] + '.tsv' if ext in ['xlsx', 'ods'] else filepath[6:],
                rev=1,
                format=result['format'],
                delimiter='\t' if ext in ['tsv', 'xlsx', 'ods'] else ',' if ext == 'csv' else 'n/a',
                df_status='format_ok',
                upload_date=None,
                header=result['columns'] if "columns" in result.keys() else [],
                numrows=result['count']
            )

            return redirect('/datasets/' + str(dsobj.id) + '/summary')

        else:
            context['action'] = 'errors'
            context['format'] = result['format']
            context['errors'] = parse_errors_lpf(result['errors']) \
                if ext == 'json' else parse_errors_tsv(result['errors'])
            context['columns'] = result['columns'] \
                if ext != 'json' else []

            return self.render_to_response(
                self.get_context_data(
                    form=form, context=context
                ))


"""
  returns public dataset 'mets' (summary) page
"""


class DatasetPublicView(DetailView):
    template_name = 'datasets/ds_meta.html'

    model = Dataset

    def get_context_data(self, **kwargs):
        context = super(DatasetPublicView, self).get_context_data(**kwargs)

        ds = get_object_or_404(Dataset, id=self.kwargs['pk'])
        file = ds.file

        placeset = ds.places.all()

        if file.file:
            context['current_file'] = file
            context['format'] = file.format
            context['numrows'] = file.numrows
            context['filesize'] = round(file.file.size / 1000000, 1)

            context['links_added'] = PlaceLink.objects.filter(
                place_id__in=placeset, task_id__contains='-').count()
            context['geoms_added'] = PlaceGeom.objects.filter(
                place_id__in=placeset, task_id__contains='-').count()
        return context


"""
  loads page for confirm ok on delete
    - delete dataset, with CASCADE to DatasetFile, places, place_name, etc
    - also deletes from index if indexed (fails silently if not)
    - also removes dataset_file records
"""


# TODO: delete other stuff: disk files; archive??
class DatasetDeleteView(DeleteView):
    template_name = 'datasets/dataset_delete.html'

    def delete_complete(self):
        ds = get_object_or_404(Dataset, pk=self.kwargs.get("id"))
        dataset_file_delete(ds)
        if ds.ds_status == 'indexed':
            pids = list(ds.placeids)
            removePlacesFromIndex(es, 'whg', pids)

    def get_object(self):
        id_ = self.kwargs.get("id")
        ds = get_object_or_404(Dataset, id=id_)
        return (ds)

    def get_context_data(self, **kwargs):
        context = super(DatasetDeleteView, self).get_context_data(**kwargs)
        ds = get_object_or_404(Dataset, id=self.kwargs.get("id"))
        context['owners'] = ds.owners
        return context

    def get_success_url(self):
        self.delete_complete()
        return reverse('data-datasets')


"""
  fetch places in specified dataset
  utility used for place collections
"""


def ds_list(request, label):
    qs = Place.objects.all().filter(dataset=label)
    geoms = []
    for p in qs.all():
        feat = {"type": "Feature",
                "properties": {"src_id": p.src_id, "name": p.title},
                "geometry": p.geoms.first().jsonb}
        geoms.append(feat)
    return JsonResponse(geoms, safe=False)


"""
  undo last review match action
  - delete any geoms or links created
  - reset flags for hit.reviewed and place.review_xxx
"""


def match_undo(request, ds, tid, pid):
    geom_matches = PlaceGeom.objects.all().filter(task_id=tid, place_id=pid)
    link_matches = PlaceLink.objects.all().filter(task_id=tid, place_id=pid)
    geom_matches.delete()
    link_matches.delete()

    # reset place.review_xxx to 0
    tasktype = TaskResult.objects.get(task_id=tid).task_name[6:]
    place = Place.objects.get(pk=pid)
    # remove any defer comments
    place.defer_comments.delete()
    # TODO: variable field name?
    if tasktype.startswith('wd'):
        place.review_wd = 0
    elif tasktype == 'tgn':
        place.review_tgn = 0
    else:
        place.review_whg = 0

    # match task_id, place_id in hits; set reviewed = false
    Hit.objects.filter(task_id=tid, place_id=pid).update(reviewed=False)

    return HttpResponseRedirect(request.META.get('HTTP_REFERER'))


"""
  returns dataset owner metadata page
"""


class DatasetSummaryView(LoginRequiredMixin, UpdateView):
    login_url = '/accounts/login/'
    redirect_field_name = 'redirect_to'

    form_class = DatasetDetailModelForm

    template_name = 'datasets/ds_summary.html'

    # Dataset has been edited, form submitted
    def form_valid(self, form):
        data = form.cleaned_data
        ds = get_object_or_404(Dataset, pk=self.kwargs.get("id"))
        if data["file"] == None:
            ds.title = data['title']
            ds.description = data['description']
            ds.uri_base = data['uri_base']
            ds.save()
        return super().form_valid(form)

    def form_invalid(self, form):
        context = {}
        context['errors'] = form.errors
        return super().form_invalid(form)

    def get_object(self):
        id_ = self.kwargs.get("id")
        return get_object_or_404(Dataset, id=id_)

    def get_context_data(self, *args, **kwargs):
        context = super(DatasetSummaryView, self).get_context_data(*args, **kwargs)
        id_ = self.kwargs.get("id")
        ds = get_object_or_404(Dataset, id=id_)

        """
      when coming from DatasetCreateView() (file.df_status == format_ok)
      runs ds_insert_tsv() or ds_insert_lpf()
      using most recent dataset file
    """
        file = ds.file
        if file.df_status == 'format_ok':
            if file.format == 'delimited':
                result = ds_insert_tsv(self.request, id_)
            else:
                result = ds_insert_lpf(self.request, id_)
            ds.numrows = result.get('numrows')
            ds.numlinked = result.get('numlinked')
            ds.total_links = result.get('total_links')
            ds.ds_status = 'uploaded'
            file.df_status = 'uploaded'
            file.numrows = result.get('numrows')
            ds.save()
            file.save()

        # build context for rendering ds_summary.html
        me = self.request.user
        placeset = ds.places.all()

        context['updates'] = {}
        context['ds'] = ds
        context['collaborators'] = ds.collaborators.all()
        context['owners'] = ds.owners

        # excludes datasets uploaded directly (1 & 2)
        if file.file:
            context['current_file'] = file
            context['format'] = file.format
            context['numrows'] = file.numrows
            context['filesize'] = round(file.file.size / 1000000, 1)

        # initial (non-task)
        context['num_names'] = PlaceName.objects.filter(place_id__in=placeset).count()
        context['num_links'] = PlaceLink.objects.filter(
            place_id__in=placeset, task_id=None).count()
        context['num_geoms'] = PlaceGeom.objects.filter(
            place_id__in=placeset, task_id=None).count()

        # augmentations (has task_id)
        context['links_added'] = PlaceLink.objects.filter(
            place_id__in=placeset, task_id__contains='-').count()
        context['geoms_added'] = PlaceGeom.objects.filter(
            place_id__in=placeset, task_id__contains='-').count()

        context['beta_or_better'] = True if self.request.user.groups.filter(
            name__in=['beta', 'admins']).exists() else False
        return context


""" 
  returns dataset owner browse table 
"""


class DatasetBrowseView(LoginRequiredMixin, DetailView):
    login_url = '/accounts/login/'
    redirect_field_name = 'redirect_to'

    model = Dataset
    template_name = 'datasets/ds_browse.html'

    def get_success_url(self):
        id_ = self.kwargs.get("id")
        return '/datasets/' + str(id_) + '/browse'

    def get_object(self):
        id_ = self.kwargs.get("id")
        return get_object_or_404(Dataset, id=id_)

    def get_context_data(self, *args, **kwargs):
        context = super(DatasetBrowseView, self).get_context_data(*args, **kwargs)
        context['mbtokenkg'] = settings.MAPBOX_TOKEN_KG
        context['mbtokenmb'] = settings.MAPBOX_TOKEN_MB

        id_ = self.kwargs.get("id")

        ds = get_object_or_404(Dataset, id=id_)
        ds_tasks = [t.task_name[6:] for t in ds.tasks.filter(status='SUCCESS')]

        context['collaborators'] = ds.collaborators.all()
        context['owners'] = ds.owners
        context['updates'] = {}
        context['ds'] = ds
        context['tgntask'] = 'tgn' in ds_tasks
        context['whgtask'] = len(set(['whg', 'idx']) & set(ds_tasks)) > 0
        context['wdtask'] = len(set(['wd', 'wdlocal']) & set(ds_tasks)) > 0
        context['beta_or_better'] = True if self.request.user.groups.filter(
            name__in=['beta', 'admins']).exists() else False

        return context


""" 
  returns public dataset browse table 
"""


class DatasetPlacesView(DetailView):
    login_url = '/accounts/login/'
    redirect_field_name = 'redirect_to'

    model = Dataset
    template_name = 'datasets/ds_places.html'

    def get_object(self):
        id_ = self.kwargs.get("id")
        return get_object_or_404(Dataset, id=id_)

    def get_context_data(self, *args, **kwargs):
        context = super(DatasetPlacesView, self).get_context_data(*args, **kwargs)
        context['mbtokenkg'] = settings.MAPBOX_TOKEN_KG
        context['mbtokenmb'] = settings.MAPBOX_TOKEN_MB

        id_ = self.kwargs.get("id")

        ds = get_object_or_404(Dataset, id=id_)
        me = self.request.user

        if not me.is_anonymous:
            context['collections'] = Collection.objects.filter(owner=me, collection_class='place')

        context['loggedin'] = 'true' if not me.is_anonymous else 'false'

        context['updates'] = {}
        context['ds'] = ds
        context['beta_or_better'] = True if self.request.user.groups.filter(
            name__in=['beta', 'admins']).exists() else False

        return context


"""
  returns dataset owner "Linking" tab listing reconciliation tasks
"""


class DatasetReconcileView(LoginRequiredMixin, DetailView):
    login_url = '/accounts/login/'
    redirect_field_name = 'redirect_to'

    model = Dataset
    template_name = 'datasets/ds_reconcile.html'

    def get_success_url(self):
        id_ = self.kwargs.get("id")
        return '/datasets/' + str(id_) + '/reconcile'

    def get_object(self):
        id_ = self.kwargs.get("id")
        return get_object_or_404(Dataset, id=id_)

    def get_context_data(self, *args, **kwargs):
        context = super(DatasetReconcileView, self).get_context_data(*args, **kwargs)

        id_ = self.kwargs.get("id")
        ds = get_object_or_404(Dataset, id=id_)

        # omits FAILURE and ARCHIVED
        ds_tasks = ds.tasks.filter(status='SUCCESS')
        context['ds'] = ds
        context['tasks'] = ds_tasks
        context['collaborators'] = ds.collaborators.all()
        context['owners'] = ds.owners

        context['beta_or_better'] = True if self.request.user.groups.filter(
            name__in=['beta', 'admins']).exists() else False

        return context


"""
  returns dataset owner "Collaborators" tab
"""


class DatasetCollabView(LoginRequiredMixin, DetailView):
    login_url = '/accounts/login/'
    redirect_field_name = 'redirect_to'

    model = DatasetUser
    template_name = 'datasets/ds_collab.html'

    def get_success_url(self):
        id_ = self.kwargs.get("id")
        return '/datasets/' + str(id_) + '/collab'

    def get_object(self):
        id_ = self.kwargs.get("id")
        return get_object_or_404(Dataset, id=id_)

    def get_context_data(self, *args, **kwargs):
        context = super(DatasetCollabView, self).get_context_data(*args, **kwargs)

        id_ = self.kwargs.get("id")
        ds = get_object_or_404(Dataset, id=id_)

        context['ds'] = ds

        context['collabs'] = ds.collabs.all()
        context['collaborators'] = ds.collaborators.all()
        context['owners'] = ds.owners

        context['beta_or_better'] = True if self.request.user.groups.filter(
            name__in=['beta', 'admins']).exists() else False

        return context


""" 
  returns add (reconciliation) task page 
"""


class DatasetAddTaskView(LoginRequiredMixin, DetailView):
    login_url = '/accounts/login/'
    redirect_field_name = 'redirect_to'

    model = Dataset
    template_name = 'datasets/ds_addtask.html'

    def get_success_url(self):
        id_ = self.kwargs.get("id")
        return '/datasets/' + str(id_) + '/log'

    def get_object(self):
        id_ = self.kwargs.get("id")
        return get_object_or_404(Dataset, id=id_)

    def get_context_data(self, *args, **kwargs):
        context = super(DatasetAddTaskView, self).get_context_data(*args, **kwargs)
        """ maps need these """
        context['mbtokenkg'] = settings.MAPBOX_TOKEN_KG
        context['mbtokenmb'] = settings.MAPBOX_TOKEN_MB

        id_ = self.kwargs.get("id")
        ds = get_object_or_404(Dataset, id=id_)

        # build context for rendering ds_addtask.html
        me = self.request.user
        area_types = ['ccodes', 'copied', 'drawn']

        # user study areas
        userareas = Area.objects.all().filter(type__in=area_types).values('id', 'title').order_by('-created')
        context['area_list'] = userareas if me.username == 'whgadmin' else userareas.filter(owner=me)

        # pre-defined UN regions
        predefined = Area.objects.all().filter(type='predefined').values('id', 'title')

        my_dataset = Dataset.objects.filter(owner=self.request.user).exclude(id=id_)
        public_dataset = Dataset.objects.filter(public=True).exclude(owner=self.request.user)

        gothits = {}
        for t in ds.tasks.filter(status='SUCCESS'):
            gothits[t.task_id] = int(json.loads(t.result)['got_hits'])

        # deliver status messae(s) to template
        msg_unreviewed = """There is a <span class='strong'>%s</span> task in progress, 
      and all %s records that got hits remain unreviewed. <span class='text-danger strong'>Starting this new task 
      will delete the existing one</span>, with no impact on your dataset."""
        msg_inprogress = """<p class='mb-1'>There is a <span class='strong'>%s</span> task in progress, 
      and %s of the %s records that had hits have been reviewed. <span class='text-danger strong'>Starting this new task 
      will archive the existing task and submit only unreviewed records.</span>. 
      If you proceed, you can keep or delete prior match results (links and/or geometry):</p>"""
        msg_done = """All records have been submitted for reconciliation to %s and reviewed. 
      To begin the step of accessioning to the WHG index, please <a href="%s">contact our editorial team</a>"""
        for i in ds.taskstats.items():
            auth = i[0][6:]
            if len(i[1]) > 0:  # there's a SUCCESS task
                tid = i[1][0]['tid']
                remaining = i[1][0]['total']
                hadhits = gothits[tid]
                reviewed = hadhits - remaining
                if remaining == 0:
                    context['msg_' + auth] = {
                        'msg': msg_done % (auth, "/contact"),
                        'type': 'done'}
                elif remaining < hadhits:
                    context['msg_' + auth] = {
                        'msg': msg_inprogress % (auth, reviewed, hadhits),
                        'type': 'inprogress'}
                else:
                    context['msg_' + auth] = {
                        'msg': msg_unreviewed % (auth, hadhits),
                        'type': 'unreviewed'
                    }
            else:
                context['msg_' + auth] = {
                    'msg': "no tasks of this type",
                    'type': 'none'
                }

        active_tasks = dict(filter(lambda elem: len(elem[1]) > 0, ds.taskstats.items()))
        remaining = {}
        for t in active_tasks.items():
            remaining[t[0][6:]] = t[1][0]['total']
        context['public_dataset'] = public_dataset
        context['my_dataset'] = my_dataset
        context['region_list'] = predefined
        context['ds'] = ds
        context['collaborators'] = ds.collabs.all()
        context['owners'] = ds.owners
        context['remain_to_review'] = remaining
        context['beta_or_better'] = True if self.request.user.groups.filter(
            name__in=['beta', 'admins']).exists() else False

        return context


"""
  returns dataset owner "Log & Comments" tab
"""


class DatasetLogView(LoginRequiredMixin, DetailView):
    login_url = '/accounts/login/'
    redirect_field_name = 'redirect_to'

    model = Dataset
    template_name = 'datasets/ds_log.html'

    def get_success_url(self):
        id_ = self.kwargs.get("id")
        return '/datasets/' + str(id_) + '/log'

    def get_object(self):
        id_ = self.kwargs.get("id")
        return get_object_or_404(Dataset, id=id_)

    def get_context_data(self, *args, **kwargs):
        context = super(DatasetLogView, self).get_context_data(*args, **kwargs)

        id_ = self.kwargs.get("id")
        ds = get_object_or_404(Dataset, id=id_)

        context['ds'] = ds
        context['log'] = ds.log.filter(category='dataset').order_by('-timestamp')
        context['comments'] = Comment.objects.filter(place_id__dataset=ds).order_by('-created')
        context['beta_or_better'] = True if self.request.user.groups.filter(
            name__in=['beta', 'admins']).exists() else False

        return context
