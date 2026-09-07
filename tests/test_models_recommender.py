from app.history import normalize_history_record
from app.models import LikeState
from app.recommender import RecommendationEngine


def test_track_normalization_is_stable_and_handles_provider_shapes():
    first = normalize_history_record({"title": "  Song  ", "artists": "Artist", "videoId": "abc"})
    second = normalize_history_record({"title": "Song", "artist": "Artist", "video_id": "abc"})
    assert first is not None and second is not None
    assert first.track_key == "video:abc"
    assert first.track_key == second.track_key


def test_event_ids_are_stable_for_the_same_history_position():
    one = normalize_history_record({"title": "Song", "artist": "Artist", "played_at": "2026-09-07T10:00:00Z"})
    two = normalize_history_record({"title": "Song", "artist": "Artist", "played_at": "2026-09-07T10:00:00Z"})
    assert one is not None and two is not None
    assert one.event_key == two.event_key


def test_recommendations_are_explainable_and_deterministic():
    rows = [
        {"track_key": "video:a", "title": "A", "artist": "Loved", "play_count": 10, "liked_count": 4, "latest_played_at": "2026-09-06T10:00:00+00:00"},
        {"track_key": "video:b", "title": "B", "artist": "New", "play_count": 1, "liked_count": 0, "latest_played_at": "2026-08-01T10:00:00+00:00"},
    ]
    engine = RecommendationEngine()
    first = engine.recommend(rows, limit=2)
    second = engine.recommend(rows, limit=2)
    assert [item.track.track_key for item in first] == [item.track.track_key for item in second]
    assert first[0].track.track_key == "video:a"
    assert first[0].reasons
    assert 0 <= first[0].confidence <= 1


def test_favorite_signal_is_not_diluted_by_repeat_listening():
    base = {"track_key": "video:a", "title": "A", "artist": "Artist", "play_count": 100, "liked_count": 0}
    engine = RecommendationEngine()
    plain = engine.recommend([base])[0]
    favorite = engine.recommend([{**base, "liked_count": 1, "local_favorite": True}])[0]
    assert round(favorite.score - plain.score, 2) == 0.27
    assert "saved as a dashboard favorite" in favorite.reasons


def test_zero_play_favorite_and_favorite_artist_affinity():
    engine=RecommendationEngine()
    seed={'track_key':'video:a','title':'Favorite','artist':'A','play_count':0,'liked_count':1,'local_favorite':True}
    assert engine.recommend([seed])[0].score > 0
    rows=[seed,{'track_key':'video:b','title':'Other A','artist':'A','play_count':1}, {'track_key':'video:c','title':'Other C','artist':'C','play_count':1}]
    scores={item.track.track_key:item.score for item in engine.recommend(rows)}
    assert scores['video:b'] > scores['video:c']
    assert engine.recommend([]) == []
