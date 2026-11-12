import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import fetch_jobs_company_careers


class FetchJobsCompanyCareersParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        fetch_jobs_company_careers._STATIC_CACHE["wayfair"] = None

    def test_extract_shopify_jobs_from_html(self) -> None:
        html = """
        <a href="/careers/facility-operations-manager_61ae4f03-54da-45ae-b7b0-e49b2bd108c3">
          Facility Operations ManagerRemote - Los Angeles
        </a>
        <a href="/careers/senior-fraud-operations-analyst_66b43ee6-e935-478f-a073-b16d4a9f0c0d">
          Senior Fraud Operations AnalystRemote - Americas
        </a>
        """

        jobs = fetch_jobs_company_careers._extract_shopify_jobs_from_html(html)

        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0]["job_title"], "Facility Operations Manager")
        self.assertEqual(jobs[0]["location"], "Remote - Los Angeles")
        self.assertEqual(
            jobs[0]["job_url"],
            "https://www.shopify.com/careers/facility-operations-manager_61ae4f03-54da-45ae-b7b0-e49b2bd108c3",
        )

    def test_extract_nulogy_jobs_from_html(self) -> None:
        html = """
        <div class="job-opening">
          <h2 class="elementor-heading-title elementor-size-default">Professional Services Consultant</h2>
          <p class="elementor-heading-title elementor-size-default">Remote</p>
          <a href="https://nulogy.applytojob.com/apply/9RrxSXp4GH/Professional-Services-Consultant">Apply Now</a>
        </div>
        <div class="job-opening">
          <h2 class="elementor-heading-title elementor-size-default">Head Of Infrastructure</h2>
          <p class="elementor-heading-title elementor-size-default">Toronto, ON</p>
          <a href="https://nulogy.applytojob.com/apply/vhw4d2PqGH/Head-Of-Infrastructure">Apply Now</a>
        </div>
        """

        jobs = fetch_jobs_company_careers._extract_nulogy_jobs_from_html(html)

        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0]["job_title"], "Professional Services Consultant")
        self.assertEqual(jobs[0]["location"], "Remote")
        self.assertEqual(jobs[1]["location"], "Toronto, ON")

    def test_extract_relex_jobs_from_payload_html(self) -> None:
        payload = {
            "html": """
            <article class="job-item">
              <div class="entry-meta-location">Atlanta, GA, United States</div>
              <h3 class="job-item__title">
                <a href="https://www.relexsolutions.com/careers/jobs/supply-chain-consultant/">Supply Chain Consultant</a>
              </h3>
            </article>
            <article class="job-item">
              <div class="entry-meta-location">Melbourne, Australia</div>
              <h3 class="job-item__title">
                <a href="https://www.relexsolutions.com/careers/jobs/it-operations-support-student/">IT Operations Support (Student)</a>
              </h3>
            </article>
            """
        }

        jobs = fetch_jobs_company_careers._extract_relex_jobs_from_payload(payload)

        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0]["job_title"], "Supply Chain Consultant")
        self.assertEqual(jobs[0]["location"], "Atlanta, GA, United States")
        self.assertEqual(
            jobs[0]["job_url"],
            "https://www.relexsolutions.com/careers/jobs/supply-chain-consultant/",
        )

    def test_extract_wayfair_detail_urls_from_listing_html(self) -> None:
        html = """
        <a href="/careers/job/warehouse-supervisor/2-10195">Warehouse Supervisor</a>
        <a href="/careers/job/senior-manager-operations/2-20202">Senior Manager Operations</a>
        <a href="/careers/job/warehouse-supervisor/2-10195">Duplicate Warehouse Supervisor</a>
        """

        urls = fetch_jobs_company_careers._extract_wayfair_detail_urls_from_html(html)

        self.assertEqual(
            urls,
            [
                "https://www.wayfair.com/careers/job/warehouse-supervisor/2-10195",
                "https://www.wayfair.com/careers/job/senior-manager-operations/2-20202",
            ],
        )

    def test_extract_wayfair_job_from_detail_html(self) -> None:
        html = """
        <link rel="canonical" href="https://www.wayfair.com/careers/job/warehouse-supervisor/2-10195" />
        <script type="application/ld+json">
        {"@context":"http://schema.org","@type":"JobPosting","url":"https://wayfair.avature.net/en_US/careers?folderId=10195"}
        </script>
        <h1>Warehouse Supervisor</h1>
        <dl>
          <dt>Location:</dt><dd>Cranbury, New Jersey</dd>
          <dt>Requisition ID:</dt><dd>10195</dd>
        </dl>
        <div class="CareersJobDetailsDisplayDescription-jobDescription">
          Warehouse operations leadership and supply chain execution.
        </div>
        """

        job = fetch_jobs_company_careers._extract_wayfair_job_from_detail_html(html)

        self.assertIsNotNone(job)
        self.assertEqual(job["job_title"], "Warehouse Supervisor")
        self.assertEqual(job["location"], "Cranbury, New Jersey")
        self.assertEqual(
            job["job_url"],
            "https://wayfair.avature.net/en_US/careers?folderId=10195",
        )
        self.assertEqual(job["_wayfair_dedupe_key"], "10195")
        self.assertIn("supply chain", job["description_snippet"].lower())

    def test_extract_wayfair_job_uses_folder_id_when_requisition_missing(self) -> None:
        html = """
        <link rel="canonical" href="https://www.wayfair.com/careers/job/program-manager/2-30303" />
        <script type="application/ld+json">
        {"@context":"http://schema.org","@type":"JobPosting","url":"https://wayfair.avature.net/en_US/careers?folderId=30303"}
        </script>
        <h1>Program Manager</h1>
        <div class="CareersJobDetailsDisplayDescription-jobDescription">
          Program delivery for warehouse systems.
        </div>
        """

        job = fetch_jobs_company_careers._extract_wayfair_job_from_detail_html(html)

        self.assertIsNotNone(job)
        self.assertEqual(job["_wayfair_dedupe_key"], "30303")

    @patch("fetch_jobs_company_careers.time.sleep", return_value=None)
    @patch("fetch_jobs_company_careers.requests.post")
    @patch("fetch_jobs_company_careers.requests.get")
    def test_fetch_wayfair_uses_canonical_url_and_dedupes_by_folder_id(
        self, mock_get: Mock, mock_post: Mock, _mock_sleep: Mock
    ) -> None:
        listing_html = """
        <a href="/careers/job/warehouse-supervisor/2-10195">Warehouse Supervisor</a>
        <a href="/careers/job/warehouse-supervisor-copy/9-88888">Warehouse Supervisor Copy</a>
        """
        detail_html_primary = """
        <link rel="canonical" href="https://www.wayfair.com/careers/job/warehouse-supervisor/2-10195" />
        <script type="application/ld+json">
        {"@context":"http://schema.org","@type":"JobPosting","url":"https://wayfair.avature.net/en_US/careers?folderId=10195"}
        </script>
        <h1>Warehouse Supervisor</h1>
        <dl><dt>Location:</dt><dd>Cranbury, New Jersey</dd></dl>
        <div class="CareersJobDetailsDisplayDescription-jobDescription">
          Supply chain leadership and warehouse operations.
        </div>
        """
        detail_html_duplicate = """
        <link rel="canonical" href="https://www.wayfair.com/careers/job/warehouse-supervisor-copy/9-88888" />
        <script type="application/ld+json">
        {"@context":"http://schema.org","@type":"JobPosting","url":"https://wayfair.avature.net/en_US/careers?folderId=10195"}
        </script>
        <h1>Warehouse Supervisor</h1>
        <dl><dt>Location:</dt><dd>Cranbury, New Jersey</dd></dl>
        <div class="CareersJobDetailsDisplayDescription-jobDescription">
          Supply chain leadership and warehouse operations.
        </div>
        """

        mock_get.side_effect = [
            Mock(status_code=200, text=listing_html, raise_for_status=Mock()),
            Mock(status_code=200, text=detail_html_primary, raise_for_status=Mock()),
            Mock(status_code=200, text=detail_html_duplicate, raise_for_status=Mock()),
        ]
        mock_post.side_effect = RuntimeError("blocked")

        jobs = fetch_jobs_company_careers._fetch_wayfair("supply chain")

        self.assertEqual(len(jobs), 1)
        self.assertEqual(
            jobs[0]["job_url"],
            "https://wayfair.avature.net/en_US/careers?folderId=10195",
        )
        self.assertNotIn("_wayfair_dedupe_key", jobs[0])
        self.assertEqual(mock_get.call_count, 3)

    def test_wayfair_is_registered_as_builtin_fetcher(self) -> None:
        self.assertIs(
            fetch_jobs_company_careers.BUILTIN_FETCHERS.get("Wayfair"),
            fetch_jobs_company_careers._fetch_wayfair,
        )

    def test_build_wayfair_job_url_from_api_payload(self) -> None:
        url = fetch_jobs_company_careers._build_wayfair_job_url_from_api_payload(
            {"title": "Warehouse Supervisor", "eid": "10195", "system": 2}
        )

        self.assertEqual(
            url,
            "https://wayfair.avature.net/careers?folderId=10195",
        )


