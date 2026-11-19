"""Check independent scenario arithmetic and full historical reuse offline."""
import importlib.util
import json
from pathlib import Path
from decimal import Decimal
import duckdb

ROOT = Path(__file__).resolve().parents[1]
HERE = ROOT/'examples/dunder-mifflin-demo'
spec = importlib.util.spec_from_file_location('team_fixture', HERE/'fixture.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


def test_all_eight_hand_derived_answers_and_relationships():
    con = duckdb.connect()
    data = fixture.dataset()
    for name,(columns,rows) in data.items():
        con.execute(f'CREATE TABLE {name} ('+','.join(f'{n} {t}' for n,t in columns)+')')
        con.executemany(f'INSERT INTO {name} VALUES ('+','.join('?' for _ in columns)+')',rows)
    questions = json.loads((HERE/'questions.json').read_text())
    assert len(questions)==8
    for q in questions:
        actual=con.execute(q['sql']).fetchall()
        def normalize(rows):
            return [[Decimal(str(v)) if isinstance(v,(int,float,Decimal)) else v for v in row] for row in rows]
        assert normalize(actual)==normalize(q['expected']),q['id']
    assert con.execute('SELECT COUNT(*) FROM orders').fetchone()==(240,)
    assert con.execute('SELECT COUNT(*) FROM orders JOIN customers USING(customer_id) JOIN employees USING(employee_id) JOIN branches USING(branch_id) JOIN products USING(product_id)').fetchone()==(240,)
    assert con.execute("SELECT COUNT(*) FROM history_orders WHERE order_contact_email NOT LIKE '%@example.com'").fetchone()==(0,)
    assert fixture.dataset()==data
    con.close()
