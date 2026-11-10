"""`Report.build`: exactly the display convention v1 of CONTRACTS.md (Contract 2)."""

import json

import pytest

from vumlo_plugins import Report
from vumlo_plugins.report import MAX_DATA_BYTES, json_size


def test_a_plain_report_has_a_summary_and_no_display():
    report = Report.build("42 words")
    assert report.body() == {"summary": "42 words", "data": {}}


def test_the_full_display():
    report = Report.build(
        "3 students at risk", level="warn", title="At risk", columns=["Student", "Risk", "Average"],
        rows=[["Olena S.", "high", 41.5], ["Ivan P.", "medium", 58]], facts=[("Mean", "71.4"), ("Late", 3)],
        links=[("Download XLSX", "/export/c1/xlsx"), ("CSV", "export/c1/csv")], plugin_id="gradebook-export",
        data={"students": 2})
    assert report.body() == {"summary": "3 students at risk", "data": {
        "students": 2,
        "display": {
            "level": "warn", "title": "At risk",
            "table": {"columns": ["Student", "Risk", "Average"],
                      "rows": [["Olena S.", "high", 41.5], ["Ivan P.", "medium", 58]]},
            "links": [{"title": "Download XLSX", "path": "/api/v1/plugins/gradebook-export/export/c1/xlsx"},
                      {"title": "CSV", "path": "/api/v1/plugins/gradebook-export/export/c1/csv"}],
            "facts": [{"label": "Mean", "value": "71.4"}, {"label": "Late", "value": "3"}],
        }}}


def test_the_link_path_always_starts_with_the_plugin_prefix():
    report = Report.build("x", links=[("a", "/export/1")], plugin_id="p")
    assert report.data["display"]["links"][0]["path"].startswith("/api/v1/plugins/p/")
    for escape in ("/../../auth/logout", "/a/../b", "//evil.example", "/a\\b"):
        with pytest.raises(ValueError, match="stays inside"):
            Report.build("x", links=[("a", escape)], plugin_id="p")


def test_links_need_the_plugin_id_and_summary_is_required():
    with pytest.raises(ValueError, match="plugin_id"):
        Report.build("x", links=[("a", "/b")])
    for empty in ("", "   ", "\n"):
        with pytest.raises(ValueError, match="summary"):
            Report.build(empty)


def test_levels_titles_and_reserved_display():
    for level in ("ok", "info", "warn", "alert"):
        assert Report.build("x", level=level).data["display"]["level"] == level
    with pytest.raises(ValueError, match="level"):
        Report.build("x", level="red")
    with pytest.raises(ValueError, match="title"):
        Report.build("x", title="t" * 81)
    Report.build("x", title="t" * 80)
    with pytest.raises(ValueError, match="reserved"):
        Report.build("x", data={"display": {}})


def test_the_summary_is_one_line_of_at_most_500_characters():
    summary = Report.build("a\nb\tc" + "x" * 600).summary
    assert len(summary) == 500 and "\n" not in summary and "\t" not in summary and summary.endswith("…")


def test_cells_are_strings_or_numbers_and_never_break_json():
    report = Report.build("x", columns=["a", "b", "c", "d", "e"],
                          rows=[[None, True, float("nan"), "line\nbreak", 1.5], [float("inf"), False, 2, "x" * 300, 0]])
    first, second = report.data["display"]["table"]["rows"]
    assert first == ["", "yes", "", "line break", 1.5]
    assert second[:3] == ["", "no", 2] and len(second[3]) == 200
    json.dumps(report.body(), allow_nan=False)


def test_a_row_must_have_one_cell_per_column_and_rows_need_columns():
    with pytest.raises(ValueError, match="one cell"):
        Report.build("x", columns=["a", "b"], rows=[["only"]])
    with pytest.raises(ValueError, match="need columns"):
        Report.build("x", rows=[["a"]])
    assert Report.build("x", columns=["a"]).data["display"]["table"] == {"columns": ["a"], "rows": []}


def test_at_most_twenty_facts():
    Report.build("x", facts=[(f"k{i}", i) for i in range(20)])
    with pytest.raises(ValueError, match="at most 20"):
        Report.build("x", facts=[(f"k{i}", i) for i in range(21)])


def test_a_long_table_is_cut_to_200_rows_and_says_so():
    rows = [[f"student {i}", i] for i in range(350)]
    report = Report.build("350 students", columns=["Student", "Points"], rows=rows)
    display = report.data["display"]
    assert len(display["table"]["rows"]) == 200
    assert display["facts"] == [{"label": "More rows", "value": "150"}]
    assert json_size(report.data) <= MAX_DATA_BYTES


def test_a_wide_table_is_cut_to_fit_16_kib_and_the_fact_counts_what_was_left_out():
    rows = [[f"Student number {i}", "x" * 150, "y" * 150] for i in range(200)]
    report = Report.build("big", columns=["a", "b", "c"], rows=rows, facts=[("Mean", 1)])
    display = report.data["display"]
    kept = len(display["table"]["rows"])
    assert 0 < kept < 200 and json_size(report.data) <= MAX_DATA_BYTES
    more = next(f for f in display["facts"] if f["label"] == "More rows")
    assert more["value"] == str(200 - kept)
    assert display["facts"][0] == {"label": "Mean", "value": "1"}
    # The most that fit: one more row would not.
    display["table"]["rows"].append(rows[kept])
    assert json_size(report.data) > MAX_DATA_BYTES


def test_a_table_that_fits_gets_no_more_rows_fact():
    report = Report.build("ok", columns=["a"], rows=[["x"]] * 5)
    assert "facts" not in report.data["display"]


def test_data_that_cannot_fit_even_without_rows_is_refused():
    with pytest.raises(ValueError, match="16 KiB"):
        Report.build("x", columns=["a"], rows=[["x"]], data={"blob": "z" * MAX_DATA_BYTES})


def test_sizes_are_measured_like_the_core_does():
    assert json_size({"a": "é"}) == len('{"a":"é"}'.encode())


def test_plugin_report_uses_the_plugin_id():
    from vumlo_plugins import Plugin
    report = Plugin("my-plugin").report("x", links=[("Get", "/export")])
    assert report.data["display"]["links"][0]["path"] == "/api/v1/plugins/my-plugin/export"
