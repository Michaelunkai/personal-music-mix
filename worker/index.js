import html from '../frontend/index.html?raw';
import css from '../frontend/styles.css?raw';
import script from '../frontend/app.js?raw';
import { normalizeImport, rankTracks } from './domain.js';

const now = () => new Date().toISOString();
const json = (body, status=200) => Response.json(body, {status, headers:{'Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff'}});
const FRESH_MIX_LIMIT = 50;
const readState = async (db,key) => { const row = await db.prepare('SELECT payload FROM music_state WHERE key=?').bind(key).first(); return row ? JSON.parse(row.payload) : null; };
const saveState = (db,key,value) => db.prepare('INSERT INTO music_state(key,payload) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload').bind(key,JSON.stringify(value));
const requestDiscovery = db => saveState(db,'discovery_request',{id:crypto.randomUUID(),requested_at:now()}).run();
const asKeys = value => new Set(Array.isArray(value) ? value.filter(key => typeof key === 'string' && key) : []);
const MAX_LEDGER_INPUT_KEYS = 100000;
const mergeKeys = (current, incoming) => [...new Set([...current, ...incoming])];
const trackKey = item => String(item?.track?.track_key || item?.track_key || '');
const trackVideoId = item => String(item?.track?.video_id || item?.video_id || '');
const playableVideoId = value => /^[A-Za-z0-9_-]{11}$/.test(String(value || ''));
const activeItem = (item, rowsByKey, favorites, nowMs = Date.now()) => {
  const track = item?.track || item || {};
  const row = rowsByKey.get(track.track_key) || track;
  const liked = favorites.has(track.track_key) || Number(row.liked_count) > 0 || Number(row.like_events) > 0 || Boolean(row.local_favorite);
  const played = Number(row.play_count || 0) > 0 || (row.latest_played_at !== null && row.latest_played_at !== undefined && String(row.latest_played_at).trim() !== '');
  const playable = playableVideoId(track.video_id);
  const freshSource = item?.source === 'favorite_discovery' || item?.source === 'related' || row.source === 'favorite_discovery' || row.source === 'related';
  const seeds = Array.isArray(row.discovery_seeds) ? row.discovery_seeds : [];
  const related = item?.source === 'related' || row.source === 'related';
  const seedBackfill = seeds.some(seed => {
    const source = rowsByKey.get(seed.track_key);
    return Boolean(source && (Number(source.play_count) > 0 || favorites.has(seed.track_key) || Number(source.liked_count) > 0 || Number(source.like_events) > 0 || Boolean(source.local_favorite)))
      || Number(seed.play_count) > 0 || Boolean(seed.liked);
  });
  return freshSource && !played && !liked && playable && (related || seedBackfill);
};
async function servedLedger(db, rows = []) {
  const [history, local, records] = await Promise.all([
    readState(db, 'recommendation_history'),
    readState(db, 'local_served_keys'),
    db.prepare('SELECT track_key,video_id FROM music_served').all(),
  ]);
  const keys = new Set([...asKeys(history), ...asKeys(local)]);
  const videos = new Set();
  for (const record of records.results || []) {
    if (record.track_key) keys.add(record.track_key);
    if (playableVideoId(record.video_id)) videos.add(record.video_id);
  }
  const rowsByKey = new Map(rows.map(row => [row.track_key, row]));
  for (const key of keys) {
    const videoId = rowsByKey.get(key)?.video_id;
    if (playableVideoId(videoId)) videos.add(videoId);
  }
  return {keys, videos};
}
async function servedKeys(db) {
  return (await servedLedger(db)).keys;
}
const releaseReservation = (db, reservationId) => db.prepare('DELETE FROM music_served WHERE reservation_id=?').bind(reservationId).run();
async function reserveFreshItems(db, items) {
  const reservationId = crypto.randomUUID();
  const candidates = [];
  const seen = new Set();
  for (const item of items) {
    const key = trackKey(item);
    const videoId = trackVideoId(item);
    const identity = `${key}\u0000${videoId}`;
    if (!key || !playableVideoId(videoId) || seen.has(identity)) continue;
    seen.add(identity);
    candidates.push(item);
  }
  if (!candidates.length) return {items:[],reservationId};
  try {
    await db.batch(candidates.map(item => db.prepare('INSERT OR IGNORE INTO music_served(track_key,video_id,served_at,reservation_id) VALUES(?,?,?,?)')
      .bind(trackKey(item),trackVideoId(item),now(),reservationId)));
    const {results} = await db.prepare('SELECT track_key,video_id FROM music_served WHERE reservation_id=?').bind(reservationId).all();
    const reserved = new Set((results || []).map(row => `${row.track_key}\u0000${row.video_id}`));
    return {items:candidates.filter(item => reserved.has(`${trackKey(item)}\u0000${trackVideoId(item)}`)),reservationId};
  } catch (error) {
    try { await releaseReservation(db,reservationId); } catch {}
    throw error;
  }
}
async function recordServedKeys(db, keys, rows = []) {
  const rowsByKey = new Map(rows.map(row => [row.track_key, row]));
  const unique = [...asKeys(keys)];
  const statements = unique.map(key => {
    const videoId = rowsByKey.get(key)?.video_id;
    return db.prepare('INSERT OR IGNORE INTO music_served(track_key,video_id,served_at,reservation_id) VALUES(?,?,?,NULL)')
      .bind(key,playableVideoId(videoId) ? videoId : null,now());
  });
  for (let index = 0; index < statements.length; index += 200) await db.batch(statements.slice(index,index + 200));
  return unique.length;
}
async function currentFreshItems(db, rows, favorites, fallback = []) {
  const rowsByKey = new Map(rows.map(row => [row.track_key, row]));
  const seenVideoIds = new Set();
  return (Array.isArray(fallback) ? fallback : []).filter(item => {
    if (!activeItem(item, rowsByKey, favorites)) return false;
    const videoId = trackVideoId(item);
    if (seenVideoIds.has(videoId)) return false;
    seenVideoIds.add(videoId);
    return true;
  });
}
async function library(db) {
  const [tracks,favorites] = await Promise.all([
    db.prepare('SELECT payload FROM music_library').all(),
    db.prepare('SELECT track_key FROM music_favorites WHERE liked=1').all(),
  ]);
  return {rows:tracks.results.map(row=>JSON.parse(row.payload)), favorites:new Set(favorites.results.map(row=>row.track_key))};
}

