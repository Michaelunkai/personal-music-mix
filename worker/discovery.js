import { normalizeProviderCandidate, recordingKeyFor } from './domain.js';

export const CATALOG_RESERVE_TARGET = 500;
export const CATALOG_REFILL_THRESHOLD = 200;
const MAX_SEEDS_PER_JOB = 2;
const AUDIUS_APP = 'personal-music-mix';
const CATALOG_LEASE_KEY = 'catalog_refill';
const CATALOG_CURSOR_KEY = 'catalog_seed_cursor';
const MB_USER_AGENT = 'PersonalMusicMix/1.0 (https://github.com/Michaelunkai/personal-music-mix)';
const nowIso = value => new Date(value).toISOString();
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

async function readState(db, key) {
  const row = await db.prepare('SELECT payload FROM music_state WHERE key=?').bind(key).first();
  if (!row) return null;
  try { return JSON.parse(row.payload); } catch { return null; }
}

const saveState = (db, key, value) => db.prepare(
  'INSERT INTO music_state(key,payload) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload',
).bind(key, JSON.stringify(value));

function feedbackFactor(events) {
  if (events.some(row => row.event === 'dislike')) return -Infinity;
  const likes = events.filter(row => row.event === 'like').length;
  const completes = events.filter(row => row.event === 'completed').length;
  const skips = events.filter(row => row.event === 'skipped').length;
  const seconds = events.reduce((sum, row) => sum + Number(row.listened_seconds || 0), 0);
  return likes * 8 + completes * 2 + Math.min(seconds / 120, 5) - skips * 2;
}

async function listTasteSeeds(db, nowMs) {
  const [{ results: tracks }, { results: favoriteRows }, { results: feedbackRows }, { results: providerTracks }, cursorState] = await Promise.all([
    db.prepare('SELECT payload FROM music_library').all(),
    db.prepare('SELECT track_key FROM music_favorites WHERE liked=1').all(),
    db.prepare("SELECT recording_key,event,listened_seconds FROM music_feedback WHERE julianday(created_at)>=julianday(?,'-180 days')").bind(nowIso(nowMs)).all(),
    db.prepare('SELECT candidate_key AS track_key,recording_key,title,artist,0 AS play_count,0 AS liked_count,0 AS like_events,NULL AS latest_played_at FROM music_candidates').all(),
    readState(db, CATALOG_CURSOR_KEY),
  ]);
  const favorites = new Set((favoriteRows || []).map(row => row.track_key));
  const feedbackByRecording = new Map();
  for (const row of feedbackRows || []) {
    const records = feedbackByRecording.get(row.recording_key) || [];
    records.push(row);
    feedbackByRecording.set(row.recording_key, records);
  }
  const seen = new Set();
  const seeds = [];
  const allTracks=[...(tracks||[]).map(value=>{try{return JSON.parse(value.payload)}catch{return null}}),...(providerTracks||[])];
  for (const row of allTracks) {
    if(!row) continue;
    const trackKey = String(row.track_key || '');
    const title = String(row.title || '').trim();
    const artist = String(row.artist || '').trim();
    const recordingKey = row.recording_key || recordingKeyFor(title, artist);
    if (!trackKey || !title || !artist || !recordingKey || seen.has(recordingKey)) continue;
    const events = feedbackByRecording.get(recordingKey) || [];
    const liked = favorites.has(trackKey) || Boolean(row.local_favorite) || Number(row.liked_count) > 0 || Number(row.like_events) > 0 || events.some(item => item.event === 'like');
    const plays = Math.max(0, Number(row.play_count) || 0);
    const feedback = feedbackFactor(events);
    if (!liked && plays <= 0 && feedback <= 0) continue;
    const playedAt = Date.parse(row.latest_played_at || '');
    const recent = Number.isFinite(playedAt) ? Math.exp(-Math.max(0, nowMs - playedAt) / 86400000 / 45) : 0.25;
    const historyPosition=row.history_position!==null&&row.history_position!==undefined&&String(row.history_position).trim()!==''&&Number.isFinite(Number(row.history_position))?Number(row.history_position):Number.POSITIVE_INFINITY;
    seeds.push({ track_key:trackKey, recording_key:recordingKey, title:title.slice(0, 240), artist:artist.slice(0, 240),
      liked, play_count:plays, score:(liked ? 7 : 0) + Math.log1p(plays) * 2 + Math.max(-6, feedback) + recent, history_position:historyPosition });
    seen.add(recordingKey);
  }
  seeds.sort((a,b) => b.score - a.score || b.history_position - a.history_position || a.track_key.localeCompare(b.track_key));
  const signature = seeds.map(row => row.track_key).join('|').slice(0, 6000);
  if (cursorState?.seed_signature !== signature) return {seeds, cursor:0, signature};
  return {seeds, cursor:Math.max(0, Number(cursorState.cursor) || 0) % Math.max(1, seeds.length), signature};
}

