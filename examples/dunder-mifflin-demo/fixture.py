#!/usr/bin/env python3
"""Deterministic fictional team warehouse; credentials never enter generated context."""
from __future__ import annotations
from decimal import Decimal
import argparse
import importlib.util
import json
import os
from pathlib import Path
import secrets
import sys

import duckdb
from psycopg import sql
import yaml
from unshackle import build, scaffold
from unshackle.imports import warehouse, dbt

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('railway_fixture', ROOT / 'deploy/railway/fixture.py')
railway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(railway)


def dataset():
    """Reuse every historical row; redact legacy fictional contact domains on export."""
    con = duckdb.connect()
    result = {}
    for name in ('accounts', 'products', 'orders', 'shippings', 'reviews'):
        path = ROOT / 'tests/data/dunder_mifflin' / (name + '.parquet')
        columns = con.execute('DESCRIBE SELECT * FROM read_parquet(?)', [str(path)]).fetchall()
        rows = con.execute('SELECT * FROM read_parquet(?)', [str(path)]).fetchall()
        if name == 'orders':
            index = [c[0] for c in columns].index('order_contact_email')
            rows = [tuple(f'contact-{n}@example.com' if i == index else v for i,v in enumerate(row)) for n,row in enumerate(rows)]
        result['history_' + name] = ([(c[0], c[1]) for c in columns], rows)
    con.close()
    result['branches'] = ([('branch_id','INTEGER PRIMARY KEY'),('branch_name','TEXT')], [(1,'Scranton'),(2,'Stamford'),(3,'Nashua')])
    names = ['Jim Halpert','Dwight Schrute','Karen Filippelli','Andy Bernard','Holly Flax','Danny Cordray']
    result['employees'] = ([('employee_id','INTEGER PRIMARY KEY'),('employee_name','TEXT'),('branch_id','INTEGER REFERENCES branches(branch_id)')], [(r,names[r-1],(r+1)//2) for r in range(1,7)])
    result['customers'] = ([('customer_id','INTEGER PRIMARY KEY'),('customer_name','TEXT'),('employee_id','INTEGER REFERENCES employees(employee_id)')], [(2*r-2+k,f'Example Customer {2*r-2+k:02}',r) for r in range(1,7) for k in (1,2)])
    # Reuse product identities/names while declaring separate, deliberately simple scenario prices.
    products = sorted(result['history_products'][1])[:3]
    result['products'] = ([('product_id','TEXT PRIMARY KEY'),('product_name','TEXT'),('unit_price','DECIMAL(12,2)'),('unit_cost','DECIMAL(12,2)')], [(p[0],p[1],10*(i+1),6*(i+1)) for i,p in enumerate(products)])
    rows = []
    for month in range(1,13):
        for rep in range(1,7):
            for i,p in enumerate(products):
                gross = rep * month * 10*(i+1)
                discount = gross // 10 if rep % 2 == 0 else 0
                rows.append((len(rows)+1,f'2025-{month:02}-15',2*rep-(month%2),p[0],rep*month,gross,discount,gross-discount,Decimal(gross-discount)*Decimal('0.6'),'completed'))
        for status,amount in [('returned',1000),('cancelled',2000)]:
            rows.append((len(rows)+1,f'2025-{month:02}-20',1,products[0][0],100,amount,0,amount,amount*6//10,status))
    result['orders'] = ([('order_id','INTEGER PRIMARY KEY'),('ordered_at','DATE'),('customer_id','INTEGER REFERENCES customers(customer_id)'),('product_id','TEXT REFERENCES products(product_id)'),('quantity','INTEGER'),('gross_usd','DECIMAL(12,2)'),('discount_usd','DECIMAL(12,2)'),('net_usd','DECIMAL(12,2)'),('cost_usd','DECIMAL(12,2)'),('status','TEXT')],rows)
    return result


def seed(output: Path, local=False):
    output = output.resolve()
    if output.is_relative_to(ROOT):
        raise ValueError('Credentials must remain outside checkout')
    public = railway.connection_url(os.environ['DATABASE_PUBLIC_URL'],local=local)
    private = railway.connection_url(os.environ['DATABASE_URL'],local=local)
    output.mkdir(mode=0o700,exist_ok=False)
    password = secrets.token_urlsafe(32)
    reader = public.set(username=railway.ROLE,password=password)
    railway.private_file(output/'warehouse-url', private.set(username=railway.ROLE,password=password).render_as_string(hide_password=False))
    railway.private_file(output/'reader-public-url',reader.render_as_string(hide_password=False))
    railway.private_file(output/'mcp-token',secrets.token_urlsafe(32))
    data = dataset()
    with railway.connect(public) as con:
        if con.execute("SELECT 1 FROM pg_roles WHERE rolname='example_reader'").fetchone() or con.execute("SELECT 1 FROM pg_tables WHERE schemaname='public'").fetchone():
            raise ValueError('Dedicated database must be empty')
        for name,(columns,rows) in data.items():
            definitions = sql.SQL(',').join(sql.SQL('{} {}').format(sql.Identifier(n),sql.SQL('DOUBLE PRECISION' if t == 'DOUBLE' else t)) for n,t in columns)
            con.execute(sql.SQL('CREATE TABLE {} ({})').format(sql.Identifier(name),definitions))
            with con.cursor().copy(sql.SQL('COPY {} FROM STDIN').format(sql.Identifier(name))) as copy:
                for row in rows: copy.write_row(row)
        con.execute(sql.SQL('CREATE ROLE example_reader LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD {}').format(sql.Literal(password)))
        con.execute(sql.SQL('REVOKE ALL ON DATABASE {} FROM PUBLIC').format(sql.Identifier(public.database)))
        con.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO example_reader').format(sql.Identifier(public.database)))
        con.execute('REVOKE CREATE ON SCHEMA public FROM PUBLIC')
        con.execute('GRANT USAGE ON SCHEMA public TO example_reader')
        con.execute('GRANT SELECT ON ALL TABLES IN SCHEMA public TO example_reader')
        con.execute('ALTER ROLE example_reader SET default_transaction_read_only=on')
    verify_reader(reader)
    repo = output/'context'
    scaffold.init(repo)
    build.apply(warehouse.introspect(reader.render_as_string(hide_password=False)),repo)
    build.apply(dbt.parse(HERE/'dbt'),repo)
    (repo/'knowledge/team-demo.md').write_text((HERE/'business-context.md').read_text())
    print('Seeded 11 fictional tables; context generated; reader grants verified; credentials private.')


def verify_reader(reader):
    with railway.connect(reader) as con:
        assert con.execute('SHOW transaction_read_only').fetchone()==('on',)
        for statement in ('UPDATE orders SET net_usd=0 WHERE false','DELETE FROM orders WHERE false','CREATE TABLE forbidden(id int)','CREATE TEMP TABLE forbidden(id int)'):
            con.execute('SET TRANSACTION READ WRITE')
            try: con.execute(statement)
            except railway.psycopg.errors.InsufficientPrivilege: con.rollback()
            else: raise RuntimeError('Reader has write privilege')

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--local-test',action='store_true')
    args=parser.parse_args()
    try: seed(args.output,args.local_test)
    except Exception:
        print('Seed failed; inspect privately. Credentials retained for recovery.',file=sys.stderr)
        sys.exit(1)