const databaseLocks = new WeakMap();
async function withDatabaseLock(db, work) {
  const previous = databaseLocks.get(db) || Promise.resolve();
  let release;
  const current = new Promise(resolve => { release = resolve; });
  databaseLocks.set(db, previous.then(() => current));
  try {
    await previous;
    return await work();
  } finally {
    release();
  }
}
async function rebuildUnlocked(db) {
  const {rows,favorites} = await library(db);
  if (!rows.length) return {status:'failed',run:{message:'No listening history is available yet. Connect the local bridge first.'}};
  await db.prepare("DELETE FROM music_served WHERE reservation_id IS NOT NULL AND julianday(served_at) < julianday('now','-15 minutes')").run();
  let served = await servedLedger(db,rows);
  let rankedItems = rankTracks(rows,favorites,FRESH_MIX_LIMIT,Date.now(),{unheardOnly:true,excludeKeys:served.keys,excludeVideoIds:served.videos});
  // The durable unique video index is the reservation boundary.  It closes
  // the race where two refreshes read the same served set before either one
  // writes its next visible batch.
  let reservation = await reserveFreshItems(db,rankedItems);
  // A separate Worker isolate can win the same reservation race between the
  // ledger read and the insert batch. Re-read the durable table once so that
  // contention advances to the next unseen batch instead of publishing an
  // empty batch that discarded still-available candidates.
  if (!reservation.items.length && rankedItems.length) {
    served = await servedLedger(db,rows);
    rankedItems = rankTracks(rows,favorites,FRESH_MIX_LIMIT,Date.now(),{unheardOnly:true,excludeKeys:served.keys,excludeVideoIds:served.videos});
    reservation = await reserveFreshItems(db,rankedItems);
  }
  const freshItems = reservation.items;
  const previous = await readState(db,'runs') || [];
  const previousRecommendations = await readState(db,'recommendations') || [];
  const oldPlan = await readState(db,'playlist');
  // A provider can deliver a partial discovery response while the local
  // bridge is still expanding its frontier. Never let that partial response
  // displace an already complete visible mix: doing so creates a moment where
  // the dashboard shows fewer than the requested fifty songs. Release the
  // reservation so those partial candidates remain available for the next
  // complete replacement attempt.
  const preservedItems = await currentFreshItems(db,rows,favorites,previousRecommendations);
  if (freshItems.length < FRESH_MIX_LIMIT && preservedItems.length >= FRESH_MIX_LIMIT) {
    await releaseReservation(db,reservation.reservationId);
    const preservedPlan = {
      ...oldPlan,
      name:oldPlan?.name || 'Your personal mix',
      status:'preview',
      requested_count:preservedItems.length,
      items:preservedItems.map(row => row.track || row),
      generated_at:oldPlan?.generated_at || now(),
    };
    const run = {run_id:crypto.randomUUID(),status:'completed',items_seen:rows.length,finished_at:now(),message:`Waiting for a complete ${FRESH_MIX_LIMIT}-song batch; ${freshItems.length} new qualifying songs are held until the provider supplies the rest.`};
    await db.batch([
      saveState(db,'recommendations',preservedItems),
      saveState(db,'playlist',preservedPlan),
      saveState(db,'runs',[run,...previous].slice(0,20)),
    ]);
    return {status:'completed',run,recommendations:preservedItems,playlist_preview:preservedPlan,preserved_previous_mix:true,available_new_items:freshItems.length,target_count:FRESH_MIX_LIMIT};
  }
  const visibleItems = freshItems;
  const nextServed = mergeKeys([...served.keys], freshItems.map(item => trackKey(item)).filter(Boolean));
  const run = {run_id:crypto.randomUUID(),status:'completed',items_seen:rows.length,finished_at:now(),message:freshItems.length
    ? `Fresh mix built from your most-listened songs and favorites; ${freshItems.length} songs are new to your listening history.`
    : 'No new unseen songs are available yet. Refresh requested another provider discovery batch.'};
  const plan = {name:oldPlan?.name || 'Your personal mix',status:'preview',requested_count:visibleItems.length,items:visibleItems.map(row=>row.track || row),generated_at:freshItems.length ? now() : (oldPlan?.generated_at || now())};
  try {
    await db.batch([saveState(db,'recommendations',visibleItems),saveState(db,'playlist',plan),saveState(db,'recommendation_history',nextServed),saveState(db,'runs',[run,...previous].slice(0,20)),db.prepare('UPDATE music_served SET reservation_id=NULL WHERE reservation_id=?').bind(reservation.reservationId)]);
  } catch (error) {
    try { await releaseReservation(db,reservation.reservationId); } catch {}
    throw error;
  }
  return {status:'completed',run,recommendations:freshItems,playlist_preview:plan,preserved_previous_mix:false};
}
async function rebuild(db) {
  return withDatabaseLock(db, () => rebuildUnlocked(db));
}

