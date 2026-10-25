"""Invalidate anticipated account pages after successful HTTP mutations.

Signals carry no account data. Conservative invalidation also covers successful
no-op submissions; the routes own validation and commit before returning headers.
This ASGI wrapper forwards bodies untouched, including event streams and beacons.
"""
from app.library import events


class AccountChangeNotifications:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get('path', '')
        mutation = (scope['type'] == 'http'
                    and scope['method'] in {'POST', 'PUT', 'PATCH', 'DELETE'}
                    and (path.startswith('/settings/') or path in {
                        '/reading-time', '/libraries', '/library/issues/clear-errors',
                        '/library/reindex-numbers'}))
        if not mutation:
            return await self.app(scope, receive, send)

        async def notify(message):
            if message['type'] == 'http.response.start' and 200 <= message['status'] < 400:
                events.publish({'type': 'account_updated'})
                if path.startswith('/settings/text-replacements'):
                    events.publish({'type': 'reading_estimate_updated'})
            await send(message)

        await self.app(scope, receive, notify)
