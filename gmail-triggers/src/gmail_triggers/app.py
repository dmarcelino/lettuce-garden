"""The ASGI app: the Gmail triggers pages under /gmail-triggers on the garden's
host (every route needs a verified Access JWT, every POST the garden's Origin
and a CSRF token), the executor's API at /executor/v1/ (executor_api.py; token,
no Access) and /healthz. The pages need no JavaScript."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from importlib import resources
from typing import Any

import anyio
from garden_common.auth import COOP, FORM, PAGE, Authenticator, AuthError, admit
from garden_common.executor import AGENT_ID
from garden_common.garden import stylesheet
from garden_common.text import encodable
from jinja2 import Environment, PackageLoader, select_autoescape
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Mount, Route

from . import icons
from .config import (
    DEFAULT_MAX_PER_HOUR,
    HEARTBEAT_STALE_SECONDS,
    LOST_AFTER_SECONDS,
    MAX_LABELS,
    MAX_LISTED_MESSAGES,
    MAX_MAX_PER_HOUR,
    MAX_PENDING_PER_RULE,
    MAX_RULE_NAME,
    MAX_RULE_PROMPT,
    MAX_RULES,
    MAX_SENDERS,
    PREFIX,
)
from .executor_api import executor_routes
from .google import LABEL_ID, Google, GoogleUnavailable, Label, Status
from .matcher import ADDRESS
from .poller import Poller
from .store import GAP_DROPPED, GAP_PAUSED, GAP_SKIPPED, RULE_ID, Agent, Rule, Store
from .triggers import send_test

log = logging.getLogger("gmail_triggers.ui")

SECURITY_HEADERS = {
    "content-security-policy": "default-src 'none'; script-src 'none'; style-src 'self'; img-src 'self'; "
    "frame-ancestors 'none'; form-action 'self'; base-uri 'none'; object-src 'none'",
    "x-content-type-options": "nosniff",
    # same-origin, not no-referrer: with no-referrer browsers send `Origin: null`
    # on form posts, which check_origin must refuse.
    "referrer-policy": "same-origin",
    "cache-control": "no-store",
    "x-frame-options": "DENY",
    **COOP,
}

Handler = Callable[[Request, str], Awaitable[Response]]

# Gmail system labels worth a trigger, with the names Gmail shows.
SYSTEM_LABELS = {
    "INBOX": "Inbox",
    "IMPORTANT": "Important",
    "CATEGORY_PERSONAL": "Primary",
    "CATEGORY_UPDATES": "Updates",
    "CATEGORY_SOCIAL": "Social",
    "CATEGORY_PROMOTIONS": "Promotions",
    "CATEGORY_FORUMS": "Forums",
}

EXAMPLES = [
    (
        "Invoices to notes",
        "Read the new emails. For each invoice or bill, note the sender, amount and due date in my memory, "
        "then tell me in one line what came in.",
    ),
    (
        "Summary",
        "Summarise each new email in two sentences and tell me whether any of them needs an answer today.",
    ),
    (
        "Draft replies",
        "Draft a short, polite reply to each new email and leave it in Gmail drafts for me to review. "
        "Don't send anything.",
    ),
]

NOTICES = {
    "saved": "Trigger saved.",
    "deleted": "Trigger deleted.",
    "test": "Test request sent. It shows below once Lettuce picks it up (within a minute or two).",
}

CONVERSATIONS = {"new": "A new conversation each time", "default": "The agent's main conversation"}

GOOGLE_BANNERS = {
    Status.NOT_CONNECTED: (
        "bad",
        "Google isn't connected",
        "Connect your Google account in Lettuce, under Settings -> Google. Nothing is checked until then.",
    ),
    Status.DISABLED: (
        "warn",
        "Google is switched off",
        "Gmail isn't checked while Google is off (or not set up) in Lettuce's Settings -> Google.",
    ),
    Status.GMAIL_NOT_GRANTED: (
        "bad",
        "Gmail isn't allowed",
        "In Lettuce's Settings -> Google, allow Gmail (reading is enough) and connect again if asked.",
    ),
    Status.RECONNECT: (
        "bad",
        "Google needs reconnecting",
        "Google no longer accepts the saved sign-in. Reconnect in Lettuce's Settings -> Google.",
    ),
    Status.ERROR: ("warn", "Gmail couldn't be checked", "Trying again at the next check."),
}

STATUS_TEXT = {
    "queued": ("", "Waiting for Lettuce"),
    "claimed": ("", "Sent to Lettuce"),
    "ok": ("ok", "Task created"),
    "failed": ("bad", "Failed"),
    "lost": ("bad", "Lost"),
}
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029]")
_SEPARATORS = re.compile(r"[\s,;]+")


def _fmt_time(ts: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else ""


def _status_text(status: str) -> tuple[str, str]:
    return STATUS_TEXT.get(status, ("", status))


class FormError(Exception):
    pass


def gap_text(gap: dict[str, Any]) -> tuple[str, str]:
    """The notice's title and text."""
    start, end = _fmt_time(gap["start"]), _fmt_time(gap["end"])
    n = gap["skipped"]
    if gap["kind"] == GAP_DROPPED:
        return "Some matched mail wasn't sent", (
            f"{n} matched email{'s' if n != 1 else ''} around {end} {'were' if n != 1 else 'was'} dropped: "
            f"more than {MAX_PENDING_PER_RULE} were waiting for one trigger. "
            "Raise its runs per hour, or narrow its labels or senders."
        )
    title = "Some mail wasn't checked"
    if gap["kind"] == GAP_SKIPPED:
        return title, (
            f"{n} new message{'s' if n != 1 else ''} around {end} {'were' if n != 1 else 'was'} not checked: "
            "too many arrived at once."
        )
    if gap["kind"] == GAP_PAUSED:
        return title, (
            f"Mail between {start} and {end} was not checked while Google was unavailable. "
            "Old mail is not replayed; checking continues from now."
        )
    return title, (
        f"Mail between {start} and {end} was not checked (Gmail's change history had expired). "
        "Checking continues from now."
    )


