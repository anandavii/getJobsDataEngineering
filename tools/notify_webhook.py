"""
Send job search notifications via Email (Gmail SMTP) and/or Discord webhook.
Each backend is independent — configure one or both via environment variables.

Usage:
    notify_webhook.run(push_result, new_jobs, config,
                       discord_url=..., email_to=..., email_from=..., email_password=...)
"""

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

log = logging.getLogger(__name__)

REQUEST_TIMEOUT = 15
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587


def _truncate(text: str, max_len: int = 100) -> str:
    return text if len(text) <= max_len else text[: max_len - 3] + "..."


def _display_name(config: dict | None) -> str:
    identity = (config or {}).get("identity", {})
    name = identity.get("display_name", "") if isinstance(identity, dict) else ""
    return str(name).strip() or "Job Search"


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------


def _build_discord_content(
    push_result: dict, new_jobs: list[dict], config: dict, source_execution: list = None, health_state: dict = None
) -> str:
    max_jobs = config.get("notification", {}).get("max_jobs_in_message", 10)
    inserted = push_result.get("inserted", 0)
    updated = push_result.get("updated", 0)
    cleaned = push_result.get("cleaned", 0)
    error = push_result.get("error")
    capacity_warning = push_result.get("capacity_warning", False)
    fmt = config.get("notification", {}).get("format", "rich")

    status_emoji = "\u2705" if not error else "\u26a0\ufe0f"
    title = f"{status_emoji} Daily {_display_name(config)} \u2014 {inserted} new job(s) found"

    if fmt == "simple":
        return title

    lines = [title, ""]

    if inserted or updated:
        lines.append(f"**Summary:** {inserted} new \u00b7 {updated} updated \u00b7 {cleaned} cleaned up")
    else:
        lines.append("**Summary:** No new jobs found today")

    if source_execution and _has_issues(source_execution):
        failed = [m["source"] for m in source_execution if not m.get("success") and not m.get("skipped")]
        skipped_srcs = [m["source"] for m in source_execution if m.get("skipped")]
        empty_srcs = [m["source"] for m in source_execution if m.get("success") and m.get("empty")]
        parts_list = []
        if failed:
            parts_list.append(f"❌ {', '.join(failed)}")
        if skipped_srcs:
            parts_list.append(f"⏭️ {', '.join(skipped_srcs)} (circuit open)")
        if empty_srcs:
            parts_list.append(f"⚠️ {', '.join(empty_srcs)} (empty)")
        if parts_list:
            lines.append(f"**Sources:** {' · '.join(parts_list)}")

    if capacity_warning:
        lines.append("\u26a0\ufe0f _Supabase approaching capacity \u2014 consider reviewing old records_")
    if error:
        lines.append(f"\u274c **Error:** {error}")

    if new_jobs:
        lines.append("")
        lines.append(f"**Top {min(max_jobs, len(new_jobs))} new jobs:**")
        for job in new_jobs[:max_jobs]:
            title_text = job.get("job_title", "Unknown Role")
            company = job.get("company", "")
            location = job.get("location", "")
            url = job.get("job_url", "")
            source = job.get("source", "")
            salary_min = job.get("salary_min")
            salary_max = job.get("salary_max")

            salary_str = ""
            if salary_min and salary_max:
                salary_str = f"  ${salary_min:,.0f}\u2013${salary_max:,.0f}"
            elif salary_min:
                salary_str = f"  ${salary_min:,.0f}+"

            meta = " | ".join(filter(None, [company, location, source]))
            if url:
                line = f"\u2022 [{_truncate(title_text, 60)}]({url})\n  {meta}{salary_str}"
            else:
                line = f"\u2022 {_truncate(title_text, 60)}\n  {meta}{salary_str}"
            lines.append(line)

        if len(new_jobs) > max_jobs:
            lines.append(f"\n_\u2026and {len(new_jobs) - max_jobs} more in Supabase_")

    content = "\n".join(lines)
    if len(content) > 1990:
        content = content[:1987] + "..."
    return content


