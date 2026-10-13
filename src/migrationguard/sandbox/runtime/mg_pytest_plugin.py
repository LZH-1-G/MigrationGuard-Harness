"""MigrationGuard pytest recorder (runs inside the sandbox).

Loaded with ``-p mg_pytest_plugin`` from a read-only mount. Writes a JSON report to
``$MG_REPORT`` containing: collected node ids, per-test outcome (incl. xfail/xpass and
setup/teardown errors), skip/xfail markers present on each test, every warning recorded
by pytest (category, location) and the versions of tracked packages.

Kept compatible with Python 3.8+ and pytest 6+.
"""

import json
import os
import sys
import time

TRACKED_MARKERS = ("skip", "skipif", "xfail")


def _version(name):
    try:
        from importlib import metadata
    except ImportError:  # pragma: no cover
        return None
    try:
        return metadata.version(name)
    except Exception:
        return None


class Recorder:
    def __init__(self, path):
        self.path = path
        self.collected = []
        self.markers = {}
        self.tests = {}
        self.collection_errors = []
        self.warnings = []
        self.started = time.time()

    def pytest_collectreport(self, report):
        if report.failed:
            text = getattr(report, "longreprtext", "") or str(report.longrepr)
            self.collection_errors.append("%s: %s" % (report.nodeid, text[-3000:]))

    def pytest_collection_finish(self, session):
        for item in session.items:
            self.collected.append(item.nodeid)
            names = sorted({m.name for m in item.iter_markers() if m.name in TRACKED_MARKERS})
            self.markers[item.nodeid] = names

    def _rec(self, nodeid):
        if nodeid not in self.tests:
            self.tests[nodeid] = {"outcome": None, "duration": 0.0, "message": ""}
        return self.tests[nodeid]

    def pytest_runtest_logreport(self, report):
        rec = self._rec(report.nodeid)
        rec["duration"] += float(getattr(report, "duration", 0.0) or 0.0)
        xfail = hasattr(report, "wasxfail")
        text = ""
        if report.failed:
            text = (getattr(report, "longreprtext", "") or str(report.longrepr))[-4000:]
        if report.when == "setup":
            if report.failed:
                rec["outcome"] = "error"
                rec["message"] = text
            elif report.skipped:
                rec["outcome"] = "xfailed" if xfail else "skipped"
                rec["message"] = str(report.longrepr)[-500:] if report.longrepr else ""
        elif report.when == "call":
            if xfail:
                rec["outcome"] = "xpassed" if report.passed else "xfailed"
            elif report.passed:
                rec["outcome"] = "passed"
            elif report.failed:
                rec["outcome"] = "failed"
                rec["message"] = text
            elif report.skipped:
                rec["outcome"] = "skipped"
        elif report.when == "teardown" and report.failed:
            if rec["outcome"] in (None, "passed"):
                rec["outcome"] = "error"
                rec["message"] = text

    def pytest_warning_recorded(self, warning_message, when, nodeid, location):
        category = warning_message.category
        self.warnings.append(
            {
                "category": "%s.%s" % (category.__module__, category.__name__),
                "message": str(warning_message.message)[:500],
                "filename": str(warning_message.filename),
                "lineno": int(warning_message.lineno or 0),
                "nodeid": nodeid or "",
                "when": when,
            }
        )

    def pytest_sessionfinish(self, session, exitstatus):
        tracked = [p for p in os.environ.get("MG_TRACK_PACKAGES", "").split(",") if p]
        payload = {
            "exit_code": int(exitstatus),
            "collected": self.collected,
            "markers": self.markers,
            "tests": self.tests,
            "collection_errors": self.collection_errors,
            "warnings": self.warnings,
            "packages": {p: _version(p) for p in tracked},
            "python": sys.version.split()[0],
            "duration": time.time() - self.started,
        }
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)


def pytest_configure(config):
    path = os.environ.get("MG_REPORT")
    if path and not config.pluginmanager.has_plugin("mg_recorder"):
        config.pluginmanager.register(Recorder(path), "mg_recorder")
