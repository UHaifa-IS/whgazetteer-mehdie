import os
import django
import unittest
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'whg.settings')
django.setup()
from datasets.utils import validate_tsv



class MyTestCase(unittest.TestCase):
    def test_validate_tsv(self):
        file_to_validate = 'datasets/tests/Mapping-person-field-types-TSV-examples-tsv.tsv'
        validate_tsv(file_to_validate)


if __name__ == '__main__':
    unittest.main()