def _send_discord(
    url: str,
    push_result: dict,
    new_jobs: list[dict],
    config: dict,
    source_execution: list = None,
    health_state: dict = None,
) -> bool:
    content = _build_discord_content(
        push_result, new_jobs, config, source_execution=source_execution, health_state=health_state
    )
    try:
        resp = requests.post(url, json={"content": content}, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        log.info("Notify: Discord webhook sent successfully")
        return True
    except requests.RequestException as e:
        log.error("Notify: Discord webhook failed \u2014 %s", e)
        return False


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------


def _build_email_subject(push_result: dict, config: dict | None = None) -> str:
    inserted = push_result.get("inserted", 0)
    error = push_result.get("error")
    display_name = _display_name(config)
    if error:
        return f"\u26a0\ufe0f {display_name} \u2014 {inserted} new job(s) (with errors)"
    return f"\u2705 {display_name} \u2014 {inserted} new job(s) found"


def _build_email_html(
    push_result: dict, new_jobs: list[dict], config: dict, source_execution: list = None, health_state: dict = None
) -> str:
    max_jobs = config.get("notification", {}).get("max_jobs_in_message", 10)
    inserted = push_result.get("inserted", 0)
    updated = push_result.get("updated", 0)
    cleaned = push_result.get("cleaned", 0)
    error = push_result.get("error")
    capacity_warning = push_result.get("capacity_warning", False)
    fmt = config.get("notification", {}).get("format", "rich")
    board_url = config.get("notification", {}).get("job_board_url", "")

    board_link = (
        f'<p><a href="{board_url}" style="display:inline-block;padding:8px 16px;'
        f'background:#2563eb;color:#fff;text-decoration:none;border-radius:4px;font-weight:bold">'
        f"View Job Board &rarr;</a></p>"
        if board_url
        else ""
    )

    parts = ['<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto">']
    parts.append(f"<h2>Daily {_display_name(config)} &mdash; {inserted} new job(s)</h2>")
    parts.append(f"<p><b>{inserted}</b> new &middot; {updated} updated &middot; {cleaned} cleaned up</p>")

    if capacity_warning:
        parts.append('<p style="color:#e67e22">&#9888; Supabase approaching capacity</p>')
    if error:
        parts.append(f'<p style="color:#e74c3c">&#10060; Error: {error}</p>')

    if fmt == "rich" and new_jobs:
        parts.append("<hr>")
        parts.append(f"<h3>Top {min(max_jobs, len(new_jobs))} new jobs</h3>")
        parts.append('<table style="width:100%;border-collapse:collapse">')
        parts.append(
            '<tr style="background:#f5f5f5">'
            '<th style="text-align:left;padding:8px">Job</th>'
            '<th style="text-align:left;padding:8px">Company</th>'
            '<th style="text-align:left;padding:8px">Location</th></tr>'
        )
        for job in new_jobs[:max_jobs]:
            title_text = _truncate(job.get("job_title", "Unknown"), 55)
            company = job.get("company", "")
            location = job.get("location", "")
            url = job.get("job_url", "")
            salary_min = job.get("salary_min")
            salary_max = job.get("salary_max")

            salary_str = ""
            if salary_min and salary_max:
                salary_str = f"<br><small>${salary_min:,.0f}&ndash;${salary_max:,.0f}</small>"
            elif salary_min:
                salary_str = f"<br><small>${salary_min:,.0f}+</small>"

            link = f'<a href="{url}">{title_text}</a>' if url else title_text
            parts.append(
                f'<tr style="border-bottom:1px solid #eee">'
                f'<td style="padding:8px">{link}{salary_str}</td>'
                f'<td style="padding:8px">{company}</td>'
                f'<td style="padding:8px">{location}</td></tr>'
            )
        parts.append("</table>")
        if len(new_jobs) > max_jobs:
            parts.append(f"<p><i>&hellip;and {len(new_jobs) - max_jobs} more in Supabase</i></p>")

    alert_threshold = (config or {}).get("health", {}).get("alert_consecutive_failures_threshold", 2)
    banner_html = _build_alert_banner_html(health_state or {}, alert_threshold)
    if banner_html:
        parts.insert(0, banner_html)
    status_html = _build_source_status_html(source_execution or [], health_state or {}, alert_threshold)
    if status_html:
        parts.append(status_html)
    if board_link:
        parts.append(board_link)
    parts.append("</div>")
    return "\n".join(parts)


def _build_email_plain(
    push_result: dict, new_jobs: list[dict], config: dict, source_execution: list = None, health_state: dict = None
) -> str:
    max_jobs = config.get("notification", {}).get("max_jobs_in_message", 10)
    inserted = push_result.get("inserted", 0)
    updated = push_result.get("updated", 0)
    cleaned = push_result.get("cleaned", 0)
    error = push_result.get("error")
    fmt = config.get("notification", {}).get("format", "rich")
    board_url = config.get("notification", {}).get("job_board_url", "")

    lines = [f"Daily {_display_name(config)} -- {inserted} new job(s)"]
    lines.append(f"{inserted} new / {updated} updated / {cleaned} cleaned up")
    if error:
        lines.append(f"Error: {error}")
    if push_result.get("capacity_warning"):
        lines.append("Warning: Supabase approaching capacity — consider reviewing old records")
    lines.append("")

    if fmt == "rich" and new_jobs:
        for job in new_jobs[:max_jobs]:
            title_text = job.get("job_title", "Unknown")
            company = job.get("company", "")
            url = job.get("job_url", "")
            lines.append(f"- {title_text} @ {company}")
            if url:
                lines.append(f"  {url}")
        if len(new_jobs) > max_jobs:
            lines.append(f"...and {len(new_jobs) - max_jobs} more in Supabase")

    alert_threshold = (config or {}).get("health", {}).get("alert_consecutive_failures_threshold", 2)
    banner_plain = _build_alert_banner_plain(health_state or {}, alert_threshold)
    if banner_plain:
        lines.insert(0, banner_plain)
    status_plain = _build_source_status_plain(source_execution or [], health_state or {}, alert_threshold)
    if status_plain:
        lines.append(status_plain)
    if board_url:
        lines.append(f"\nView Job Board: {board_url}")
    return "\n".join(lines)


def _send_email(
    email_to: str,
    email_from: str,
    email_password: str,
    push_result: dict,
    new_jobs: list[dict],
    config: dict,
    source_execution: list = None,
    health_state: dict = None,
) -> bool:
    recipients = [addr.strip() for addr in email_to.split(",") if addr.strip()]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = _build_email_subject(push_result, config)
    msg["From"] = email_from
    msg["To"] = ", ".join(recipients)

    plain = _build_email_plain(
        push_result, new_jobs, config, source_execution=source_execution, health_state=health_state
    )
    html = _build_email_html(
        push_result, new_jobs, config, source_execution=source_execution, health_state=health_state
    )
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=REQUEST_TIMEOUT) as server:
            server.starttls()
            server.login(email_from, email_password)
            server.sendmail(email_from, recipients, msg.as_string())
        log.info("Notify: email sent successfully to %s", ", ".join(recipients))
        return True
    except Exception as e:
        log.error("Notify: email failed -- %s", e)
        return False


