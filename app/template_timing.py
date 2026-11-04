"""Optional output-preserving spans compiled away when diagnostics are off.

The flag is process configuration: recreate the application after changing it,
just as for the other APP_* settings. No buffering or escaping changes.
"""
from jinja2 import nodes
from jinja2.ext import Extension

from app import request_timing
from app.config import settings


class RenderTimingExtension(Extension):
    tags = {'render_timing'}

    def parse(self, parser):
        token = next(parser.stream)
        name = parser.stream.expect('string').value
        if name not in request_timing._RENDER_PHASES:
            parser.fail('Unknown render timing phase', token.lineno)
        body = parser.parse_statements(('name:endrender_timing',), drop_needle=True)
        if not settings.detailed_server_timing:
            return body
        clock = parser.free_identifier(token.lineno)
        begin = nodes.Assign(clock, self.call_method('_begin')).set_lineno(token.lineno)
        end = nodes.ExprStmt(self.call_method('_end', [nodes.Const(name), clock])).set_lineno(token.lineno)
        return [begin, *body, end]

    def _begin(self):
        return request_timing.start()

    def _end(self, name, started):
        request_timing.finish(name, started)
