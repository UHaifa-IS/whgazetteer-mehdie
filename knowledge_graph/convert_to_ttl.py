from pyrml import RMLConverter
import os
import logging

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    # Create an instance of the class RMLConverter.
    rml_converter = RMLConverter()

    '''
    Invoke the method convert on the instance of class RMLConverter by:
     - using the file examples/artist/artist-map.ttl (see the examples in this repo);
     - obtaining an RDF graph as output.
    '''
    rml_file_path = os.path.join('mappings', 'datasets.ttl')  # 'pyrml-example.ttl')
    rdf_graph = rml_converter.convert(rml_file_path)
    logging.info('Converted datasets table to RDF graph.')
    rdf_graph.bind('mhd', 'http://tools.mehdie.org/ns/mehdie#')
    # Print the triples contained into the RDF graph.
    # for s, p, o in rdf_graph:
    #     print(s, p, o)

    rdf_graph.serialize(destination='output.ttl', format='turtle')
    logging.info('Serialized RDF graph to output.ttl.')