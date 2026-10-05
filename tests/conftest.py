import json
from pathlib import Path

import pytest

from pbi_migrate.extract import BIObject, normalize

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def objects() -> list[BIObject]:
    data = json.loads((FIXTURES / "bi_server_objects.json").read_text())
    return [normalize(otype, raw) for otype, raws in data.items() if otype.startswith("bi_") for raw in raws]
