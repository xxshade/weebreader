"""The one Jinja environment every router renders with.

Each router used to build its own from the same directory and register the
same globals and filters on it -- six copies of the template cache, and a
filter registered on one but forgotten on another rendered as an error only
on the page that happened to use it."""

from fastapi.templating import Jinja2Templates

from app.asset_versioning import register as register_asset_versioning
from app.library.events import register as register_events_stamp
from app.template_filters import register as register_template_filters
from app.ui_prefs import register as register_ui_prefs

templates = Jinja2Templates(directory="app/templates")
register_asset_versioning(templates)
register_events_stamp(templates)
register_ui_prefs(templates)
register_template_filters(templates)
