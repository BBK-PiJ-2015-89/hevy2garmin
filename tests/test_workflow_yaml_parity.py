"""The web dashboard builds the fork's sync workflow too (web/lib/github.ts). Both stacks must
write the same file: this golden fixture is generated from the Python builder and the web test
(web/lib/github.test.ts) asserts against the same file (#458)."""
import os
from pathlib import Path

import pytest

from hevy2garmin.server import _build_sync_workflow_yaml, _minutes_to_cron

FIXTURE = Path(__file__).parent / "fixtures" / "sync_workflow_120.yml"


def test_workflow_yaml_matches_golden():
    assert _build_sync_workflow_yaml(120) == FIXTURE.read_text()


def test_cron_table():
    assert _minutes_to_cron(30) == "*/30 * * * *"
    assert _minutes_to_cron(60) == "0 * * * *"
    assert _minutes_to_cron(240) == "0 */4 * * *"
    assert _minutes_to_cron(1440) == "0 0 * * *"
    assert _minutes_to_cron(45) == "0 */2 * * *"


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set")
def test_flat_garmin_token_row_is_nested_on_schema_init():
    """#459: a row written by garmin-auth < 0.3 (flat DI payload) self-heals into the nested shape."""
    import json

    from hevy2garmin.db_postgres import PostgresDatabase

    db = PostgresDatabase(os.environ["DATABASE_URL"])
    with db._get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM platform_credentials WHERE platform = 'garmin_tokens'")
            cur.execute(
                "INSERT INTO platform_credentials (platform, auth_type, credentials, status) VALUES ('garmin_tokens', 'oauth', %s, 'active')",
                (json.dumps({"di_token": "t", "di_refresh_token": "r", "di_client_id": "c"}),),
            )
        conn.commit()
    db._ensure_tables()
    db._ensure_tables()  # idempotent
    with db._get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT credentials FROM platform_credentials WHERE platform = 'garmin_tokens'")
            row = cur.fetchone()  # RealDictCursor: a dict, not a tuple
            creds = row["credentials"] if isinstance(row, dict) else row[0]
            creds = json.loads(creds) if isinstance(creds, str) else creds
            cur.execute("DELETE FROM platform_credentials WHERE platform = 'garmin_tokens'")
        conn.commit()
    assert creds == {"garmin_tokens": {"di_token": "t", "di_refresh_token": "r", "di_client_id": "c"}}
