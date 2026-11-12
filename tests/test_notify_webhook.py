import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import notify_webhook


class NotificationIdentityTests(unittest.TestCase):
    def test_uses_configured_data_engineering_display_name_everywhere(self):
        config = {"identity": {"display_name": "Data Engineering Job Search"}}
        push_result = {"inserted": 1, "updated": 0, "cleaned": 0}

        self.assertIn("Data Engineering Job Search", notify_webhook._build_discord_content(push_result, [], config))
        self.assertIn("Data Engineering Job Search", notify_webhook._build_email_subject(push_result, config))
        self.assertIn("Data Engineering Job Search", notify_webhook._build_email_html(push_result, [], config))
        self.assertIn("Data Engineering Job Search", notify_webhook._build_email_plain(push_result, [], config))

    def test_uses_neutral_fallback_when_identity_is_not_configured(self):
        self.assertIn("Job Search", notify_webhook._build_discord_content({"inserted": 0}, [], {}))
        self.assertNotIn("QA Job Search", notify_webhook._build_discord_content({"inserted": 0}, [], {}))


class SendEmailMultiRecipientTests(unittest.TestCase):
    def _make_smtp(self):
        smtp = MagicMock()
        smtp.__enter__ = MagicMock(return_value=smtp)
        smtp.__exit__ = MagicMock(return_value=False)
        return smtp

    @patch("notify_webhook.smtplib.SMTP")
    def test_single_recipient_still_works(self, mock_smtp_cls):
        smtp = self._make_smtp()
        mock_smtp_cls.return_value = smtp

        notify_webhook._send_email(
            email_to="only@example.com",
            email_from="from@example.com",
            email_password="pw",
            push_result={"inserted": 1},
            new_jobs=[],
            config={},
        )

        _, sendmail_args, _ = smtp.sendmail.mock_calls[0]
        recipients = sendmail_args[1]
        self.assertEqual(recipients, ["only@example.com"])

    @patch("notify_webhook.smtplib.SMTP")
    def test_comma_separated_sends_to_all_recipients(self, mock_smtp_cls):
        smtp = self._make_smtp()
        mock_smtp_cls.return_value = smtp

        notify_webhook._send_email(
            email_to="a@example.com, b@example.com",
            email_from="from@example.com",
            email_password="pw",
            push_result={"inserted": 2},
            new_jobs=[],
            config={},
        )

        _, sendmail_args, _ = smtp.sendmail.mock_calls[0]
        recipients = sendmail_args[1]
        self.assertIn("a@example.com", recipients)
        self.assertIn("b@example.com", recipients)
        self.assertEqual(len(recipients), 2)

    @patch("notify_webhook.smtplib.SMTP")
    def test_to_header_is_string_not_list(self, mock_smtp_cls):
        smtp = self._make_smtp()
        mock_smtp_cls.return_value = smtp

        notify_webhook._send_email(
            email_to="a@example.com,b@example.com",
            email_from="from@example.com",
            email_password="pw",
            push_result={"inserted": 1},
            new_jobs=[],
            config={},
        )

        # Grab the raw message string passed to sendmail
        _, sendmail_args, _ = smtp.sendmail.mock_calls[0]
        raw_msg = sendmail_args[2]
        self.assertIn("To: a@example.com, b@example.com", raw_msg)


SOURCE_EXECUTION_FIXTURE = [
    {
        "source": "jsearch",
        "success": True,
        "error": None,
        "fetched_count": 47,
        "empty": False,
        "skipped": False,
        "skip_reason": None,
    },
    {
        "source": "greenhouse",
        "success": True,
        "error": None,
        "fetched_count": 0,
        "empty": True,
        "skipped": False,
        "skip_reason": None,
    },
    {
        "source": "dice",
        "success": False,
        "error": "403 Forbidden",
        "fetched_count": 0,
        "empty": False,
        "skipped": False,
        "skip_reason": None,
    },
    {
        "source": "lever",
        "success": False,
        "error": None,
        "fetched_count": 0,
        "empty": False,
        "skipped": True,
        "skip_reason": "circuit open",
    },
]

HEALTH_STATE_FIXTURE = {
    "jsearch": {"consecutive_failures": 0},
    "greenhouse": {"consecutive_failures": 0},
    "dice": {"consecutive_failures": 2},
    "lever": {"consecutive_failures": 3},
}

ALL_HEALTHY_FIXTURE = [
    {
        "source": "jsearch",
        "success": True,
        "error": None,
        "fetched_count": 10,
        "empty": False,
        "skipped": False,
        "skip_reason": None,
    },
]


