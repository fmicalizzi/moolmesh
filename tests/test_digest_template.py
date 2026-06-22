"""Tests para L2 Template rendering."""
import pytest
from hub.digests.template import render_daily, render_weekly, render_technical_summary, _format_date_es, _truncate


class TestFormatDateEs:
    def test_normal_date(self):
        assert _format_date_es("2026-04-16") == "16 de abril de 2026"

    def test_january(self):
        assert _format_date_es("2026-01-01") == "1 de enero de 2026"

    def test_invalid_date(self):
        assert _format_date_es("invalid") == "invalid"


class TestRenderDaily:
    def test_empty_day(self):
        stats = {"commits": 0, "authors": [], "hot_files": [],
                 "loc_added": 0, "loc_removed": 0,
                 "prs_merged": [], "prs_opened": [],
                 "issues_closed": [], "issues_opened": []}

        text = render_daily(stats, "test/repo", "2026-04-16")

        assert "No hubo actividad" in text
        assert "16 de abril de 2026" in text

    def test_with_activity(self):
        stats = {
            "commits": 5, "loc_added": 100, "loc_removed": 20,
            "authors": [{"author_name": "Franco", "commits": 5, "insertions": 100, "deletions": 20}],
            "hot_files": [{"file_path": "main.py", "changes": 3, "insertions": 50, "deletions": 10}],
            "prs_merged": [{"number": 1, "title": "Fix auth", "author": "franco"}],
            "prs_opened": [],
            "issues_closed": [{"number": 42, "title": "Bug"}],
            "issues_opened": [],
        }

        text = render_daily(stats, "test/repo", "2026-04-16")

        assert "5 commits" in text
        assert "+100/-20" in text
        assert "Fix auth" in text
        assert "#42" in text

    def test_multiple_authors(self):
        stats = {
            "commits": 10, "loc_added": 200, "loc_removed": 50,
            "authors": [
                {"author_name": "A", "commits": 6, "insertions": 120, "deletions": 30},
                {"author_name": "B", "commits": 4, "insertions": 80, "deletions": 20},
            ],
            "hot_files": [], "prs_merged": [], "prs_opened": [],
            "issues_closed": [], "issues_opened": [],
        }

        text = render_daily(stats, "test/repo", "2026-04-16")
        assert "2 autores" in text
        assert "Autores más activos" in text


class TestRenderWeekly:
    def test_weekly_header(self):
        stats = {"commits": 0, "authors": [], "hot_files": [],
                 "loc_added": 0, "loc_removed": 0,
                 "prs_merged": [], "prs_opened": [],
                 "issues_closed": [], "issues_opened": []}

        text = render_weekly(stats, "test/repo", "2026-04-13")

        assert "Resumen Semanal" in text
        assert "13 de abril" in text
        assert "19 de abril" in text


class TestTruncate:
    """Tests for _truncate function."""

    def test_truncate_at_boundary(self):
        """Longitud exacta → sin …."""
        s = "exactly_ten"  # 11 chars
        result = _truncate(s, 11)
        assert result == "exactly_ten"
        assert len(result) == 11

    def test_truncate_over_boundary(self):
        """Un char sobre max → … al final, len == max."""
        s = "this_is_long"  # 12 chars
        result = _truncate(s, 10)
        assert len(result) == 10
        assert result.endswith("…")
        assert result == "this_is_l…"

    def test_truncate_short_string(self):
        """String corto sin cambios."""
        s = "short"
        result = _truncate(s, 25)
        assert result == "short"
        assert len(result) == 5

    def test_truncate_long_author_name(self):
        """Nombre largo no rompe alineación."""
        long_name = "Jesús Adrián Rojas Hernández"  # 30 chars
        result = _truncate(long_name, 25)
        assert len(result) == 25
        assert result.endswith("…")

    def test_truncate_long_file_path(self):
        """Path largo truncado correctamente."""
        long_path = "apps/web/src/components/cases/detail/case-resolution-form.tsx"  # 60 chars
        result = _truncate(long_path, 55)
        assert len(result) == 55
        assert result.endswith("…")


class TestRenderTechnicalSummary:
    """Tests for render_technical_summary with truncation."""

    def test_render_technical_summary_long_names(self):
        """Nombre largo no rompe alineación."""
        stats = {
            "commits": 10,
            "authors": [
                {"author_name": "Jesús Adrián Rojas Hernández", "commits": 5, "insertions": 50, "deletions": 10},
                {"author_name": "Ana", "commits": 5, "insertions": 30, "deletions": 20},
            ],
            "hot_files": [
                {"file_path": "apps/web/src/components/cases/detail/case-resolution-form.tsx", "changes": 5, "insertions": 20, "deletions": 10},
            ],
            "branches": [
                {"name": "feature/very-long-branch-name-for-testing", "commits": 5},
            ],
            "loc_added": 80, "loc_removed": 30,
            "prs_merged": [], "prs_opened": [],
            "issues_closed": [], "issues_opened": [],
        }

        text = render_technical_summary(stats, "test/repo", "2026-04-17")

        # Should contain truncated names (exact truncation may vary based on length)
        assert "Jesús Adrián Rojas Herná" in text  # Truncated author name
        assert "apps/web/src/components/cases/detail/case-resolution-f" in text  # Truncated file path
        assert "feature/very-long-branch-name-for-testi" in text  # Truncated branch name
