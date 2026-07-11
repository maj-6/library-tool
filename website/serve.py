#!/usr/bin/env python3
"""A dev server for the site, with the one fix `python -m http.server` lacks.

The reader imports pdf.js as an ES module (assets/vendor/pdfjs/*.mjs). Browsers
refuse to run a module script served with a non-JavaScript MIME type, and on
some platforms (notably Windows, where the type comes from the registry) the
stock http.server hands `.mjs` back as text/plain — so the reader silently
fails to load pdf.js locally. GitHub Pages, the production host, serves `.mjs`
as text/javascript and needs none of this; this helper only makes local
development match.

    python3 website/serve.py [port]        # default 8000
"""
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, test

# Force the correct JavaScript MIME for both extensions, regardless of the
# platform's mimetypes database.
SimpleHTTPRequestHandler.extensions_map[".mjs"] = "text/javascript"
SimpleHTTPRequestHandler.extensions_map[".js"] = "text/javascript"

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    test(HandlerClass=partial(SimpleHTTPRequestHandler, directory=here),
         port=port, bind="0.0.0.0")
