from __future__ import annotations

from alembic import context

config = context.config
connection = config.attributes["connection"]

context.configure(connection=connection, transaction_per_migration=True)
with context.begin_transaction():
    context.run_migrations()
