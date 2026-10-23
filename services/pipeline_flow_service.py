from __future__ import annotations

import logging
import textwrap
import xml.etree.ElementTree as ET
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from services.svg_export import write_svg


logger = logging.getLogger(__name__)
XSI_TYPE = '{http://www.w3.org/2001/XMLSchema-instance}type'
PROXY_SUFFIX = '.proxy'
PIPELINE_SUFFIX = '.pipeline'
BUSINESS_SUFFIX = '.bix'
HandlerResolver = Callable[[str | None, str | None], str | None]


def local_name(element: ET.Element) -> str:
    return element.tag.rsplit('}', 1)[-1]


def child(element: ET.Element, name: str) -> ET.Element | None:
    return next((item for item in element if local_name(item) == name), None)


def children(element: ET.Element, name: str) -> list[ET.Element]:
    return [item for item in element if local_name(item) == name]


def content(element: ET.Element | None) -> str:
    return ''.join(element.itertext()).strip() if element is not None else ''


def expression(element: ET.Element | None) -> str:
    if element is None:
        return ''
    values = []
    for item in element.iter():
        name = local_name(item)
        if name in {'xqueryText', 'xpathText', 'path'}:
            values.append(content(item))
        elif name in {'resource', 'schema'} and item.get('ref'):
            values.append(f"{name}: {item.get('ref')}")
        elif name == 'param':
            values.append(f"parameter {item.get('name', '?')}:")
    return '\n'.join(values) or content(element)


@dataclass
class Resource:
    reference: str
    path: Path
    root: ET.Element

    def endpoint(self) -> str:
        endpoint = child(self.root, 'endpointConfig')
        if endpoint is None:
            return 'Transport / endpoint: unresolved (no endpoint configuration)'
        provider = content(child(endpoint, 'provider-id')).lower()
        binding = next(iter(self.root.findall('.//{*}binding')), None)
        binding_type = binding.get('type', '') if binding is not None else ''
        uris = [
            content(value)
            for uri in children(endpoint, 'URI')
            for value in children(uri, 'value')
        ]
        if provider == 'jms':
            queue_flag = next(iter(endpoint.findall('.//{*}is-queue')), None)
            flag = content(queue_flag).lower()
            transport = {'true': 'JMS queue', 'false': 'JMS topic'}.get(
                flag, 'JMS (queue/topic unresolved)'
            )
        elif provider in {'http', 'https'}:
            if 'soap' in binding_type.lower():
                transport = 'SOAP over HTTP(S)'
            elif 'rest' in binding_type.lower() or self.root.find('.//{*}rest') is not None:
                transport = 'REST over HTTP(S)'
            else:
                transport = 'HTTP(S) (SOAP/REST unresolved)'
        else:
            transport = provider or 'Transport unresolved'
        lines = [transport]
        if binding_type:
            lines.append(f'Binding: {binding_type}')
        lines.extend(uris or ['Endpoint URI: not declared'])
        selector = next(iter(endpoint.findall('.//{*}message-selector')), None)
        if content(selector):
            lines.append(f'JMS selector: {content(selector)}')
        return '\n'.join(lines)


class ResourceIndex:
    def __init__(self, repositories: list[Path]):
        self.resources: dict[tuple[str, str], Resource] = {}
        project_roots = set(repositories)
        for repository in repositories:
            for path in sorted(repository.rglob('*')):
                if path.suffix not in {PROXY_SUFFIX, PIPELINE_SUFFIX, BUSINESS_SUFFIX}:
                    continue
                if any(parent in project_roots for parent in path.parents if parent != repository
                       and repository in parent.parents):
                    continue
                reference = f'{repository.name}/{path.relative_to(repository).with_suffix("").as_posix()}'
                key = (reference, path.suffix)
                if key in self.resources:
                    raise ValueError(f'Duplicate OSB resource: {reference}{path.suffix}')
                self.resources[key] = Resource(reference, path, ET.parse(path).getroot())

    def get(self, reference: str, suffix: str) -> Resource | None:
        return self.resources.get((reference, suffix))

    def service(self, element: ET.Element) -> Resource | None:
        reference = element.get('ref', '')
        kind = element.get(XSI_TYPE, '').rsplit(':', 1)[-1]
        suffix = {'BusinessServiceRef': BUSINESS_SUFFIX, 'BusinessRef': BUSINESS_SUFFIX,
                  'PipelineRef': PIPELINE_SUFFIX, 'ProxyRef': PROXY_SUFFIX}.get(kind)
        if suffix:
            found = self.get(reference, suffix)
            if found is not None:
                return found
        # Some exports identify a proxy as PipelineRef. Resolve only an exact path.
        matches = [
            resource for (ref, _), resource in self.resources.items()
            if ref == reference
        ]
        return matches[0] if len(matches) == 1 else None


