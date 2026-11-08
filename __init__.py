import json
from .core import Overlays

_state = {}


def overlays():
    if 'override' in _state: return _state['override']
    if 'o' not in _state:
        from plugins.plugin_storage import plugin_data_dir
        _state['o'] = Overlays(plugin_data_dir('overlay'))
    return _state['o']


def on_pre_tool_call(tool_name='', args=None, **kwargs):
    args = args or {}
    try:
        if tool_name == 'overlay_apply':
            return {'action': 'approve', 'message': 'Overlay: applying writes the accepted hunks to your real files. A person must confirm.', 'rule_key': 'overlay-apply'}
        if tool_name == 'terminal': return overlays().terminal_guard(args.get('command'))
        return overlays().rewrite(tool_name, args)
    except (ValueError, OSError, KeyError) as exc:
        return {'action': 'block', 'message': f'Overlay could not shadow this call safely: {exc}'}


def on_transform_tool_result(result='', **kwargs):
    try: return overlays().unmap(result)
    except (ValueError, OSError): return None


def invoke(name, args):
    try:
        o = overlays()
        if name == 'overlay_start': return json.dumps(o.start(args['root']))
        if name == 'overlay_status': return json.dumps(o.status())
        if name == 'overlay_diff': return json.dumps(o.diff(args['overlay_id']))
        if name == 'overlay_apply': return json.dumps(o.apply(args['overlay_id'], args['accept']))
        return json.dumps(o.discard(args['overlay_id']))
    except (ValueError, TypeError, OSError, KeyError) as exc:
        return json.dumps({'error': str(exc)})


S = {'type': 'string'}
TOOLS = {
    'overlay_start': ('Start an overlay on a project folder: from now on read_file, write_file and patch inside it work on a shadow copy and real files are untouched. Terminal commands are not captured.', {'root': S}, ['root']),
    'overlay_status': ('List active overlays.', {}, []),
    'overlay_diff': ('Show every changed file as numbered hunks with removed and added lines, and flag files whose real copy changed meanwhile.', {'overlay_id': S}, ['overlay_id']),
    'overlay_apply': ('Apply chosen hunks to the real files. accept maps a relative file to "all" or a list of hunk numbers; everything else is rejected and the overlay closes. Requires a person to confirm.', {'overlay_id': S, 'accept': {'type': 'object'}}, ['overlay_id', 'accept']),
    'overlay_discard': ('Throw the whole overlay away; real files stay as they were.', {'overlay_id': S}, ['overlay_id']),
}


def register(ctx):
    ctx.register_hook('pre_tool_call', on_pre_tool_call)
    ctx.register_hook('transform_tool_result', on_transform_tool_result)
    for name, (description, properties, required) in TOOLS.items():
        ctx.register_tool(name=name, toolset='overlay', schema={'name': name, 'description': description,
            'parameters': {'type': 'object', 'properties': properties, 'required': required, 'additionalProperties': False}},
            handler=lambda args, _name=name, **kwargs: invoke(_name, args))
