import textwrap
import xml.etree.ElementTree as ET
from pathlib import Path


SVG_NAMESPACE = 'http://www.w3.org/2000/svg'


def svg_element(parent: ET.Element, tag: str, **attributes: str) -> ET.Element:
    return ET.SubElement(parent, f'{{{SVG_NAMESPACE}}}{tag}', attributes)


def wrapped_lines(value: str) -> list[str]:
    lines = []
    for line in value.splitlines():
        lines.extend(textwrap.wrap(line, width=58, replace_whitespace=False) or [''])
    return lines


def place_label(
    x: float,
    y: float,
    width: float,
    occupied: list[tuple[float, float, float, float]],
) -> tuple[float, float]:
    while any(
        x - 4 < ox + ow and ox < x + width + 4
        and y - 14 < oy + oh and oy < y + 8
        for ox, oy, ow, oh in occupied
    ):
        y += 26
    occupied.append((x - 4, y - 14, width + 8, 22))
    return x, y


def write_svg(page: ET.Element, destination: Path) -> None:
    """Export a generated Draw.io page without remote resources or executable markup."""
    nodes = {}
    edges = []
    for cell in page.iter('mxCell'):
        if cell.get('vertex') == '1':
            geometry = cell.find('mxGeometry')
            if geometry is None:
                raise ValueError(f'Missing node geometry: {cell.get("id")}')
            identity = cell.get('id')
            if identity is None:
                raise ValueError('SVG node is missing an ID')
            nodes[identity] = (
                cell,
                tuple(float(geometry.get(key, '0')) for key in ('x', 'y', 'width', 'height')),
            )
        elif cell.get('edge') == '1':
            edges.append(cell)
    if not nodes:
        raise ValueError('Cannot export an empty diagram page')
    right = max(x + width for _, (x, _, width, _) in nodes.values())
    bottom = max(y + height for _, (_, y, _, height) in nodes.values())
    width = right + 500 + len(edges) * 12
    canvas_height = bottom + 60
    svg = ET.Element(f'{{{SVG_NAMESPACE}}}svg', {
        'viewBox': f'0 0 {width} {bottom + 60}',
        'width': str(width), 'height': str(bottom + 60),
        'role': 'img', 'aria-label': page.get('name', 'OSB flow'),
    })
    svg_element(svg, 'title').text = page.get('name', 'OSB flow')
    svg_element(svg, 'desc').text = (
        'Local OSB configuration flow. Red dashed arrows indicate fault paths. '
        'Orange boxes indicate unresolved configuration. Full labels are available on hover.'
    )
    svg_element(svg, 'rect', width='100%', height='100%', fill='white')
    definitions = svg_element(svg, 'defs')
    for name, color in (('normal', '#666666'), ('error', '#b85450')):
        marker = svg_element(
            definitions, 'marker', id=f'arrow-{name}', markerWidth='10', markerHeight='10',
            refX='9', refY='5', orient='auto', markerUnits='userSpaceOnUse',
        )
        svg_element(marker, 'path', d='M 0 0 L 10 5 L 0 10 z', fill=color)
    occupied = [geometry for _, geometry in nodes.values()]
    labels = []
    for index, edge in enumerate(edges):
        source = edge.get('source')
        target = edge.get('target')
        if source not in nodes or target not in nodes:
            raise ValueError(f'SVG edge has an unknown endpoint: {source} -> {target}')
        _, (sx, sy, sw, sh) = nodes[source]
        _, (tx, ty, tw, th) = nodes[target]
        is_error = 'dashed=1' in edge.get('style', '')
        kind = 'error' if is_error else 'normal'
        color = '#b85450' if is_error else '#666666'
        if not is_error and ty >= sy + sh:
            start_x, start_y = sx + sw / 2, sy + sh
            end_x, end_y = tx + tw / 2, ty
            middle = (start_y + end_y) / 2
            points = [(start_x, start_y), (start_x, middle), (end_x, middle), (end_x, end_y)]
            label_x, label_y = end_x + 6, middle - 6
        else:
            lane = right + 40 + index * 12
            start_y, end_y = sy + sh / 2, ty + th / 2
            points = [(sx + sw, start_y), (lane, start_y), (lane, end_y), (tx + tw, end_y)]
            label_x, label_y = lane + 4, (start_y + end_y) / 2
        group = svg_element(svg, 'g')
        svg_element(group, 'title').text = edge.get('value', '')
        attributes = {
            'points': ' '.join(f'{x},{y}' for x, y in points),
            'fill': 'none', 'stroke': color, 'stroke-width': '1.5',
            'marker-end': f'url(#arrow-{kind})',
        }
        if is_error:
            attributes['stroke-dasharray'] = '7 5'
        svg_element(group, 'polyline', **attributes)
        label = edge.get('value', '')
        if label:
            display = textwrap.shorten(label, width=60, placeholder='...')
            label_width = len(display) * 7
            label_x, label_y = place_label(label_x, label_y, label_width, occupied)
            labels.append((label, display, label_x, label_y, label_width, color))
            width = max(width, label_x + label_width + 40)
            canvas_height = max(canvas_height, label_y + 40)
    for cell, (x, y, node_width, height) in nodes.values():
        style = dict(
            part.split('=', 1) for part in cell.get('style', '').split(';') if '=' in part
        )
        group = svg_element(svg, 'g')
        label = cell.get('value', '')
        svg_element(group, 'title').text = label
        attributes = {'fill': style.get('fillColor', '#ffffff'), 'stroke': '#666666'}
        if style.get('shape') == 'hexagon':
            svg_element(
                group, 'polygon',
                points=' '.join(f'{px},{py}' for px, py in (
                    (x + 10, y), (x + node_width - 10, y),
                    (x + node_width, y + height / 2),
                    (x + node_width - 10, y + height), (x + 10, y + height),
                    (x, y + height / 2),
                )), **attributes,
            )
        else:
            svg_element(group, 'rect', x=str(x), y=str(y), width=str(node_width),
                        height=str(height), rx='8', **attributes)
        text = svg_element(group, 'text', fill='#111111',
                           **{'font-family': 'monospace', 'font-size': '11'})
        for line_index, line in enumerate(wrapped_lines(label)):
            svg_element(text, 'tspan', x=str(x + 12), y=str(y + 24 + line_index * 18)).text = line
    for label, display, x, y, label_width, color in labels:
        group = svg_element(svg, 'g')
        svg_element(group, 'title').text = label
        svg_element(group, 'rect', x=str(x - 4), y=str(y - 14),
                    width=str(label_width + 8), height='22', rx='3',
                    fill='white', stroke='#dddddd')
        svg_element(
            group, 'text', x=str(x), y=str(y), fill=color,
            **{'font-size': '11', 'font-family': 'monospace'},
        ).text = display
    svg.set('width', str(width))
    svg.set('height', str(canvas_height))
    svg.set('viewBox', f'0 0 {width} {canvas_height}')
    destination.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(svg).write(destination, encoding='utf-8', xml_declaration=True)
