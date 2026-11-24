import os
import subprocess
import sys
from pathlib import Path
from time import monotonic, sleep
from urllib.parse import urlsplit
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.errors import InsufficientPrivilege

pytestmark = pytest.mark.integration


def test_parallel_predeploys_finish_on_an_empty_database() -> None:
    url = os.environ.get("VERSTAK_TEST_DATABASE_URL")
    if not url:
        pytest.skip("set VERSTAK_TEST_DATABASE_URL to test parallel migrations")
    name = f"verstak_migrations_{uuid4().hex}"
    test_url = urlsplit(url)._replace(path=f"/{name}").geturl()
    cwd = Path(__file__).resolve().parents[2]
    env = {**os.environ, "VERSTAK_DATABASE_URL": test_url}
    migrations: list[subprocess.Popen[bytes]] = []

    with psycopg.connect(url, autocommit=True) as admin:
        try:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        except InsufficientPrivilege:
            pytest.skip("the migration test requires permission to create its temporary database")
        try:
            with psycopg.connect(test_url, autocommit=True) as holder:
                holder.execute(
                    "select pg_advisory_lock(%s, %s)", (int.from_bytes(b"vstk", "big"), 1)
                )
                for _ in range(3):
                    migrations.append(
                        subprocess.Popen(
                            [sys.executable, "-m", "alembic", "upgrade", "head"],
                            cwd=cwd,
                            env=env,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                    )
                # Release the lock only after every deploy has started asking for it.
                deadline = monotonic() + 15
                while monotonic() < deadline:
                    waiting = holder.execute(
                        "select count(*) from pg_stat_activity where datname = current_database() "
                        "and pid <> pg_backend_pid() and query like 'select pg%advisory_lock(%'"
                    ).fetchone()
                    if waiting == (3,):
                        break
                    sleep(0.1)
                else:
                    pytest.fail("all three migrations did not reach the advisory lock")

            for process in migrations:
                assert process.wait(timeout=20) == 0

            with psycopg.connect(test_url) as database:
                assert database.execute("select count(*) from alembic_version").fetchone() == (1,)
                indexes = database.execute(
                    "select count(*) from pg_index join pg_class on pg_class.oid = indexrelid "
                    "where indisvalid and relname in ('checkpoints_thread_id_idx', "
                    "'checkpoint_blobs_thread_id_idx', 'checkpoint_writes_thread_id_idx')"
                ).fetchone()
                assert indexes == (3,)
        finally:
            for process in migrations:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