class RunHealthTrackingTests(unittest.TestCase):
    """Tests for the per-company upsert_company_health() calls added to run()."""

    def _make_config(self, companies: list[dict]) -> dict:
        return {
            "search": {"keywords": ["quality engineer"]},
            "companies": companies,
        }

    @patch("fetch_jobs_company_careers.upsert_company_health")
    @patch("fetch_jobs_company_careers.time.sleep", return_value=None)
    def test_health_upsert_called_on_successful_fetch(
        self, _mock_sleep: Mock, mock_upsert: Mock
    ) -> None:
        """upsert_company_health is called once per company when fetch succeeds."""
        config = self._make_config(
            [{"name": "Chase", "ats": "custom", "url": "https://example.com/jobs", "enabled": True}]
        )
        with patch.dict("os.environ", {"SUPABASE_URL": "https://db.example.com", "SUPABASE_SERVICE_KEY": "key"}):
            with patch("fetch_jobs_company_careers.BUILTIN_FETCHERS", {"Chase": lambda kw: []}):
                fetch_jobs_company_careers.run(config)

        mock_upsert.assert_called_once()
        call_kwargs = mock_upsert.call_args.kwargs
        self.assertEqual(call_kwargs["company"], "Chase")
        self.assertEqual(call_kwargs["ats_type"], "company_careers")
        self.assertEqual(call_kwargs["ats_token"], "chase")

    @patch("fetch_jobs_company_careers.upsert_company_health")
    @patch("fetch_jobs_company_careers.time.sleep", return_value=None)
    def test_health_upsert_skipped_when_all_fetches_raise(
        self, _mock_sleep: Mock, mock_upsert: Mock
    ) -> None:
        """upsert_company_health is NOT called when every keyword fetch raises."""
        config = self._make_config(
            [{"name": "Chase", "ats": "custom", "url": "https://example.com/jobs", "enabled": True}]
        )
        with patch.dict("os.environ", {"SUPABASE_URL": "https://db.example.com", "SUPABASE_SERVICE_KEY": "key"}):
            with patch("fetch_jobs_company_careers.BUILTIN_FETCHERS", {"Chase": Mock(side_effect=RuntimeError("network timeout"))}):
                fetch_jobs_company_careers.run(config)

        mock_upsert.assert_not_called()

    @patch("fetch_jobs_company_careers.upsert_company_health")
    @patch("fetch_jobs_company_careers.time.sleep", return_value=None)
    def test_health_upsert_uses_explicit_board_url(
        self, _mock_sleep: Mock, mock_upsert: Mock
    ) -> None:
        """board_url from config is preferred over falling back to the fetch URL."""
        config = self._make_config(
            [
                {
                    "name": "Amazon",
                    "ats": "custom",
                    "url": "https://www.amazon.jobs/en/search.json?base_query={keyword}",
                    "board_url": "https://www.amazon.jobs/en/",
                    "enabled": True,
                }
            ]
        )
        with patch.dict("os.environ", {"SUPABASE_URL": "https://db.example.com", "SUPABASE_SERVICE_KEY": "key"}):
            with patch("fetch_jobs_company_careers.BUILTIN_FETCHERS", {"Amazon": lambda kw: []}):
                fetch_jobs_company_careers.run(config)

        call_kwargs = mock_upsert.call_args.kwargs
        self.assertEqual(call_kwargs["job_board_url"], "https://www.amazon.jobs/en/")

    @patch("fetch_jobs_company_careers.upsert_company_health")
    @patch("fetch_jobs_company_careers.time.sleep", return_value=None)
    def test_health_upsert_falls_back_to_stripped_url(
        self, _mock_sleep: Mock, mock_upsert: Mock
    ) -> None:
        """When no board_url, query-string and {keyword} are stripped from fetch URL."""
        config = self._make_config(
            [
                {
                    "name": "Chase",
                    "ats": "custom",
                    "url": "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1002",
                    "enabled": True,
                }
            ]
        )
        with patch.dict("os.environ", {"SUPABASE_URL": "https://db.example.com", "SUPABASE_SERVICE_KEY": "key"}):
            with patch("fetch_jobs_company_careers.BUILTIN_FETCHERS", {"Chase": lambda kw: []}):
                fetch_jobs_company_careers.run(config)

        call_kwargs = mock_upsert.call_args.kwargs
        self.assertEqual(
            call_kwargs["job_board_url"],
            "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1002",
        )

    @patch("fetch_jobs_company_careers.upsert_company_health")
    @patch("fetch_jobs_company_careers.time.sleep", return_value=None)
    def test_health_upsert_uses_cache_size_for_static_cache_adapters(
        self, _mock_sleep: Mock, mock_upsert: Mock
    ) -> None:
        """Static-cache adapters report raw board size, not keyword-match count."""
        config = self._make_config(
            [{"name": "Netflix", "ats": "lever", "url": "https://api.lever.co/v0/postings/netflix", "enabled": True}]
        )
        # Simulate cache populated with 50 board jobs (only 0 matched the keyword)
        fake_cache = [{"title": "Software Engineer", "hostedUrl": f"https://jobs.netflix.com/{i}"} for i in range(50)]
        with patch.dict("os.environ", {"SUPABASE_URL": "https://db.example.com", "SUPABASE_SERVICE_KEY": "key"}):
            with patch("fetch_jobs_company_careers.BUILTIN_FETCHERS", {"Netflix": lambda kw: []}):
                with patch.dict("fetch_jobs_company_careers._STATIC_CACHE", {"netflix": fake_cache}):
                    fetch_jobs_company_careers.run(config)

        call_kwargs = mock_upsert.call_args.kwargs
        # Should use the 50-job cache size, not the 0 keyword-match count
        self.assertEqual(call_kwargs["jobs_found"], 50)


