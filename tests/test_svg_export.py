import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from services.svg_export import write_svg


class SvgExportTests(unittest.TestCase):
    def test_branch_labels_have_separate_readable_positions(self):
        page = ET.Element('diagram', name='Branches')
        for identity, x, y in (('source', 40, 40), ('a', 40, 240),
                                ('b', 500, 240), ('c', 960, 240)):
            node = ET.SubElement(page, 'mxCell', id=identity, vertex='1', value=identity)
            ET.SubElement(node, 'mxGeometry', x=str(x), y=str(y), width='400', height='80')
        for identity in ('a', 'b', 'c'):
            ET.SubElement(page, 'mxCell', edge='1', source='source', target=identity,
                          value=f'equals "Message_{identity}"')
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'overview.svg'
            write_svg(page, destination)
            root = ET.parse(destination).getroot()
        ns = {'s': 'http://www.w3.org/2000/svg'}
        labels = [item for item in root.findall('.//s:text', ns)
                  if item.get('x') is not None]
        self.assertEqual(len(labels), 3)
        rectangles = []
        for label in labels:
            x, y = float(label.get('x')), float(label.get('y'))
            rectangles.append((x - 4, y - 14, len(label.text) * 7 + 8, 22))
        for index, (x, y, width, height) in enumerate(rectangles):
            for ox, oy, ow, oh in rectangles[index + 1:]:
                self.assertTrue(x + width <= ox or ox + ow <= x
                                or y + height <= oy or oy + oh <= y,
                                'Connector labels overlap')

    def test_labels_are_text_not_executable_markup_and_keep_full_hover_text(self):
        page = ET.Element('diagram', name='Overview')
        label = '<script>alert("not executable")</script> & $body\n' + 'destination/' * 20
        node = ET.SubElement(page, 'mxCell', id='n1', vertex='1', value=label)
        ET.SubElement(node, 'mxGeometry', x='40', y='40', width='400', height='300')
        edge = ET.SubElement(page, 'mxCell', id='e1', edge='1', source='n1', target='n1',
                             value='fault <condition>', style='dashed=1;')
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'overview.svg'
            write_svg(page, destination)
            root = ET.parse(destination).getroot()
        ns = {'s': 'http://www.w3.org/2000/svg'}
        self.assertIn(label, [title.text for title in root.findall('.//s:title', ns)])
        self.assertFalse(root.findall('.//s:script', ns))
        self.assertTrue(root.findall('.//s:polyline[@stroke-dasharray]', ns))
        self.assertGreater(len(root.findall('.//s:tspan', ns)), 2)
        self.assertTrue(root.get('viewBox'))

    def test_invalid_geometry_or_edges_fail_explicitly(self):
        page = ET.Element('diagram')
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'invalid.svg'
            with self.assertRaisesRegex(ValueError, 'empty'):
                write_svg(page, destination)
            node = ET.SubElement(page, 'mxCell', id='n1', vertex='1')
            with self.assertRaisesRegex(ValueError, 'geometry'):
                write_svg(page, destination)
            ET.SubElement(node, 'mxGeometry', x='40', y='40', width='400', height='70')
            ET.SubElement(page, 'mxCell', edge='1', source='n1', target='missing')
            with self.assertRaisesRegex(ValueError, 'endpoint'):
                write_svg(page, destination)


if __name__ == '__main__':
    unittest.main()
