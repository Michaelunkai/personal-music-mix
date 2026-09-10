export function normalizeImport(payload) {
  if (!payload || !Array.isArray(payload.tracks) || payload.tracks.length > 10000) throw new Error('Expected up to 10,000 library tracks');
  const keys = new Set();
  return payload.tracks.map(row => {
    const key = String(row.track_key || '');
    if (!key || key.length > 300 || keys.has(key) || !String(row.title || '').trim()) throw new Error('Invalid or duplicate library track');
    keys.add(key);
    const count = value => Math.max(0, Math.min(10000000, Number(value) || 0));
    const position = value => Number.isFinite(Number(value)) && Number(value) >= 0 ? Math.min(500000, Math.floor(Number(value))) : null;
    return {
      track_key: key, title: String(row.title).slice(0, 500), artist: String(row.artist || 'Unknown artist').slice(0, 500),
      album: String(row.album || '').slice(0, 500), video_id: /^[\w-]{11}$/.test(row.video_id || '') ? row.video_id : null,
      url: String(row.url || '').slice(0, 1000), play_count: count(row.play_count),
      liked_count: count(row.provider_liked_count ?? (count(row.liked_count) - (row.local_favorite ? 1 : 0))),
      like_events: count(row.like_events), latest_played_at: row.latest_played_at || null, history_position: position(row.history_position),
      local_favorite: Boolean(row.local_favorite),
      local_favorite_updated_at: Number.isFinite(Date.parse(row.local_favorite_updated_at)) ? new Date(row.local_favorite_updated_at).toISOString() : null,
      source: row.source === 'favorite_discovery' || row.source === 'related' ? row.source : 'history',
      discovery_seeds: (Array.isArray(row.discovery_seeds) ? row.discovery_seeds : []).slice(0,30)
        .filter(seed => typeof seed?.track_key === 'string' && seed.track_key.length <= 300 && Number.isFinite(seed.expires_at))
        .map(seed => ({track_key:seed.track_key, title:String(seed.title || 'a song you enjoy').slice(0,500), expires_at:seed.expires_at,
          seed_kind:seed.seed_kind === 'most_listened' ? 'most_listened' : 'favorite',
          play_count:count(seed.play_count), liked:Boolean(seed.liked), history_position:position(seed.history_position)})),
    };
  });
}

export function rankTracks(rows, favorites, limit = 20, now = Date.now(), options = {}) {
  const unheardOnly = Boolean(options?.unheardOnly);
  const excluded = new Set(options?.excludeKeys || []);
  const excludedVideos = new Set(options?.excludeVideoIds || []);
  if (unheardOnly) return rankUnheard(rows, favorites, limit, excluded, excludedVideos);
  const active = new Set(rows.filter(row => favorites.has(row.track_key) || row.liked_count > 0 || row.like_events > 0).map(row=>row.track_key));
  const seedsFor = row => (row.discovery_seeds || []).filter(seed=>active.has(seed.track_key) && seed.expires_at * 1000 > now);
  rows = rows.filter(row => row.source !== 'favorite_discovery' || row.play_count > 0 || active.has(row.track_key) || seedsFor(row).length);
  const maxPlays = Math.max(1, ...rows.map(r => r.play_count));
  const affinity = new Map();
  for (const row of rows) {
    const liked = favorites.has(row.track_key) || row.liked_count > 0 || row.like_events > 0;
    affinity.set(row.artist.toLowerCase(), (affinity.get(row.artist.toLowerCase()) || 0) + row.play_count + (liked ? maxPlays : 0));
  }
  const maxAffinity = Math.max(1, ...affinity.values());
  const ranked = rows.map(row => {
    const local = favorites.has(row.track_key);
    const liked = local || row.liked_count > 0 || row.like_events > 0;
    const frequency = Math.min(1, Math.log1p(row.play_count) / Math.max(1, Math.log1p(maxPlays)));
    const played = Date.parse(row.latest_played_at);
    const recent = Number.isFinite(played) ? Math.exp(-Math.max(0, now - played) / 86400000 / 45) : 0;
    const score = .48 * frequency + .27 * Number(liked) + .15 * recent + .10 * (affinity.get(row.artist.toLowerCase()) || 0) / maxAffinity;
    const discovery = row.source === 'favorite_discovery' && row.play_count === 0 && !liked;
    const reasons = row.play_count > 0 ? [row.play_count > 1 ? `listened ${row.play_count} times` : 'appears in your listening history'] : [];
    if(discovery) reasons.push(`recommended from your favorite: ${seedsFor(row)[0].title}`, 'new song discovery');
    if (liked) reasons.push(local ? 'saved as a dashboard favorite' : 'matches your liked-music signal');
    if (recent > .4) reasons.push('fits your recent listening pattern');
    reasons.push(`artist affinity: ${row.artist}`);
    return { track: row, score:discovery ? .45 + .10 * (affinity.get(row.artist.toLowerCase()) || 0) / maxAffinity : score, confidence: Math.min(.99, .35 + .30 * frequency + .25 * Number(liked) + .10 * recent), reasons, source: discovery ? 'favorite_discovery' : 'history' };
  }).sort((a,b) => b.score - a.score || a.track.artist.localeCompare(b.track.artist) || a.track.title.localeCompare(b.track.title));
  const discoveries = ranked.filter(row=>row.source === 'favorite_discovery').slice(0,Math.ceil(limit*.3));
  const reserved = new Set(discoveries.map(row=>row.track.track_key));
  const anchors = ranked.filter(row=>!reserved.has(row.track.track_key)).slice(0,limit-discoveries.length);
  // Reserve room for new songs even when frequently played favorites score higher.
  return [...anchors.slice(0,3),...discoveries,...anchors.slice(3)].map((row,i)=>({...row,rank:i+1}));
}

