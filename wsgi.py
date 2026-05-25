"""
PythonAnywhere WSGI entry point.

In the PythonAnywhere Web tab → WSGI configuration file, set the path to
point at this file, e.g.:
    /home/<username>/<project_dir>/wsgi.py

The WSGI configuration file content should be:
    import sys
    sys.path.insert(0, '/home/<username>/<project_dir>')
    from wsgi import application

Or simply set the WSGI file to this file directly.
"""

import os
import sys

# Add the project root to sys.path so all imports resolve correctly.
_project_root = os.path.dirname(os.path.abspath(__file__))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from main_v2.application import app as application  # noqa: F401 — PythonAnywhere expects `application`