def label_choices(gmail_labels: list[Label] | None, rule: Rule) -> list[dict[str, Any]]:
    """Checkbox rows: Gmail's labels when known, plus whatever the rule has that Gmail no longer lists."""
    chosen = set(rule.label_ids)
    rows: list[dict[str, Any]] = []
    if gmail_labels is not None:
        by_id = {lb.id: lb for lb in gmail_labels}
        for lid, name in SYSTEM_LABELS.items():
            if lid in by_id:
                rows.append({"id": lid, "name": name, "checked": lid in chosen})
        users = sorted((lb for lb in gmail_labels if lb.type == "user"), key=lambda lb: lb.name.lower())
        rows += [{"id": lb.id, "name": lb.name, "checked": lb.id in chosen} for lb in users]
    listed = {r["id"] for r in rows}
    for lid in rule.label_ids:
        if lid not in listed:
            name = rule.label_names.get(lid) or SYSTEM_LABELS.get(lid, lid)
            note = "" if gmail_labels is None else " (no longer in Gmail)"
            rows.append({"id": lid, "name": name + note, "checked": True})
    return rows


def parse_rule(
    form: Any, current: Rule | None, gmail_labels: list[Label] | None, agents: list[Agent] | None
) -> tuple[Rule, list[str]]:
    """The submitted form as a Rule (for re-display) and what is wrong with it."""
    problems: list[str] = []

    name = " ".join(_CONTROL.sub(" ", encodable(str(form.get("name", "")))).split())
    if not name:
        problems.append("Give the trigger a name.")
    elif len(name) > MAX_RULE_NAME:
        problems.append(f"Keep the name under {MAX_RULE_NAME} characters.")

    # Labels: from Gmail's list, or (when Gmail can't be asked) ones the rule already had.
    known: dict[str, str] = {}
    if gmail_labels is not None:
        known = {lb.id: SYSTEM_LABELS.get(lb.id, lb.name) for lb in gmail_labels}
    if current is not None:
        for lid in current.label_ids:
            known.setdefault(lid, current.label_names.get(lid, lid))
    label_ids = [str(v) for v in dict.fromkeys(form.getlist("labels"))]
    label_ids = [lid for lid in label_ids if LABEL_ID.fullmatch(lid) and lid in known]
    if not label_ids:
        problems.append("Choose at least one label.")
    elif len(label_ids) > MAX_LABELS:
        problems.append(f"Choose at most {MAX_LABELS} labels.")

    senders_text = str(form.get("senders", ""))
    senders = list(dict.fromkeys(s.lower() for s in _SEPARATORS.split(senders_text) if s))
    bad = [s for s in senders if not ADDRESS.fullmatch(s)]
    if bad:
        problems.append(f"Not an email address: {bad[0][:80]}")
    if len(senders) > MAX_SENDERS:
        problems.append(f"List at most {MAX_SENDERS} senders.")

    agent_id = str(form.get("agent_id", ""))
    agent_name = ""
    snapshot = {a.id: a.name for a in agents} if agents is not None else {}
    if current is not None and current.agent_id:
        snapshot.setdefault(current.agent_id, current.agent_name)
    if not AGENT_ID.fullmatch(agent_id) or agent_id not in snapshot:
        problems.append("Choose an agent.")
        agent_id = agent_id if AGENT_ID.fullmatch(agent_id) else ""
    else:
        agent_name = snapshot[agent_id]

    conversation = str(form.get("conversation", ""))
    if conversation not in CONVERSATIONS:
        problems.append("Choose where the agent answers.")
        conversation = "new"

    prompt = _CONTROL.sub("", encodable(str(form.get("prompt", ""))).replace("\r\n", "\n")).strip()
    if not prompt:
        problems.append("Say what the agent should do.")
    elif len(prompt) > MAX_RULE_PROMPT:
        problems.append(f"Keep the instructions under {MAX_RULE_PROMPT} characters.")

    try:
        max_per_hour = int(str(form.get("max_per_hour", "")).strip())
        if not 1 <= max_per_hour <= MAX_MAX_PER_HOUR:
            raise ValueError
    except ValueError:
        problems.append(f"Runs per hour must be a number from 1 to {MAX_MAX_PER_HOUR}.")
        max_per_hour = DEFAULT_MAX_PER_HOUR

    rule = Rule(
        id=current.id if current else "",
        enabled=current.enabled if current else True,
        created=current.created if current else 0,
        name=name[:MAX_RULE_NAME],
        label_ids=label_ids[:MAX_LABELS],
        label_names={lid: known[lid] for lid in label_ids[:MAX_LABELS]},
        senders=senders[:MAX_SENDERS],
        verified_only=form.get("verified") == "on",
        agent_id=agent_id,
        agent_name=agent_name,
        conversation=conversation,
        prompt=prompt[:MAX_RULE_PROMPT],
        max_per_hour=max_per_hour,
    )
    return rule, problems