@dataclass
class FlowNode:
    id: str
    label: str
    kind: str
    scope: str


@dataclass
class FlowEdge:
    source: str
    target: str
    label: str = ''
    kind: str = 'normal'


@dataclass
class FlowGraph:
    nodes: list[FlowNode] = field(default_factory=list)
    edges: list[FlowEdge] = field(default_factory=list)

    def node(self, label: str, kind: str, scope: str) -> str:
        identity = f'n{len(self.nodes) + 1}'
        self.nodes.append(FlowNode(identity, label, kind, scope))
        return identity

    def edge(self, source: str, target: str, label: str = '', kind: str = 'normal') -> None:
        self.edges.append(FlowEdge(source, target, label, kind))


class PipelineFlow:
    def __init__(self, index: ResourceIndex):
        self.index = index
        self.graph = FlowGraph()
        self.visited_proxies: set[str] = set()
        self.reply_returns: list[list[tuple[str, bool]]] = []

    def build(self, repository: Path) -> FlowGraph:
        proxies = [
            resource for resource in self.index.resources.values()
            if resource.path.suffix == PROXY_SUFFIX
            and resource.reference.startswith(f'{repository.name}/')
        ]
        called = {
            service.get('ref')
            for resource in self.index.resources.values()
            for service in resource.root.findall('.//{*}service')
        }
        roots = [proxy for proxy in proxies if proxy.reference not in called]
        for proxy in roots + proxies:
            if proxy.reference not in self.visited_proxies:
                self.proxy(proxy, (), None)
        if not proxies:
            raise ValueError(f'No .proxy resources found in {repository}')
        return self.graph

    def proxy(self, resource: Resource, stack: tuple[str, ...], outer_error: str | None) -> tuple[str, list[str]]:
        self.visited_proxies.add(resource.reference)
        scope = resource.reference
        entry = self.graph.node(f'Proxy: {scope}\n{resource.endpoint()}', 'service', scope)
        invoke = resource.root.find('.//{*}invoke')
        reference = invoke.get('ref', '') if invoke is not None else ''
        pipeline = self.index.get(reference, PIPELINE_SUFFIX)
        if pipeline is None:
            missing = self.graph.node(
                f'Unresolved pipeline: {reference or "not declared"}\nLocal definition unavailable',
                'unresolved', scope,
            )
            self.graph.edge(entry, missing)
            logger.warning('Unresolved pipeline reference: %s', reference)
            return entry, [missing]
        start, exits = self.pipeline(pipeline, stack + (resource.reference,), outer_error)
        self.graph.edge(entry, start, 'invoke pipeline')
        return entry, exits

    def pipeline(self, resource: Resource, stack: tuple[str, ...], outer_error: str | None) -> tuple[str, list[str]]:
        scope = resource.reference
        entry = self.graph.node(f'Pipeline: {scope}', 'stage', scope)
        router = child(resource.root, 'router')
        if router is None:
            unknown = self.graph.node('Unsupported pipeline: router not declared', 'unresolved', scope)
            self.graph.edge(entry, unknown)
            return entry, [unknown]
        self.reply_returns.append([])
        definitions = {item.get('name', ''): item for item in children(router, 'pipeline')}
        handlers = {
            name: self.graph.node(
                f'Error handler: {name}\nPipeline: {scope}', 'error', f'{scope} / error / {name}'
            )
            for name, item in definitions.items() if item.get('type') == 'error'
        }

        def handler(name: str | None, fallback: str | None) -> str | None:
            if not name:
                return fallback
            if name not in handlers:
                handlers[name] = self.graph.node(
                    f'Unresolved error handler: {name}', 'unresolved', scope
                )
                logger.warning('Unresolved error handler: %s in %s', name, scope)
            return handlers[name]

        default_error = handler(router.get('errorHandler'), outer_error)
        tails = self.flow(child(router, 'flow'), definitions, [entry], default_error, handler, scope, stack)
        if tails:
            end = self.graph.node(f'Pipeline complete: {scope}', 'end', scope)
            self.link(tails, end)
            tails = [end]
        self.error_handlers(definitions, handlers, default_error, outer_error, handler, scope, stack)
        replies = self.reply_returns.pop()
        for reply, is_error in replies:
            if not is_error:
                tails.append(reply)
            elif outer_error:
                self.graph.edge(reply, outer_error, 'error response to caller', 'error')
        return entry, tails

    def flow(
        self,
        flow: ET.Element | None,
        definitions: dict[str, ET.Element],
        tails: list[str],
        error: str | None,
        handler: HandlerResolver,
        scope: str,
        stack: tuple[str, ...],
    ) -> list[str]:
        if flow is None:
            return self.unsupported('Unsupported pipeline: flow not declared', tails, scope)
        responses = []
        for flow_node in flow:
            if not tails:
                break
            kind = local_name(flow_node)
            if kind == 'pipeline-node':
                tails = self.request(flow_node, definitions, tails, error, handler, scope, stack)
                response_name = content(child(flow_node, 'response'))
                if response_name in definitions:
                    responses.append(definitions[response_name])
            elif kind == 'route-node':
                route_error = handler(flow_node.get('error-handler'), error)
                start = self.graph.node(f'Route: {flow_node.get("name", "")}', 'stage', scope)
                self.link(tails, start)
                tails = self.actions(child(flow_node, 'actions'), [start], route_error, scope, stack)
            else:
                tails = self.unsupported(f'Unsupported flow node: {kind}', tails, scope)
        for response in reversed(responses):
            tails = self.stages(response, tails, error, handler, scope, stack)
        return tails

    def request(
        self,
        node: ET.Element,
        definitions: dict[str, ET.Element],
        tails: list[str],
        error: str | None,
        handler: HandlerResolver,
        scope: str,
        stack: tuple[str, ...],
    ) -> list[str]:
        name = content(child(node, 'request'))
        if name not in definitions:
            return self.unsupported(f'Unresolved request pipeline: {name}', tails, scope)
        return self.stages(definitions[name], tails, error, handler, scope, stack)

    def error_handlers(
        self,
        definitions: dict[str, ET.Element],
        handlers: dict[str, str],
        default_error: str | None,
        outer_error: str | None,
        handler: HandlerResolver,
        scope: str,
        stack: tuple[str, ...],
    ) -> None:
        for name, definition in definitions.items():
            if definition.get('type') != 'error':
                continue
            error_entry = handlers[name]
            error_scope = f'{scope} / error / {name}'
            # Errors in a local handler escalate to the pipeline handler, never itself.
            parent_error = outer_error if error_entry == default_error else default_error
            remaining = self.stages(definition, [error_entry], parent_error, handler, error_scope, stack)
            if remaining:
                unresolved = self.graph.node(
                    'Handler falls through: runtime continuation not resolved', 'unresolved', error_scope
                )
                self.link(remaining, unresolved)
                logger.warning('Handler continuation unresolved: %s in %s', name, scope)

    def link(self, sources: list[str], target: str, label: str = '', kind: str = 'normal') -> None:
        for source in sources:
            self.graph.edge(source, target, label, kind)

    def unsupported(self, label: str, tails: list[str], scope: str) -> list[str]:
        logger.warning('%s', label)
        node = self.graph.node(label, 'unresolved', scope)
        self.link(tails, node)
        return [node] if tails else []

    def stages(
        self,
        definition: ET.Element,
        tails: list[str],
        error: str | None,
        handler: HandlerResolver,
        scope: str,
        stack: tuple[str, ...],
    ) -> list[str]:
        for stage in children(definition, 'stage'):
            if not tails:
                break
            stage_error = handler(stage.get('errorHandler'), error)
            node = self.graph.node(
                f'{definition.get("type", "")} stage: {stage.get("name", "")}', 'stage', scope
            )
            self.link(tails, node)
            tails = self.actions(child(stage, 'actions'), [node], stage_error, scope, stack)
        return tails

    def actions(
        self,
        container: ET.Element | None,
        tails: list[str],
        error: str | None,
        scope: str,
        stack: tuple[str, ...],
    ) -> list[str]:
        if container is None:
            return tails
        for action in container:
            name = local_name(action)
            if name in {'id', 'comment', 'operator', 'value', 'condition', 'expression'}:
                continue
            if not tails:
                break
            if name in {'ifThenElse', 'routingTable'}:
                tails = self.branch(action, tails, error, scope, stack)
            elif name in {'route', 'wsCallout', 'publish'}:
                tails = self.call(action, tails, error, scope, stack)
            else:
                tails = self.simple_action(action, tails, error, scope)
        return tails

    def branch(
        self,
        action: ET.Element,
        tails: list[str],
        error: str | None,
        scope: str,
        stack: tuple[str, ...],
    ) -> list[str]:
        name = local_name(action)
        decision = self.graph.node(
            'IF (ordered conditions)' if name == 'ifThenElse'
            else f'Routing table\n{expression(child(action, "expression"))}',
            'decision', scope,
        )
        self.link(tails, decision)
        if error:
            self.graph.edge(decision, error, 'evaluation fault', 'error')
        branches = []
        has_default = False
        for case in action:
            case_name = local_name(case)
            if case_name not in {'case', 'default', 'defaultCase'}:
                continue
            has_default = has_default or case_name != 'case'
            label = self.condition_label(case, name)
            branch = self.graph.node(label, 'condition', scope)
            self.graph.edge(decision, branch, label)
            case_actions = child(case, 'actions')
            branches.extend(self.actions(
                case_actions if case_actions is not None else case,
                [branch], error, scope, stack,
            ))
        if not has_default:
            otherwise = self.graph.node('Otherwise: continue', 'condition', scope)
            self.graph.edge(decision, otherwise, 'no match')
            branches.append(otherwise)
        if not branches:
            return []
        join = self.graph.node('Continue after branch', 'join', scope)
        self.link(branches, join)
        return [join]

    @staticmethod
    def condition_label(case: ET.Element, branch_kind: str) -> str:
        if local_name(case) != 'case':
            return 'Otherwise (no earlier match)'
        if branch_kind == 'routingTable':
            return f'{content(child(case, "operator"))} {content(child(case, "value"))}'
        return expression(child(case, 'condition')) or 'Condition unresolved'

    def simple_action(
        self, action: ET.Element, tails: list[str], error: str | None, scope: str
    ) -> list[str]:
        name = local_name(action)
        node = self.graph.node(self.action_label(action), 'action', scope)
        self.link(tails, node)
        if name.lower() == 'error':
            target = error or self.graph.node('Unhandled fault', 'end', scope)
            self.graph.edge(node, target, 'raise fault', 'error')
            return []
        if name == 'reply':
            self.reply_returns[-1].append((node, action.get('isError', 'false') in {'true', '1'}))
            return []
        if name in {'resume', 'skip', 'exit'}:
            self.graph.nodes[-1].label += '\nRuntime continuation not expanded'
            return []
        if error:
            self.graph.edge(node, error, 'on fault', 'error')
        return [node]

    def action_label(self, action: ET.Element) -> str:
        name = local_name(action)
        known = {
            'assign', 'replace', 'insert', 'delete', 'rename', 'validate', 'log',
            'report', 'Error', 'reply', 'resume', 'skip', 'exit', 'transport-headers',
            'set-header', 'remove-header', 'routing-options',
        }
        label = name if name in known else f'Unsupported action: {name} (not expanded)'
        if name not in known:
            logger.warning('Unsupported pipeline action: %s', name)
        details = [f'{key}={value}' for key, value in action.attrib.items()]
        for item in action:
            item_name = local_name(item)
            if item_name in {'id', 'comment'}:
                continue
            if item_name in {'expr', 'location'}:
                details.append(f'{item_name}: {expression(item)}')
            elif item_name in {'schema'}:
                details.append(f'{item_name}: {item.get("ref", "")}')
            else:
                details.append(f'{item_name}: {content(item)}')
        return '\n'.join([label, *details])

    def call(
        self,
        action: ET.Element,
        tails: list[str],
        error: str | None,
        scope: str,
        stack: tuple[str, ...],
    ) -> list[str]:
        name = local_name(action)
        service = child(action, 'service')
        target = self.index.service(service) if service is not None else None
        reference = service.get('ref', '') if service is not None else ''
        label = self.call_label(action, target, reference)
        call_node = self.graph.node(label, 'service' if target else 'unresolved', scope)
        self.link(tails, call_node)
        current = [call_node]
        before = child(action, 'requestTransform')
        if before is None:
            before = child(action, 'outboundTransform')
        current = self.actions(before, current, error, scope, stack)
        if not current:
            return []
        dispatch = self.graph.node(f'Send: {reference or "dynamic target (unresolved)"}', 'action', scope)
        self.link(current, dispatch)
        if error:
            self.graph.edge(dispatch, error, 'on service fault', 'error')
        current = [dispatch]
        if target and name != 'publish' and target.path.suffix != BUSINESS_SUFFIX:
            current = self.invoke(target, current, error, scope, stack)
        if name != 'publish':
            current = self.actions(child(action, 'responseTransform'), current, error, scope, stack)
        return current

    def call_label(self, action: ET.Element, target: Resource | None, reference: str) -> str:
        name = local_name(action)
        label = {'route': 'Route (request / response)', 'wsCallout': 'Service callout',
                 'publish': 'Publish (asynchronous)'}[name]
        label += f'\n{reference or "Dynamic target: unresolved"}'
        operation = content(child(action, 'operation'))
        if operation:
            label += f'\nOperation: {operation}'
        for direction in ('request', 'response'):
            message = child(action, direction)
            if message is not None:
                label += f'\n{direction}: ' + ', '.join(
                    f'{local_name(item)}=${content(item)}' for item in message
                )
        if not reference:
            label += f'\nTarget expression: {expression(action)}'
        if target is None:
            label += '\nUnresolved service boundary: local definition unavailable'
            label += '\nTransport / endpoint: unknown (not inferred)'
            logger.warning('Unresolved service boundary: %s', reference or 'dynamic target')
        elif target.path.suffix != PIPELINE_SUFFIX:
            label += f'\n{target.endpoint()}'
        else:
            label += '\nInternal pipeline invocation (no transport)'
        return label

    def invoke(
        self,
        target: Resource,
        current: list[str],
        error: str | None,
        scope: str,
        stack: tuple[str, ...],
    ) -> list[str]:
        if target.reference in stack:
            boundary = self.graph.node(
                f'Recursive service reference: {target.reference}\nNot expanded again',
                'unresolved', scope,
            )
            self.link(current, boundary)
            return [boundary]
        if target.path.suffix == PROXY_SUFFIX:
            entry, exits = self.proxy(target, stack, error)
        else:
            entry, exits = self.pipeline(target, stack + (target.reference,), error)
        self.link(current, entry, 'invoke')
        return exits