async function fetchJson(fetchImpl, url, {timeoutMs=3500,userAgent}={}) {
  try {
    const response = await fetchImpl(url, {headers:{Accept:'application/json', ...(userAgent ? {'User-Agent':userAgent} : {})}, signal:AbortSignal.timeout(timeoutMs)});
    if (!response.ok) return {ok:false,status:response.status, data:null};
    return {ok:true,status:response.status,data:await response.json()};
  } catch { return {ok:false,status:0,data:null}; }
}

async function findMusicBrainzArtist(fetchImpl, artist) {
  const url = new URL('https://musicbrainz.org/ws/2/artist/');
  url.search = new URLSearchParams({query:`artist:"${String(artist).replace(/["\\]/g,' ').slice(0,120)}"`,fmt:'json',limit:'3',inc:'genres+tags'}).toString();
  const result = await fetchJson(fetchImpl,url.toString(),{timeoutMs:3000,userAgent:MB_USER_AGENT});
  const artists = Array.isArray(result.data?.artists) ? result.data.artists : [];
  const wanted = String(artist).toLocaleLowerCase('en-US');
  const match = artists.find(row => String(row.name || '').toLocaleLowerCase('en-US') === wanted);
  if (!match || !/^[0-9a-f-]{36}$/i.test(match.id || '')) return null;
  const tags = [...(Array.isArray(match.genres) ? match.genres : []), ...(Array.isArray(match.tags) ? match.tags : [])]
    .map(row=>typeof row==='string'?row:String(row?.name||''))
    .filter(Boolean);
  return {...match,genres:[...new Set(tags)].slice(0,12)};
}

async function relatedArtistsFromListenBrainz(fetchImpl, artistMbid) {
  const url = `https://api.listenbrainz.org/1/lb-radio/artist/${encodeURIComponent(artistMbid)}?mode=medium&max_similar_artists=5&max_recordings_per_artist=4&pop_begin=10&pop_end=95`;
  const result = await fetchJson(fetchImpl,url,{timeoutMs:3500,userAgent:MB_USER_AGENT});
  if (!result.ok) return [];
  const payload = result.data?.payload || result.data || {};
  const map = payload.artist_map || result.data?.artist_map || {};
  const out = [];
  const seedName=String(payload.artist_name || '').toLocaleLowerCase('en-US');
  const seen=new Set();
  const visit=(value,depth=0)=>{
    if(!value || typeof value!=='object' || depth>6 || out.length>=3) return;
    const rows=Array.isArray(value)?value:Object.values(value);
    for(const row of rows) {
      if(!row || typeof row!=='object') continue;
      const name=String(row.similar_artist_name || row.artist_name || row.name || row.artist || '').trim();
      const id=String(row.similar_artist_mbid || row.artist_mbid || row.artist_id || row.id || '');
      const folded=name.toLocaleLowerCase('en-US');
      if(name && folded!==seedName && !seen.has(folded)) { seen.add(folded); out.push({name:name.slice(0,160),mbid:/^[0-9a-f-]{36}$/i.test(id)?id:null}); }
      if(out.length<3) visit(row,depth+1);
      if(out.length>=3) break;
    }
  };
  visit(map);
  if(out.length<3) visit(payload.recordings || payload.data);
  return out;
}

async function searchAudius(fetchImpl, query, seedKey, musicBrainzGenres=[], relationship='direct') {
  const url = new URL('https://api.audius.co/v1/tracks/search');
  url.search = new URLSearchParams({query,app_name:AUDIUS_APP,limit:'50'}).toString();
  const result = await fetchJson(fetchImpl,url.toString(),{timeoutMs:4000});
  const rows = Array.isArray(result.data?.data) ? result.data.data : [];
  return rows.map(row=>{
    const item=normalizeProviderCandidate({ ...row, provider:'audius', seed_keys:[seedKey] });
    if(item && musicBrainzGenres.length) item.genre=[...new Set([item.genre,...musicBrainzGenres].filter(Boolean))].join(' · ').slice(0,160);
    return item ? {...item,origin_seed_key:seedKey,origin_relationship:relationship} : null;
  }).filter(Boolean);
}