class RelexDateTests(unittest.TestCase):
    def setUp(self) -> None:
        fetch_jobs_company_careers._STATIC_CACHE["relex"] = None

    def tearDown(self) -> None:
        fetch_jobs_company_careers._STATIC_CACHE["relex"] = None

    def test_fetch_relex_published_date_extracts_article_published_time(self) -> None:
        html = '<meta property="article:published_time" content="2026-02-10T08:00:00+00:00" />'
        result = fetch_jobs_company_careers._fetch_relex_published_date_from_html(html)
        self.assertEqual(result, "2026-02-10")

    def test_fetch_relex_published_date_falls_back_to_json_ld(self) -> None:
        html = '<script type="application/ld+json">{"@type":"JobPosting","datePublished":"2025-11-03"}</script>'
        result = fetch_jobs_company_careers._fetch_relex_published_date_from_html(html)
        self.assertEqual(result, "2025-11-03")

    def test_fetch_relex_published_date_returns_none_when_no_date(self) -> None:
        result = fetch_jobs_company_careers._fetch_relex_published_date_from_html("<html></html>")
        self.assertIsNone(result)

    def test_fetch_relex_passes_date_to_canonical_job(self) -> None:
        fetch_jobs_company_careers._STATIC_CACHE["relex"] = [
            {
                "job_title": "Supply Chain Consultant",
                "location": "Atlanta, GA, United States",
                "job_url": "https://www.relexsolutions.com/careers/jobs/supply-chain-consultant/",
            }
        ]
        detail_html = '<meta property="article:published_time" content="2026-03-01T00:00:00+00:00" />'
        mock_resp = Mock()
        mock_resp.text = detail_html
        mock_resp.raise_for_status = Mock()
        with patch("fetch_jobs_company_careers.requests.get", return_value=mock_resp):
            jobs = fetch_jobs_company_careers._fetch_relex("supply chain")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["date_posted"], "2026-03-01")


