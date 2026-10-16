"""Object projections follow the GitHub runner's Index/FilteredArray semantics."""

import pytest

from tests.ci import _gha_expr as gha


@pytest.mark.parametrize("value,path,expected", [
    ([{"name": "apple"}, {"name": "pear"}], "data.*.name", ["apple", "pear"]),
    ({"a": {"name": "apple"}, "b": {"name": "pear"}}, "data.*.name", ["apple", "pear"]),
    ([{}, None, "scalar", {"name": None}, {"name": "pear"}], "data.*.name", [None, "pear"]),
    ({"a": {"parts": ["roots", "stems"]}, "b": {"parts": ["leaves"]}},
     "data.*.parts", [["roots", "stems"], ["leaves"]]),
    ([{"parts": ["roots", "stems"]}, {"parts": ["leaves"]}],
     "data.*.parts.*", ["roots", "stems", "leaves"]),
    ([{"a": {"name": "apple"}}, {"b": {"name": "pear"}}], "data.*.*.name", ["apple", "pear"]),
    ([], "data.*.name", []), (None, "data.*.name", []),
    ("scalar", "data.*.name", []), ({}, "data.missing.*.name", []),
    ([{"name": "apple"}], "data.name", None),
])
def test_object_filter_projection(value, path, expected):
    assert gha.evaluate(path, {"data": value}) == expected


def test_filtered_contains_handles_missing_context_without_hiding_unsupported_syntax():
    expression = "contains(github.event.pull_request.labels.*.name, 'run-os-tests')"
    assert not gha.evaluate(expression, {})
    context = {"github": {"event": {"pull_request": {"labels": [{"name": "RUN-OS-TESTS"}]}}}}
    assert gha.evaluate(expression, context)
    for unsupported in ("unknownFunction(data.*.name)", "data[0]", "data.*.name + 'x'"):
        with pytest.raises(gha.Unsupported):
            gha.evaluate(unsupported, context)
