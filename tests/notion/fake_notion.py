"""In-memory fake of the Notion REST API for tests (httpx.MockTransport).

Usage: ``fake = FakeNotion(); api = NotionApi("key", log, transport=fake.transport)``.

State lives in plain dicts so tests can assert on it: ``databases``,
``data_sources``, ``pages``, ``views`` (all keyed by id) and ``requests``
(list of ``(method, path, json)`` in call order).

Implemented endpoints: POST /databases, GET/PATCH /data_sources/{id},
POST /data_sources/{id}/query (page_size + start_cursor pagination, trashed
pages excluded, only a title/rich_text equals filter, no sort support),
POST /pages, GET/PATCH /pages/{id}
(property merge and ``in_trash``), POST /views, GET /views (?database_id=,
?data_source_id=, returns ``{id}`` items only, paginated by ``views_page_size``), GET /views/{id}.

Limits: formulas and rollups are never evaluated; their definitions are
returned as given and page values must be set by hand in tests. Property
values written to pages are stored without validation; title/rich_text items get
``plain_text`` added. Dual relations
create the reverse property on the target data source; a PATCH from
single_property to dual_property creates it too. Select options are not
validated. Unknown routes return 404 object_not_found. Failure injection:
``ignore_query_filters`` (True returns unfiltered rows),
``trash_data_source(id)`` makes a data source answer 404, ``mark_trashed(id)``
reports ``in_trash: true``, ``fail_once(method, prefix, status, skip)`` fails
one matching request.
"""
import json
import re
import uuid
from typing import Any
from urllib.parse import parse_qs, quote

import httpx

_DEFAULT_PAGE_SIZE = 100


def _to_read_shape(value: Any) -> Any:
    """Add ``plain_text`` to written title/rich_text items, as Notion returns them."""
    if not isinstance(value, dict):
        return value
    out = dict(value)
    for key in ("title", "rich_text"):
        if isinstance(out.get(key), list):
            out[key] = [
                {**i, "plain_text": i.get("plain_text", i.get("text", {}).get("content", ""))}
                for i in out[key]
            ]
    return out


def _matches_filter(page: dict[str, Any], flt: dict[str, Any] | None) -> bool:
    """Only ``{"property": n, "title"|"rich_text": {"equals": v}}`` is supported."""
    if not flt:
        return True
    prop = page["properties"].get(flt["property"], {})
    kind = "title" if "title" in flt else "rich_text"
    text = "".join(i.get("plain_text", "") for i in prop.get(kind, []))
    return text == flt[kind]["equals"]


def _err(status: int, code: str, message: str) -> httpx.Response:
    return httpx.Response(
        status, json={"object": "error", "status": status, "code": code, "message": message}
    )


