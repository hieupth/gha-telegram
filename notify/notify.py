#!/usr/bin/env python3
"""Render a Telegram HTML message for the notify composite action.

Pure stdlib — no third-party dependencies, nothing to install at runtime.
Inputs arrive via environment variables (never via GitHub expression
interpolation into this script), and the rendered message is written to
$RUNNER_TEMP/telegram-message.html for the send step.

Layout:
    1. Constants
    2. Icon maps (one per built-in template)
    3. Sanitization helpers
    4. Markdown → HTML converter (template=custom)
    5. Template renderers
    6. Entry point
"""

import html
import json
import os
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# 1. Constants
# ---------------------------------------------------------------------------

TELEGRAM_MAX_LENGTH = 4096
OUTPUT_FILENAME = "telegram-message.html"

# ---------------------------------------------------------------------------
# 2. Icon maps — one per built-in template, keyed by event action/status
# ---------------------------------------------------------------------------

WORKFLOW_ICONS = {
    "success": "✅",
    "failure": "❌",
    "cancelled": "⏹️",
    "timed_out": "⏱️",
    "in_progress": "▶️",
    "queued": "⏳",
    "requested": "⏳",
}

ISSUE_ICONS = {
    "opened": "🟢",
    "reopened": "🔄",
    "closed": "✅",
    "assigned": "👤",
    "unassigned": "👤",
    "labeled": "🏷️",
    "unlabeled": "🏷️",
    "edited": "✏️",
}

PULL_REQUEST_ICONS = {
    "opened": "🟢",
    "reopened": "🔄",
    "ready_for_review": "👀",
    "assigned": "👤",
    "unassigned": "👤",
    "labeled": "🏷️",
    "unlabeled": "🏷️",
    "edited": "✏️",
}

RELEASE_ICONS = {
    "published": "🚀",
    "released": "🚀",
    "prereleased": "🧪",
    "edited": "✏️",
    "deleted": "🗑️",
}

PROJECT_ICONS = {
    "created": "🆕",
    "edited": "✏️",
    "closed": "✅",
    "reopened": "🔄",
    "archived": "📦",
    "converted": "🔁",
    "added": "➕",
    "removed": "➖",
    "moved": "➡️",
}

# ---------------------------------------------------------------------------
# 3. Sanitization helpers
# ---------------------------------------------------------------------------


def esc(value) -> str:
    """HTML-escape any dynamic value (quote=True also blocks attribute breakout)."""
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def fail(message: str) -> None:
    # Collapse whitespace so caller-controlled values (payload snippets,
    # template names) cannot inject extra lines or workflow commands into logs.
    flat = " ".join(message.split())
    print(f"::error::{flat}", file=sys.stderr)
    sys.exit(1)


def compact(text: str) -> str:
    """Collapse runs of blank lines; content lines keep their indentation."""
    out = []
    previous_blank = True
    for line in text.splitlines():
        if line.strip():
            out.append(line)
            previous_blank = False
        elif not previous_blank:
            out.append("")
            previous_blank = True
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out)


def sub_dict(value) -> dict:
    """Return value if it is a dict, else {} — payload shapes are not trusted."""
    return value if isinstance(value, dict) else {}


# ---------------------------------------------------------------------------
# 4. Markdown → HTML converter (template=custom)
# ---------------------------------------------------------------------------

# HTML is escaped before any marker is transformed, so unrecognized or
# unclosed markup can only ever render as literal text. Links are restricted
# to http/https.
MD_FENCE_RE = re.compile(r"```(.+?)```", re.DOTALL)
MD_CODE_RE = re.compile(r"`([^`\n]+)`")
MD_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
MD_STRIKE_RE = re.compile(r"~~(.+?)~~", re.DOTALL)
MD_ITALIC_STAR_RE = re.compile(r"\*(\S(?:[^*\n]*?\S)?)\*")
MD_ITALIC_UNDER_RE = re.compile(r"(?<![A-Za-z0-9_])_([^_\n]+)_(?![A-Za-z0-9_])")


