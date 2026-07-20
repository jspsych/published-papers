"""Unit tests for find_materials.py link extraction and normalization.

Run with:  python3 -m unittest discover tests

Network access is not used; only pure functions are tested.
"""

import os
import sys
import unittest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

import find_materials as fm  # noqa: E402


class ExtractLinksTests(unittest.TestCase):
    def test_osf_guid(self):
        links = fm.extract_links("Materials at https://osf.io/ab3d9/ today")
        self.assertEqual(links, [("https://osf.io/ab3d9", "osf")])

    def test_osf_reserved_words_skipped(self):
        for word in ("search", "login", "prereg", "view"):
            self.assertEqual(fm.extract_links(f"see osf.io/{word}/x"), [])

    def test_osf_own_guid_skipped(self):
        links = fm.extract_links("preprint at osf.io/ab3d9",
                                 own_guids={"ab3d9"})
        self.assertEqual(links, [])

    def test_github_repo(self):
        links = fm.extract_links("code: github.com/someuser/my-task.git.")
        self.assertEqual(links,
                         [("https://github.com/someuser/my-task", "github")])

    def test_github_jspsych_org_excluded(self):
        self.assertEqual(
            fm.extract_links("we used github.com/jspsych/jsPsych v7"), [])

    def test_zenodo_doi_and_url_normalize_same(self):
        links = fm.extract_links(
            "doi 10.5281/zenodo.12345 and https://zenodo.org/record/12345")
        self.assertEqual(links,
                         [("https://zenodo.org/records/12345", "zenodo")])

    def test_trailing_punctuation_stripped(self):
        links = fm.extract_links("(https://github.com/a/b).")
        self.assertEqual(links, [("https://github.com/a/b", "github")])

    def test_dedup_case_insensitive(self):
        links = fm.extract_links("OSF.IO/AB3D9 and osf.io/ab3d9")
        self.assertEqual(len(links), 1)


class SectionExtractionTests(unittest.TestCase):
    XML = """<article>
      <body>
        <sec><title>Methods</title><p>see github.com/lab/task-code</p></sec>
        <sec sec-type="data-availability"><title>Data availability</title>
          <p>All materials at osf.io/ab3d9.</p></sec>
      </body>
      <back>
        <ref-list><ref>Smith 2020, osf.io/zz9k2</ref></ref-list>
      </back>
    </article>"""

    def test_sections_classified(self):
        result = {u: s for u, _, s in
                  fm.extract_links_with_sections(self.XML)}
        self.assertEqual(result["https://osf.io/ab3d9"], "data_availability")
        self.assertEqual(result["https://github.com/lab/task-code"], "body")
        self.assertEqual(result["https://osf.io/zz9k2"], "references")

    def test_malformed_xml_falls_back(self):
        result = fm.extract_links_with_sections("not xml < osf.io/ab3d9")
        self.assertEqual(result, [("https://osf.io/ab3d9", "osf", "unknown")])

    def test_availability_title_without_sectype(self):
        xml = ("<article><body><sec><title>Open practices statement</title>"
               "<p>osf.io/ab3d9</p></sec></body></article>")
        result = fm.extract_links_with_sections(xml)
        self.assertEqual(result,
                         [("https://osf.io/ab3d9", "osf", "data_availability")])


class PreprintGuidTests(unittest.TestCase):
    def test_psyarxiv_with_version(self):
        self.assertEqual(
            fm.osf_guid_from_preprint_doi("10.31234/osf.io/dztvp_v1"), "dztvp")

    def test_plain_osf_preprint(self):
        self.assertEqual(
            fm.osf_guid_from_preprint_doi("10.31219/osf.io/9rfx8"), "9rfx8")

    def test_non_osf_doi(self):
        self.assertEqual(
            fm.osf_guid_from_preprint_doi("10.1101/2024.01.01.573000"), "")


class CachePolicyTests(unittest.TestCase):
    def test_never_extracted(self):
        self.assertTrue(fm._should_reextract(None))

    def test_ok_not_rechecked(self):
        self.assertFalse(
            fm._should_reextract({"date": "2020-01-01", "status": "ok"}))

    def test_noft_rechecked_after_window(self):
        self.assertTrue(
            fm._should_reextract({"date": "2020-01-01", "status": "noft"}))
        self.assertFalse(
            fm._should_reextract({"date": fm.TODAY, "status": "noft"}))


if __name__ == "__main__":
    unittest.main()