function liked(row, favorites) {
  return favorites.has(row.track_key) || Number(row.liked_count) > 0 || Number(row.like_events) > 0 || Boolean(row.local_favorite);
}
function hasPlayedEvidence(value) {
  return value !== null && value !== undefined && String(value).trim() !== '';
}

function playable(row) { return /^[A-Za-z0-9_-]{11}$/.test(String(row.video_id || '')); }

function historyPosition(row) {
  const value = Number(row?.history_position);
  return Number.isFinite(value) && value >= 0 ? value : Number.POSITIVE_INFINITY;
}

function compareHistoryPosition(a, b) {
  const left = historyPosition(a), right = historyPosition(b);
  if (left === right || (!Number.isFinite(left) && !Number.isFinite(right))) return 0;
  return left - right;
}

function historyOrderScore(value) {
  return Number.isFinite(value) ? Math.exp(-value / 50) : 0;
}

function rankUnheard(rows, favorites, limit, excluded, excludedVideos) {
  const byKey = new Map(rows.map(row => [row.track_key, row]));
  const seedRows = rows.filter(row => (Number(row.play_count) > 0 || liked(row, favorites)) && playable(row));
  const favoriteSeeds = seedRows.filter(row => liked(row, favorites))
    .sort((a,b) => Number(b.liked_count || 0) - Number(a.liked_count || 0)
      || Number(b.like_events || 0) - Number(a.like_events || 0)
      || Number(b.play_count || 0) - Number(a.play_count || 0)
      || compareHistoryPosition(a, b)
      || String(a.track_key).localeCompare(String(b.track_key)));
  const listenedSeeds = seedRows.filter(row => !liked(row, favorites))
    .sort((a,b) => Number(b.play_count || 0) - Number(a.play_count || 0)
      || Number(b.liked_count || 0) - Number(a.liked_count || 0)
      || compareHistoryPosition(a, b)
      || String(a.track_key).localeCompare(String(b.track_key)));
  const allSeeds = [...favoriteSeeds, ...listenedSeeds];
  // Keep a balanced scoring frontier: explicit favorites get up to six
  // slots, and the remaining slots come from the strongest listened songs.
  // Every current playable seed remains active so rotated cached discovery
  // cannot disappear, but the output must cover this frontier instead of
  // letting one high-play song consume the whole mix.
  const favoriteFrontier = favoriteSeeds.slice(0, 6);
  const seeds = [...favoriteFrontier, ...listenedSeeds.slice(0, Math.max(0, 12 - favoriteFrontier.length))];
  const scoringSeedKeys = new Set(seeds.map(row => row.track_key));
  const active = new Set(allSeeds.map(row => row.track_key));
  const maxPlays = Math.max(1, ...seeds.map(row => Number(row.play_count || 0)));
  const artistWeights = new Map();
  for (const seed of seeds) {
    const artist = String(seed.artist || 'Unknown artist').toLowerCase();
    artistWeights.set(artist, Math.max(artistWeights.get(artist) || 0, Number(seed.play_count || 0)));
  }
  // A previously discovered song remains a valid backfill candidate while it
  // is still unheard. Expiry controls when the companion refreshes provider
  // data; it must not make an existing unseen song disappear from the mix.
  const seedFor = row => (Array.isArray(row.discovery_seeds) ? row.discovery_seeds : [])
    .filter(seed => active.has(seed.track_key))
    .map(seed => ({...seed, source:byKey.get(seed.track_key)}))
    .map(seed => ({...seed,
      title:String(seed.title || seed.source?.title || 'a song you enjoy'),
      play_count:Number(seed.play_count ?? seed.source?.play_count ?? 0),
      liked:Boolean(seed.liked ?? liked(seed.source || {}, favorites)),
      seed_kind:seed.seed_kind === 'most_listened' ? 'most_listened' : (seed.seed_kind || (seed.liked ? 'favorite' : 'most_listened')),
      history_position:Number.isFinite(Number(seed.history_position)) ? Number(seed.history_position) : historyPosition(seed.source),
    }))
    .sort((a,b) => Number(scoringSeedKeys.has(a.track_key)) - Number(scoringSeedKeys.has(b.track_key))
      || Number(b.liked) - Number(a.liked)
      || Number(b.play_count || 0) - Number(a.play_count || 0)
      || compareHistoryPosition(a, b)
      || String(a.track_key).localeCompare(String(b.track_key)));
  const scored = new Map();
  const candidateSeedKeys = new Map();
  for (const row of rows) {
    const plays = Number(row.play_count || 0);
    const isLiked = liked(row, favorites);
    if (excluded.has(row.track_key) || excludedVideos.has(row.video_id) || plays > 0 || hasPlayedEvidence(row.latest_played_at) || isLiked || row.source !== 'favorite_discovery' || !playable(row)) continue;
    const seedsForRow = seedFor(row);
    if (!seedsForRow.length) continue;
    const seed = seedsForRow[0];
    candidateSeedKeys.set(row.track_key, seedsForRow
      .map(value => value.track_key)
      .filter(key => scoringSeedKeys.has(key)));
    const frequency = Math.min(1, Math.log1p(seed.play_count) / Math.max(1, Math.log1p(maxPlays)));
    const artistAffinity = Math.min(1, (artistWeights.get(String(row.artist || 'Unknown artist').toLowerCase()) || 0) / maxPlays);
    const favoriteSeed = Boolean(seed.liked) || seed.seed_kind === 'favorite';
    const recency = historyOrderScore(historyPosition(seed));
    const score = Math.min(.99, .50 + .18 * frequency + .30 * Number(favoriteSeed) + .12 * artistAffinity + .10 * recency);
    let reason = favoriteSeed
      ? `recommended from your favorite: ${seed.title}`
      : `recommended because you listen to ${seed.title} often`;
    if (seedsForRow.length > 1) reason += `; also matches ${seedsForRow[1].title}`;
    scored.set(row.track_key, {track:row,score,confidence:Math.min(.98,.45+.25*frequency+.20*Number(favoriteSeed)+.10*artistAffinity+.05*recency),
      reasons:[reason,'new to your listening history',`artist affinity: ${row.artist || 'Unknown artist'}`],source:'favorite_discovery'});
  }
  // Direct related imports may be represented separately by callers. Accept
  // only unseen playable rows that have an active listening seed.
  const strongest = seeds[0];
  for (const row of rows) {
    if (row.source !== 'related' || excluded.has(row.track_key) || excludedVideos.has(row.video_id) || Number(row.play_count || 0) > 0 || hasPlayedEvidence(row.latest_played_at) || liked(row, favorites) || !playable(row) || scored.has(row.track_key) || !strongest) continue;
    const relatedSeeds = seedFor(row).filter(value => scoringSeedKeys.has(value.track_key));
    candidateSeedKeys.set(row.track_key, relatedSeeds.map(value => value.track_key));
    const seed = relatedSeeds[0] || strongest;
    let reason = Number(seed.play_count || 0) > 0
      ? `recommended because you listen to ${seed.title} often`
      : `recommended from your favorite: ${seed.title}`;
    if (relatedSeeds.length > 1) reason += `; also matches ${relatedSeeds[1].title}`;
    scored.set(row.track_key, {track:row,score:.44,confidence:.40,reasons:[reason,'new to your listening history',`artist affinity: ${row.artist || 'Unknown artist'}`],source:'related'});
  }
  const seenVideoIds = new Set();
  const order = (a,b) => b.score - a.score || b.confidence - a.confidence || String(a.track.artist || '').localeCompare(String(b.track.artist || '')) || String(a.track.title || '').localeCompare(String(b.track.title || ''));
  const ranked = [...scored.values()].sort(order);
  // Interleave distinct seed groups so the first page represents the user's
  // broader taste. A candidate matching multiple seeds is emitted once.
  const grouped = new Map(seeds.map(seed => [seed.track_key, []]));
  for (const item of ranked) {
    for (const seedKey of candidateSeedKeys.get(item.track.track_key) || []) {
      if (grouped.has(seedKey)) grouped.get(seedKey).push(item);
    }
  }
  for (const values of grouped.values()) values.sort(order);
  const ordered = [];
  const emitted = new Set();
  while (ordered.length < Math.max(1, Math.min(Number(limit) || 20, 200))) {
    let progress = false;
    for (const seed of seeds) {
      for (const item of grouped.get(seed.track_key) || []) {
        if (emitted.has(item.track.track_key)) continue;
        emitted.add(item.track.track_key);
        ordered.push(item);
        progress = true;
        break;
      }
      if (ordered.length >= Math.max(1, Math.min(Number(limit) || 20, 200))) break;
    }
    if (!progress) break;
  }
  ordered.push(...ranked.filter(item => !emitted.has(item.track.track_key)));
  return ordered
    .filter(item => {
      const videoId = item.track.video_id;
      if (seenVideoIds.has(videoId)) return false;
      seenVideoIds.add(videoId);
      return true;
    })
    .slice(0, Math.max(1, Math.min(Number(limit) || 20, 200)))
    .map((row,i) => ({...row,rank:i+1}));
}