def flow_ranks(graph: FlowGraph) -> dict[str, int]:
    normal_edges = [edge for edge in graph.edges if edge.kind == 'normal']
    successors: dict[str, list[str]] = defaultdict(list)
    incoming = {node.id: 0 for node in graph.nodes}
    rank = {node.id: 0 for node in graph.nodes}
    for edge in normal_edges:
        successors[edge.source].append(edge.target)
        incoming[edge.target] += 1
    ready = deque(identity for identity, count in incoming.items() if count == 0)
    visited = 0
    while ready:
        identity = ready.popleft()
        visited += 1
        for target in successors[identity]:
            rank[target] = max(rank[target], rank[identity] + 1)
            incoming[target] -= 1
            if incoming[target] == 0:
                ready.append(target)
    if visited != len(graph.nodes):
        raise ValueError('Normal flow contains an unexpanded cycle')
    return rank


def overview_flow(graph: FlowGraph) -> FlowGraph:
    """Collapse implementation steps while retaining routing and service boundaries."""
    retained = {}
    for node in graph.nodes:
        in_handler = ' / error / ' in node.scope
        keep = node.kind in {'service', 'unresolved', 'error', 'end'}
        keep = keep or (not in_handler and node.kind in {'decision', 'condition'})
        keep = keep or node.label.startswith(('Error\n', 'reply\n'))
        if keep:
            label = node.label
            if node.kind == 'service':
                label = '\n'.join(
                    line for line in label.splitlines()
                    if not line.startswith(('request:', 'response:'))
                )
            if node.kind == 'error':
                label = f'Error handler\n{node.scope.split(" / error / ")[0]}'
            retained[node.id] = FlowNode(node.id, label, node.kind, node.scope)
    outgoing: dict[str, list[FlowEdge]] = defaultdict(list)
    for edge in graph.edges:
        outgoing[edge.source].append(edge)
    overview = FlowGraph(nodes=list(retained.values()))
    seen_edges = set()
    for source in retained:
        pending = deque((edge.target, edge.label, edge.kind) for edge in outgoing[source])
        visited = set()
        while pending:
            target, label, kind = pending.popleft()
            state = (target, label, kind)
            if state in visited:
                continue
            visited.add(state)
            if target in retained:
                key = (source, target, label, kind)
                if source != target and key not in seen_edges:
                    overview.edge(source, target, label, kind)
                    seen_edges.add(key)
                continue
            for edge in outgoing[target]:
                next_kind = 'error' if 'error' in {kind, edge.kind} else 'normal'
                next_label = label or edge.label
                if next_kind == 'error':
                    next_label = 'on fault'
                pending.append((edge.target, next_label, next_kind))
    return overview