def create_app(
    store: Store,
    google: Google,
    authenticator: Authenticator,
    executor_token: str,
    poller: Poller | None = None,
) -> Starlette:
    env = Environment(loader=PackageLoader("gmail_triggers", "templates"), autoescape=select_autoescape(default=True))
    env.filters["time"] = _fmt_time
    env.globals.update(base=PREFIX, status_text=_status_text, conversations=CONVERSATIONS)
    garden_css = stylesheet()
    app_css = (resources.files("gmail_triggers") / "static" / "app.css").read_bytes()

    def render(name: str, email: str, status: int = 200, **ctx: Any) -> HTMLResponse:
        html = env.get_template(name).render(email=email, csrf=authenticator.csrf_token(email) if email else "", **ctx)
        return HTMLResponse(html, status_code=status, headers=SECURITY_HEADERS)

    def error(message: str, status: int, email: str = "") -> HTMLResponse:
        return render("error.html", email, status=status, message=message)

    def back(path: str) -> RedirectResponse:
        return RedirectResponse(PREFIX + path, status_code=303, headers=SECURITY_HEADERS)

    def page(handler: Handler, post: bool = False, asset: bool = False) -> Callable[[Request], Awaitable[Response]]:
        """`asset`: a stylesheet or icon, loaded by pages as a subresource; it holds nothing to protect."""

        async def endpoint(request: Request) -> Response:
            try:
                email = await admit(authenticator, request.headers, None if asset else FORM if post else PAGE)
            except AuthError as e:
                log.info("refused %s %s reason=%s", request.method, request.url.path, e.reason)
                return error(str(e), e.status)
            if post:
                try:
                    authenticator.check_origin(request.headers)
                    token = (await request.form()).get("csrf")
                    authenticator.check_csrf(email, token if isinstance(token, str) else None)
                except AuthError as e:
                    return error(str(e), e.status, email)
            try:
                return await handler(request, email)
            except FormError as e:
                return error(str(e), 400, email)

        return endpoint

    def get_rule(request: Request) -> Rule:
        rid = request.path_params["rid"]
        rule = store.rule(rid) if RULE_ID.fullmatch(rid) else None
        if rule is None:
            raise FormError("This trigger doesn't exist (any more).")
        return rule

    # -- status ---------------------------------------------------------------
    def banners() -> list[dict[str, str]]:
        out = []
        now = store.now()
        status, detail = google.status()
        if status != Status.OK:
            level, title, text = GOOGLE_BANNERS[status]
            out.append({"level": level, "title": title, "text": f"{detail}. {text}" if detail else text})
        beat = store.heartbeat()
        if beat is None:
            out.append(
                {
                    "level": "bad",
                    "title": "Lettuce isn't picking up requests",
                    "text": "Lettuce hasn't checked for requests yet. Matches wait until it does. "
                    "If this stays, ask whoever runs your Lettuce to check the gmail-triggers setup.",
                }
            )
        elif now - beat.at > HEARTBEAT_STALE_SECONDS:
            out.append(
                {
                    "level": "bad",
                    "title": "Lettuce isn't picking up requests",
                    "text": f"Lettuce last checked for requests at {_fmt_time(beat.at)}. Matches wait until it does.",
                }
            )
        elif not beat.letta_ok:
            out.append(
                {
                    "level": "warn",
                    "title": "Lettuce reported a problem",
                    "text": beat.error or "Lettuce could not run its command line tool.",
                }
            )
        failures = store.failures_since(now - 86400)
        if failures:
            plural = "s" if failures != 1 else ""
            out.append(
                {
                    "level": "warn",
                    "title": f"{failures} request{plural} failed in the last day",
                    "text": "Open a trigger's history to see what Lettuce reported.",
                }
            )
        for gap in store.gaps_since(now - 7 * 86400)[:3]:
            title, text = gap_text(gap)
            out.append({"level": "warn", "title": title, "text": text})
        return out

    # -- pages ----------------------------------------------------------------
    async def index(request: Request, email: str) -> Response:
        store.expire_claims(LOST_AFTER_SECONDS)
        return render(
            "triggers.html",
            email,
            rules=store.rules(),
            last_runs=store.last_runs(),
            pending=store.pending_counts(),
            banners=banners(),
            notice=NOTICES.get(request.query_params.get("done", "")),
            can_add=store.count_rules() < MAX_RULES,
        )

    async def gmail_labels() -> tuple[list[Label] | None, str]:
        try:
            gmail = await google.connect()
            return await gmail.labels(), ""
        except GoogleUnavailable as e:
            status_title = GOOGLE_BANNERS.get(e.status, ("", "Gmail is unavailable", ""))[1]
            return None, status_title

    def example(request: Request) -> str | None:
        raw = request.query_params.get("example", "")
        if raw.isdigit() and 1 <= int(raw) <= len(EXAMPLES):
            return EXAMPLES[int(raw) - 1][1]
        return None

    async def form_page(
        email: str, rule: Rule, is_new: bool, problems: list[str] | None = None, senders_text: str | None = None
    ) -> Response:
        labels, labels_problem = await gmail_labels()
        # The picker should list agents made since the last list: ask for a new one.
        store.ask_for_agents()
        snapshot = store.agents()
        agents = list(snapshot or [])
        if rule.agent_id and rule.agent_id not in {a.id for a in agents}:
            agents.append(Agent(rule.agent_id, rule.agent_name or rule.agent_id))
        names = [a.name for a in agents]
        return render(
            "rule_form.html",
            email,
            status=400 if problems else 200,
            rule=rule,
            is_new=is_new,
            problems=problems or [],
            labels=label_choices(labels, rule),
            labels_problem=labels_problem,
            agents=[(a, names.count(a.name) > 1) for a in agents],
            agents_known=snapshot is not None,
            senders_text=senders_text if senders_text is not None else "\n".join(rule.senders),
            examples=EXAMPLES,
            max_per_hour_limit=MAX_MAX_PER_HOUR,
            max_per_run=MAX_LISTED_MESSAGES,
        )

    async def new_form(request: Request, email: str) -> Response:
        if store.count_rules() >= MAX_RULES:
            raise FormError(f"You have {MAX_RULES} triggers, the most there can be. Delete one first.")
        rule = Rule(name="", label_ids=["INBOX"], agent_id="", prompt=example(request) or "")
        return await form_page(email, rule, is_new=True)

    async def create(request: Request, email: str) -> Response:
        if store.count_rules() >= MAX_RULES:
            raise FormError(f"You have {MAX_RULES} triggers, the most there can be. Delete one first.")
        form = await request.form()
        labels, _ = await gmail_labels()
        rule, problems = parse_rule(form, None, labels, store.agents())
        if problems:
            return await form_page(email, rule, True, problems, str(form.get("senders", "")))
        store.save_rule(rule)
        return back("/?done=saved")

    async def edit_form(request: Request, email: str) -> Response:
        rule = get_rule(request)
        text = example(request)
        return await form_page(email, replace(rule, prompt=text) if text else rule, is_new=False)

    async def update(request: Request, email: str) -> Response:
        current = get_rule(request)
        form = await request.form()
        labels, _ = await gmail_labels()
        rule, problems = parse_rule(form, current, labels, store.agents())
        if problems:
            return await form_page(email, rule, False, problems, str(form.get("senders", "")))
        store.save_rule(rule)
        return back("/?done=saved")

    async def toggle(request: Request, email: str) -> Response:
        rule = get_rule(request)
        form = await request.form()
        store.set_enabled(rule.id, form.get("enabled") == "1")
        return back("/")

    async def delete(request: Request, email: str) -> Response:
        rule = get_rule(request)
        store.delete_rule(rule.id)
        return back("/?done=deleted")

    async def history(request: Request, email: str) -> Response:
        rule = get_rule(request)
        store.expire_claims(LOST_AFTER_SECONDS)
        return render(
            "history.html",
            email,
            rule=rule,
            runs=store.runs(rule.id),
            held=len(store.pending(rule.id)),
            max_per_run=MAX_LISTED_MESSAGES,
            notice=NOTICES.get(request.query_params.get("done", "")),
        )

    async def test(request: Request, email: str) -> Response:
        rule = get_rule(request)
        send_test(store, rule, store.now())
        return back(f"/r/{rule.id}/history?done=test")

    # -- static: behind Access too (fetched with the Access cookie) -----------
    def static(body: bytes, media_type: str) -> Handler:
        async def serve(request: Request, email: str) -> Response:
            return Response(body, media_type=media_type, headers={**SECURITY_HEADERS, "cache-control": "no-cache"})

        return serve

    def icon(size: int) -> Handler:
        async def serve(request: Request, email: str) -> Response:
            return Response(
                icons.png(size), media_type="image/png", headers={**SECURITY_HEADERS, "cache-control": "max-age=86400"}
            )

        return serve

    async def healthz(request: Request) -> Response:
        return PlainTextResponse("ok")

    async def root(request: Request) -> Response:
        return RedirectResponse(PREFIX + "/", status_code=308)

    ui = [
        Route("/", page(index)),
        Route("/new", page(new_form)),
        Route("/rules", page(create, post=True), methods=["POST"]),
        Route("/r/{rid}", page(edit_form)),
        Route("/r/{rid}", page(update, post=True), methods=["POST"]),
        Route("/r/{rid}/toggle", page(toggle, post=True), methods=["POST"]),
        Route("/r/{rid}/delete", page(delete, post=True), methods=["POST"]),
        Route("/r/{rid}/history", page(history)),
        Route("/r/{rid}/test", page(test, post=True), methods=["POST"]),
        Route("/garden.css", page(static(garden_css, "text/css"), asset=True)),
        Route("/app.css", page(static(app_css, "text/css"), asset=True)),
        Route("/icon-192.png", page(icon(192), asset=True)),
        Route("/icon-512.png", page(icon(512), asset=True)),
    ]

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        if poller is None:
            yield
            return
        async with anyio.create_task_group() as tg:
            tg.start_soon(poller.run)
            try:
                yield
            finally:
                tg.cancel_scope.cancel()

    return Starlette(
        routes=[
            Route("/healthz", healthz),
            *executor_routes(store, executor_token),
            Route(PREFIX, root),
            Mount(PREFIX, routes=ui),
        ],
        lifespan=lifespan,
    )
