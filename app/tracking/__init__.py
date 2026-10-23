"""Reporting reading progress to an external tracking service.

Two tables carry all of it (see schema.sql): `tracker_accounts` holds one
session per (reader, service), `tracker_links` says what a local series or
novel IS on that service. No link means not tracked, which is the ordinary
state -- a large part of any library is not in either catalogue, and a
folder full of CBZs has no id anyone else would recognise.

Layout mirrors the rest of the app: `mangaupdates.py` is the service's own
API and knows nothing about this database; `store.py` is the database and
knows nothing about HTTP; `service.py` is what joins them and is the only
thing the routers call.
"""
