from pyrml import RMLConverter
import os
import logging
from rdflib import ConjunctiveGraph, Literal, Namespace
from rdflib.plugins.stores.berkeleydb import has_bsddb
from rdflib.store import NO_STORE, VALID_STORE

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    # Create an instance of the class RMLConverter.
    rml_converter = RMLConverter()

    '''
    Invoke the method convert on the instance of class RMLConverter by:
     - using the file examples/artist/artist-map.ttl (see the examples in this repo);
     - obtaining an RDF graph as output.
    '''
    rml_file_path = os.path.join('mappings', 'datasets_and_places.ttl')  # 'pyrml-example.ttl')
    rdf_graph = rml_converter.convert(rml_file_path)
    logging.info('Converted datasets table to RDF graph.')
    rdf_graph.bind('mhd', 'http://tools.mehdie.org/ns/mehdie#')
    # Print the triples contained into the RDF graph.
    # for s, p, o in rdf_graph:
    #     print(s, p, o)

    # Create new BerkelyDB store
    path = 'knowledge_graph/bdb_store'
    graph = ConjunctiveGraph("BerkeleyDB")

    # Open previously created store, or create it if it doesn't exist yet
    # (always doesn't exist in this example as using temp file location)
    rt = graph.open(path, create=False)

    if rt == NO_STORE:
        # There is no underlying BerkeleyDB infrastructure, so create it
        print("Creating new DB")
        graph.open(path, create=True)
    else:
        print("Using existing DB")
        assert rt == VALID_STORE, "The underlying store is corrupt"

    # empty store
    graph.remove((None, None, None))
    print("Triples in graph before add:", len(graph))

    # transfer triples from rdf_graph to graph
    for s, p, o in rdf_graph:
        graph.add((s, p, o))

    print("Triples in graph after add:", len(graph))

    graph.close()

    graph = None
    # rdf_graph.serialize(destination='output.ttl', format='turtle')
    logging.info('Serialized RDF graph to berkelyDB')