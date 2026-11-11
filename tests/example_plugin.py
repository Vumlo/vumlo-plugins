"""The plugin of the README, "a plugin in 30 lines". It lives in the tests, not in the catalog."""

from vumlo_plugins import Plugin, attachment, setting

plugin = Plugin(
    "word-count", "1.0.0", scopes=["submissions:read", "reports:write", "settings:read", "roster:read", "mail:send"],
    settings={"max_words": setting("number", default=500, minimum=1, scope="course", description="Warn above this")},
)


@plugin.on("submission.submitted")
async def submitted(event):
    sub = await plugin.client.submission(event.data["submission_id"])
    words = sum(len(p.get("text", "").split()) for block in sub["content"]["content"] for p in block["content"])
    limit = (await plugin.config.get(sub["course_id"]))["max_words"]
    report = plugin.report(f"{words} words", level="warn" if words > limit else "ok", facts=[("Limit", limit)])
    await plugin.client.reports.put("submission", sub["id"], "word-count", report)
    if words > limit:
        await plugin.client.mail.send(
            kind="too_long", user_id=sub["student_id"], subject="Your work is long",
            text=f"{words} words, the limit is {limit}.", dedupe_key=f"too-long-{sub['id']}")


@plugin.route("GET", "/export/{course_id}/csv", roles=["admin", "teacher"], course_roles=["teacher", "assistant"])
async def export(request, who, course_id):
    rows = [m["user_id"] for m in await plugin.client.members(course_id, "student")]
    return attachment("\n".join(rows).encode(), f"{course_id}.csv", "text/csv")


@plugin.route("GET", "/whoami", roles=["admin", "teacher", "student"])
async def whoami(request, who):
    return {"user": who.user_id, "role": who.role}


@plugin.job("sweep", every=3600)
async def sweep():
    return {"swept": 0}


app = plugin.app  # uvicorn word-count.app:app