async function excludedRecordings(db) {
  const [{ results: tracks }, { results: served }, { results: disliked }] = await Promise.all([
    db.prepare('SELECT payload FROM music_library').all(),
    db.prepare('SELECT track_key,recording_key FROM music_served').all(),
    db.prepare("SELECT recording_key FROM music_feedback WHERE event='dislike'").all(),
  ]);
  const keys = new Set((disliked || []).map(row=>row.recording_key));
  const libraryByKey = new Map();
  for (const {payload} of tracks || []) {
    try {
      const row=JSON.parse(payload);
      libraryByKey.set(row.track_key, row.recording_key || recordingKeyFor(row.title,row.artist));
      // Never recommend a title that already exists in the saved listening library.
      const key=row.recording_key || recordingKeyFor(row.title,row.artist);
      if (key) keys.add(key);
    } catch {}
  }
  for (const row of served || []) {
    if (row.recording_key) keys.add(row.recording_key);
    else if (libraryByKey.get(row.track_key)) keys.add(libraryByKey.get(row.track_key));
  }
  return keys;
}

async function writeCandidates(db, candidates, excluded, nowMs) {
  const byRecording = new Map();
  for (const item of candidates) {
    if (excluded.has(item.recording_key)) continue;
    const previous = byRecording.get(item.recording_key);
    const origins=[...(previous?.origins || [])];
    const origin={seed_track_key:item.origin_seed_key,relationship:item.origin_relationship === 'similar_artist' ? 'similar_artist' : 'direct'};
    if (origin.seed_track_key && !origins.some(value=>value.seed_track_key===origin.seed_track_key && value.relationship===origin.relationship)) origins.push(origin);
    byRecording.set(item.recording_key,{...previous,...item,
      seed_keys:[...new Set([...(previous?.seed_keys || []),...item.seed_keys])].slice(0,8),
      origins:origins.slice(0,16)});
  }
  const items=[...byRecording.values()].slice(0,200);
  if (!items.length) return 0;
  const stamp=nowIso(nowMs);
  for (let i=0;i<items.length;i+=80) {
    const chunk=items.slice(i,i+80);
    const placeholders=chunk.map(()=>'?').join(',');
    const existing=await db.prepare(`SELECT candidate_key,recording_key FROM music_candidates WHERE recording_key IN (${placeholders})`)
      .bind(...chunk.map(item=>item.recording_key)).all();
    const keyByRecording=new Map((existing.results || []).map(row=>[row.recording_key,row.candidate_key]));
    for(const item of chunk) item.candidate_key=keyByRecording.get(item.recording_key) || item.candidate_key;
    const statements=chunk.map(item=>db.prepare(`
      INSERT INTO music_candidates(candidate_key,recording_key,provider,provider_track_id,title,artist,album,genre,mood,duration_seconds,audio_url,provider_url,artwork_url,seed_keys,discovered_at,last_verified_at)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
      ON CONFLICT(recording_key) DO UPDATE SET
        seed_keys=excluded.seed_keys,
        discovered_at=excluded.discovered_at,
        last_verified_at=CASE WHEN excluded.last_verified_at IS NOT NULL THEN excluded.last_verified_at ELSE music_candidates.last_verified_at END
    `).bind(item.candidate_key,item.recording_key,item.provider,item.provider_track_id,item.title,item.artist,item.album,item.genre,item.mood,item.duration_seconds,item.audio_url,item.provider_url,item.artwork_url,JSON.stringify(item.seed_keys),stamp,item.last_verified_at || null));
    await db.batch(statements);
  }
  const origins=[...new Map(items.flatMap(item=>item.origins.map(origin=>[
    `${item.candidate_key}\u0000${origin.seed_track_key}\u0000${origin.relationship}`,
    {candidate_key:item.candidate_key,...origin},
  ]))).values()];
  for (let i=0;i<origins.length;i+=80) {
    await db.batch(origins.slice(i,i+80).map(origin=>db.prepare(`
      INSERT OR IGNORE INTO music_candidate_origins(candidate_key,seed_track_key,relationship) VALUES(?,?,?)
    `).bind(origin.candidate_key,origin.seed_track_key,origin.relationship)));
  }
  return items.length;
}

export async function catalogStatus(db, nowMs=Date.now()) {
  const [count, control] = await Promise.all([
    db.prepare('SELECT COUNT(*) AS count FROM music_candidates c WHERE NOT EXISTS(SELECT 1 FROM music_served s WHERE s.recording_key=c.recording_key)').first(),
    readState(db,CATALOG_LEASE_KEY),
  ]);
  return {available:Math.max(0,Number(count?.count)||0),target:CATALOG_RESERVE_TARGET,refill_threshold:CATALOG_REFILL_THRESHOLD,
    state:control?.state || 'idle', checked_at:control?.checked_at || null, retry_at:control?.retry_at || null,
    pending:Number(control?.lease_until_ms)>nowMs};
}

