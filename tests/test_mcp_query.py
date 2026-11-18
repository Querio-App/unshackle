import datetime
import decimal
import time

import pytest

from unshackle.mcp.query import validate_sql, run_query, json_value
from unshackle.run import query


@pytest.mark.parametrize('sql', ['delete from x', 'select 1; select 2', 'with x as (delete from t returning *) select * from x',
    'select * into other from t', 'copy (select 1) to \'x\'', 'select * from t for update', 'create table t as select 1'])
def test_reject_writes(sql):
    with pytest.raises(ValueError):
        validate_sql(sql)


@pytest.mark.parametrize('sql', ['-- comment\nselect 1;', 'with x as (select 1) select * from x', 'select 1 union all select 2'])
def test_accept_queries(sql):
    assert validate_sql(sql)


def test_caps_and_json(monkeypatch):
    monkeypatch.setenv('UNSHACKLE_WAREHOUSE_URL', 'duckdb://')
    result = run_query('select * from range(10000000)', 3)
    assert result == {'columns': ['range'], 'rows': [[0], [1], [2]], 'row_count': 3, 'truncated': True}
    assert run_query('select 1', 1)['truncated'] is False
    for limit in (0, -1, 10001, True):
        with pytest.raises(ValueError):
            run_query('select 1', limit)
    assert json_value(decimal.Decimal('1.234567890123456789')) == '1.234567890123456789'
    assert json_value(datetime.date(2026, 1, 1)) == '2026-01-01'
    assert json_value(float('nan')) is None


@pytest.mark.parametrize('url,sql', [
    ('duckdb://', 'select sum(a.i*b.i) from range(100000000) a(i), range(100000000) b(i)'),
    ('sqlite://', 'WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) SELECT sum(x) FROM c')])
def test_native_timeout_stops_query(url, sql):
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        query(sql, {}, url, limit=2, timeout=.05)
    assert time.monotonic() - started < 3
    assert query('select 42 as answer', {}, url, limit=1, timeout=1).iloc[0, 0] == 42


def test_errors_do_not_expose_credentials(monkeypatch):
    monkeypatch.setenv('UNSHACKLE_WAREHOUSE_URL', 'postgresql+psycopg://secret:password@example.invalid/db')
    monkeypatch.setattr('unshackle.run.query', lambda *a, **k: (_ for _ in ()).throw(ValueError('secret password')))
    with pytest.raises(ValueError, match='Warehouse query failed') as error:
        run_query('select 1')
    assert 'password' not in str(error.value)


@pytest.mark.parametrize('sql', ['select 1; -- trailing comment', 'select 1 -- trailing comment'])
def test_trailing_comments(monkeypatch, sql):
    monkeypatch.setenv('UNSHACKLE_WAREHOUSE_URL', 'duckdb://')
    assert run_query(sql)['rows'] == [[1]]


@pytest.mark.parametrize('url', ['duckdb://', 'sqlite://'])
def test_literal_json_colons_and_percent_are_not_bind_parameters(url):
    frame = query("select '{\"a\":1}' as doc, '100%' as percent", {}, url, limit=1, timeout=1)
    assert frame.iloc[0].tolist() == ['{"a":1}', '100%']


def test_postgres_dialect_preserved_at_execution_boundary(monkeypatch):
    import pandas as pd
    monkeypatch.setenv('UNSHACKLE_WAREHOUSE_URL', 'postgresql+psycopg://reader:example@example.invalid/db')
    sqls = []
    def capture(sql, params, **kwargs):
        sqls.append(sql)
        return pd.DataFrame({'answer': [1]})
    monkeypatch.setattr('unshackle.run.query', capture)
    run_query("select CURRENT_TIMESTAMP::date, '{\"a\":1}'::jsonb ->> 'a', ARRAY[1,2]")
    assert 'CURRENT_TIMESTAMP()' not in sqls[0]
    assert 'JSON_EXTRACT' not in sqls[0]
    assert 'ARRAY[1, 2]' in sqls[0]
    assert "->> 'a'" in sqls[0]