# ---------------------------------------------------------------------------
# Source status helpers
# ---------------------------------------------------------------------------


def _has_issues(source_execution: list) -> bool:
    """True if any source failed, returned empty, or was skipped."""
    return any(not m.get("success") or m.get("empty") or m.get("skipped") for m in (source_execution or []))


def _source_icon(meta: dict, health_row: dict, alert_threshold: int) -> str:
    """Return status icon for one source."""
    if meta.get("skipped"):
        return "⏭️"
    if not meta.get("success"):
        consec = int((health_row or {}).get("consecutive_failures", 0) or 0)
        return "🔴" if consec >= alert_threshold else "❌"
    if meta.get("empty"):
        return "⚠️"
    return "✅"


def _build_source_status_html(
    source_execution: list,
    health_state: dict,
    alert_threshold: int,
) -> str:
    """HTML source status table. Returns '' if all sources are healthy with jobs."""
    if not source_execution or not _has_issues(source_execution):
        return ""
    health_state = health_state or {}
    rows = []
    for meta in source_execution:
        source = meta.get("source", "unknown")
        icon = _source_icon(meta, health_state.get(source, {}), alert_threshold)
        jobs = meta.get("fetched_count", 0) if not meta.get("skipped") else "—"
        note = ""
        if meta.get("skipped"):
            note = f"skipped ({meta.get('skip_reason') or 'circuit open'})"
        elif meta.get("error"):
            note = str(meta["error"])[:60]
        elif meta.get("empty"):
            note = "returned 0 jobs"
        bg = "#fff3f3" if icon in ("❌", "🔴") else ("#fffbec" if icon == "⚠️" else "#ffffff")
        rows.append(
            f"<tr style='background:{bg}'>"
            f"<td style='padding:5px 8px;font-family:monospace'>{source}</td>"
            f"<td style='padding:5px 8px;text-align:center'>{icon}</td>"
            f"<td style='padding:5px 8px;text-align:right'>{jobs}</td>"
            f"<td style='padding:5px 8px;color:#555;font-size:12px'>{note}</td>"
            f"</tr>"
        )
    return (
        "<h3 style='margin:24px 0 8px;font-size:14px;color:#333'>Source Status</h3>"
        "<table style='width:100%;border-collapse:collapse;font-size:13px;margin-bottom:16px'>"
        "<thead><tr style='background:#f5f5f5'>"
        "<th style='text-align:left;padding:5px 8px'>Source</th>"
        "<th style='text-align:center;padding:5px 8px'>Status</th>"
        "<th style='text-align:right;padding:5px 8px'>Jobs</th>"
        "<th style='text-align:left;padding:5px 8px'>Note</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )


def _build_source_status_plain(
    source_execution: list,
    health_state: dict,
    alert_threshold: int,
) -> str:
    """Plain-text source status summary. Returns '' if all sources are healthy with jobs."""
    if not source_execution or not _has_issues(source_execution):
        return ""
    health_state = health_state or {}
    lines = ["Source Status:"]
    for meta in source_execution:
        source = meta.get("source", "unknown")
        icon = _source_icon(meta, health_state.get(source, {}), alert_threshold)
        jobs = str(meta.get("fetched_count", 0)) if not meta.get("skipped") else "—"
        note = ""
        if meta.get("skipped"):
            note = f" — skipped ({meta.get('skip_reason') or 'circuit open'})"
        elif meta.get("error"):
            note = f" — {str(meta['error'])[:60]}"
        elif meta.get("empty"):
            note = " — returned 0 jobs"
        lines.append(f"  {icon} {source}: {jobs} jobs{note}")
    return "\n".join(lines)


def _build_alert_banner_html(health_state: dict, alert_threshold: int) -> str:
    """Red/amber banner if any source has consecutive_failures >= threshold. Returns '' otherwise."""
    if not health_state:
        return ""
    failing = [
        s for s, r in health_state.items() if int((r or {}).get("consecutive_failures", 0) or 0) >= alert_threshold
    ]
    if not failing:
        return ""
    names = ", ".join(sorted(failing))
    return (
        f"<div style='background:#fff3cd;border:1px solid #ffc107;border-radius:4px;"
        f"padding:12px 16px;margin-bottom:16px;font-size:13px'>"
        f"⚠️ <strong>{len(failing)} source(s) have failed {alert_threshold}+ consecutive runs:</strong> "
        f"{names} — check Source Status below."
        f"</div>"
    )


def _build_alert_banner_plain(health_state: dict, alert_threshold: int) -> str:
    """Plain-text version of the alert banner. Returns '' if no sources at threshold."""
    if not health_state:
        return ""
    failing = [
        s for s, r in health_state.items() if int((r or {}).get("consecutive_failures", 0) or 0) >= alert_threshold
    ]
    if not failing:
        return ""
    return f"⚠️ ALERT: {len(failing)} source(s) failing {alert_threshold}+ consecutive runs: " + ", ".join(
        sorted(failing)
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run(
    push_result: dict,
    new_jobs: list[dict],
    config: dict,
    *,
    discord_url: str = "",
    email_to: str = "",
    email_from: str = "",
    email_password: str = "",
    source_execution: list = None,
    health_state: dict = None,
) -> dict:
    """
    Send notifications via all configured backends.
    Returns dict with per-backend success status.
    """
    results: dict[str, bool | None] = {
        "discord": None,
        "email": None,
    }

    if discord_url:
        log.info("Notify: sending Discord webhook")
        results["discord"] = _send_discord(
            discord_url, push_result, new_jobs, config, source_execution=source_execution, health_state=health_state
        )
    else:
        log.info("Notify: DISCORD_WEBHOOK_URL not set -- skipping Discord")

    if email_to and email_from and email_password:
        log.info("Notify: sending email to %s", email_to)
        results["email"] = _send_email(
            email_to,
            email_from,
            email_password,
            push_result,
            new_jobs,
            config,
            source_execution=source_execution,
            health_state=health_state,
        )
    else:
        log.info("Notify: email credentials not fully set -- skipping email")

    return results
