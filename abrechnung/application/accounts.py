from datetime import datetime, timezone

from abrechnung.domain.accounts import Account
from . import Application, NotFoundError, check_group_permissions, InvalidCommand


class AccountService(Application):
    async def list_accounts(self, *, user_id: int, group_id: int) -> list[Account]:
        async with self.db_pool.acquire() as conn:
            async with conn.transaction():
                await check_group_permissions(
                    conn=conn, group_id=group_id, user_id=user_id
                )
                cur = conn.cursor(
                    "select id, type, revision_id, name, description, priority "
                    "from latest_account "
                    "where group_id = $1 and user_id = $2 and deleted = false",
                    group_id,
                    user_id,
                )
                result = []
                async for account in cur:
                    result.append(
                        Account(
                            id=account["id"],
                            type=account["type"],
                            name=account["name"],
                            description=account["description"],
                            priority=account["priority"],
                            deleted=False,
                        )
                    )

                return result

    async def get_account(
        self, *, user_id: int, group_id: int, account_id: int
    ) -> Account:
        async with self.db_pool.acquire() as conn:
            await check_group_permissions(conn=conn, group_id=group_id, user_id=user_id)
            account = await conn.fetchrow(
                "select id, type, revision_id, name, description, priority "
                "from latest_account "
                "where group_id = $1 and user_id = $2 and id = $3 and deleted = false",
                group_id,
                user_id,
                account_id,
            )
            if account is None:
                raise NotFoundError(f"No account with id {account_id} exists")

            return Account(
                id=account["id"],
                type=account["type"],
                name=account["name"],
                description=account["description"],
                priority=account["priority"],
                deleted=False,
            )

    async def create_account(
        self,
        *,
        user_id: int,
        group_id: int,
        type: str,
        name: str,
        description: str,
        priority: int = 0,
    ) -> int:
        async with self.db_pool.acquire() as conn:
            async with conn.transaction():
                await check_group_permissions(
                    conn=conn, group_id=group_id, user_id=user_id, can_write=True
                )
                account_id = await conn.fetchval(
                    "insert into account (group_id, type) values ($1, $2) returning id",
                    group_id,
                    type,
                )
                now = datetime.now(tz=timezone.utc)
                revision_id = await conn.fetchval(
                    "insert into account_revision (user_id, account_id, started, committed) "
                    "values ($1, $2, $3, $4) returning id",
                    user_id,
                    account_id,
                    now,
                    now,
                )
                await conn.execute(
                    "insert into account_history (id, revision_id, name, description, priority) "
                    "values ($1, $2, $3, $4, $5)",
                    account_id,
                    revision_id,
                    name,
                    description,
                    priority,
                )
                return account_id

    async def update_account(
        self,
        user_id: int,
        group_id: int,
        account_id: int,
        name: str,
        description: str,
        priority: int = 0,
    ):
        # TODO: figure out the more complex logic once we have accounts stuck in uncommitted states
        async with self.db_pool.acquire() as conn:
            async with conn.transaction():
                await check_group_permissions(
                    conn=conn, group_id=group_id, user_id=user_id, can_write=True
                )
                account = await conn.fetchrow(
                    "select id, type, revision_id, name, description, priority "
                    "from latest_account "
                    "where group_id = $1 and user_id = $2 and id = $3 and deleted = false",
                    group_id,
                    user_id,
                    account_id,
                )
                if account is None:
                    raise NotFoundError(f"No account with id {account_id} exists")

                if (
                    name != account["name"]
                    or description != account["description"]
                    or priority != account["priority"]
                ):
                    """if there is something to change initialize a new revision and a new entry in the history table"""
                    now = datetime.now(tz=timezone.utc)
                    revision_id = await conn.fetchval(
                        "insert into account_revision (user_id, account_id, started, committed) "
                        "values ($1, $2, $3, $4) returning id",
                        user_id,
                        account_id,
                        now,
                        now,
                    )
                    await conn.execute(
                        "insert into account_history (id, revision_id, name, description, priority) "
                        "values ($1, $2, $3, $4, $5)",
                        account_id,
                        revision_id,
                        name,
                        description,
                        priority,
                    )

    async def delete_account(
        self,
        user_id: int,
        group_id: int,
        account_id: int,
    ):
        async with self.db_pool.acquire() as conn:
            async with conn.transaction():
                await check_group_permissions(
                    conn=conn, group_id=group_id, user_id=user_id, can_write=True
                )
                balance = await conn.fetchval(
                    "select balance from account_balance where account_id = $1",
                    account_id,
                )

                if balance is None:
                    raise InvalidCommand(f"Cannot delete a non existing account")

                if balance != 0:
                    raise InvalidCommand(
                        f"Cannot delete an account with a balance != 0"
                    )

                row = await conn.fetchrow(
                    "select revision_id, deleted from latest_account where id = $1 and group_id = $2",
                    account_id,
                    group_id,
                )
                if row is None:
                    raise InvalidCommand(
                        f"Cannot delete an account without any committed changes"
                    )

                if row["deleted"]:
                    raise InvalidCommand(f"Cannot delete an already deleted account")

                now = datetime.now(tz=timezone.utc)
                revision_id = await conn.fetchval(
                    "insert into account_revision (user_id, account_id, started, committed) "
                    "values ($1, $2, $3, $4) returning id",
                    user_id,
                    account_id,
                    now,
                    now,
                )
                await conn.execute(
                    "insert into account_history (id, revision_id, name, description, priority, deleted) "
                    "select $1, $2, name, description, priority, true "
                    "from account_history ah where ah.id = $1 and ah.revision_id = $3 ",
                    account_id,
                    revision_id,
                    row["revision_id"],
                )
