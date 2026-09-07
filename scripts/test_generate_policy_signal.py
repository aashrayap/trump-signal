"""Offline CLI regression checks; no provider calls or generated output writes."""

import contextlib
import datetime as dt
import io
import sys
import unittest
from unittest import mock

import generate_policy_signal as signal


class ExtractionComplete(Exception):
    """Stop the CLI after real extraction, before rollups or output writes."""


class OfflineCliTests(unittest.TestCase):
    def test_no_fetch_skips_llm_refinement(self):
        docs = [{
            "source": "truth_social",
            "date": dt.date(2026, 1, 1),
            "text": "NVIDIA manufactures chips in America.",
            "url": "https://example.test/source",
        }]
        with (
            mock.patch.object(sys, "argv", ["generate_policy_signal.py", "--no-fetch"]),
            mock.patch.object(signal, "load_social_docs", return_value=docs),
            mock.patch.object(signal, "load_official_and_transcript_docs", return_value=([], {})),
            mock.patch.object(signal.EX, "haiku_refine", side_effect=AssertionError(
                "offline mode attempted LLM refinement"
            )) as refine,
            mock.patch.object(signal, "finalize_companies", side_effect=ExtractionComplete),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(ExtractionComplete):
                signal.main()
        refine.assert_not_called()


if __name__ == "__main__":
    unittest.main()
