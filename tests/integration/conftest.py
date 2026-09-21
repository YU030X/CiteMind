import os

import pytest
from database_guard import (
    DestructiveTestDatabase,
    GuardError,
    MissingTestDatabaseError,
    resolve_destructive_test_database,
)


@pytest.fixture(scope="session")
def destructive_test_database() -> DestructiveTestDatabase:
    try:
        return resolve_destructive_test_database(os.environ)
    except MissingTestDatabaseError as error:
        pytest.skip(str(error))
    except GuardError as error:
        pytest.fail(str(error), pytrace=False)
