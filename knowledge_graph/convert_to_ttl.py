from pyrml import RMLConverter
import os
import logging

from rdflib import Graph

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    rml_converter = RMLConverter()
    rml_file_path = os.path.join('mappings', 'datasets_and_places.ttl')
    rdf_graph = rml_converter.convert(rml_file_path)
    logging.info('Converted datasets table to RDF graph.')
    rdf_graph.bind('mhd', 'http://tools.mehdie.org/ns/mehdie#')

    # Query to select 1000 triples
    query = '''
    CONSTRUCT {
        ?s ?p ?o
    } WHERE {
        ?s ?p ?o
    }
    LIMIT 1000
    '''
    # Create a new graph with the subset of triples
    subset_graph = Graph()
    for triple in rdf_graph.query(query):
        subset_graph.add(triple)

    # Serialize the subset graph
    subset_graph.serialize(destination='output.ttl', format='turtle')
    logging.info('Serialized subset of RDF graph to output.ttl.')