def markdown_to_html(text: str) -> str:
    r"""Convert the supported markdown subset to Telegram-safe HTML.

    Renders `**bold**`, `*italic*`, `_italic_`, `~~strike~~`, `` `code` ``,
    fenced ``` blocks, and `[label](http…)` links. Escape-first means no
    caller markup can produce tags of its own; markers are swapped for
    \x00n\x00 placeholders (NUL cannot occur in env-sourced input) so later
    marker passes never see earlier matches, and restoring in reverse order
    resolves markers nested inside earlier matches (e.g. bold in italic).
    """
    sink: list = []

    def wrap(tag):
        def replace(match):
            sink.append(f"<{tag}>{match.group(1)}</{tag}>")
            return f"\x00{len(sink) - 1}\x00"

        return replace

    def replace_link(match):
        sink.append(f'<a href="{match.group(2)}">{match.group(1)}</a>')
        return f"\x00{len(sink) - 1}\x00"

    text = esc(text)
    text = MD_FENCE_RE.sub(wrap("pre"), text)
    text = MD_CODE_RE.sub(wrap("code"), text)
    text = MD_LINK_RE.sub(replace_link, text)
    text = MD_BOLD_RE.sub(wrap("b"), text)
    text = MD_STRIKE_RE.sub(wrap("s"), text)
    text = MD_ITALIC_STAR_RE.sub(wrap("i"), text)
    text = MD_ITALIC_UNDER_RE.sub(wrap("i"), text)

    for index in range(len(sink) - 1, -1, -1):
        text = text.replace(f"\x00{index}\x00", sink[index])
    return text


# ---------------------------------------------------------------------------
# 5. Template renderers — one function per built-in template. Every dynamic
#    field goes through esc(); payload shapes are never trusted.
# ---------------------------------------------------------------------------


# Caller payload: toJSON(github.event.workflow_run)
def render_workflow(payload: dict, repository: str) -> str:
    status = payload.get("conclusion") or payload.get("status") or "unknown"
    lines = [
        f"{WORKFLOW_ICONS.get(status, 'ℹ️')} <b>{esc(payload.get('name') or 'Workflow')}</b>",
        "",
        f"<b>Repository:</b> {esc(repository)}",
        f"<b>Status:</b> {esc(str(status).upper())}",
    ]
    if payload.get("run_number"):
        lines.append(f"<b>Run:</b> #{esc(payload['run_number'])}")
    if payload.get("head_branch"):
        lines.append(f"<b>Branch:</b> {esc(payload['head_branch'])}")
    actor = sub_dict(payload.get("actor"))
    if actor.get("login"):
        lines.append(f"<b>Actor:</b> {esc(actor['login'])}")
    if payload.get("html_url"):
        lines += ["", f'<a href="{esc(payload["html_url"])}">Open workflow run</a>']
    return "\n".join(lines)


# Caller payload: toJSON(github.event)  (issues events)
def render_issue(payload: dict, repository: str) -> str:
    action = payload.get("action") or ""
    issue = sub_dict(payload.get("issue"))
    lines = [
        f"{ISSUE_ICONS.get(action, '🎫')} <b>Issue #{esc(issue.get('number', '?'))} · {esc(str(action).upper())}</b>",
        "",
        f"<b>Repository:</b> {esc(repository)}",
        f"<b>{esc(issue.get('title'))}</b>",
    ]
    sender = sub_dict(payload.get("sender"))
    if sender.get("login"):
        lines.append(f"<b>By:</b> {esc(sender['login'])}")
    assignee = sub_dict(issue.get("assignee"))
    if assignee.get("login"):
        lines.append(f"<b>Assignee:</b> {esc(assignee['login'])}")
    label = sub_dict(payload.get("label"))
    if label.get("name"):
        lines.append(f"<b>Label:</b> {esc(label['name'])}")
    milestone = sub_dict(issue.get("milestone"))
    if milestone.get("title"):
        lines.append(f"<b>Milestone:</b> {esc(milestone['title'])}")
    if issue.get("html_url"):
        lines += ["", f'<a href="{esc(issue["html_url"])}">Open issue</a>']
    return "\n".join(lines)


