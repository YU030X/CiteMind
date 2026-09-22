import os

import database_roles_guard
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


def resolve_role_test_databases_or_skip_or_fail() -> database_roles_guard.RoleTestDatabases:
    """三个 DSN 都缺失时跳过；部分缺失或不合法时失败，不尝试连接。"""

    try:
        return database_roles_guard.resolve_role_test_databases(os.environ)
    except database_roles_guard.MissingTestDatabasesError as error:
        pytest.skip(str(error))
    except database_roles_guard.GuardError as error:
        pytest.fail(str(error), pytrace=False)


@pytest.fixture(scope="session")
def role_test_databases() -> database_roles_guard.RoleTestDatabases:
    return resolve_role_test_databases_or_skip_or_fail()