export async function refillCatalog(db,{fetchImpl=fetch,now=Date.now,wait=sleep,maxSeeds=MAX_SEEDS_PER_JOB}={}) {
  const started=now();
  const status=await catalogStatus(db,started);
  if(status.available>=CATALOG_REFILL_THRESHOLD) return {...status,started:false};
  const old=await readState(db,CATALOG_LEASE_KEY) || {};
  if(Number(old.lease_until_ms)>started) return {...status,started:false};
  if(Number(old.retry_after_ms)>started) return {...status,started:false};
  const ownerId=crypto.randomUUID();
  await db.prepare(`INSERT OR IGNORE INTO music_state(key,payload) VALUES(?,?)`).bind(CATALOG_LEASE_KEY,JSON.stringify({lease_until_ms:0})).run();
  const leased=await db.prepare(`UPDATE music_state SET payload=? WHERE key=? AND COALESCE(json_extract(payload,'$.lease_until_ms'),0)<=?`)
    .bind(JSON.stringify({...old,owner_id:ownerId,lease_until_ms:started+25000,state:'running',started_at:nowIso(started)}),CATALOG_LEASE_KEY,started).run();
  if(Number(leased?.meta?.changes||leased?.meta?.rows_written||0)===0) return {...status,started:false};
  let fetched=0, saved=0, rejected=0, requests=0, failure=false;
  try {
    const {seeds,cursor,signature}=await listTasteSeeds(db,started);
    if(!seeds.length) {
      const final={...old,owner_id:null,lease_until_ms:0,state:'needs_history',seed_count:0,candidate_count:status.available,checked_at:nowIso(now())};
      await saveState(db,CATALOG_LEASE_KEY,final).run();
      return {...status,...final,started:true};
    }
    const excluded=await excludedRecordings(db);
    const batch=[];
    const count=Math.max(1,Math.min(MAX_SEEDS_PER_JOB,Number(maxSeeds)||MAX_SEEDS_PER_JOB));
    const selected=Array.from({length:Math.min(count,seeds.length)},(_,i)=>seeds[(cursor+i)%seeds.length]);
    let lastMusicBrainzAt=0;
    for(const seed of selected) {
      const calls=[];
      const remaining=await catalogStatus(db,now());
      if(remaining.available+saved>=CATALOG_RESERVE_TARGET) break;
      let artistMbid=null, musicBrainzGenres=[];
      const mbCacheKey=`musicbrainz_artist:${seed.artist.toLocaleLowerCase('en-US')}`;
      const cached=await readState(db,mbCacheKey);
      if(cached && Number(cached.expires_at_ms)>now()) { artistMbid=cached.artist_mbid || null; musicBrainzGenres=Array.isArray(cached.genres)?cached.genres:[]; }
      else {
        const gap=1100-(now()-lastMusicBrainzAt);
        if(lastMusicBrainzAt && gap>0) await wait(gap);
        lastMusicBrainzAt=now(); requests++;
        const match=await findMusicBrainzArtist(fetchImpl,seed.artist);
        if(match) {artistMbid=match.id;musicBrainzGenres=match.genres;}
        await saveState(db,mbCacheKey,{artist_mbid:artistMbid,genres:musicBrainzGenres,expires_at_ms:now()+30*86400000,checked_at:nowIso(now())}).run();
      }
      const rootQuery=seed.artist.slice(0,160);
      calls.push(searchAudius(fetchImpl,rootQuery,seed.track_key,musicBrainzGenres,'direct'));
      if(artistMbid) {
        requests++;
        const related=await relatedArtistsFromListenBrainz(fetchImpl,artistMbid);
        for(const artist of related.slice(0,3)) calls.push(searchAudius(fetchImpl,artist.name,seed.track_key,[], 'similar_artist'));
      }
      const result=await Promise.all(calls);
      for(const candidates of result) {fetched+=candidates.length;batch.push(...candidates);}
    }
    saved=await writeCandidates(db,batch,excluded,now());
    const nextCursor=(cursor+selected.length)%seeds.length;
    await saveState(db,CATALOG_CURSOR_KEY,{cursor:nextCursor,seed_signature:signature,updated_at:nowIso(now())}).run();
    const finalStatus=await catalogStatus(db,now());
    const final={...old,owner_id:null,lease_until_ms:0,state:saved?'ready':(fetched?'filtered':'temporarily_unavailable'),
      checked_at:nowIso(now()),seed_count:selected.length,fetched_count:fetched,saved_count:saved,candidate_count:finalStatus.available,
      cursor:nextCursor,retry_after_ms:saved?0:now()+60000};
    await saveState(db,CATALOG_LEASE_KEY,final).run();
    return {...finalStatus,...final,started:true};
  } catch {
    failure=true;
    const final={...old,owner_id:null,lease_until_ms:0,state:'temporarily_unavailable',checked_at:nowIso(now()),retry_after_ms:now()+60000};
    await saveState(db,CATALOG_LEASE_KEY,final).run();
    return {...status,...final,started:true,failed:failure};
  }
}