# Caller payload: toJSON(github.event)  (pull_request events).
# "merge" is not an event: a closed PR renders as MERGED when
# pull_request.merged is true, else CLOSED.
def render_pull_request(payload: dict, repository: str) -> str:
    action = payload.get("action") or ""
    pull = sub_dict(payload.get("pull_request"))
    if action == "closed" and pull.get("merged") is True:
        icon, state = "🔀", "MERGED"
    elif action == "closed":
        icon, state = "❌", "CLOSED"
    else:
        icon = PULL_REQUEST_ICONS.get(action, "🔔")
        state = str(action).upper()
    merged = state == "MERGED"

    header = f"{icon} <b>PR #{esc(pull.get('number', '?'))} · {esc(state)}"
    if pull.get("draft") and action != "closed":
        header += " · DRAFT"
    header += "</b>"

    lines = [
        header,
        "",
        f"<b>Repository:</b> {esc(repository)}",
        f"<b>{esc(pull.get('title'))}</b>",
    ]
    user = sub_dict(pull.get("user"))
    if user.get("login"):
        lines.append(f"<b>By:</b> {esc(user['login'])}")
    if merged:  # merged_by is only meaningful on a merged PR
        merged_by = sub_dict(pull.get("merged_by"))
        if merged_by.get("login"):
            lines.append(f"<b>Merged by:</b> {esc(merged_by['login'])}")
    assignee = sub_dict(pull.get("assignee"))
    if assignee.get("login"):
        lines.append(f"<b>Assignee:</b> {esc(assignee['login'])}")
    label = sub_dict(payload.get("label"))
    if label.get("name"):
        lines.append(f"<b>Label:</b> {esc(label['name'])}")
    if pull.get("html_url"):
        lines += ["", f'<a href="{esc(pull["html_url"])}">Open pull request</a>']
    return "\n".join(lines)


# Caller payload: toJSON(github.event)  (push events). Note: the commits
# array is truncated silently on large pushes (no flag in the payload), so
# render at most the first few commits and never assume it is complete.
PUSH_MAX_COMMITS = 3


def render_push(payload: dict, repository: str) -> str:
    ref = str(payload.get("ref") or "")
    branch = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref

    if payload.get("deleted"):
        return "\n".join([
            f"🗑️ <b>Deleted {esc(branch)}</b>",
            "",
            f"<b>Repository:</b> {esc(repository)}",
        ])

    pusher = sub_dict(payload.get("pusher"))
    commits = payload.get("commits")
    commits = commits if isinstance(commits, list) else []
    size = payload.get("size") or len(commits)

    lines = [
        f"⬆️ <b>Push to {esc(branch)}</b>",
        "",
        f"<b>Repository:</b> {esc(repository)}",
    ]
    if pusher.get("name"):
        lines.append(f"<b>By:</b> {esc(pusher['name'])}")
    lines.append(f"<b>Commits:</b> {esc(size)}")
    if commits:
        lines.append("")
        for commit in commits[:PUSH_MAX_COMMITS]:
            if not isinstance(commit, dict):
                continue
            sha = str(commit.get("id") or "")[:7]
            first_line = str(commit.get("message") or "").split("\n", 1)[0]
            lines.append(f"• {esc(sha)} {esc(first_line)}")
        if len(commits) > PUSH_MAX_COMMITS:
            lines.append(f"• … and {esc(len(commits) - PUSH_MAX_COMMITS)} more")
    if payload.get("compare"):
        lines += ["", f'<a href="{esc(payload["compare"])}">View compare</a>']
    return "\n".join(lines)


