from vumlo_plugins import Plugin, setting

plugin = Plugin(
    "word-count", "1.0.0",
    scopes=["submissions:read", "reports:write", "settings:read", "mail:send"],
    settings={"max_words": setting("number", default=500, minimum=1, scope="course")},
)


@plugin.on("submission.submitted")  # runs after the core got its 202; must be idempotent
async def submitted(event):
    sub = await plugin.client.submission(event.data["submission_id"])
    words = sum(len(p["text"].split()) for b in sub["content"]["content"] for p in b["content"])
    limit = (await plugin.config.get(sub["course_id"]))["max_words"]
    report = plugin.report(f"{words} words", level="warn" if words > limit else "ok")
    await plugin.client.reports.put("submission", sub["id"], "word-count", report)  # same key: replaced
    if words > limit:
        await plugin.client.mail.send(kind="too_long", user_id=sub["student_id"], subject="Your work is long",
                                      text=f"{words} words, the limit is {limit}.", dedupe_key=sub["id"])


@plugin.route("GET", "/whoami", roles=["admin", "teacher", "student"])  # /api/v1/plugins/word-count/whoami
async def whoami(request, who):  # `who` is the signed identity, never read from the body
    return {"user": who.user_id, "role": who.role}


app = plugin.app  # uvicorn word-count.app:app
