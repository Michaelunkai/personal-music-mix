import html from '../frontend/index.html?raw';
import css from '../frontend/styles.css?raw';
import script from '../frontend/app.js?raw';
import { normalizeImport, rankTracks } from './domain.js';

const now = () => new Date().toISOString();
const json = (body, status=200) => Response.json(body, {status, headers:{'Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff'}});
const readState = async (db,key) => { const row = await db.prepare('SELECT payload FROM music_state WHERE key=?').bind(key).first(); return row ? JSON.parse(row.payload) : null; };
const saveState = (db,key,value) => db.prepare('INSERT INTO music_state(key,payload) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload').bind(key,JSON.stringify(value));
const requestDiscovery = db => saveState(db,'discovery_request',{id:crypto.randomUUID(),requested_at:now()}).run();

async function library(db) {
  const [tracks,favorites] = await Promise.all([
    db.prepare('SELECT payload FROM music_library').all(),
    db.prepare('SELECT track_key FROM music_favorites WHERE liked=1').all(),
  ]);
  return {rows:tracks.results.map(row=>JSON.parse(row.payload)), favorites:new Set(favorites.results.map(row=>row.track_key))};
}

async function rebuild(db) {
  const {rows,favorites} = await library(db);
  if (!rows.length) return {status:'failed',run:{message:'No listening history is available yet. Connect the local bridge first.'}};
  const items = rankTracks(rows,favorites);
  const run = {run_id:crypto.randomUUID(),status:'completed',items_seen:rows.length,finished_at:now(),message:`Mix refreshed from saved favorites and history, including ${items.filter(item=>item.source==='favorite_discovery').length} new song suggestions.`};
  const previous = await readState(db,'runs') || [];
  const oldPlan = await readState(db,'playlist');
  const plan = {name:oldPlan?.name || 'Your personal mix',status:'preview',requested_count:items.length,items:items.map(row=>row.track),generated_at:now()};
  await db.batch([saveState(db,'recommendations',items),saveState(db,'playlist',plan),saveState(db,'runs',[run,...previous].slice(0,20))]);
  return {status:'completed',run,playlist_preview:plan};
}

async function handleApi(request,env,path) {
  const db = env.DB;
  if (!db) return json({detail:'Persistent database is unavailable'},503);
  const method = request.method;
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
    if(!discovery || !['needs_favorites','temporarily_unavailable','updated','cached'].includes(discovery.state)) return json({detail:'A valid discovery status is required'},422);
    await saveState(db,'companion',{received_at:now(),discovery:{state:discovery.state,
      candidate_count:Math.max(0,Number(discovery.candidate_count)||0), seed_count:Math.max(0,Number(discovery.seed_count)||0),
      checked_at:Number.isFinite(Date.parse(discovery.checked_at)) ? discovery.checked_at : null,
      request_id:typeof discovery.request_id === 'string' ? discovery.request_id.slice(0,100) : null}}).run();
    return json({received:true});
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
    const updated = {...plan,name:String(payload.name || plan.name).slice(0,120)};
    await saveState(db,'playlist',updated).run();
    return json({plan:updated,write_enabled:false});
  }
  if(path === '/api/playlists/write') return json({detail:'Provider playlist creation requires the authenticated local YouTube connector. You can listen to this mix here.'},409);
  if(method !== 'GET') return json({detail:'Action is not available'},405);
  if(path === '/api/recommendations') {const {rows,favorites}=await library(db);const items=rankTracks(rows,favorites);return json({items,count:items.length});}
  if(path === '/api/playlists/latest') {let plan = await readState(db,'playlist');if(plan){const {rows,favorites}=await library(db);const items=rankTracks(rows,favorites).map(item=>item.track);plan={...plan,items,requested_count:items.length};}return json({available:!!plan,plan,write_enabled:false});}
  if(path === '/api/runs') return json({items:await readState(db,'runs') || []});
  if(path === '/api/favorites') { const {results:records} = await db.prepare('SELECT track_key,liked,updated_at FROM music_favorites').all(); return json({track_keys:records.filter(row=>row.liked).map(row=>row.track_key),records,source:'dashboard',discovery_request:await readState(db,'discovery_request')}); }
  if(path === '/api/status') return json({scheduler_running:false,scheduler_mode:'browser_bridge_event_driven',scheduler_healthy:true});
  const {rows,favorites} = await library(db);
  const plays = rows.reduce((sum,row)=>sum+row.play_count,0);
  const sync = await readState(db,'sync');
  const connection = {state: rows.length ? 'history_cached_bridge_offline':'awaiting_browser_bridge_sync',message:rows.length ? 'Your saved library is online. Refresh mix uses your latest saved history and favorites; new YouTube history comes from the local browser bridge.':'Waiting for your listening history from the local browser bridge.',history_events:plays,tracks:rows.length,last_sync_at:sync?.source_sync_at,cloud_received_at:sync?.received_at,browser_bridge:{ready:false,live:false},hosted:true};
  const [companion,discoveryRequest] = await Promise.all([readState(db,'companion'),readState(db,'discovery_request')]);
  const companionOnline = !!companion && Date.now()-Date.parse(companion.received_at)<120000;
  const hasFavorites=rows.some(row=>favorites.has(row.track_key)||row.liked_count>0||row.like_events>0);
  const pending=!!discoveryRequest && discoveryRequest.id !== companion?.discovery?.request_id;
  connection.companion={online:companionOnline,last_seen_at:companion?.received_at};
  connection.discovery={...companion?.discovery,pending};
  connection.message = !hasFavorites ? 'Your saved library is ready. Add a heart to a song, or import your YouTube likes, to discover new music from favorites.'
    : !companionOnline ? 'Your saved mix is ready. Fresh song suggestions will arrive when the local music app reconnects.'
    : companion?.discovery?.state === 'temporarily_unavailable' ? 'Your saved mix is ready. YouTube recommendations are temporarily unavailable; the local app will retry.'
    : pending ? 'Finding fresh songs from your favorites. Your mix will update automatically when they arrive.'
    : companion?.discovery?.candidate_count > 0 ? 'Song suggestions from your favorites are ready. Press Refresh mix whenever you want another request.'
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
