from app.services.suppressions import add_suppression, is_suppressed, suppressed_subset


async def test_add_and_check_normalizes_email(pool):
    await add_suppression(pool, " Ada@Example.COM ", reason="unsubscribe", source="unsub_page")
    assert await is_suppressed(pool, "ada@example.com") is True
    assert await is_suppressed(pool, "ADA@EXAMPLE.COM") is True
    assert await is_suppressed(pool, "other@example.com") is False


async def test_first_reason_wins(pool):
    await add_suppression(pool, "a@b.co", reason="hard_bounce", source="resend", ghl_contact_id="c1")
    await add_suppression(pool, "a@b.co", reason="complaint", source="resend")
    row = await pool.fetchrow("select reason, ghl_contact_id from suppressions where email='a@b.co'")
    assert row["reason"] == "hard_bounce"
    assert row["ghl_contact_id"] == "c1"


async def test_suppressed_subset(pool):
    await add_suppression(pool, "a@b.co", reason="ghl_dnd", source="ghl")
    await add_suppression(pool, "c@d.co", reason="complaint", source="resend")
    result = await suppressed_subset(pool, ["A@B.CO", "x@y.co", "c@d.co"])
    assert result == {"a@b.co", "c@d.co"}
