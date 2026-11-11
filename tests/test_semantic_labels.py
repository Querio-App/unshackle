"""MetricFlow dimension labels must be unique within each semantic model."""

from copy import deepcopy
from pathlib import Path

import pytest
import yaml
from jinja2 import Environment

from unshackle.ir import Bundle, Dimension, SemanticModel
from unshackle.target.layout import Layout
from unshackle.target.semantic import render


@pytest.mark.parametrize("reverse", [False, True])
def test_dimension_labels_reserve_existing_fallback_and_suffixed_labels(tmp_path: Path, reverse: bool):
    dimensions = [
        Dimension("user_id", label="User ID"),
        Dimension("name", label="User ID", expr="person_name", description="Account owner",
                  source_expression="people.name", source_language="sql"),
        Dimension("another_name", label="User ID"),
        Dimension("existing", label="User ID (name)"),
        Dimension("suffixed", label="User ID (name) (2)"),
        Dimension("unlabelled"),
        Dimension("explicit", label="unlabelled"),
        Dimension("month", "time"),
        Dimension("reserved", label="orders_month"),
        Dimension("unique", label="Unique"),
        Dimension("pending", label="Unique", todo="translate expression", source_expression="source()"),
        Dimension("templated", label="Unique", expr="{{ source() }}"),
        Dimension("template_a", label="{{ display }}"),
        Dimension("template_b", label="{{ display }}"),
        Dimension("template_existing", label="{{ display }} (template_b)"),
    ]
    if reverse:
        dimensions.reverse()
    bundle = Bundle(tool="demo", semantic_models=[
        SemanticModel("orders", "orders", dimensions=dimensions),
        SemanticModel("other", "other", dimensions=[Dimension("user_id", label="User ID")]),
    ])
    original = deepcopy(bundle)
    files = render(bundle, Layout.load(tmp_path))
    assert files == render(bundle, Layout.load(tmp_path))
    assert bundle == original  # retain source labels, SQL, descriptions and provenance in the IR
    text = files["semantic/orders.yml"]
    dims = {d["name"]: d for d in yaml.safe_load(text)["semantic_models"][0]["dimensions"]}
    labels = [Environment().from_string(d.get("label", d["name"])).render() for d in dims.values()]
    assert len(labels) == len(set(labels))
    assert dims["existing"]["label"] == "User ID (name)"
    assert dims["suffixed"]["label"] == "User ID (name) (2)"
    assert dims["unique"]["label"] == "Unique"
    assert "label" not in dims["unlabelled"] and "label" not in dims["orders_month"]
    assert dims["explicit"]["label"] == "unlabelled (explicit)"
    assert dims["reserved"]["label"] == "orders_month (reserved)"
    assert dims["name"]["expr"] == "person_name" and dims["name"]["description"] == "Account owner"
    assert dims["orders_month"]["expr"] == "month"
    assert "pending" not in dims and "templated" not in dims
    assert "# TODO(unshackle): dimension orders.pending: translate expression source()" in text
    assert yaml.safe_load(files["semantic/other.yml"])["semantic_models"][0]["dimensions"][0]["label"] == "User ID"
    if not reverse:
        assert dims["user_id"]["label"] == "User ID"
        assert dims["name"]["label"] == "User ID (name) (3)"
        assert dims["another_name"]["label"] == "User ID (another_name)"
        assert dims["template_b"]["label"] == "{% raw %}{{ display }} (template_b) (2){% endraw %}"
