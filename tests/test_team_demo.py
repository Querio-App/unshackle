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


def test_seed_failure_does_not_print_credentials(tmp_path):
    import os
    import subprocess
    import sys
    result = subprocess.run([sys.executable,str(HERE/'fixture.py'),'--output',str(tmp_path/'fixture')],
                            env={**os.environ,'DATABASE_URL':'private-invalid-url','DATABASE_PUBLIC_URL':'private-invalid-url'},
                            capture_output=True,text=True)
    assert result.returncode==1
    assert 'private-invalid-url' not in result.stdout+result.stderr
    assert 'Traceback' not in result.stderr


def test_fresh_postgres_seed_context_permissions(tmp_path,monkeypatch):
    import os
    import secrets
    import subprocess
    import time
    import pytest
    from sqlalchemy.engine import URL
    if os.environ.get('UNSHACKLE_TEAM_DEMO_LOCAL_TEST')!='1':
        pytest.skip('explicit isolated Docker opt-in required')
    name='unshackle-team-test-'+secrets.token_hex(5)
    password=secrets.token_urlsafe(24)
    started=False
    try:
        subprocess.run(['docker','run','-d','--name',name,'-p','127.0.0.1::5432','-e','POSTGRES_PASSWORD','-e','POSTGRES_DB=example','postgres:16-alpine'],env={**os.environ,'POSTGRES_PASSWORD':password},check=True,capture_output=True)
        started=True
        for _ in range(100):
            if subprocess.run(['docker','exec',name,'pg_isready','-h','127.0.0.1','-U','postgres','-d','example'],capture_output=True).returncode==0: break
            time.sleep(.2)
        else: pytest.fail('Postgres readiness timeout')
        port=int(subprocess.check_output(['docker','port',name,'5432/tcp'],text=True).strip().rsplit(':',1)[1])
        url=URL.create('postgresql',username='postgres',password=password,host='127.0.0.1',port=port,database='example').render_as_string(hide_password=False)
        monkeypatch.setenv('DATABASE_URL',url)
        monkeypatch.setenv('DATABASE_PUBLIC_URL',url)
        out=tmp_path/'fixture'
        fixture.seed(out,local=True)
        reader=fixture.railway.connection_url((out/'reader-public-url').read_text(),local=True)
        fixture.verify_reader(reader)
        with fixture.railway.connect(reader) as con:
            for q in json.loads((HERE/'questions.json').read_text()):
                actual=con.execute(q['sql']).fetchall()
                assert [[str(v) if isinstance(v,str) else Decimal(str(v)) for v in row] for row in actual]==[[str(v) if isinstance(v,str) else Decimal(str(v)) for v in row] for row in q['expected']]
        assert (out/'context/knowledge/team-demo.md').is_file()
        assert (out/'context/semantic/orders.ossie.yaml').is_file()
        catalog=(out/'context/catalog/warehouse/public/orders.yml').read_text()
        assert 'foreign_keys:' in catalog and 'referred_table: customers' in catalog
        for file in ('mcp-token','warehouse-url','reader-public-url'):
            assert (out/file).stat().st_mode&0o777==0o600
        for p in (out/'context').rglob('*'):
            if p.is_file():
                contents=p.read_text()
                assert password not in contents and reader.password not in contents
                assert (out/'mcp-token').read_text() not in contents
        with pytest.raises(ValueError,match='empty'):
            fixture.seed(tmp_path/'second',local=True)
    finally:
        if started:
            mounts=json.loads(subprocess.check_output(['docker','inspect',name,'--format','{{json .Mounts}}'],text=True))
            volumes=[v['Name'] for v in mounts if v['Type']=='volume']
            subprocess.run(['docker','rm','-fv',name],check=True,capture_output=True)
            for volume in volumes:
                assert subprocess.run(['docker','volume','inspect',volume],capture_output=True).returncode!=0