def write_page(graph: FlowGraph, document: ET.Element, name: str, identity: str) -> None:
    """Lay out normal flow top-to-bottom; error edges cross to separate handler lanes."""
    rank = flow_ranks(graph)
    rows: dict[int, list[FlowNode]] = defaultdict(list)
    scopes: dict[str, int] = {}
    widths: dict[str, int] = defaultdict(lambda: 1)
    counts: dict[tuple[str, int], int] = defaultdict(int)
    for node in graph.nodes:
        scopes.setdefault(node.scope, len(scopes))
        rows[rank[node.id]].append(node)
        counts[(node.scope, rank[node.id])] += 1
        widths[node.scope] = max(widths[node.scope], counts[(node.scope, rank[node.id])])
    offsets = {}
    left = 40
    for scope in scopes:
        offsets[scope] = left
        left += widths[scope] * 460 + 100
    diagram = ET.SubElement(document, 'diagram', name=name, id=identity)
    model = ET.SubElement(diagram, 'mxGraphModel', grid='1', gridSize='10', page='0')
    root = ET.SubElement(model, 'root')
    ET.SubElement(root, 'mxCell', id='0')
    ET.SubElement(root, 'mxCell', id='1', parent='0')
    colors = {
        'service': '#dae8fc', 'stage': '#d5e8d4', 'action': '#ffffff',
        'decision': '#fff2cc', 'condition': '#fff2cc', 'unresolved': '#ffe6cc',
        'error': '#f8cecc', 'end': '#e1d5e7', 'join': '#eeeeee',
    }
    y = 40
    for row in sorted(rows):
        positions: dict[str, int] = defaultdict(int)
        row_height = 0
        for node in rows[row]:
            lines = []
            for line in node.label.splitlines():
                lines.extend(textwrap.wrap(line, width=58, replace_whitespace=False) or [''])
            height = max(70, len(lines) * 18 + 30)
            x = offsets[node.scope] + positions[node.scope] * 460
            positions[node.scope] += 1
            row_height = max(row_height, height)
            style = (
                f'rounded=1;whiteSpace=wrap;html=0;fillColor={colors[node.kind]};'
                'strokeColor=#666666;fontSize=12;align=left;spacing=12;'
            )
            if node.kind == 'decision':
                style += 'shape=hexagon;'
            cell = ET.SubElement(root, 'mxCell', id=node.id, value=node.label,
                                 vertex='1', parent='1', style=style)
            ET.SubElement(cell, 'mxGeometry', x=str(x), y=str(y), width='400',
                          height=str(height), attrib={'as': 'geometry'})
        y += row_height + 100
    for index, edge in enumerate(graph.edges):
        style = 'edgeStyle=orthogonalEdgeStyle;rounded=0;html=0;endArrow=block;'
        if edge.kind == 'error':
            style += 'dashed=1;strokeColor=#b85450;fontColor=#b85450;'
        cell = ET.SubElement(root, 'mxCell', id=f'e{index}', value=edge.label,
                             source=edge.source, target=edge.target,
                             edge='1', parent='1', style=style)
        ET.SubElement(cell, 'mxGeometry', relative='1', attrib={'as': 'geometry'})


