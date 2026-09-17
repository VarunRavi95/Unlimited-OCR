from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_trust_thresholds_must_be_ordered(tmp_path) -> None:
    with pytest.raises(ValidationError, match="TRUST_SUPPORTED_THRESHOLD"):
        Settings(
            data_dir=tmp_path,
            trust_supported_threshold=0.70,
            trust_review_threshold=0.90,
        )
