"""
Replay-aware FreqUI — make browser-side "now" follow the replay clock.

Most of FreqUI's charts are driven by server data and already follow replay
time because the API server runs inside the replay process.  A few places,
however, use the *browser's* clock.  The one that matters is the Cumulative
Profit chart: with open trades it appends a final point at
`Date.now() + 1 day`, so the line runs from the replay's dates to today.

Rather than forking FreqUI, we serve a patched copy of the installed bundle:
  * `Date.now()+864e5` → `(globalThis.__replayNow?.() ?? Date.now())+864e5`
  * index.html loads /replay_clock.js, which defines __replayNow() from the
    /replay_now endpoint (polled; first value fetched synchronously so the
    first render already uses replay time).
Everything else in the bundle is untouched.
"""

import logging
import re
import shutil
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# Browser-clock call sites that should follow replay time.  Matched against
# the minified bundle, so keep them exact; a FreqUI upgrade that changes them
# is reported as a warning (the UI still works, those charts just use wall time).
_JS_PATCHES = [
    ("Date.now()+864e5", "(globalThis.__replayNow?.()??Date.now())+864e5"),
]

_CLOCK_JS = """\
(function () {
  var now = null;
  function apply(text) {
    try { var v = JSON.parse(text).now_ms; if (typeof v === "number") now = v; } catch (e) {}
  }
  try {  // synchronous first fetch so the first chart render uses replay time
    var x = new XMLHttpRequest();
    x.open("GET", "/replay_now", false);
    x.send();
    if (x.status === 200) apply(x.responseText);
  } catch (e) {}
  setInterval(function () {
    fetch("/replay_now").then(function (r) { return r.text(); }).then(apply).catch(function () {});
  }, 2000);
  globalThis.__replayNow = function () { return now === null ? Date.now() : now; };
})();
"""


def _build_patched_ui(src: Path) -> Path:
    dst = Path(tempfile.mkdtemp(prefix="replay_frequi_"))
    shutil.copytree(src, dst, dirs_exist_ok=True)

    hits = {old: 0 for old, _ in _JS_PATCHES}
    for js in (dst / "assets").glob("*.js"):
        text = js.read_text(encoding="utf-8")
        new = text
        for old, repl in _JS_PATCHES:
            hits[old] += new.count(old)
            new = new.replace(old, repl)
        if new != text:
            js.write_text(new, encoding="utf-8")
    for old, n in hits.items():
        if n == 0:
            logger.warning(
                "FreqUI patch target %r not found (FreqUI version changed?) — "
                "Cumulative Profit's open-trade point will use wall-clock time.", old,
            )

    (dst / "replay_clock.js").write_text(_CLOCK_JS, encoding="utf-8")
    index = dst / "index.html"
    html = index.read_text(encoding="utf-8")
    # Classic (non-module) script in <head> runs before the deferred module bundle.
    html, n = re.subn(r"<head>", '<head>\n  <script src="/replay_clock.js"></script>', html, count=1)
    if n == 0:
        logger.warning("Could not inject replay clock into FreqUI index.html")
    index.write_text(html, encoding="utf-8")
    return dst


_UI_SPECIAL = {
    "favicon.ico": "favicon",
    "fallback_file.html": "fallback",
    "ui_version": "ui_version",
}


def _ui_endpoints(web_ui) -> tuple:
    return (web_ui.index_html, *(getattr(web_ui, n) for n in _UI_SPECIAL.values()))


def _patch_ws_serializer() -> None:
    """Let FreqUI's websocket serialize freezegun's FakeDatetime.

    orjson only serializes exact `datetime` instances, not subclasses, so every
    websocket message carrying a timestamp (trade entry/exit events, ...)
    raised "Type is not JSON serializable: FakeDatetime" and dropped the
    connection, which stopped live updates.  The fallback encoder now emits the
    same ISO-8601 string orjson would produce for a real datetime.
    """
    from datetime import date

    from freqtrade.rpc.api_server.ws import serializer

    original = serializer._json_default
    if getattr(original, "_replay_patched", False):
        return

    def _json_default(z):
        if isinstance(z, date):  # datetime is a date subclass
            return z.isoformat()
        return original(z)

    _json_default._replay_patched = True
    serializer._json_default = _json_default  # looked up at call time


def install_replay_ui(bot, clock) -> Path | None:
    """Serve a replay-time-aware FreqUI from the bot's in-process API server.

    Returns the temp dir holding the patched UI (caller removes it), or None
    when there is no API server / no installed FreqUI.
    """
    from fastapi.exceptions import HTTPException
    from fastapi.routing import APIRoute
    from starlette.responses import FileResponse, JSONResponse
    from starlette.routing import Route

    from freqtrade.rpc.api_server import ApiServer
    from freqtrade.rpc.api_server import web_ui

    server = next((m for m in bot.rpc.registered_modules if isinstance(m, ApiServer)), None)
    if server is None:
        return None
    _patch_ws_serializer()
    src = Path(web_ui.__file__).parent / "ui" / "installed"
    if not (src / "index.html").is_file():
        logger.warning("FreqUI is not installed in this image — nothing to patch.")
        return None

    uibase = _build_patched_ui(src).resolve()

    async def replay_now(request):
        now = clock.now()
        return JSONResponse({"now_ms": int(now.timestamp() * 1000) if now else None})

    async def index_html(rest_of_path: str):
        # Same as freqtrade's web_ui.index_html, but rooted at the patched copy.
        if rest_of_path.startswith(("api", ".")):
            raise HTTPException(status_code=404, detail="Not Found")
        if rest_of_path in _UI_SPECIAL:  # freqtrade's own non-catch-all UI routes
            return await getattr(web_ui, _UI_SPECIAL[rest_of_path])()
        filename = (uibase / rest_of_path).resolve()
        media_type = "application/javascript" if filename.suffix == ".js" else None
        if filename.is_file() and filename.is_relative_to(uibase):
            return FileResponse(str(filename), media_type=media_type)
        return FileResponse(str(uibase / "index.html"))

    # Put our catch-all directly before freqtrade's UI routes so it shadows
    # them while every API router (registered earlier) keeps priority.  Older
    # FastAPI flattens included routes; 0.14x keeps the router as one entry
    # (`original_router`), so match either layout without touching internals.
    routes = server.app.router.routes
    pos = next(
        (
            i for i, r in enumerate(routes)
            if getattr(r, "original_router", None) is web_ui.router_ui
            or getattr(r, "endpoint", None) in _ui_endpoints(web_ui)
        ),
        None,
    )
    if pos is None:
        logger.warning("FreqUI routes not found — serving the unpatched UI.")
        shutil.rmtree(uibase, ignore_errors=True)
        return None
    routes.insert(pos, APIRoute("/{rest_of_path:path}", index_html, methods=["GET"], include_in_schema=False))

    # Specific path, so it must come before the catch-all.
    routes.insert(0, Route("/replay_now", replay_now, methods=["GET"]))
    return uibase
