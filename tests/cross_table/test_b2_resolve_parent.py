# -*- coding: utf-8 -*-
"""B2: resolve_parent_table_id."""

from __future__ import annotations

import pytest

from pipeline.block_cross_table.api import resolve_parent_table_id
from pipeline.block_cross_table.errors import CrossTableError


def test_length_one_returns_only_id():
    assert (
        resolve_parent_table_id({}, {"parent_table_ids": ["t0"]}, None) == "t0"
    )


@pytest.mark.parametrize("ids", [[], ["t0", "t1"], ["t0", "t1", "t2"]])
def test_length_not_one_unresolvable(ids):
    with pytest.raises(CrossTableError) as ei:
        resolve_parent_table_id({}, {"parent_table_ids": ids}, None)
    assert ei.value.code == "cross_table_parent_table_unresolvable"
