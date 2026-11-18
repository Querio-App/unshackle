import yaml
from unshackle.ir import Bundle, Column, Table
from unshackle.target.files import render_table


def test_metadata_roundtrip_and_hand_annotations():
    table = Table('warehouse', 'main', 'orders', columns=[Column('id', 'INTEGER', nullable=False)],
                  primary_key=['id'], foreign_keys=[{'columns': ['id'], 'referred_schema': 'main',
                  'referred_table': 'customers', 'referred_columns': ['id']}])
    restored = Bundle.from_json(Bundle('warehouse', tables=[table]).to_json()).tables[0]
    assert restored == table
    doc = yaml.safe_load(render_table(restored, 'columns:\n- name: id\n  description: Hand annotation\n'))
    assert doc['columns'][0]['nullable'] is False
    assert doc['columns'][0]['description'] == 'Hand annotation'
    assert doc['primary_key'] == ['id']
    assert doc['foreign_keys'][0]['referred_columns'] == ['id']


def test_old_ir_defaults():
    table = Bundle.from_json('{"tool":"test","tables":[{"datasource":"d","schema":"s","name":"t","columns":[{"name":"id"}]}]}').tables[0]
    assert table.primary_key == []
    assert table.columns[0].nullable is None
