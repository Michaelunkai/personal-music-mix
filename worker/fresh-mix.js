import { recordingKeyFor } from './domain.js';
import { rankMusicCandidates } from './taste.js';

export const FRESH_MIX_SIZE = 50;
const REQUEST_LEASE_MS = 15000;
const REQUEST_ID = /^[A-Za-z0-9_-]{16,100}$/;
const iso = value => new Date(value).toISOString();
const isVideoId = value => /^[A-Za-z0-9_-]{11}$/.test(String(value || ''));
const trackKey = row => String(row?.track?.track_key || row?.track?.candidate_key || row?.track_key || row?.candidate_key || '');
const parsed = value => { try { return JSON.parse(value); } catch { return null; } };
const changes = result => Number(result?.meta?.changes || result?.meta?.rows_written || 0);

async function readState(db,key) {
  const row=await db.prepare('SELECT payload FROM music_state WHERE key=?').bind(key).first();
  return row?parsed(row.payload):null;
}

const saveState=(db,key,value)=>db.prepare('INSERT INTO music_state(key,payload) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload')
  .bind(key,JSON.stringify(value));

function canonicalTrack(row) {
  return row?.recording_key || recordingKeyFor(row?.title,row?.artist);
}

async function claimRequest(db,requestId,nowMs) {
  const owner=crypto.randomUUID();
  const stamp=iso(nowMs);
  await db.prepare(`INSERT OR IGNORE INTO music_mix_requests(request_id,state,lease_owner,lease_until_ms,response_json,created_at,updated_at)
    VALUES(?,'pending',?,?,NULL,?,?)`).bind(requestId,owner,nowMs+REQUEST_LEASE_MS,stamp,stamp).run();
  let row=await db.prepare('SELECT request_id,state,lease_owner,lease_until_ms,response_json FROM music_mix_requests WHERE request_id=?').bind(requestId).first();
  if(row?.state==='completed'||row?.state==='exhausted') return {owner,row};
  if(row?.lease_owner!==owner) {
    if(Number(row?.lease_until_ms)>nowMs) return {owner,row,pending:true};
    const claimed=await db.prepare(`UPDATE music_mix_requests SET lease_owner=?,lease_until_ms=?,updated_at=?
      WHERE request_id=? AND state='pending' AND lease_until_ms<=?`).bind(owner,nowMs+REQUEST_LEASE_MS,stamp,requestId,nowMs).run();
    if(!changes(claimed)) return {owner,row,pending:true};
    row=await db.prepare('SELECT request_id,state,lease_owner,lease_until_ms,response_json FROM music_mix_requests WHERE request_id=?').bind(requestId).first();
  }
  return {owner,row};
}

function unpackLibrary(rows) {
  const result=[];
  for(const item of rows||[]) {
    const row=parsed(item.payload);
    if(row&&typeof row==='object'&&!Array.isArray(row)) result.push(row);
  }
  return result;
}

function providerSeeds(candidates,feedback) {
  const grouped=new Map();
  for(const event of feedback||[]) {
    const list=grouped.get(event.recording_key)||[];
    list.push(event);grouped.set(event.recording_key,list);
  }
  return (candidates||[]).filter(row=>grouped.has(row.recording_key)).map(row=>{
    const events=grouped.get(row.recording_key);
    const latest=events.map(event=>Date.parse(event.created_at||'')).filter(Number.isFinite).sort((a,b)=>b-a)[0];
    return {track_key:row.candidate_key,recording_key:row.recording_key,title:row.title,artist:row.artist,genre:row.genre,
      play_count:0,liked_count:0,like_events:0,latest_played_at:Number.isFinite(latest)?iso(latest):null,provider:row.provider};
  });
}

function musicBrainzGenres(states) {
  const byArtist={};
  for(const state of states||[]) {
    if(!state.key?.startsWith('musicbrainz_artist:')) continue;
    const artist=state.key.slice('musicbrainz_artist:'.length).trim().toLocaleLowerCase('en-US');
    const value=parsed(state.payload);
    if(artist&&Array.isArray(value?.genres)) byArtist[artist]=value.genres;
  }
  return byArtist;
}

