import socket
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from services.pipeline_flow_service import (
    PipelineFlow,
    Resource,
    ResourceIndex,
    discover_projects,
    generate_flows,
    overview_flow,
    write_diagram,
)


def service_xml(pipeline, provider='jms', binding='Any SOAP', queue='true'):
    return f'''<proxyServiceEntry>
      <coreEntry><binding type="{binding}"/><invoke ref="{pipeline}"/></coreEntry>
      <endpointConfig><provider-id>{provider}</provider-id>
        <URI><value>{provider}://example.invalid/messages</value></URI>
        <provider-specific><is-queue>{queue}</is-queue></provider-specific>
      </endpointConfig>
    </proxyServiceEntry>'''


class PipelineFlowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.repository = self.directory / 'example'
        self.repository.mkdir(parents=True)
        (self.repository / 'example.jpr').touch()

    def write(self, name, xml):
        destination = self.repository / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(xml)

    def graph(self):
        return PipelineFlow(ResourceIndex([self.repository])).build(self.repository)

    def test_routing_conditions_transforms_endpoints_and_error_paths(self):
        self.write('Dispatcher.proxy', service_xml('example/Dispatcher'))
        self.write('Target.bix', service_xml('', provider='https'))
        self.write('Unknown.proxy', service_xml('example/Unknown', provider='local'))
        self.write('Unknown.pipeline', '''<pipelineEntry><router><pipeline name="request" type="request">
          <stage name="Reject"><actions><Error><errCode>UnknownMessage</errCode></Error></actions></stage>
          </pipeline><flow><pipeline-node><request>request</request></pipeline-node></flow>
          </router></pipelineEntry>''')
        self.write('Dispatcher.pipeline', '''<pipelineEntry xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
          <router errorHandler="main-error">
            <pipeline name="main-error" type="error"><stage name="Handle error">
              <actions><log><expr><xqueryText>$fault</xqueryText></expr></log><reply isError="true"/></actions>
            </stage></pipeline>
            <pipeline name="request" type="request"><stage name="Validate" errorHandler="validation-error"><actions>
              <validate><schema ref="common/Header"/><varName>header</varName></validate>
              <assign varName="message"><expr><xqueryTransform><resource ref="common/Transform"/>
                <param name="input"><path>$body</path></param></xqueryTransform></expr></assign>
            </actions></stage></pipeline>
            <pipeline name="validation-error" type="error"><stage name="Validation failed"><actions>
              <Error><errCode>InvalidHeader</errCode></Error>
            </actions></stage></pipeline>
            <pipeline name="response" type="response"><stage name="Respond"><actions>
              <log><expr><xqueryText>$body</xqueryText></expr></log>
            </actions></stage></pipeline>
            <flow><pipeline-node><request>request</request><response>response</response></pipeline-node>
              <route-node name="Route"><actions><routingTable>
                <expression><xqueryText>$header/MessageType</xqueryText></expression>
                <case><operator>equals</operator><value>"Invoice"</value><route>
                  <service ref="example/Target" xsi:type="ref:BusinessServiceRef"/>
                  <outboundTransform><assign varName="outbound"><expr><xqueryText>$message</xqueryText></expr></assign></outboundTransform>
                </route></case>
                <defaultCase><route><service ref="example/Unknown" xsi:type="ref:ProxyRef"/></route></defaultCase>
              </routingTable></actions></route-node>
            </flow>
          </router></pipelineEntry>''')
        graph = self.graph()
        labels = '\n'.join(node.label for node in graph.nodes)
        for expected in (
            'JMS queue', 'SOAP over HTTP(S)', '$header/MessageType', 'equals "Invoice"',
            'Otherwise (no earlier match)', 'common/Transform', 'input', '$body',
            'request stage: Validate', 'response stage: Respond', 'Error', 'UnknownMessage',
        ):
            self.assertIn(expected, labels)
        self.assertNotIn('Unsupported action:', labels)
        validation = next(node for node in graph.nodes if node.label.startswith('validate\n'))
        validation_handler = next(node for node in graph.nodes if node.label.startswith('Error handler: validation-error'))
        self.assertIn((validation.id, validation_handler.id),
                      [(edge.source, edge.target) for edge in graph.edges if edge.kind == 'error'])
        raised = next(node for node in graph.nodes if 'InvalidHeader' in node.label)
        main_handler = next(node for node in graph.nodes if node.label.startswith('Error handler: main-error'))
        self.assertTrue(any(edge.source == raised.id and edge.target == main_handler.id
                            for edge in graph.edges))
        self.assertFalse(any(edge.source == raised.id and edge.kind == 'normal'
                             for edge in graph.edges))
        outbound = next(node for node in graph.nodes if 'varName=outbound' in node.label)
        send = next(node for node in graph.nodes if node.label == 'Send: example/Target')
        self.assertTrue(any(edge.source == outbound.id and edge.target == send.id for edge in graph.edges))
        self.assertEqual(sum(node.label.startswith('Proxy: example/Unknown\n') for node in graph.nodes), 1)

        destination = self.directory / 'flow.drawio'
        write_diagram(graph, destination)
        document = ET.parse(destination)
        self.assertEqual(
            [page.get('name') for page in document.findall('diagram')],
            ['Overview', 'Pipeline flow'],
        )
        root = document.find("diagram[@name='Pipeline flow']")
        vertices = [cell for cell in root.iter('mxCell') if cell.get('vertex') == '1']
        self.assertEqual(len(vertices), len(graph.nodes))
        ids = {cell.get('id') for cell in vertices}
        rectangles = []
        for vertex in vertices:
            geometry = vertex.find('mxGeometry')
            rectangles.append(tuple(float(geometry.get(key)) for key in ('x', 'y', 'width', 'height')))
        for index, (x, y, width, height) in enumerate(rectangles):
            for other_x, other_y, other_width, other_height in rectangles[index + 1:]:
                self.assertTrue(x + width <= other_x or other_x + other_width <= x
                                or y + height <= other_y or other_y + other_height <= y)
        for edge in root.iter('mxCell'):
            if edge.get('edge') == '1':
                self.assertIn(edge.get('source'), ids)
                self.assertIn(edge.get('target'), ids)
        overview = document.find("diagram[@name='Overview']")
        overview_labels = '\n'.join(cell.get('value', '') for cell in overview.iter('mxCell'))
        self.assertIn('$header/MessageType', overview_labels)
        self.assertIn('equals "Invoice"', overview_labels)
        self.assertIn('Otherwise (no earlier match)', overview_labels)
        self.assertIn('JMS queue', overview_labels)
        self.assertIn('SOAP over HTTP(S)', overview_labels)
        self.assertNotIn('varName=message', overview_labels)
        self.assertNotIn('varName=outbound', overview_labels)
        summary = overview_flow(graph)
        self.assertLess(len(summary.nodes), len(graph.nodes))
        summary_ids = {node.id for node in summary.nodes}
        self.assertTrue(all(edge.source in summary_ids and edge.target in summary_ids
                            for edge in summary.edges))
        self.assertTrue(any(edge.kind == 'error' for edge in summary.edges))
        namespace = {'s': 'http://www.w3.org/2000/svg'}
        for page_name in ('overview', 'pipeline-flow'):
            svg = ET.parse(destination.parent / f'{page_name}.svg')
            page = document.find(f"diagram[@id='{page_name}']")
            self.assertEqual(
                len(svg.findall('.//s:polyline', namespace)),
                sum(cell.get('edge') == '1' for cell in page.iter('mxCell')),
            )
            self.assertTrue(svg.findall('.//s:tspan', namespace))
            self.assertFalse(svg.findall('.//s:script', namespace))
            self.assertFalse(svg.findall('.//s:image', namespace))

    def test_transport_annotations_do_not_guess(self):
        for provider, binding, queue, expected in (
            ('jms', 'Any SOAP', 'true', 'JMS queue'),
            ('jms', 'Any SOAP', 'false', 'JMS topic'),
            ('jms', 'Any SOAP', '', 'queue/topic unresolved'),
            ('http', 'SOAP', '', 'SOAP over HTTP(S)'),
            ('http', 'REST', '', 'REST over HTTP(S)'),
            ('http', 'Mixed', '', 'SOAP/REST unresolved'),
            ('local', '', '', 'local'),
        ):
            with self.subTest(provider=provider, binding=binding, queue=queue):
                resource = Resource('test', Path('test.proxy'), ET.fromstring(
                    service_xml('', provider, binding, queue)
                ))
                self.assertIn(expected, resource.endpoint())

    def test_unresolved_dynamic_calls_and_unknown_actions_are_visible(self):
        self.write('Dispatcher.proxy', service_xml('example/Dispatcher'))
        self.write('Dispatcher.pipeline', '''<pipelineEntry><router>
          <pipeline type="request" name="request"><stage name="Actions"><actions>
            <customAction><expression>runtime-code</expression></customAction>
            <wsCallout><service ref="missing/Target"/></wsCallout>
            <route><dynamic-service><xqueryText>$destination</xqueryText></dynamic-service></route>
          </actions></stage></pipeline>
          <flow><pipeline-node><request>request</request></pipeline-node></flow>
          </router></pipelineEntry>''')
        with self.assertLogs('services.pipeline_flow_service', level='WARNING'):
            graph = self.graph()
        labels = '\n'.join(node.label for node in graph.nodes)
        self.assertIn('Unsupported action: customAction', labels)
        self.assertIn('Unresolved service boundary', labels)
        self.assertIn('Dynamic target: unresolved', labels)
        self.assertIn('$destination', labels)
        self.assertIn('Transport / endpoint: unknown (not inferred)', labels)

    def test_recursion_is_bounded_and_if_has_an_implicit_false_path(self):
        self.write('Dispatcher.proxy', service_xml('example/Dispatcher'))
        self.write('Dispatcher.pipeline', '''<pipelineEntry xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><router>
          <pipeline type="request" name="request"><stage><actions>
            <ifThenElse><case><condition><xqueryText>$enabled</xqueryText></condition><actions>
              <wsCallout><service ref="example/Dispatcher" xsi:type="ref:ProxyRef"/></wsCallout>
            </actions></case></ifThenElse>
            <reply/>
            <assign varName="unreachable"/>
          </actions></stage></pipeline>
          <flow><pipeline-node><request>request</request></pipeline-node></flow>
          </router></pipelineEntry>''')
        graph = self.graph()
        labels = '\n'.join(node.label for node in graph.nodes)
        self.assertIn('Recursive service reference:', labels)
        self.assertIn('Otherwise: continue', labels)
        self.assertNotIn('unreachable', labels)
        self.assertLess(len(graph.nodes), 20)

    def test_generation_is_local_and_writes_diagrams(self):
        self.write('Dispatcher.proxy', service_xml('example/Dispatcher'))
        self.write('Dispatcher.pipeline', '<pipelineEntry><router><flow/></router></pipelineEntry>')
        with patch.object(socket, 'socket', side_effect=AssertionError('Network access forbidden')):
            outputs = generate_flows(self.directory, self.directory / 'output')
        self.assertEqual(outputs, [self.directory / 'output' / 'example' / 'example.drawio'])
        self.assertTrue(outputs[0].is_file())
        self.assertTrue((outputs[0].parent / 'overview.svg').is_file())
        self.assertTrue((outputs[0].parent / 'pipeline-flow.svg').is_file())
    def test_local_cross_project_call_and_reply_return_to_caller(self):
        self.write('Dispatcher.proxy', service_xml('example/Dispatcher'))
        self.write('Dispatcher.pipeline', '''<pipelineEntry xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><router>
          <pipeline type="request" name="request"><stage><actions>
            <wsCallout><service ref="other/Target" xsi:type="ref:ProxyRef"/></wsCallout>
            <assign varName="afterCall"><expr><xqueryText>$result</xqueryText></expr></assign>
          </actions></stage></pipeline>
          <flow><pipeline-node><request>request</request></pipeline-node></flow>
          </router></pipelineEntry>''')
        other = self.repository.parent / 'other'
        other.mkdir()
        (other / 'Target.proxy').write_text(service_xml('other/Target', 'jms', queue='false'))
        (other / 'Target.pipeline').write_text('''<pipelineEntry><router>
          <pipeline name="request" type="request"><stage><actions><reply isError="false"/></actions></stage></pipeline>
          <flow><pipeline-node><request>request</request></pipeline-node></flow>
          </router></pipelineEntry>''')
        graph = PipelineFlow(ResourceIndex([self.repository, other])).build(self.repository)
        reply = next(node for node in graph.nodes if node.label.startswith('reply\n'))
        after_call = next(node for node in graph.nodes if 'varName=afterCall' in node.label)
        self.assertTrue(any(edge.source == reply.id and edge.target == after_call.id for edge in graph.edges))
        self.assertTrue(any('JMS topic' in node.label and 'other/Target' in node.label for node in graph.nodes))

    def test_malformed_xml_fails_explicitly(self):
        self.write('Broken.proxy', '<broken>')
        with self.assertRaises(ET.ParseError):
            self.graph()

    def test_root_discovers_nested_jpr_projects_and_indexes_shared_resources(self):
        root = self.directory / 'workspace'
        project = root / 'group' / 'dispatcher'
        project.mkdir(parents=True)
        (project / 'dispatcher.jpr').write_text('<project/>')
        (project / 'other.jpr').write_text('<project/>')
        (project / 'Dispatcher.proxy').write_text(service_xml('dispatcher/Dispatcher'))
        (project / 'Dispatcher.pipeline').write_text('''<pipelineEntry
          xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><router>
          <pipeline name="request" type="request"><stage><actions>
          <wsCallout><service ref="shared/Target" xsi:type="ref:BusinessServiceRef"/></wsCallout>
          </actions></stage></pipeline>
          <flow><pipeline-node><request>request</request></pipeline-node></flow>
          </router></pipelineEntry>''')
        shared = project / 'shared'
        shared.mkdir()
        (shared / 'shared.jpr').write_text('<project/>')
        (shared / 'Target.bix').write_text(service_xml('', 'jms', queue='false'))
        self.assertEqual(discover_projects(root), [project.resolve(), shared.resolve()])
        index = ResourceIndex([project, shared])
        self.assertIsNone(index.get('dispatcher/shared/Target', '.bix'))
        self.assertIsNotNone(index.get('shared/Target', '.bix'))
        with patch.object(socket, 'socket', side_effect=AssertionError('Network forbidden')):
            with self.assertLogs('services.pipeline_flow_service', level='WARNING') as logs:
                outputs = generate_flows(root, self.directory / 'output')
        self.assertTrue(any('no proxy services' in line for line in logs.output))
        self.assertEqual([path.name for path in outputs], ['dispatcher.drawio'])
        document = ET.parse(outputs[0])
        self.assertEqual([page.get('name') for page in document.findall('diagram')],
                         ['Overview', 'Pipeline flow'])
        self.assertIn('JMS topic', outputs[0].read_text())

    def test_project_discovery_rejects_missing_empty_and_duplicate_roots(self):
        with self.assertRaises(NotADirectoryError):
            discover_projects(self.directory / 'missing')
        empty_root = self.directory / 'empty'
        empty_root.mkdir()
        with self.assertRaisesRegex(ValueError, 'No .jpr'):
            discover_projects(empty_root)
        for group in ('one', 'two'):
            project = self.directory / group / 'duplicate'
            project.mkdir(parents=True)
            (project / 'duplicate.jpr').write_text('<project/>')
        with self.assertRaisesRegex(ValueError, 'Duplicate OSB project'):
            discover_projects(self.directory)


if __name__ == '__main__':
    unittest.main()
