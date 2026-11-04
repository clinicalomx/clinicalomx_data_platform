#!/usr/bin/env python3
"""Generate the provisioned Grafana dashboards.

Run: make ops-dashboards

The output JSON is committed and loaded at Grafana startup. Edits made in the Grafana UI are
not saved here: export the JSON and fold it into this script. Standard library only.
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent

PROM = {"type": "prometheus", "uid": "prometheus"}
LOKI = {"type": "loki", "uid": "loki"}

# Multi-value variables are interpolated as a regex alternation; Grafana's default is a glob.
CONTAINER = "${container:regex}"


def timeseries(title, x, y, w, h, expr, unit, legend="{{name}}"):
    return {
        "type": "timeseries", "title": title, "datasource": PROM,
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "targets": [{"datasource": PROM, "editorMode": "code", "expr": expr,
                     "legendFormat": legend, "range": True, "refId": "A"}],
        "fieldConfig": {"defaults": {"unit": unit, "custom": {
            "fillOpacity": 8, "lineWidth": 1, "showPoints": "never"}}, "overrides": []},
        "options": {"legend": {"displayMode": "table", "placement": "bottom",
                               "calcs": ["mean", "max", "lastNotNull"]},
                    "tooltip": {"mode": "multi", "sort": "desc"}},
    }


def overview_table(x, y, w, h, dash_uid):
    """Every container, its state and its current cost. Clicking a row filters this dashboard."""
    q = lambda expr, ref: {"datasource": PROM, "editorMode": "code", "expr": expr,
                           "format": "table", "instant": True, "range": False, "refId": ref}
    return {
        "type": "table", "title": "Containers", "datasource": PROM,
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "description": "Click a row to filter every panel below to that container.",
        "targets": [
            # cAdvisor stops emitting container_last_seen when a container goes away.
            q('max by (name) (time() - container_last_seen{name!=""})', "seen"),
            q('sum by (name) (rate(container_cpu_usage_seconds_total{name!=""}[$__rate_interval])) * 100', "cpu"),
            q('sum by (name) (container_memory_working_set_bytes{name!=""})', "mem"),
        ],
        "transformations": [
            {"id": "joinByField", "options": {"byField": "name", "mode": "outer"}},
            {"id": "organize", "options": {
                "excludeByName": {"Time": True, "Time 1": True, "Time 2": True, "Time 3": True},
                "renameByName": {"name": "Container", "Value #seen": "Last seen (s)",
                                 "Value #cpu": "CPU %", "Value #mem": "Memory"},
                "indexByName": {"name": 0, "Value #seen": 1, "Value #cpu": 2, "Value #mem": 3}}},
        ],
        "fieldConfig": {
            "defaults": {"custom": {"align": "left", "filterable": True}},
            "overrides": [
                {"matcher": {"id": "byName", "options": "Memory"},
                 "properties": [{"id": "unit", "value": "bytes"},
                                {"id": "custom.cellOptions",
                                 "value": {"type": "gauge", "mode": "gradient"}}]},
                {"matcher": {"id": "byName", "options": "CPU %"},
                 "properties": [{"id": "unit", "value": "percent"},
                                {"id": "decimals", "value": 2}]},
                {"matcher": {"id": "byName", "options": "Last seen (s)"},
                 "properties": [{"id": "unit", "value": "s"}, {"id": "decimals", "value": 0},
                                # Amber past a minute; cAdvisor scrapes every 30s.
                                {"id": "thresholds", "value": {"mode": "absolute", "steps": [
                                    {"color": "green", "value": None},
                                    {"color": "orange", "value": 60}]}},
                                {"id": "custom.cellOptions",
                                 "value": {"type": "color-text"}}]},
                {"matcher": {"id": "byName", "options": "Container"},
                 "properties": [{"id": "links", "value": [{
                     "title": "Filter to this container",
                     "url": f"/grafana/d/{dash_uid}?var-container=${{__value.raw}}",
                     "targetBlank": False}]}]},
            ]},
        "options": {"showHeader": True, "sortBy": [{"desc": True, "displayName": "Memory"}]},
    }


def logs_panel(x, y, w, h):
    return {
        "type": "logs", "title": "Logs", "datasource": LOKI,
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        # An empty line filter matches everything.
        "targets": [{"datasource": LOKI, "editorMode": "code", "queryType": "range", "refId": "A",
                     "expr": f'{{job="docker", container=~"{CONTAINER}"}} |= "$search"'}],
        "options": {"showTime": True, "wrapLogMessage": True, "prettifyLogMessage": False,
                    "enableLogDetails": True, "dedupStrategy": "none", "sortOrder": "Descending",
                    "showCommonLabels": False},
        "description": ("Polls on refresh, which is off by default; use the refresh picker for "
                        "near-live. True streaming tail is Explore only, linked at the top."),
    }


def build_containers():
    uid = "clinicalomxdb-containers"
    explore = ('/grafana/explore?left=%7B%22datasource%22:%22loki%22,%22queries%22:'
               '%5B%7B%22expr%22:%22%7Bjob%3D%5C%22docker%5C%22%7D%22%7D%5D,'
               '%22range%22:%7B%22from%22:%22now-1h%22,%22to%22:%22now%22%7D%7D')
    return {
        "uid": uid,
        "title": "Containers",
        "tags": ["clinicalomxdb"],
        "timezone": "browser",
        "schemaVersion": 39,
        "version": 1,
        "editable": True,
        # Nothing polls until a refresh interval is chosen.
        "refresh": "",
        "time": {"from": "now-3h", "to": "now"},
        "templating": {"list": [
            {"name": "container", "label": "Container", "type": "query", "datasource": PROM,
             "refresh": 2, "includeAll": True, "multi": True, "allValue": ".+",
             "current": {"text": "All", "value": "$__all"},
             # From cAdvisor, so new containers appear automatically.
             "definition": 'label_values(container_memory_working_set_bytes{name!=""}, name)',
             "query": {"query": 'label_values(container_memory_working_set_bytes{name!=""}, name)',
                       "refId": "v"},
             "sort": 1},
            {"name": "search", "label": "Log contains", "type": "textbox",
             "current": {"text": "", "value": ""}, "query": ""},
        ]},
        "links": [{"title": "Live tail in Explore", "type": "link", "icon": "external link",
                   "url": explore, "targetBlank": True}],
        "panels": [
            overview_table(0, 0, 24, 9, uid),
            timeseries("CPU", 0, 9, 12, 8,
                       f'sum by (name) (rate(container_cpu_usage_seconds_total{{name=~"{CONTAINER}"}}[$__rate_interval])) * 100',
                       "percent"),
            timeseries("Memory (working set)", 12, 9, 12, 8,
                       f'sum by (name) (container_memory_working_set_bytes{{name=~"{CONTAINER}"}})',
                       "bytes"),
            logs_panel(0, 17, 24, 14),
        ],
    }


def main() -> None:
    for name, dash in [("containers", build_containers())]:
        path = HERE / f"{name}.json"
        path.write_text(json.dumps(dash, indent=2) + "\n")
        print(f"[dashboards] wrote {path.relative_to(HERE.parents[2])} "
              f"({len(dash['panels'])} panels)")


if __name__ == "__main__":
    main()