function playlistItems(value) {
  if(!Array.isArray(value)) return [];
  return value.map(item=>item?.track||item).filter(item=>item&&typeof item==='object');
}

function buildCandidatePool(library, favorites, providerCandidates, libraryRecordingKeys, nowMs) {
  const youtube=[];
  const origins=[];
  for(const row of library) {
    if(!['favorite_discovery','related'].includes(row.source)||!isVideoId(row.video_id)) continue;
    if(Number(row.play_count||0)>0||String(row.latest_played_at||'').trim()||favorites.has(row.track_key)||row.local_favorite
      ||Number(row.liked_count||0)>0||Number(row.like_events||0)>0) continue;
    const candidate={...row,provider:'youtube',candidate_key:row.track_key,recording_key:canonicalTrack(row)};
    youtube.push(candidate);
    for(const seed of Array.isArray(row.discovery_seeds)?row.discovery_seeds:[]) {
      if(Number(seed?.expires_at)<=nowMs/1000) continue;
      if(typeof seed?.track_key!=='string') continue;
      origins.push({candidate_key:candidate.candidate_key,seed_track_key:seed.track_key,
        relationship:row.source==='related'?'similar_artist':'direct'});
    }
  }
  const audius=(providerCandidates||[]).filter(row=>!libraryRecordingKeys.has(row.recording_key));
  return {candidates:[...youtube,...audius],origins};
}

function previousRecordingKeys(recommendations,playlist,libraryByKey,candidateByKey) {
  const keys=new Set();
  const add=item=>{
    const row=item?.track||item||{};
    const key=canonicalTrack(row)||candidateByKey.get(trackKey(item))||libraryByKey.get(trackKey(item));
    if(key) keys.add(key);
  };
  for(const item of recommendations||[]) add(item);
  for(const item of playlistItems(playlist?.items)) add(item);
  return keys;
}

async function responseForRequest(db,requestId) {
  const row=await db.prepare('SELECT state,response_json FROM music_mix_requests WHERE request_id=?').bind(requestId).first();
  if(row?.response_json&&(row.state==='completed'||row.state==='exhausted')) return parsed(row.response_json);
  return null;
}

async function finishUnavailable(db,requestId,owner,available,at) {
  const body={status:'unavailable',request_id:requestId,target_count:FRESH_MIX_SIZE,available_count:available,
    message:`Only ${available} new eligible songs are ready; the current mix is unchanged until a complete ${FRESH_MIX_SIZE}-song batch is available.`};
  await db.batch([
    db.prepare(`DELETE FROM music_served WHERE reservation_id=? AND EXISTS(SELECT 1 FROM music_mix_requests WHERE request_id=? AND lease_owner=? AND state='pending')`)
      .bind(requestId,requestId,owner),
    db.prepare(`UPDATE music_mix_requests SET state='exhausted',lease_owner=NULL,lease_until_ms=0,response_json=?,updated_at=?
      WHERE request_id=? AND lease_owner=? AND state='pending'`).bind(JSON.stringify(body),iso(at),requestId,owner),
  ]);
  return {httpStatus:200,body:(await responseForRequest(db,requestId))||body};
}

