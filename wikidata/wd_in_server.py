#!/usr/bin/python

# wd_.py 17 Jan 2021; rev 20 Feb 2021
# build wikidata index for reconciliation
# change wd for server

import codecs, glob, json, os, re, sys, time
from copy import deepcopy
from collections import OrderedDict
from datetime import date
from django.contrib.gis.geos import MultiPoint, Point
from elasticsearch import Elasticsearch
es = Elasticsearch([{'host': '0.0.0.0', 'port': 9200}])

wd = './'
#wd = '/Users/karlg/Documents/Repos/_whgdata/elastic/wikidata/'
os.chdir(wd)
print(glob.glob('*.jsonl'))
fn = input('file: ')
infile = wd + fn; print('infile', infile)
fn_prefix = re.search('^(.*?)_', fn).group(1); print('fn_prefix', fn_prefix)
# file exists
if os.path.isfile(infile):
  # get index
  index = input('index: ')  
else:
  print("file '"+fn+"' does not exist")
  sys.exit()
  
# got file and intended index
if es.indices.exists(index):
  q = input('Zap previous ('+index+')? [y, n] ')
  zapit = True if q == 'y' else False
  
start_time = time.time(); print('start_time', start_time)
today=date.today().strftime("%Y%m%d"); print('today', today)
#wd='./'
#curdir = os.listdir(wd); print('curdir', curdir)

errfile = fn_prefix+'_err_'+today+".txt"; print("errfile", errfile)
fout = codecs.open(wd + 'results/'+ errfile, mode='w', encoding='utf8')
print("fout", fout)

# 3141 placetypes of interest
classes = json.loads(codecs.open(wd + 'goodqs_fclasses_3141.json','r').read())
print(str(len(classes.keys())) + " classes mapped")
mappings = codecs.open(wd +'es_mappings_wd.json', 'r', 'utf8').read()

# pause it here
#sys.exit()

bad_coords=[]

def reverseLatLon(coordsarr):
  new = []
  for coords in coordsarr:
    new.append([coords[1],coords[0]])
    if coords[1] > 180:
      bad_coords += doc['id']
  return new  

def reprPoint(coordsarr):
  g = MultiPoint([Point(p[0], p[1]) for p in coordsarr ])
  repr_point = list(g.centroid.coords)
  return {"lat": repr_point[0], "lon": repr_point[1] }

def indexPlaces(idx, zapit=False): 
  if es.indices.exists(idx) and zapit:
    try:
      es.indices.delete(idx)
      print("deleted", idx)
    except Exception as ex:
      print(idx+" delete failed", ex, sys.exc_info())
  else:
    # create
    try:
      es.indices.create(index=idx, ignore=400, body=mappings)
      print("created", idx)
    except:
      print(idx+" create failed", sys.exc_info()) 
  
  print ("adding to index", idx)
  count,count_idx = [0,0]
  start = time.time()
  
  keepers = OrderedDict([('P31','type'),('P625','location'),('P17','country'),('P571','inception'),('P1319','earliest'),('P1326','latest'),('P1566','gn'),('P1584','pl'),('P244','loc'),('P214','viaf'),('P268','bnf'),('P1667','tgn'),('P2503','gov'),('P1871','cerl'),('P227','gnd')])

  with codecs.open(infile, 'r', 'utf8') as f:
    for line in f:
      try:
        #print(doc['tgnid'])
        doc=json.loads(line)

        # lose extraneous claims
        claims = dict(filter(lambda x: x[0] in keepers.keys(), doc['claims'].items()))
        
        # isolate authority ids (index >= 6)
        auth_claims = dict(filter(lambda x: x[0] in list(keepers.keys())[6:], doc['claims'].items()))        
        
        # reformat doc
        variants, timestamps, years, descrips, links, types, authids, fclasses = [[],[],[],[],[],[],[],[]]
        
        authids += ['wd:'+doc['id']]
        for c in auth_claims.items():
          authids += [keepers[c[0]]+':'+d for d in c[1]]
          #print([keepers[c[0]]+':'+d for d in c[1]])
        #print(auths)
        doc['authids'] = authids
          
        # merge labels and aliases
        # 1) seed names with aliases
        # e.g. {'de': ['römisches Theater in Lyon']. 'fr': ...}
        variants = deepcopy(doc['aliases'])

        # 2) selectively add labels
        for l in doc['labels'].items():
          if l[0] in variants and len(l[0]) == 2:
            # if lang is there, add label to list
            lst = variants[l[0]]
            lst.append(l[1]) if l[1] not in lst else lst
          elif len(l[0]) == 2:
            # add new element
            variants[l[0]] = [l[1]]
        # shape it for index
        variants = [{'lang':v[0], 'names':v[1]} for v in variants.items()]
        doc['variants'] = variants

        # don't need these in index
        del doc['labels']
        del doc['aliases']

        # any dates?
        if 'P571' in claims: timestamps += claims['P571']
        if 'P1319' in claims: timestamps += claims['P1319']
        if 'P1326' in claims: timestamps += claims['P1326']
        try:          
          if len(timestamps) > 0:          
            for t in timestamps:
              y = int(re.search("^(-?.*?)-",t).group(1))
              years.append(y)
            if len(years) > 0:
              minmax = [min(years), max(years)]
              doc['minmax'] = {"gte":minmax[0],"lte":minmax[1]} 
            else:
              doc['minmax'] = []
        except:
          print('minmax failed at',doc['id'])
          fout.write('minmax fail on: ' + line + '\n')
          pass
          #sys.exit()
        # agg descriptions > descrips[]
        for d in doc['descriptions'].items():
          descrips.append({"lang":d[0], "text":d[1]})
        doc['descriptions'] = descrips
        
        # agg sitelinks > links[]
        for l in doc['sitelinks'].items():
          #print(l[0][:2],l[1])
          links.append({"lang":l[0][:2], "title":l[1]})        
        doc['sitelinks'] = links
        
        # build type objects (P31) w/ qid, label
        # add fclasses
        for c in claims['P31']:
          if c in classes:
            #print(classes[c])
            types.append({"id":c, "label":classes[c]['label']})
            fclasses += classes[c]['fclasses'].split(';')
          else:
            types.append({"id":c, "label":""})
        doc['types'] = types
        doc['fclasses'] = fclasses
        
        if 'P625' in claims and len(claims['P625']) > 0:
          #claims['P625'] = {"type":"MultiPoint", "coordinates":reverseLatLon(claims['P625'])}
          doc['location'] = {"type":"MultiPoint", "coordinates":reverseLatLon(claims['P625'])}
          # TODO: representative_point
          doc['repr_point'] = reprPoint(claims['P625'])
          
        doc['claims'] = claims
        
        #print('doc', doc)
        es.index(index=idx, doc_type='place', id=doc['id'], body=doc)

        count +=1
        count_idx +=1
      except:
        count +=1
        print('line #',count, doc['id'])
        print("error:", sys.exc_info())
        fout.write('line #'+str(count)+': ' + line)
        #sys.exit()
  if 'test' not in infile:
    es.indices.put_alias(idx, 'wd')
  else:
    es.indices.put_alias(idx, 'wdtest')
  fout.close()
  end = time.time()
  print(int((end - start)), "seconds elapsed")
  print(str(count_idx) + ' of '+str(count)+' records indexed')
  #print(list(set(placetypes)))

indexPlaces(index)

