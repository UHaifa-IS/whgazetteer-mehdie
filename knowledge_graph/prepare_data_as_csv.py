"""
Connect to the posdgresql database at localhost:5432 and export the tables to csv files.
"""

import psycopg2
import csv
import logging

# Connect to the database
conn = psycopg2.connect(
    host="localhost",
    database="mehdie-gui",
    user="mehdie",
    password="aLSDmci4243Q")

OUT_FOLDER = 'database_dump'

def dump_table(table_name, fields: list):
    # Open a cursor to perform database operations
    cur = conn.cursor()

    # Execute a query
    cur.execute(f"SELECT {','.join(fields)} FROM {table_name}")

    # Fetch all the results
    rows = cur.fetchall()

    # Close communication with the database
    cur.close()

    # Write the results to a csv file
    with open(f'{OUT_FOLDER}/{table_name}.csv', 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(fields)  # Write the header
        for row in rows:
            # Process each field in the row
            processed_row = []
            for item in row:
                if item is None:
                    processed_row.append('')
                elif isinstance(item, str):
                    # Replace newlines with spaces in string fields
                    processed_row.append(item.replace('\n', ' ').replace('\r', ' '))
                else:
                    processed_row.append(str(item))
            writer.writerow(processed_row)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    # Dump the tables
    dump_table('datasets', ['id', 'label', 'title', 'description', 'webpage', 'uri_base', 'datatype'])
    logging.info('Dumped datasets table')
    dump_table('places', ['id', 'title', 'src_id'])
    logging.info('Dumped place table')