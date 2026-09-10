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


def test_unheard_mix_keeps_candidates_from_rotated_listening_seed():
    import time

    seeds = [
        {
            "track_key": f"video:{index:011d}",
            "title": f"Top seed {index}",
            "artist": "Top artist",
            "video_id": f"{index:011d}",
            "play_count": 10,
            "liked_count": 0,
        }
        for index in range(10)
    ]
    rotated = {
        "track_key": "video:99999999999",
        "title": "Rotated seed",
        "artist": "Rotated artist",
        "video_id": "99999999999",
        "play_count": 1,
        "liked_count": 0,
    }
    candidate = {
        "track_key": "video:88888888888",
        "title": "Rotated discovery",
        "artist": "Discovery artist",
        "video_id": "88888888888",
        "play_count": 0,
        "liked_count": 0,
        "source": "favorite_discovery",
        "discovery_seeds": [{
            "track_key": rotated["track_key"],
            "title": rotated["title"],
            "seed_kind": "most_listened",
            "play_count": rotated["play_count"],
            "liked": False,
            "expires_at": time.time() + 3600,
        }],
    }
    result = RecommendationEngine().recommend(
        [*seeds, rotated, candidate],
        limit=5,
        exclude_keys=set(),
        only_unheard=True,
    )
    assert [item.track.track_key for item in result] == [candidate["track_key"]]


def test_unheard_mix_interleaves_distinct_taste_seeds():
    import time

    expiry = time.time() + 3600
    seeds = [
        {
            "track_key": f"video:{index:011d}",
            "title": f"Taste root {index}",
            "artist": f"Root artist {index}",
            "video_id": f"{index:011d}",
            "play_count": 10,
            "liked_count": 0,
        }
        for index in range(6)
    ]
    candidates = []
    for seed_index, seed in enumerate(seeds):
        for candidate_index in range(4):
            video_id = f"{seed_index:02d}{candidate_index:09d}"
            candidates.append(
                {
                    "track_key": f"candidate:{video_id}",
                    "title": f"Candidate {seed_index}-{candidate_index}",
                    "artist": f"Discovery artist {seed_index}",
                    "video_id": video_id,
                    "play_count": 0,
                    "liked_count": 0,
                    "source": "favorite_discovery",
                    "discovery_seeds": [
                        {
                            "track_key": seed["track_key"],
                            "title": seed["title"],
                            "seed_kind": "most_listened",
                            "play_count": seed["play_count"],
                            "liked": False,
                            "expires_at": expiry,
                        }
                    ],
                }
            )

    result = RecommendationEngine().recommend(
        [*seeds, *candidates], limit=12, exclude_keys=set(), only_unheard=True
    )
    root_counts = {
        seed["title"]: sum(seed["title"] in " ".join(item.reasons) for item in result)
        for seed in seeds
    }
    assert len(result) == 12
    assert root_counts == {seed["title"]: 2 for seed in seeds}
    assert result[0].reasons[0].endswith("Taste root 0 often")


def test_unheard_mix_includes_rotated_roots_outside_scoring_frontier():
    import time

    expiry = time.time() + 3600
    seeds = [
        {
            "track_key": f"video:{index:011d}",
            "title": f"Wide root {index}",
            "artist": f"Root artist {index}",
            "video_id": f"{index:02d}{'a' * 9}",
            "play_count": 10,
            "liked_count": 0,
        }
        for index in range(14)
    ]
    candidates = []
    for seed_index, seed in enumerate(seeds[12:]):
        for candidate_index in range(4):
            video_id = f"{seed_index + 20:02d}{candidate_index:09d}"
            candidates.append(
                {
                    "track_key": f"wide-candidate:{seed_index}:{candidate_index}",
                    "title": f"Wide candidate {seed_index}-{candidate_index}",
                    "artist": f"Wide artist {seed_index}",
                    "video_id": video_id,
                    "play_count": 0,
                    "liked_count": 0,
                    "source": "favorite_discovery",
                    "discovery_seeds": [
                        {
                            "track_key": seed["track_key"],
                            "title": seed["title"],
                            "seed_kind": "most_listened",
                            "play_count": 10,
                            "liked": False,
                            "expires_at": expiry,
                        }
                    ],
                }
            )

    result = RecommendationEngine().recommend(
        [*seeds, *candidates], limit=8, exclude_keys=set(), only_unheard=True
    )
    assert len(result) == 8
    assert all("Wide root" in item.reasons[0] for item in result[:4])
    assert len({item.reasons[0] for item in result[:4]}) == 2
