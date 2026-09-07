export function normalizeImport(payload) {
  if (!payload || !Array.isArray(payload.tracks) || payload.tracks.length > 10000) throw new Error('Expected up to 10,000 library tracks');
  const keys = new Set();
  return payload.tracks.map(row => {
    const key = String(row.track_key || '');
    if (!key || key.length > 300 || keys.has(key) || !String(row.title || '').trim()) throw new Error('Invalid or duplicate library track');
    keys.add(key);
    const count = value => Math.max(0, Math.min(10000000, Number(value) || 0));
    return {
      track_key: key, title: String(row.title).slice(0, 500), artist: String(row.artist || 'Unknown artist').slice(0, 500),
      album: String(row.album || '').slice(0, 500), video_id: /^[\w-]{11}$/.test(row.video_id || '') ? row.video_id : null,
      url: String(row.url || '').slice(0, 1000), play_count: count(row.play_count),
      liked_count: count(row.provider_liked_count ?? (count(row.liked_count) - (row.local_favorite ? 1 : 0))),
      like_events: count(row.like_events), latest_played_at: row.latest_played_at || null,
      local_favorite: Boolean(row.local_favorite),
      local_favorite_updated_at: Number.isFinite(Date.parse(row.local_favorite_updated_at)) ? new Date(row.local_favorite_updated_at).toISOString() : null,
    };
  });
}

export function rankTracks(rows, favorites, limit = 20, now = Date.now()) {
  const maxPlays = Math.max(1, ...rows.map(r => r.play_count));
  const affinity = new Map();
  for (const row of rows) {
    const liked = favorites.has(row.track_key) || row.liked_count > 0 || row.like_events > 0;
    affinity.set(row.artist.toLowerCase(), (affinity.get(row.artist.toLowerCase()) || 0) + row.play_count + (liked ? maxPlays : 0));
  }
  const maxAffinity = Math.max(1, ...affinity.values());
  return rows.map(row => {
    const local = favorites.has(row.track_key);
    const liked = local || row.liked_count > 0 || row.like_events > 0;
    const frequency = Math.min(1, Math.log1p(row.play_count) / Math.max(1, Math.log1p(maxPlays)));
    const played = Date.parse(row.latest_played_at);
    const recent = Number.isFinite(played) ? Math.exp(-Math.max(0, now - played) / 86400000 / 45) : 0;
    const score = .48 * frequency + .27 * Number(liked) + .15 * recent + .10 * (affinity.get(row.artist.toLowerCase()) || 0) / maxAffinity;
    const reasons = [row.play_count > 1 ? `listened ${row.play_count} times` : 'appears in your listening history'];
    if (liked) reasons.push(local ? 'saved as a dashboard favorite' : 'matches your liked-music signal');
    if (recent > .4) reasons.push('fits your recent listening pattern');
    reasons.push(`artist affinity: ${row.artist}`);
    return { track: row, score, confidence: Math.min(.99, .35 + .30 * frequency + .25 * Number(liked) + .10 * recent), reasons, source: 'history' };
  }).sort((a,b) => b.score - a.score || a.track.artist.localeCompare(b.track.artist) || a.track.title.localeCompare(b.track.title)).slice(0, limit).map((row,i) => ({...row, rank:i+1}));
}
