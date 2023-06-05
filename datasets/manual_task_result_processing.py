"""
This script takes a csv file from the result of a matching task and uploads it into MEHDIE db and GUI
"""
from datasets.tasks import align_match_data
import argparse
def process(dataset_1, dataset_2, aug_geom, language, userid, csv_url):
    align_match_data.delay(
        dataset_1,
        dataset_id=dataset_1,
        dataset_2=dataset_2,
        csv_url=csv_url,
        aug_geom=aug_geom,
        lang=language,
        user=userid,
        tsv_url=csv_url,
    )



if __name__ == '__main__':
    argparser = argparse.ArgumentParser()
    argparser.add_argument('--dataset_1', type=str, help='dataset_1')
    argparser.add_argument('--dataset_2', type=str, help='dataset_2')
    argparser.add_argument('--userid', type=str, help='userid')
    argparser.add_argument('--csv_url', type=str, help='csv_url')
    args = argparser.parse_args()
    dataset_1 = args.dataset_1
    dataset_2 = args.dataset_2
    aug_geom = None
    language = 'en'
    userid = args.userid
    csv_url = args.csv_url
    process(dataset_1, dataset_2, aug_geom, language, userid, csv_url)