def write_diagram(graph: FlowGraph, destination: Path) -> None:
    document = ET.Element('mxfile', host='OSB Diagram Generator')
    write_page(overview_flow(graph), document, 'Overview', 'overview')
    write_page(graph, document, 'Pipeline flow', 'pipeline-flow')
    destination.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(document).write(destination, encoding='utf-8', xml_declaration=True)
    for page in document.findall('diagram'):
        write_svg(page, destination.parent / f'{page.get("id")}.svg')


def discover_projects(root_dir: Path) -> list[Path]:
    root_dir = root_dir.expanduser().resolve()
    if not root_dir.is_dir():
        raise NotADirectoryError(f'Project root is not a directory: {root_dir}')
    projects = sorted({project.parent for project in root_dir.rglob('*.jpr')})
    if not projects:
        raise ValueError(f'No .jpr project files found under {root_dir}')
    names: dict[str, Path] = {}
    for project in projects:
        if project.name in names:
            raise ValueError(
                f'Duplicate OSB project directory name {project.name}: '
                f'{names[project.name]} and {project}. Use a root with unique project names.'
            )
        names[project.name] = project
    logger.info('Discovered %d projects using .jpr files under %s', len(projects), root_dir)
    return projects


def generate_flows(
    root_dir: Path, output_dir: Path
) -> list[Path]:
    repositories = discover_projects(root_dir)
    index = ResourceIndex(repositories)
    outputs = []
    for repository in repositories:
        if not any(
            resource.path.suffix == PROXY_SUFFIX
            and reference.startswith(f'{repository.name}/')
            for (reference, _), resource in index.resources.items()
        ):
            logger.warning('Skipping flow generation for %s: no proxy services; resources remain indexed',
                           repository)
            continue
        graph = PipelineFlow(index).build(repository)
        destination = output_dir / repository.name / f'{repository.name}.drawio'
        write_diagram(graph, destination)
        logger.info('Generated pipeline flow: %s (%d nodes, %d edges)',
                    destination, len(graph.nodes), len(graph.edges))
        outputs.append(destination)
    if not outputs:
        raise ValueError('No proxy services found in the selected projects; no diagrams generated')
    logger.info('Generated %d diagram files in %s', len(outputs), output_dir)
    return outputs
