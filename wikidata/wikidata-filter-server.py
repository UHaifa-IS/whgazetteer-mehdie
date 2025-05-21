#!/usr/bin/python
# wikidata-filter.py rev. 20210125
# further filters a dump filtered for P625 (coordinates)
# to retain only item types inn'goodqs_fclasses_3141.json'

import codecs, os, sys, time
import simplejson as json
from datetime import date

fn = sys.argv[1]
# wd='/Users/karlg/Documents/repos/_whgdata/elastic/wikidata/'
start = time.time()
today = date.today().strftime("%Y%m%d")
wd = './'
curdir = os.listdir('.')
fin = wd + fn

if os.path.isfile(fin):
    fnout = wd + fn.split('.')[0] + '_filtered_' + today + '.jsonl'
    fout = codecs.open(fnout, 'w', encoding='utf8')
    qs = json.loads(codecs.open(wd + 'goodqs_fclasses_3141.json', 'r').read())
    count = 0
    with open(fin) as f:
        for line in f:
            row = json.loads(line)
            #
            if 'P31' in row['claims']:
                # is it one of the types in goodqs?
                if len(set(qs.keys()) & set(row['claims']['P31'])) > 0:
                    count += 1
                    fout.write(line)
                    # print(row['claims']['P31'])
    fout.close()
else:
    ...
