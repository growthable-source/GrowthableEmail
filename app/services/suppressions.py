def normalize(email: str) -> str:
    return email.strip().lower()


async def add_suppression(pool, email: str, *, reason: str, source: str,
                          ghl_contact_id: str | None = None) -> None:
    await pool.execute(
        "insert into suppressions (email, ghl_contact_id, reason, source) "
        "values ($1, $2, $3, $4) on conflict (email) do nothing",
        normalize(email), ghl_contact_id, reason, source,
    )


async def is_suppressed(pool, email: str) -> bool:
    return await pool.fetchval(
        "select exists(select 1 from suppressions where email = $1)", normalize(email)
    )


async def suppressed_subset(pool, emails: list[str]) -> set[str]:
    rows = await pool.fetch(
        "select email from suppressions where email = any($1::text[])",
        [normalize(e) for e in emails],
    )
    return {r["email"] for r in rows}
