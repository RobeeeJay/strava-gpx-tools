from geepeex import mapview
from geepeex.coverage import OPPOSITE, SAME, UNRIDDEN

RUNS = [(SAME, [(51.5, -0.1), (51.5, -0.09)]), (UNRIDDEN, [(51.5, -0.09), (51.5, -0.08)])]
STATS = {SAME: 700.0, OPPOSITE: 0.0, UNRIDDEN: 300.0}
TRAINING = [[(51.6, -0.1), (51.6, -0.09)]]


def render(tmp_path, training=TRAINING):
    path = tmp_path / "out.html"
    mapview.render(str(path), RUNS, STATS, title="demo", training=training)
    return path.read_text()


def test_training_tracks_sit_in_a_pane_below_the_route(tmp_path):
    # Ordering has to survive the layer control: toggling the training layer
    # re-adds it, which would lift grey over the colouring if z came from the
    # order layers were added in.
    assert mapview.TRAINING_Z < mapview.ROUTE_Z
    html = render(tmp_path)
    assert f"createPane('trainingPane').style.zIndex = {mapview.TRAINING_Z}" in html
    assert f"createPane('routePane').style.zIndex = {mapview.ROUTE_Z}" in html


def test_each_layer_is_drawn_into_its_own_pane(tmp_path):
    html = render(tmp_path)
    assert "pane: 'trainingPane'" in html
    assert "pane: 'routePane'" in html


def test_training_tracks_are_shown_by_default(tmp_path):
    assert "trainingLayer.addTo(map);" in render(tmp_path)


def test_training_tracks_are_grey_and_thinner_than_the_route(tmp_path):
    html = render(tmp_path)
    assert mapview.TRAINING_COLOUR in html
    assert "weight: 2" in html and "weight: 5" in html


def test_the_legend_explains_the_grey(tmp_path):
    assert "All training tracks" in render(tmp_path)


def test_a_page_without_training_has_no_grey_layer_or_legend_row(tmp_path):
    html = render(tmp_path, training=())
    assert "All training tracks" not in html
    assert "const training = [];" in html


def test_distances_and_percentages_come_from_the_stats(tmp_path):
    html = render(tmp_path)
    assert "0.7 km" in html and "70%" in html
    assert "0.3 km" in html and "30%" in html