async function handleApi(request,env,path) {
  const db = env.DB;
  if (!db) return json({detail:'Persistent database is unavailable'},503);
  const method = request.method;
  // The dashboard opts into the fresh-only contract explicitly. Keeping the
  // legacy read shape for unmarked API clients preserves compatibility for
  // older integrations while the user-facing site always sends this header.
  const freshRequested = request.headers.get('X-Mix-Mode') === 'fresh' || new URL(request.url).searchParams.get('unheard_only') === '1';
  const origin = request.headers.get('Origin');
  if (method !== 'GET' && origin && origin !== new URL(request.url).origin) return json({detail:'Cross-origin changes are not allowed'},403);
  if (method !== 'GET' && !request.headers.get('content-type')?.startsWith('application/json')) return json({detail:'JSON is required'},415);
  const body = async () => {
    if (Number(request.headers.get('content-length') || 0) > 8e6) throw new Error('Request is too large');
    const text = await request.text(); if(text.length > 8e6) throw new Error('Request is too large');
    return JSON.parse(text);
  };
  if(path === '/api/sync/heartbeat' && method === 'POST') {
    const {discovery} = await body();
    if(!discovery || !['needs_favorites','temporarily_unavailable','updated','cached','exhausted'].includes(discovery.state)) return json({detail:'A valid discovery status is required'},422);
    await saveState(db,'companion',{received_at:now(),discovery:{state:discovery.state,
      candidate_count:Math.max(0,Number(discovery.candidate_count)||0), seed_count:Math.max(0,Number(discovery.seed_count)||0),
      checked_at:Number.isFinite(Date.parse(discovery.checked_at)) ? discovery.checked_at : null,
      request_id:typeof discovery.request_id === 'string' ? discovery.request_id.slice(0,100) : null}}).run();
    return json({received:true});
  }
  if(path === '/api/sync/ledger' && method === 'POST') {
    const payload = await body();
    if(!Array.isArray(payload.served_keys) || payload.served_keys.length > MAX_LEDGER_INPUT_KEYS) return json({detail:'A bounded served_keys array is required'},422);
    const result = await withDatabaseLock(db, async () => {
      const current = await servedKeys(db);
      const merged = mergeKeys([...current], [...asKeys(payload.served_keys)]);
      const {rows} = await library(db);
      await recordServedKeys(db,payload.served_keys,rows);
      await saveState(db,'local_served_keys',merged).run();
      return {status:'completed',served_keys:(await servedLedger(db,rows)).keys.size};
    });
    return json(result);
  }
  if (path === '/api/sync/import' && method === 'POST') {
    const payload = await body(); const tracks = normalizeImport(payload);
    if (!tracks.length) return json({detail:'An empty import will not replace your library'},422);
    const statements = tracks.flatMap(track=>[
      db.prepare('INSERT INTO music_library(track_key,payload) VALUES(?,?) ON CONFLICT(track_key) DO UPDATE SET payload=excluded.payload').bind(track.track_key,JSON.stringify(track)),
      ...(track.local_favorite_updated_at ? [db.prepare('INSERT INTO music_favorites(track_key,liked,updated_at) VALUES(?,?,?) ON CONFLICT(track_key) DO UPDATE SET liked=excluded.liked,updated_at=excluded.updated_at WHERE julianday(excluded.updated_at) > julianday(music_favorites.updated_at)').bind(track.track_key,Number(track.local_favorite),track.local_favorite_updated_at)] : []),
    ]);
    // Each chunk and its receipt are committed together; retries are idempotent.
    await db.batch([...statements,saveState(db,'sync',{received_at:now(),source_sync_at:payload.last_sync_at || null,tracks:tracks.length,source:'local_browser_bridge'})]);
    // A large local library arrives in bounded chunks.  Rebuilding after an
    // intermediate chunk can consume or hide the fresh pool before the
    // remaining rows (including its seeds) arrive, leaving the final visible
    // mix empty.  The publisher marks every non-final chunk explicitly so the
    // complete library is present before the one authoritative rebuild.
    if (payload.defer_rebuild === true) return json({status:'completed',imported:tracks.length,rebuild_deferred:true});
    return json({...(await rebuild(db)),imported:tracks.length});
  }
  if (path === '/api/favorites' && method === 'POST') {
    const payload = await body();
    if(typeof payload.track_key !== 'string' || typeof payload.liked !== 'boolean') return json({detail:'A track and boolean liked value are required'},422);
    if(!await db.prepare('SELECT track_key FROM music_library WHERE track_key=?').bind(payload.track_key).first()) return json({detail:'Track not found'},404);
    await db.prepare("INSERT INTO music_favorites(track_key,liked,updated_at) VALUES(?,?,?) ON CONFLICT(track_key) DO UPDATE SET liked=excluded.liked,updated_at=CASE WHEN julianday(excluded.updated_at)>julianday(music_favorites.updated_at) THEN excluded.updated_at ELSE strftime('%Y-%m-%dT%H:%M:%fZ',music_favorites.updated_at,'+0.001 seconds') END").bind(payload.track_key,Number(payload.liked),now()).run();
    await requestDiscovery(db);
    return json({saved:true,liked:payload.liked,source:'dashboard',mix_status:(await rebuild(db)).status});
  }
  if(path === '/api/scan' && method === 'POST') {await requestDiscovery(db);return json({...await rebuild(db),discovery_pending:true});}
  if(path === '/api/playlists/preview' && method === 'POST') {
    const payload = await body(); const plan = await readState(db,'playlist');
    if(!plan) return json({detail:'Refresh your mix first'},409);
    const {rows,favorites}=await library(db);
    const saved = await readState(db,'recommendations') || [];
    // A saved playlist name must never resurrect heard or expired songs.  The
    // dashboard already requests fresh mode, but this endpoint also enforces
    // the same unread-only contract for direct/API callers without that header.
    const items=(await currentFreshItems(db,rows,favorites,saved)).map(item=>item.track || item);
    const updated = {...plan,name:String(payload.name || plan.name).slice(0,120),items,requested_count:items.length};
    await saveState(db,'playlist',updated).run();
    return json({plan:updated,write_enabled:false});
  }
  if(path === '/api/playlists/write') return json({detail:'Provider playlist creation requires the authenticated local YouTube connector. You can listen to this mix here.'},409);
  if(method !== 'GET') return json({detail:'Action is not available'},405);
  if(path === '/api/recommendations') {
    const {rows,favorites}=await library(db);
    const items=freshRequested
      ? await currentFreshItems(db,rows,favorites,await readState(db,'recommendations') || [])
      : rankTracks(rows,favorites);
    return json({items,count:items.length});
  }
  if(path === '/api/playlists/latest') {
    let plan = await readState(db,'playlist');
    if(plan){
      const {rows,favorites}=await library(db);
      const saved = await readState(db,'recommendations') || [];
      const items=(freshRequested ? await currentFreshItems(db,rows,favorites,saved) : rankTracks(rows,favorites)).map(item=>item.track || item);
      plan={...plan,items,requested_count:items.length};
    }
    return json({available:!!plan,plan,write_enabled:false});
  }
  if(path === '/api/runs') return json({items:await readState(db,'runs') || []});
  if(path === '/api/favorites') { const {results:records} = await db.prepare('SELECT track_key,liked,updated_at FROM music_favorites').all(); return json({track_keys:records.filter(row=>row.liked).map(row=>row.track_key),records,source:'dashboard',discovery_request:await readState(db,'discovery_request'),served_keys:[...await servedKeys(db)]}); }
  if(path === '/api/status') return json({scheduler_running:false,scheduler_mode:'browser_bridge_event_driven',scheduler_healthy:true});
  const {rows,favorites} = await library(db);
  const plays = rows.reduce((sum,row)=>sum+row.play_count,0);
  const sync = await readState(db,'sync');
  const connection = {state: rows.length ? 'history_cached_bridge_offline':'awaiting_browser_bridge_sync',message:rows.length ? 'Your saved library is online. Refresh mix uses your latest saved history and favorites; new YouTube history comes from the local browser bridge.':'Waiting for your listening history from the local browser bridge.',history_events:plays,tracks:rows.length,last_sync_at:sync?.source_sync_at,cloud_received_at:sync?.received_at,browser_bridge:{ready:false,live:false},hosted:true};
  const [companion,discoveryRequest] = await Promise.all([readState(db,'companion'),readState(db,'discovery_request')]);
  const companionOnline = !!companion && Date.now()-Date.parse(companion.received_at)<120000;
  const hasTaste=rows.some(row=>Number(row.play_count)>0||favorites.has(row.track_key)||row.liked_count>0||row.like_events>0);
  const pending=!!discoveryRequest && discoveryRequest.id !== companion?.discovery?.request_id;
  connection.companion={online:companionOnline,last_seen_at:companion?.received_at};
  connection.discovery={...companion?.discovery,pending};
  connection.message = !hasTaste ? 'Your saved library is ready. Import YouTube history or likes to start a fresh mix.'
    : !companionOnline ? 'Your saved mix is ready. Fresh song suggestions will arrive when the local music app reconnects.'
    : companion?.discovery?.state === 'temporarily_unavailable' ? 'Your saved mix is ready. YouTube recommendations are temporarily unavailable; the local app will retry.'
    : pending ? 'Finding fresh songs from your favorites. Your mix will update automatically when they arrive.'
    : companion?.discovery?.candidate_count > 0 ? 'Fresh songs from your listening signals are ready. Press Refresh mix whenever you want another new batch.'
    : companion?.discovery?.state === 'exhausted' ? 'You have already seen every currently available candidate. Press Refresh mix to request another provider batch.'
    : 'Your saved mix is ready. No new song suggestions are available yet; try Refresh mix again later.';
  if(path === '/api/connection') return json(connection);
  if(path === '/api/health') return json({ok:true,database:{ok:true,tracks:rows.length,history_events:plays},account_readiness:connection});
  if(path === '/api/overview') return json({track_count:rows.length,play_count:plays,liked_track_count:rows.filter(r=>r.liked_count>0 || r.like_events>0 || favorites.has(r.track_key)).length,local_favorite_count:favorites.size,provider_liked_track_count:rows.filter(r=>r.liked_count>0 || r.like_events>0).length});
  if(path === '/api/library') return json({items:rows.map(track=>({track})),count:rows.length});
  return json({detail:'Not found'},404);
}

export default {
  async fetch(request,env) {
    const path = new URL(request.url).pathname;
    try {
      if(path.startsWith('/api/')) return await handleApi(request,env,path);
      const asset = path === '/' ? [html,'text/html'] : path === '/static/app.js' ? [script,'text/javascript'] : path === '/static/styles.css' ? [css,'text/css'] : null;
      if(!asset) return new Response('Not found',{status:404});
      return new Response(asset[0],{headers:{'Content-Type':asset[1]+'; charset=utf-8','Cache-Control':'private, no-cache','Referrer-Policy':'strict-origin-when-cross-origin','X-Content-Type-Options':'nosniff','X-Frame-Options':'SAMEORIGIN'}});
    } catch(error) { return json({detail:error instanceof SyntaxError ? 'Invalid JSON' : 'The request could not be completed. Please retry.'},400); }
  },
};
