"""Announce to the origin Slack thread the moment something actually goes out —
distinct from the button-click confirmation, which fires at approval time."""
import json


async def notify_campaign_going_out(pool, slack, campaign_id) -> None:
    row = await pool.fetchrow(
        "select name, channel, thread_ts from campaigns where id=$1", campaign_id)
    if row is None or not row["channel"]:
        return
    await slack.post_message(
        row["channel"],
        text=f"<!channel> 🚀 *{row['name']}* is going out now.",
        thread_ts=row["thread_ts"])


async def notify_post_going_out(pool, slack, post_id) -> None:
    row = await pool.fetchrow(
        "select content, channel, thread_ts from social_posts where id=$1", post_id)
    if row is None or not row["channel"]:
        return
    text = json.loads(row["content"])["text"]
    preview = text if len(text) <= 300 else text[:300] + "…"
    await slack.post_message(
        row["channel"],
        text=f"<!channel> 📣 Post is going live now:\n>{preview}",
        thread_ts=row["thread_ts"])
