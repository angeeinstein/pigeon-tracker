"""ByteTrack behaviour.

The state machine's "this bird has been here for N seconds" rule is only as
good as track id stability, so that is what these tests check.
"""

from __future__ import annotations

from app.services.settings_schema import TrackerSettings
from app.vision.tracker import ByteTracker, iou_matrix, linear_assignment
from tests.conftest import make_detection


def tracker(**kwargs) -> ByteTracker:
    defaults = {"min_hits": 1, "track_buffer": 30}
    return ByteTracker(TrackerSettings(**{**defaults, **kwargs}))


class TestAssociation:
    def test_iou_of_identical_boxes_is_one(self) -> None:
        boxes = [[0, 0, 10, 10]]
        assert iou_matrix(boxes, boxes)[0][0] == 1.0

    def test_disjoint_boxes_have_zero_iou(self) -> None:
        assert iou_matrix([[0, 0, 10, 10]], [[20, 20, 30, 30]])[0][0] == 0.0

    def test_empty_input_is_handled(self) -> None:
        assert iou_matrix([], [[0, 0, 1, 1]]).shape == (0, 1)

    def test_assignment_respects_the_threshold(self) -> None:
        import numpy as np

        cost = np.array([[0.1, 0.9], [0.9, 0.2]])
        matches, unmatched_rows, unmatched_cols = linear_assignment(cost, 0.5)
        assert sorted(matches) == [(0, 0), (1, 1)]
        assert unmatched_rows == [] and unmatched_cols == []

        matches, unmatched_rows, _ = linear_assignment(cost, 0.15)
        assert matches == [(0, 0)]
        assert unmatched_rows == [1]


class TestTracking:
    def test_creates_a_track(self) -> None:
        tracks = tracker().update([make_detection()], now=0.0)
        assert len(tracks) == 1
        assert tracks[0].track_id == 1

    def test_keeps_the_id_across_frames(self) -> None:
        t = tracker()
        ids = []
        for step in range(10):
            offset = step * 3.0
            tracks = t.update(
                [make_detection(100 + offset, 100, 140 + offset, 140)], now=step * 0.1
            )
            ids.append(tracks[0].track_id)
        assert len(set(ids)) == 1

    def test_two_objects_get_distinct_ids(self) -> None:
        tracks = tracker().update(
            [make_detection(0, 0, 40, 40), make_detection(300, 300, 340, 340)], now=0.0
        )
        assert {track.track_id for track in tracks} == {1, 2}

    def test_low_confidence_detection_keeps_a_track_alive(self) -> None:
        """ByteTrack's second association pass: a bird that turns side-on and
        drops in confidence must not become a new object."""
        t = tracker(track_thresh=0.6, low_thresh=0.1)
        first = t.update([make_detection(confidence=0.9)], now=0.0)
        original_id = first[0].track_id

        for step in range(3):
            tracks = t.update(
                [make_detection(100 + step, 100, 140 + step, 140, confidence=0.3)],
                now=0.1 * (step + 1),
            )
            assert tracks and tracks[0].track_id == original_id

    def test_track_is_dropped_after_the_buffer_expires(self) -> None:
        t = tracker(track_buffer=3)
        t.update([make_detection()], now=0.0)
        for step in range(6):
            t.update([], now=0.1 * (step + 1))
        tracks = t.update([make_detection()], now=1.0)
        assert tracks[0].track_id != 1

    def test_min_hits_gates_confirmation(self) -> None:
        t = tracker(min_hits=3)
        tracks = t.update([make_detection()], now=0.0)
        assert tracks[0].confirmed is False
        for step in range(3):
            tracks = t.update([make_detection()], now=0.1 * (step + 1))
        assert tracks[0].confirmed is True

    def test_classes_are_never_mixed(self) -> None:
        t = tracker()
        t.update([make_detection(class_name="bird")], now=0.0)
        tracks = t.update([make_detection(class_name="cat")], now=0.1)
        # Same position, different class: this must be a new object.
        assert all(track.class_name == "cat" for track in tracks)
        assert tracks[0].track_id != 1

    def test_reset_clears_state(self) -> None:
        t = tracker()
        t.update([make_detection()], now=0.0)
        t.reset()
        tracks = t.update([make_detection()], now=1.0)
        assert tracks[0].track_id == 1


class TestTrackHelpers:
    def test_aim_point_respects_the_ratios(self) -> None:
        track = tracker().update([make_detection(0, 0, 100, 200)], now=0.0)[0]
        assert track.aim_point(0.5, 0.5) == (50.0, 100.0)
        assert track.aim_point(0.0, 1.0) == (0.0, 200.0)

    def test_duration(self) -> None:
        track = tracker().update([make_detection()], now=100.0)[0]
        assert track.duration_s(now=105.0) == 5.0
