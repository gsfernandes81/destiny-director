"""destiny manifest in postgres

The two tables that take the Destiny manifest off local disk: a version row per
manifest Bungie has published and we have loaded, and the flattened definitions
themselves. See dd.common.schemas.DestinyManifestVersion / DestinyManifestDefinition
and plans/manifest_in_postgres.md.

Both are pure additions — nothing reads them until the loader lands, and the on-disk
resolver keeps working untouched while they are empty.

Revision ID: c1a4e93f5b20
Revises: b7df22b81517
Create Date: 2026-08-11 06:15:00.000000+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c1a4e93f5b20"
down_revision: str | None = "b7df22b81517"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "destiny_manifest_version",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("version", sa.VARCHAR(length=160), nullable=False),
        sa.Column("state", sa.VARCHAR(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("activated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("version"),
    )
    op.create_table(
        "destiny_manifest_definition",
        sa.Column("version_id", sa.Integer(), nullable=False),
        sa.Column("table_name", sa.VARCHAR(length=64), nullable=False),
        sa.Column("hash", sa.BigInteger(), nullable=False),
        sa.Column(
            "definition",
            sa.JSON().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("version_id", "table_name", "hash"),
    )


def downgrade() -> None:
    op.drop_table("destiny_manifest_definition")
    op.drop_table("destiny_manifest_version")
