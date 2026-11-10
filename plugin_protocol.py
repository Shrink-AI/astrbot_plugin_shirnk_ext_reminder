"""Plugin capability protocol v1. Consumers vendor this file without cross-plugin imports."""
import asyncio
import json


class CapabilityError(ValueError):
    pass


class CapabilityClient:
    def __init__(self, registry=None):
        self._registry = registry

    def _providers(self):
        if self._registry is not None:
            return self._registry()
        from astrbot.core.star.star import star_registry
        return star_registry

    def catalog(self):
        entries = []
        for meta in self._providers():
            if not getattr(meta, 'activated', False):
                continue
            getter = getattr(meta.star_cls, 'plugin_capabilities', None)
            if not callable(getter):
                continue
            manifest = getter()
            if not isinstance(manifest, dict) or manifest.get('protocol') != 1:
                continue
            for cap in manifest.get('capabilities', []):
                if isinstance(cap, dict) and isinstance(cap.get('name'), str) and isinstance(cap.get('version'), int):
                    entries.append({**cap, 'provider': meta.name})
        return json.loads(json.dumps(entries))

    async def call(self, capability, action, arguments=None, *, version=1, provider=None, scope=None, timeout=40):
        candidates = [c for c in self.catalog() if c['name'] == capability and c['version'] == version
                      and (provider is None or c['provider'] == provider)]
        if not candidates:
            raise CapabilityError(f'能力不可用：{capability} v{version}')
        if len(candidates) != 1:
            raise CapabilityError(f'能力提供者不唯一，请明确 provider：{capability}')
        selected = candidates[0]
        if action not in selected.get('actions', {}):
            raise CapabilityError(f'能力不支持操作：{action}')
        if (arguments is not None and not isinstance(arguments, dict)) or (scope is not None and not isinstance(scope, dict)):
            raise CapabilityError('参数和调用范围必须是对象')
        meta = next((m for m in self._providers() if m.activated and m.name == selected['provider']), None)
        handler = getattr(getattr(meta, 'star_cls', None), 'invoke_capability', None)
        if not callable(handler):
            raise CapabilityError('能力提供者已卸载')
        request = json.loads(json.dumps({'protocol': 1, 'capability': capability, 'version': version,
                                       'action': action, 'arguments': arguments or {}, 'scope': scope or {}}))
        response = await asyncio.wait_for(handler(request), timeout)
        response = json.loads(json.dumps(response))
        if not isinstance(response, dict) or response.get('protocol') != 1:
            raise CapabilityError('能力返回协议无效')
        if response.get('ok') is not True:
            raise CapabilityError(str(response.get('error') or '能力执行失败'))
        return response.get('data')


def success(data):
    return {'protocol': 1, 'ok': True, 'data': data}


def validate(request, capability, actions):
    if request.get('protocol') != 1 or request.get('version') != 1 or request.get('capability') != capability:
        raise CapabilityError('不支持的协议或能力版本')
    if request.get('action') not in actions or not isinstance(request.get('arguments'), dict):
        raise CapabilityError('不支持的操作或参数')