class ShopifyDateTests(unittest.TestCase):
    # Hydration snippet format found in real Shopify HTML: escaped-quote JSON-within-JSON
    _HYDRATION_SNIPPET = (
        r"\"2026-01-15\",\"https://app.ashbyhq.com/api?ashby_jid=61ae4f03-54da-45ae-b7b0-e49b2bd108c3\""
        + "\n"
        + r"\"2026-03-20\",\"https://app.ashbyhq.com/api?ashby_jid=66b43ee6-e935-478f-a073-b16d4a9f0c0d\""
    )

    def test_build_shopify_date_map_extracts_uuid_date_pairs(self) -> None:
        date_map = fetch_jobs_company_careers._build_shopify_date_map(self._HYDRATION_SNIPPET)
        self.assertEqual(date_map.get("61ae4f03-54da-45ae-b7b0-e49b2bd108c3"), "2026-01-15")
        self.assertEqual(date_map.get("66b43ee6-e935-478f-a073-b16d4a9f0c0d"), "2026-03-20")

    def test_extract_shopify_jobs_includes_date_posted_when_hydration_matches(self) -> None:
        html = (
            '<a href="/careers/facility-operations-manager_61ae4f03-54da-45ae-b7b0-e49b2bd108c3">'
            "Facility Operations ManagerRemote - Los Angeles</a>" + self._HYDRATION_SNIPPET
        )
        jobs = fetch_jobs_company_careers._extract_shopify_jobs_from_html(html)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["date_posted"], "2026-01-15")

    def test_extract_shopify_jobs_date_posted_none_when_uuid_not_in_hydration(self) -> None:
        html = (
            '<a href="/careers/no-match-job_aaaaaaaa-0000-0000-0000-bbbbbbbbbbbb">'
            "No Match JobRemote - Americas</a>" + self._HYDRATION_SNIPPET
        )
        jobs = fetch_jobs_company_careers._extract_shopify_jobs_from_html(html)
        self.assertEqual(len(jobs), 1)
        self.assertIsNone(jobs[0]["date_posted"])


class KomatsuDateTests(unittest.TestCase):
    def setUp(self) -> None:
        fetch_jobs_company_careers._STATIC_CACHE["komatsu"] = None

    def tearDown(self) -> None:
        fetch_jobs_company_careers._STATIC_CACHE["komatsu"] = None

    def test_parse_date_handles_m_d_yy_format(self) -> None:
        self.assertEqual(fetch_jobs_company_careers._parse_date("2/16/26"), "2026-02-16")

    def test_parse_date_handles_m_d_yy_single_digit_month(self) -> None:
        self.assertEqual(fetch_jobs_company_careers._parse_date("1/5/26"), "2026-01-05")

    def test_fetch_komatsu_includes_date_posted(self) -> None:
        fetch_jobs_company_careers._STATIC_CACHE["komatsu"] = [
            {
                "title": "Supply Chain Manager",
                "location": "Milwaukee, WI",
                "job_url": "https://komatsu.jobs/job/supply-chain-manager/12345-en_US",
                "date_posted": "2/16/26",
            }
        ]
        jobs = fetch_jobs_company_careers._fetch_komatsu("supply chain")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["date_posted"], "2026-02-16")


if __name__ == "__main__":
    unittest.main()
