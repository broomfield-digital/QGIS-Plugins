"""Ring-2 tests: a real headless QGIS, no network.

These need QGIS's bundled interpreter, so they run under
``./scripts/qgis_python.sh -m unittest discover -s tests/qgis -t .``
(``make test-qgis``) and not under the interpreter that runs ``tests/unit``.

Every payload still comes from ``tests/fixtures``. What is under test here is
the part ring 1 cannot reach: layers, renderers, temporal properties, the
network stack and the task wrapper.
"""
