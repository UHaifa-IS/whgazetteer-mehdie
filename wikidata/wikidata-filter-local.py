#!/usr/bin/python
# wikidata-filter-local.py rev. 20210304
# further filters a dump filtered for P625 (coordinates)
# to retain 3141 item types in'goodqs_fclasses_3141.json'
# (up from 447)

import codecs, os, sys, time
import simplejson as json
from datetime import date
fn = sys.argv[1]
start = time.time()
today = date.today().strftime("%Y%m%d")
#wd='./'
wd='/Volumes/Backup Plus/whg misc/'
#curdir = os.listdir('.')
curdir = os.listdir(wd)
fin = wd + fn
print(fin)
# in: p625_20210303.jsonl; out: idx_p625_20210303_filtered.jsonl
if os.path.isfile(fin):
  fnout = wd + 'idx_'+fn.split('.')[0] + '_filtered.jsonl'
  fout = codecs.open(fnout,'w',encoding='utf8')
  qs = json.loads(codecs.open(wd + 'goodqs_fclasses_3141.json','r').read())
  count = 0
  with open(fin) as f:
    for line in f:
      row=json.loads(line)
      # 
      if 'P31' in row['claims']:
        # is it one of the types in goodqs?
        if len(set(qs.keys()) & set(row['claims']['P31'])) > 0:
          count +=1
          fout.write(line)
          #print(row['claims']['P31'])
  fout.close()
  print(str(count)+' written to '+fnout)
  print("--- %s seconds ---" % (time.time() - start))  
else:
  print('file ' + fin + ' does not exist')