class SourceStatusHtmlTests(unittest.TestCase):
    def test_returns_empty_string_when_all_sources_healthy(self):
        result = notify_webhook._build_source_status_html(ALL_HEALTHY_FIXTURE, {}, alert_threshold=2)
        self.assertEqual(result, "")

    def test_contains_failed_source_name(self):
        result = notify_webhook._build_source_status_html(
            SOURCE_EXECUTION_FIXTURE, HEALTH_STATE_FIXTURE, alert_threshold=2
        )
        self.assertIn("dice", result)

    def test_contains_error_message(self):
        result = notify_webhook._build_source_status_html(
            SOURCE_EXECUTION_FIXTURE, HEALTH_STATE_FIXTURE, alert_threshold=2
        )
        self.assertIn("403 Forbidden", result)

    def test_contains_html_table(self):
        result = notify_webhook._build_source_status_html(
            SOURCE_EXECUTION_FIXTURE, HEALTH_STATE_FIXTURE, alert_threshold=2
        )
        self.assertIn("<table", result)

    def test_skipped_source_shown(self):
        result = notify_webhook._build_source_status_html(
            SOURCE_EXECUTION_FIXTURE, HEALTH_STATE_FIXTURE, alert_threshold=2
        )
        self.assertIn("lever", result)


class SourceStatusPlainTests(unittest.TestCase):
    def test_returns_empty_string_when_all_sources_healthy(self):
        result = notify_webhook._build_source_status_plain(ALL_HEALTHY_FIXTURE, {}, alert_threshold=2)
        self.assertEqual(result, "")

    def test_contains_failed_source_info(self):
        result = notify_webhook._build_source_status_plain(
            SOURCE_EXECUTION_FIXTURE, HEALTH_STATE_FIXTURE, alert_threshold=2
        )
        self.assertIn("dice", result)
        self.assertIn("403 Forbidden", result)

    def test_returns_no_html_tags(self):
        result = notify_webhook._build_source_status_plain(
            SOURCE_EXECUTION_FIXTURE, HEALTH_STATE_FIXTURE, alert_threshold=2
        )
        self.assertNotIn("<table", result)
        self.assertNotIn("<tr", result)


class AlertBannerTests(unittest.TestCase):
    def test_html_returns_empty_when_no_sources_at_threshold(self):
        state = {
            "jsearch": {"consecutive_failures": 0},
            "greenhouse": {"consecutive_failures": 1},  # below threshold of 2
        }
        result = notify_webhook._build_alert_banner_html(state, alert_threshold=2)
        self.assertEqual(result, "")

    def test_html_returns_banner_when_threshold_met(self):
        state = {"dice": {"consecutive_failures": 2}, "lever": {"consecutive_failures": 3}}
        result = notify_webhook._build_alert_banner_html(state, alert_threshold=2)
        self.assertIn("dice", result)
        self.assertIn("⚠️", result)

    def test_html_returns_empty_when_health_state_none(self):
        result = notify_webhook._build_alert_banner_html(None, alert_threshold=2)
        self.assertEqual(result, "")

    def test_html_returns_empty_when_health_state_empty(self):
        result = notify_webhook._build_alert_banner_html({}, alert_threshold=2)
        self.assertEqual(result, "")

    def test_plain_returns_empty_when_no_failures(self):
        result = notify_webhook._build_alert_banner_plain({}, alert_threshold=2)
        self.assertEqual(result, "")

    def test_plain_names_failing_sources(self):
        state = {"dice": {"consecutive_failures": 2}}
        result = notify_webhook._build_alert_banner_plain(state, alert_threshold=2)
        self.assertIn("dice", result)
        self.assertIn("⚠️", result)


class AirtableReferenceTests(unittest.TestCase):
    def test_discord_capacity_warning_says_supabase(self):
        push_result = {"inserted": 0, "updated": 0, "cleaned": 0, "capacity_warning": True}
        content = notify_webhook._build_discord_content(push_result, [], {})
        self.assertNotIn("Airtable", content)
        self.assertIn("Supabase", content)

    def test_email_html_capacity_warning_says_supabase(self):
        push_result = {"inserted": 0, "updated": 0, "cleaned": 0, "capacity_warning": True}
        html = notify_webhook._build_email_html(push_result, [], {})
        self.assertNotIn("Airtable", html)
        self.assertIn("Supabase", html)

    def test_email_plain_capacity_warning_says_supabase(self):
        push_result = {"inserted": 0, "updated": 0, "cleaned": 0, "capacity_warning": True}
        plain = notify_webhook._build_email_plain(push_result, [], {})
        self.assertNotIn("Airtable", plain)
        self.assertIn("Supabase", plain)