class FakeNotion:
    def __init__(self) -> None:
        self.databases: dict[str, dict[str, Any]] = {}
        self.data_sources: dict[str, dict[str, Any]] = {}
        self.pages: dict[str, dict[str, Any]] = {}
        self.views: dict[str, dict[str, Any]] = {}
        self.requests: list[tuple[str, str, Any]] = []
        self.views_page_size = _DEFAULT_PAGE_SIZE
        self.ignore_query_filters = False
        self._counter = 0
        self._failures: list[list[Any]] = []
        self.transport = httpx.MockTransport(self._handle)

    # ---- helpers for tests -------------------------------------------------

    def trash_data_source(self, ds_id: str) -> None:
        """Make a data source answer 404 from now on."""
        del self.data_sources[ds_id]

    def mark_trashed(self, ds_id: str) -> None:
        """Keep a data source but report ``in_trash: true`` on GET."""
        self.data_sources[ds_id]["in_trash"] = True

    def fail_once(self, method: str, prefix: str, status: int = 400, skip: int = 0) -> None:
        """Fail one future request matching method and path prefix.

        ``skip`` lets that many matching requests succeed first. Use a 4xx status
        to avoid the client's retry sleeps.
        """
        self._failures.append([method, prefix, status, skip])

    def ds_by_title(self, title: str) -> dict[str, Any]:
        return next(d for d in self.data_sources.values() if d["_title"] == title)

    def requests_to(self, method: str, prefix: str) -> list[tuple[str, str, Any]]:
        return [r for r in self.requests if r[0] == method and r[1].startswith(prefix)]

    # ---- internals ---------------------------------------------------------

    def _id(self) -> str:
        self._counter += 1
        return str(uuid.UUID(int=self._counter))

    def _prop_id(self) -> str:
        self._counter += 1
        return quote(f"p{self._counter}", safe="")

    def _make_property(self, ds: dict[str, Any], name: str, spec: dict[str, Any]) -> None:
        """Insert/replace a property from a request spec ({type_key: config})."""
        old = ds["properties"].get(name)
        prop_id = old["id"] if old else ("title" if "title" in spec else self._prop_id())
        kind = next(iter(spec))
        config = dict(spec[kind])
        if kind == "number":
            config.setdefault("format", "number")
        if kind == "select":
            config["options"] = [
                {"id": self._id(), "color": "default", "description": None, **o}
                for o in config.get("options", [])
            ]
        prop: dict[str, Any] = {
            "id": prop_id, "name": name, "description": None, "type": kind, kind: config,
        }
        if kind == "relation":
            self._make_relation(ds, prop, config)
        ds["properties"][name] = prop

    def _make_relation(
        self, ds: dict[str, Any], prop: dict[str, Any], config: dict[str, Any]
    ) -> None:
        target = self.data_sources[config["data_source_id"]]
        dual = config.get("dual_property")
        rel: dict[str, Any] = {
            "database_id": target["parent"]["database_id"],
            "data_source_id": target["id"],
            "limit": None,
        }
        if dual is None:
            rel.update(type="single_property", single_property={})
        else:
            existing = prop.get("relation", {}).get("dual_property", {})
            rev_name = dual["synced_property_name"]
            rev_id = existing.get("synced_property_id") or self._prop_id()
            rel.update(
                type="dual_property",
                dual_property={"synced_property_name": rev_name, "synced_property_id": rev_id},
            )
            target["properties"][rev_name] = {
                "id": rev_id, "name": rev_name, "description": None, "type": "relation",
                "relation": {
                    "database_id": ds["parent"]["database_id"],
                    "data_source_id": ds["id"],
                    "limit": None,
                    "type": "dual_property",
                    "dual_property": {
                        "synced_property_name": prop["name"], "synced_property_id": prop["id"],
                    },
                },
            }
        prop["relation"] = rel

    def _public_ds(self, ds: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in ds.items() if not k.startswith("_")}

    def _new_data_source(self, db_id: str, title: str, props: dict[str, Any]) -> dict[str, Any]:
        ds: dict[str, Any] = {
            "object": "data_source", "id": self._id(),
            "title": [{"type": "text", "plain_text": title}], "_title": title, "properties": {},
            "parent": {"type": "database_id", "database_id": db_id}, "in_trash": False,
        }
        self.data_sources[ds["id"]] = ds
        # Title first so relation targets exist; relations need self registered.
        for name, spec in props.items():
            self._make_property(ds, name, spec)
        return ds

    # ---- routing -----------------------------------------------------------

    def _handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        method, path = request.method, request.url.path
        path = path.removeprefix("/v1")
        self.requests.append((method, path, body))
        query = parse_qs(request.url.query.decode())
        for f in self._failures:
            if f[0] == method and path.startswith(f[1]):
                if f[3] > 0:
                    f[3] -= 1
                    continue
                self._failures.remove(f)
                return _err(f[2], "validation_error", "injected failure")

        if method == "POST" and path == "/databases":
            return self._create_database(body)
        if m := re.fullmatch(r"/data_sources/([^/]+)", path):
            ds = self.data_sources.get(m[1])
            if ds is None:
                return _err(404, "object_not_found", f"data source {m[1]} not found")
            if method == "GET":
                return httpx.Response(200, json=self._public_ds(ds))
            if method == "PATCH":
                return self._patch_data_source(ds, body)
        if m := re.fullmatch(r"/data_sources/([^/]+)/query", path):
            ds = self.data_sources.get(m[1])
            if ds is None or method != "POST":
                return _err(404, "object_not_found", "data source not found")
            return self._query(ds, body or {})
        if method == "POST" and path == "/pages":
            return self._create_page(body)
        if m := re.fullmatch(r"/pages/([^/]+)", path):
            page = self.pages.get(m[1])
            if page is None:
                return _err(404, "object_not_found", "page not found")
            if method == "PATCH":
                for k, v in (body or {}).get("properties", {}).items():
                    page["properties"][k] = _to_read_shape(v)
                if "in_trash" in (body or {}):
                    page["in_trash"] = body["in_trash"]
            return httpx.Response(200, json=page)
        if method == "POST" and path == "/views":
            view = {"object": "view", "id": self._id(), **(body or {})}
            self.views[view["id"]] = view
            return httpx.Response(200, json=view)
        if method == "GET" and path == "/views":
            return self._list_views(query)
        if m := re.fullmatch(r"/views/([^/]+)", path):
            view = self.views.get(m[1])
            if view is None or method != "GET":
                return _err(404, "object_not_found", "view not found")
            return httpx.Response(200, json=view)
        return _err(404, "object_not_found", f"no route {method} {path}")

    def _create_database(self, body: dict[str, Any]) -> httpx.Response:
        title = body["title"][0]["text"]["content"]
        db_id = self._id()
        ds = self._new_data_source(
            db_id, title, body.get("initial_data_source", {}).get("properties", {})
        )
        db = {
            "object": "database", "id": db_id, "title": body["title"], "parent": body["parent"],
            "in_trash": False, "data_sources": [{"id": ds["id"], "name": title}],
        }
        self.databases[db_id] = db
        view = {
            "object": "view", "id": self._id(), "name": "Default view", "type": "table",
            "database_id": db_id, "data_source_id": ds["id"],
        }
        self.views[view["id"]] = view
        return httpx.Response(200, json=db)

    def _patch_data_source(self, ds: dict[str, Any], body: dict[str, Any]) -> httpx.Response:
        for name, spec in (body or {}).get("properties", {}).items():
            if spec is None:
                if name not in ds["properties"]:
                    return _err(404, "object_not_found", f"property {name} not found")
                del ds["properties"][name]
                continue
            existing = ds["properties"].get(name)
            if existing is not None and "relation" in spec and existing["type"] == "relation":
                # relation upgrade (e.g. single -> dual): keep id, rebuild config
                prop = {**existing}
                prop["relation"] = {}
                self._make_relation(ds, prop, dict(spec["relation"]))
                ds["properties"][name] = prop
                continue
            self._make_property(ds, name, spec)
        return httpx.Response(200, json=self._public_ds(ds))

    def _create_page(self, body: dict[str, Any]) -> httpx.Response:
        ds_id = body["parent"]["data_source_id"]
        if ds_id not in self.data_sources:
            return _err(404, "object_not_found", "data source not found")
        page = {
            "object": "page", "id": self._id(), "in_trash": False, "is_archived": False,
            "parent": body["parent"],
            "properties": {
                k: _to_read_shape(v) for k, v in body.get("properties", {}).items()
            },
        }
        self.pages[page["id"]] = page
        return httpx.Response(200, json=page)

    def _query(self, ds: dict[str, Any], body: dict[str, Any]) -> httpx.Response:
        rows = [
            p for p in self.pages.values()
            if p["parent"].get("data_source_id") == ds["id"] and not p["in_trash"]
            and (self.ignore_query_filters or _matches_filter(p, body.get("filter")))
        ]
        size = min(int(body.get("page_size", _DEFAULT_PAGE_SIZE)), _DEFAULT_PAGE_SIZE)
        start = int(body.get("start_cursor") or 0)
        chunk = rows[start:start + size]
        more = start + size < len(rows)
        return httpx.Response(200, json={
            "object": "list", "results": chunk, "has_more": more,
            "next_cursor": str(start + size) if more else None,
        })

    def _list_views(self, query: dict[str, list[str]]) -> httpx.Response:
        db = query.get("database_id", [None])[0]
        dsid = query.get("data_source_id", [None])[0]
        items = [
            {"id": v["id"]} for v in self.views.values()
            if (db is None or v.get("database_id") == db)
            and (dsid is None or v.get("data_source_id") == dsid)
        ]
        start = int(query.get("start_cursor", ["0"])[0])
        size = self.views_page_size
        more = start + size < len(items)
        return httpx.Response(200, json={
            "object": "list", "results": items[start:start + size], "has_more": more,
            "next_cursor": str(start + size) if more else None,
        })