# Caller payload: toJSON(github.event)  (release events). The release body is
# intentionally not rendered — it is unbounded user content; use a workflow
# step that normalizes it first if needed.
def render_release(payload: dict, repository: str) -> str:
    action = payload.get("action") or ""
    release = sub_dict(payload.get("release"))
    tag = esc(release.get("tag_name") or "")
    header = f"{RELEASE_ICONS.get(action, '📦')} <b>{('Release ' + tag).rstrip()} · {esc(str(action).upper())}</b>"
    if release.get("prerelease"):
        header += " · PRE-RELEASE"

    lines = [
        header,
        "",
        f"<b>Repository:</b> {esc(repository)}",
    ]
    if release.get("name"):
        lines.append(f"<b>{esc(release['name'])}</b>")
    sender = sub_dict(payload.get("sender"))
    if sender.get("login"):
        lines.append(f"<b>By:</b> {esc(sender['login'])}")
    if release.get("html_url"):
        lines += ["", f'<a href="{esc(release["html_url"])}">Open release</a>']
    return "\n".join(lines)


# Caller payload: normalized client_payload from repository_dispatch
# (Projects v2 events need an external webhook normalizer — see README).
# The repository argument is ignored: this payload shape has no repository.
def render_project(payload: dict, repository: str) -> str:
    action = payload.get("action") or ""
    lines = [
        f"{PROJECT_ICONS.get(action, '📋')} <b>Project · {esc(str(action).upper())}</b>",
        "",
    ]
    if payload.get("organization"):
        lines.append(f"<b>Organization:</b> {esc(payload['organization'])}")
    project = sub_dict(payload.get("project"))
    if project.get("title"):
        lines.append(f"<b>Project:</b> {esc(project['title'])}")
    item = sub_dict(payload.get("item"))
    if item.get("title"):
        lines.append(f"<b>Item:</b> {esc(item['title'])}")
    for label, key in (
        ("Field", "field"),
        ("From", "from_value"),
        ("To", "to_value"),
        ("By", "sender"),
    ):
        if payload.get(key):
            lines.append(f"<b>{label}:</b> {esc(payload[key])}")
    if payload.get("url"):
        lines += ["", f'<a href="{esc(payload["url"])}">Open project</a>']
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 6. Entry point
# ---------------------------------------------------------------------------


def resolve_repository(payload: dict) -> str:
    """Owner/repo string: payload.repository (string or {full_name}) or GITHUB_REPOSITORY."""
    repo = payload.get("repository")
    if isinstance(repo, dict):  # event payloads nest repository as an object
        repo = repo.get("full_name")
    if isinstance(repo, str) and repo:
        return repo
    fallback = os.environ.get("GITHUB_REPOSITORY", "")
    if fallback:
        return fallback
    fail("Cannot resolve repository: payload.repository missing and GITHUB_REPOSITORY not set")


RENDERERS = {
    "workflow": render_workflow,
    "issue": render_issue,
    "pull_request": render_pull_request,
    "push": render_push,
    "release": render_release,
    "project": render_project,
}


def main() -> None:
    template_name = os.environ.get("TEMPLATE_NAME", "").strip()
    raw_payload = os.environ.get("PAYLOAD_JSON", "{}")
    custom_message = os.environ.get("CUSTOM_MESSAGE", "")

    try:
        payload = json.loads(raw_payload or "{}")
    except ValueError as exc:  # includes json.JSONDecodeError
        fail(f"Invalid payload JSON: {exc}")
    except RecursionError:
        fail("payload JSON is nested too deeply")

    if not isinstance(payload, dict):
        fail("payload must be a JSON object")

    if template_name == "custom":
        if not custom_message.strip():
            fail("message input is required when template=custom")
        rendered = markdown_to_html(custom_message)
    elif template_name in RENDERERS:
        rendered = RENDERERS[template_name](payload, resolve_repository(payload))
    else:
        fail(
            "Unknown template: supported templates are "
            "workflow, issue, pull_request, push, release, project, custom"
        )

    rendered = compact(rendered)

    if len(rendered) > TELEGRAM_MAX_LENGTH:
        fail(f"Rendered message exceeds the Telegram limit: {len(rendered)} > {TELEGRAM_MAX_LENGTH} characters")

    temp_dir = os.environ.get("RUNNER_TEMP") or "/tmp"
    output_path = Path(temp_dir) / OUTPUT_FILENAME
    output_path.write_text(rendered, encoding="utf-8")
    print(f"Rendered {len(rendered)} characters to {output_path}")


if __name__ == "__main__":
    main()
