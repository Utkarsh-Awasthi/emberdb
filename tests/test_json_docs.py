from emberdb.protocol import RespError

import pytest


def test_json_set_get_root(d, conn):
    d(conn, "JSON.SET", "doc", "$", '{"name": "Widget", "price": 9.5}')
    assert d(conn, "JSON.GET", "doc", "$.name") == '"Widget"'
    assert d(conn, "JSON.GET", "doc", "$.price") == "9.5"


def test_json_get_whole_doc_default_path(d, conn):
    d(conn, "JSON.SET", "doc", "$", '{"a": 1}')
    import json
    assert json.loads(d(conn, "JSON.GET", "doc")) == {"a": 1}


def test_json_set_nested_path(d, conn):
    d(conn, "JSON.SET", "doc", "$", '{"user": {"name": "Bob"}}')
    d(conn, "JSON.SET", "doc", ".user.age", "30")
    assert d(conn, "JSON.GET", "doc", "$.user.age") == "30"


def test_json_set_on_missing_key_requires_root_path(d, conn):
    with pytest.raises(RespError):
        d(conn, "JSON.SET", "nosuch", ".field", "1")


def test_json_del_whole_key(d, conn):
    d(conn, "JSON.SET", "doc", "$", '{"a": 1}')
    assert d(conn, "JSON.DEL", "doc") == 1
    assert d(conn, "EXISTS", "doc") == 0


def test_json_del_nested_path(d, conn):
    d(conn, "JSON.SET", "doc", "$", '{"a": 1, "b": 2}')
    assert d(conn, "JSON.DEL", "doc", "$.a") == 1
    import json
    assert json.loads(d(conn, "JSON.GET", "doc")) == {"b": 2}


def test_json_type(d, conn):
    d(conn, "JSON.SET", "doc", "$", '{"s": "x", "n": 1, "a": [1,2], "o": {}, "b": true, "z": null}')
    assert d(conn, "JSON.TYPE", "doc", "$.s") == "string"
    assert d(conn, "JSON.TYPE", "doc", "$.n") == "number"
    assert d(conn, "JSON.TYPE", "doc", "$.a") == "array"
    assert d(conn, "JSON.TYPE", "doc", "$.o") == "object"
    assert d(conn, "JSON.TYPE", "doc", "$.b") == "boolean"
    assert d(conn, "JSON.TYPE", "doc", "$.z") == "null"


def test_json_numincrby(d, conn):
    d(conn, "JSON.SET", "doc", "$", '{"n": 10}')
    assert d(conn, "JSON.NUMINCRBY", "doc", "$.n", "5") == "15"


def test_json_arrlen_arrappend(d, conn):
    d(conn, "JSON.SET", "doc", "$", '{"tags": ["a", "b"]}')
    assert d(conn, "JSON.ARRLEN", "doc", "$.tags") == 2
    assert d(conn, "JSON.ARRAPPEND", "doc", "$.tags", '"c"') == 3
    import json
    assert json.loads(d(conn, "JSON.GET", "doc", "$.tags")) == ["a", "b", "c"]


def test_json_objlen(d, conn):
    d(conn, "JSON.SET", "doc", "$", '{"a": 1, "b": 2}')
    assert d(conn, "JSON.OBJLEN", "doc", "$") == 2


def test_json_strlen(d, conn):
    d(conn, "JSON.SET", "doc", "$", '{"s": "hello"}')
    assert d(conn, "JSON.STRLEN", "doc", "$.s") == 5


def test_json_set_nx_xx(d, conn):
    d(conn, "JSON.SET", "doc", "$", '{"a": 1}')
    assert d(conn, "JSON.SET", "doc", "$", '{"a": 2}', "NX") is None
    import json
    assert json.loads(d(conn, "JSON.GET", "doc")) == {"a": 1}


def test_json_clear(d, conn):
    d(conn, "JSON.SET", "doc", "$", '{"tags": ["a", "b"], "n": 5}')
    d(conn, "JSON.CLEAR", "doc", "$.tags")
    d(conn, "JSON.CLEAR", "doc", "$.n")
    import json
    assert json.loads(d(conn, "JSON.GET", "doc")) == {"tags": [], "n": 0}


def test_json_invalid_json_raises(d, conn):
    with pytest.raises(RespError):
        d(conn, "JSON.SET", "doc", "$", "not valid json")
