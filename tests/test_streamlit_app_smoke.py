"""Runtime smoke test for the app without any credential or network action."""

from __future__ import annotations

import importlib.util
import os
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
STREAMLIT_AVAILABLE = importlib.util.find_spec("streamlit") is not None


@unittest.skipUnless(STREAMLIT_AVAILABLE, "install requirements.txt to test Streamlit runtime")
class StreamlitAppSmokeTests(unittest.TestCase):
    def test_app_renders_and_requires_token_before_collection(self):
        from streamlit.testing.v1 import AppTest
        from python_script import fetch_github_pr_data

        with patch.object(fetch_github_pr_data, "urlopen") as github_request:
            app = AppTest.from_file(
                str(ROOT / "dashboard" / "app.py"),
                default_timeout=20,
            ).run()
            self.assertEqual([], list(app.exception))
            self.assertTrue(any(title.value == "Pull request intelligence" for title in app.title))
            self.assertTrue(any(
                item.value == "NEXT PR ACTION PLAN"
                for item in app.subheader
            ))
            self.assertEqual("NEXT PR ACTION PLAN", app.subheader[0].value)
            self.assertEqual("WHY THESE ACTIONS?", app.subheader[1].value)
            self.assertTrue(any(widget.label == "Repositories to include" for widget in app.multiselect))
            self.assertTrue(any(widget.label == "Add repository (OWNER/REPOSITORY)" for widget in app.text_input))
            token_widget = next(widget for widget in app.text_input
                                if widget.label == "GitHub Enterprise token")
            self.assertIn('"password"', (ROOT / "dashboard" / "app.py").read_text(encoding="utf-8"))
            self.assertTrue(any("Enter your GitHub token" in item.value for item in app.info))
            sidebar = list(app.sidebar)
            token_index = next(i for i, widget in enumerate(sidebar)
                               if getattr(widget, "label", None) == "GitHub Enterprise token")
            discovery_index = next(i for i, widget in enumerate(sidebar)
                                   if getattr(widget, "label", None) == "Discover accessible repositories")
            filters_heading_index = next(i for i, widget in enumerate(sidebar)
                                         if getattr(widget, "value", None) == "### Filters")
            filter_button_index = next(i for i, widget in enumerate(sidebar)
                                       if getattr(widget, "label", None) == "Apply Filters")
            pages_heading_index = next(i for i, widget in enumerate(sidebar)
                                       if getattr(widget, "value", None) == "### Pages")
            navigation_index = next(i for i, widget in enumerate(sidebar)
                                    if getattr(widget, "label", None) == "Pages")
            self.assertLess(token_index, discovery_index)
            self.assertLess(discovery_index, filters_heading_index)
            self.assertLess(filter_button_index, pages_heading_index)
            self.assertLess(pages_heading_index, navigation_index)
            self.assertLess(
                max(i for i, widget in enumerate(sidebar)
                    if getattr(widget, "label", None) in {
                        "Repositories to include", "Add repository (OWNER/REPOSITORY)",
                        "PR created from (inclusive)", "PR created to (inclusive)",
                        "Author filter (comma-separated)",
                    }),
                filter_button_index,
            )
            navigation = next(
                widget for widget in app.radio
                if widget.label == "Pages"
            )
            app = navigation.set_value("Quality & Reliability").run()
            self.assertEqual([], list(app.exception))
            self.assertTrue(any("Quality score: unavailable" in item.value for item in app.warning))
            navigation = next(
                widget for widget in app.radio
                if widget.label == "Pages"
            )
            app = navigation.set_value("Insights & Recommendations").run()
            self.assertEqual([], list(app.exception))
            self.assertTrue(any("No repository results are available to summarize" in item.value for item in app.info))

            self.assertTrue(next(item for item in app.button
                                 if item.label == "Discover accessible repositories").disabled)
            self.assertTrue(next(item for item in app.button
                                 if item.label == "Apply Filters").disabled)

        github_request.assert_not_called()
        self.assertEqual([], list(app.exception))
        self.assertTrue(any("Enter your GitHub token" in item.value for item in app.info))
        self.assertFalse(app.session_state.get("collection_complete", False))

    def test_review_submit_invokes_runtime_orchestrator(self):
        from streamlit.testing.v1 import AppTest
        from python_script.orchestrator import Orchestrator

        runtime = patch("python_script.orchestrator.Orchestrator")
        with runtime as factory:
            factory.return_value.run.return_value = {
                "status": {}, "results": {},
                "failures": {"collection": "mocked collection failure"},
            }
            app = AppTest.from_file(
                str(ROOT / "dashboard" / "app.py"), default_timeout=20
            ).run()
            token = next(widget for widget in app.text_input
                         if widget.label == "GitHub Enterprise token")
            token.set_value("mock-read-only-token").run()
            load_button = next(item for item in app.button if item.label == "Apply Filters")
            app = load_button.click().run()

        self.assertEqual([], list(app.exception))
        factory.return_value.run.assert_called_once()
        self.assertEqual("abhijit-pradhan1", factory.return_value.run.call_args.kwargs["author"])
        self.assertEqual("mock-read-only-token", factory.return_value.run.call_args.kwargs["token"])
        self.assertTrue(app.session_state["collection_complete"])

    def test_review_submit_normalizes_multiple_author_filter(self):
        from streamlit.testing.v1 import AppTest

        with patch("python_script.orchestrator.Orchestrator") as factory:
            factory.return_value.run.return_value = {
                "status": {}, "results": {},
                "failures": {"collection": "mocked collection failure"},
            }
            app = AppTest.from_file(
                str(ROOT / "dashboard" / "app.py"), default_timeout=20
            ).run()
            token = next(widget for widget in app.text_input
                         if widget.label == "GitHub Enterprise token")
            token.set_value("mock-read-only-token").run()
            author = next(widget for widget in app.text_input
                          if widget.label == "Author filter (comma-separated)")
            self.assertIn("separated by commas", author.help)
            author.set_value(" author1, , author2, author1 ")
            load_button = next(item for item in app.button if item.label == "Apply Filters")
            load_button.click()
            app.run()

        self.assertEqual([], list(app.exception))
        self.assertEqual("author1, author2", factory.return_value.run.call_args.kwargs["author"])
        self.assertEqual("mock-read-only-token", factory.return_value.run.call_args.kwargs["token"])


if __name__ == "__main__":
    unittest.main()
