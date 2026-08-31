from geepeex import charts


def test_axis_ticks_cover_the_end_of_the_data():
    # A 39.99 km ride must still show a 40 label; without covering the top of
    # the range the last axis label silently disappears.
    assert charts.nice_ticks(0, 39.99, cover=True)[-1] == 40
    assert charts.nice_ticks(0, 40.0, cover=True)[-1] == 40


def test_axis_ticks_are_round_numbers():
    assert charts.nice_ticks(0, 97) == [0, 20, 40, 60, 80]
    assert charts.nice_ticks(0, 4.2) == [0, 1, 2, 3, 4]


def test_a_chart_reports_where_it_drew_each_point():
    # The crosshair reads these back, so they have to match the polyline.
    html, geom = charts.line_chart("speed", "Speed", "km/h", [0, 1, 2],
                                   [("Speed", [10, 20, 30], 0)], 2)
    ys = geom["speed.0"]
    assert len(ys) == 3
    assert ys[0] > ys[-1], "a rising series must be drawn upwards"
    assert f"{ys[1]}" in html


def test_a_single_series_gets_no_legend_but_two_series_do():
    one, _ = charts.line_chart("a", "A", "u", [0, 1], [("A", [1, 2], 0)], 1)
    two, _ = charts.line_chart("b", "B", "u", [0, 1],
                               [("B", [1, 2], 0), ("Trend", [1, 2], 1)], 1)
    assert "legend" not in one
    assert "legend" in two and "Trend" in two


def test_the_page_defines_a_dark_palette_for_both_the_toggle_and_the_os_setting():
    assert 'prefers-color-scheme: dark' in charts.PAGE
    assert ':root[data-theme="dark"]' in charts.PAGE
    assert ':root:not([data-theme="light"])' in charts.PAGE


def test_the_page_uses_the_validated_series_colours():
    for colour in charts.SERIES:
        assert colour in charts.PAGE
    for colour in charts.SERIES_DARK:
        assert colour in charts.PAGE


def test_the_axis_is_not_stretched_past_the_data_for_a_round_number():
    # A 42 km ride must not push the axis out to 50 and waste a fifth of the plot.
    html, geom = charts.line_chart("d", "D", "u", [0, 21, 42],
                                   [("D", [1, 2, 3], 0)], 42)
    assert ">50<" not in html
    assert ">40<" in html


def test_a_ride_ending_just_short_of_a_round_number_still_labels_it():
    html, _ = charts.line_chart("e", "E", "u", [0, 20, 39.99],
                                [("E", [1, 2, 3], 0)], 39.99)
    assert ">40<" in html