export async function refreshFreshMix(db,payload,{now=Date.now}={}) {
  const requestId=String(payload?.request_id||'');
  if(!REQUEST_ID.test(requestId)) return {httpStatus:422,body:{detail:'A stable refresh request_id is required'}};
  const started=now();
  await db.prepare(`DELETE FROM music_served WHERE reservation_id IS NOT NULL AND julianday(served_at)<julianday(?,'-15 minutes')
    AND NOT EXISTS(SELECT 1 FROM music_mix_requests r WHERE r.request_id=music_served.reservation_id
      AND (r.state='completed' OR (r.state='pending' AND r.lease_until_ms>?)))`).bind(iso(started),started).run();
  const claim=await claimRequest(db,requestId,started);
  if(claim.row?.state==='completed'||claim.row?.state==='exhausted') {
    const body=parsed(claim.row.response_json);
    if(body) return {httpStatus:200,body};
  }
  if(claim.pending) return {httpStatus:202,body:{status:'pending',request_id:requestId,retry_after_ms:250}};

  try {
    const [libraryResult,favoriteResult,feedbackResult,candidateResult,originResult,stateResult,servedResult]=await Promise.all([
      db.prepare('SELECT payload FROM music_library').all(),
      db.prepare('SELECT track_key FROM music_favorites WHERE liked=1').all(),
      db.prepare("SELECT event_id,track_key,recording_key,provider,event,listened_seconds,duration_seconds,created_at FROM music_feedback WHERE julianday(created_at)>=julianday(?,'-365 days') OR event IN ('like','dislike')")
        .bind(iso(started)).all(),
      db.prepare('SELECT * FROM music_candidates').all(),
      db.prepare('SELECT candidate_key,seed_track_key,relationship FROM music_candidate_origins').all(),
      db.prepare("SELECT key,payload FROM music_state WHERE key IN ('recommendations','playlist','runs','recommendation_history','local_served_keys') OR key LIKE 'musicbrainz_artist:%'").all(),
      db.prepare('SELECT track_key,video_id,recording_key,reservation_id FROM music_served').all(),
    ]);
    const library=unpackLibrary(libraryResult.results);
    const favorites=new Set((favoriteResult.results||[]).map(row=>row.track_key));
    const feedback=feedbackResult.results||[];
    const providerCandidates=candidateResult.results||[];
    const storedStates=new Map((stateResult.results||[]).map(row=>[row.key,row.payload]));
    const storedRecommendations=parsed(storedStates.get('recommendations'))||[];
    const oldPlan=parsed(storedStates.get('playlist'))||null;
    const previousRuns=parsed(storedStates.get('runs'))||[];
    const libraryByKey=new Map(library.map(row=>[row.track_key,canonicalTrack(row)]));
    const providerByKey=new Map(providerCandidates.map(row=>[row.candidate_key,row.recording_key]));
    const candidateByKey=new Map([...libraryByKey,...providerByKey]);
    const libraryRecordingKeys=new Set(library.map(canonicalTrack).filter(Boolean));
    const seedRows=[...library,...providerSeeds(providerCandidates,feedback)];
    const {candidates,origins:youtubeOrigins}=buildCandidatePool(library,favorites,providerCandidates,libraryRecordingKeys,started);
    const allOrigins=[...(originResult.results||[]),...youtubeOrigins];
    const seedGenresByArtist=musicBrainzGenres((stateResult.results||[]).map(row=>({key:row.key,payload:row.payload})));
    const servedRows=servedResult.results||[];
    const ownedRows=()=>servedRows.filter(row=>row.reservation_id===requestId);
    const owns=new Map();
    for(const row of ownedRows()) {
      const recordingKey=row.recording_key||candidateByKey.get(row.track_key)||libraryByKey.get(row.track_key);
      if(recordingKey) owns.set(recordingKey,row);
    }
    const olderServed=new Set();
    for(const row of servedRows) {
      if(row.reservation_id===requestId) continue;
      const recordingKey=row.recording_key||candidateByKey.get(row.track_key)||libraryByKey.get(row.track_key);
      if(recordingKey) olderServed.add(recordingKey);
    }
    const history=parsed(storedStates.get('recommendation_history'))||[];
    const local=parsed(storedStates.get('local_served_keys'))||[];
    for(const key of [...history,...local]) {
      const recordingKey=candidateByKey.get(key)||libraryByKey.get(key);
      if(recordingKey) olderServed.add(recordingKey);
    }
    for(const key of previousRecordingKeys(storedRecommendations,oldPlan,libraryByKey,providerByKey)) olderServed.add(key);

    let selected=[];
    let ranked=[];
    for(let attempt=0;attempt<5&&owns.size<FRESH_MIX_SIZE;attempt++) {
      const latestServed=await db.prepare('SELECT track_key,video_id,recording_key,reservation_id FROM music_served').all();
      for(const row of latestServed.results||[]) {
        const recordingKey=row.recording_key||candidateByKey.get(row.track_key)||libraryByKey.get(row.track_key);
        if(!recordingKey) continue;
        if(row.reservation_id===requestId) owns.set(recordingKey,row);
        else olderServed.add(recordingKey);
      }
      const excluded=new Set(olderServed);
      const rankedNow=rankMusicCandidates({seeds:seedRows,favorites,feedback,candidates,origins:allOrigins,
        seedGenresByArtist,excludeRecordingKeys:excluded,now:started,limit:200});
      ranked=rankedNow;
      const remaining=FRESH_MIX_SIZE-owns.size;
      const toReserve=rankedNow.filter(item=>!owns.has(item.recording_key)).slice(0,remaining);
      if(toReserve.length<remaining) return await finishUnavailable(db,requestId,claim.owner,owns.size+toReserve.length,now());
      await db.batch(toReserve.map(item=>db.prepare(`INSERT OR IGNORE INTO music_served(track_key,video_id,recording_key,served_at,reservation_id)
        VALUES(?,?,?,?,?)`).bind(trackKey(item),item.track.provider==='youtube'?item.track.video_id:null,item.recording_key,iso(now()),requestId)));
      const latestOwned=await db.prepare('SELECT track_key,video_id,recording_key,reservation_id FROM music_served WHERE reservation_id=?').bind(requestId).all();
      for(const row of latestOwned.results||[]) {
        const recordingKey=row.recording_key||candidateByKey.get(row.track_key)||libraryByKey.get(row.track_key);
        if(recordingKey) owns.set(recordingKey,row);
      }
    }
    if(owns.size<FRESH_MIX_SIZE) return await finishUnavailable(db,requestId,claim.owner,owns.size,now());

    const ownKeys=new Set(owns.keys());
    const finalRank=rankMusicCandidates({seeds:seedRows,favorites,feedback,candidates,origins:allOrigins,
      seedGenresByArtist,excludeRecordingKeys:olderServed,now:started,limit:200});
    selected=finalRank.filter(item=>ownKeys.has(item.recording_key)).slice(0,FRESH_MIX_SIZE);
    if(selected.length<FRESH_MIX_SIZE) return await finishUnavailable(db,requestId,claim.owner,selected.length,now());

    const generatedAt=iso(now());
    const recommendations=selected.map((item,index)=>{
      const original=item.track;
      const track=original.provider==='audius'
        ? {...original,track_key:original.candidate_key,url:original.provider_url,provider:'audius',recording_key:item.recording_key}
        : {...original,provider:'youtube',recording_key:item.recording_key};
      return {track,rank:index+1,score:item.score,reasons:item.reasons,source:original.provider==='audius'?'provider_discovery':'history'};
    });
    const plan={...oldPlan,name:oldPlan?.name||'Your personal mix',status:'preview',requested_count:FRESH_MIX_SIZE,
      items:recommendations.map(item=>item.track),generated_at:generatedAt};
    const run={run_id:requestId,status:'completed',items_seen:candidates.length,finished_at:generatedAt,
      message:`Fresh mix built from your listening history, feedback, artist links, and styles; all ${FRESH_MIX_SIZE} songs are new.`};
    const body={status:'completed',request_id:requestId,target_count:FRESH_MIX_SIZE,available_count:FRESH_MIX_SIZE,
      recommendations,playlist_preview:plan,run};
    const batch=[
      saveState(db,'recommendations',recommendations),
      saveState(db,'playlist',plan),
      saveState(db,'runs',[run,...previousRuns].slice(0,20)),
      db.prepare(`UPDATE music_mix_requests SET state='completed',lease_owner=NULL,lease_until_ms=0,response_json=?,updated_at=?
        WHERE request_id=? AND lease_owner=? AND state='pending'`).bind(JSON.stringify(body),generatedAt,requestId,claim.owner),
    ];
    await db.batch(batch);
    return {httpStatus:200,body:(await responseForRequest(db,requestId))||body};
  } catch(error) {
    await db.prepare(`UPDATE music_mix_requests SET lease_until_ms=0,updated_at=? WHERE request_id=? AND lease_owner=? AND state='pending'`)
      .bind(iso(now()),requestId,claim.owner).run();
    throw error;
  }
}
