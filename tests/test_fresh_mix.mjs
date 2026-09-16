import { test } from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { readFileSync } from 'node:fs';
import { performance } from 'node:perf_hooks';
import { recordingKeyFor } from '../worker/domain.js';
import { FRESH_MIX_SIZE, refreshFreshMix } from '../worker/fresh-mix.js';

function database() {
  const sqlite=new DatabaseSync(':memory:');
  for(const name of ['0000_lively_gressill.sql','0001_durable_served_ledger.sql','0002_thick_praxagora.sql','0003_curly_xorn.sql','0004_conscious_boom_boom.sql'])
    sqlite.exec(readFileSync(new URL(`../drizzle/${name}`,import.meta.url),'utf8').split('--> statement-breakpoint').join('\n'));
  const prepare=(sql,args=[])=>({
    bind(...values){return prepare(sql,values);},
    async first(){return sqlite.prepare(sql).get(...args)||null;},
    async all(){return{results:sqlite.prepare(sql).all(...args)};},
    async run(){const result=sqlite.prepare(sql).run(...args);return{success:true,meta:{changes:Number(result.changes||0),rows_written:Number(result.changes||0)}};},
  });
  let batchTail=Promise.resolve();
  const db={prepare,async batch(items){
    let release;const held=new Promise(resolve=>{release=resolve;});const previous=batchTail;batchTail=previous.then(()=>held);await previous;
    sqlite.exec('BEGIN');try{const results=[];for(const item of items)results.push(await item.run());sqlite.exec('COMMIT');return results;}
    catch(error){sqlite.exec('ROLLBACK');throw error;}finally{release();}
  }};
  return{db,sqlite};
}

function addSeed(sqlite,now) {
  const seed={track_key:'video:aaaaaaaaaaa',title:'Most Played',artist:'Taste Artist',video_id:'aaaaaaaaaaa',provider:'youtube',source:'history',
    play_count:40,liked_count:0,latest_played_at:new Date(now-3600000).toISOString(),recording_key:recordingKeyFor('Most Played','Taste Artist')};
  sqlite.prepare('INSERT INTO music_library(track_key,payload) VALUES(?,?)').run(seed.track_key,JSON.stringify(seed));
  return seed;
}

function addAudius(sqlite,index,seedKey) {
  const id=`track_${String(index).padStart(5,'0')}`;
  const title=`Unheard Song ${index}`;
  const artist=`Discovery Artist ${index}`;
  sqlite.prepare(`INSERT INTO music_candidates(candidate_key,recording_key,provider,provider_track_id,title,artist,album,genre,mood,duration_seconds,audio_url,provider_url,artwork_url,seed_keys,discovered_at,last_verified_at)
    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`).run(`audius:${id}`,recordingKeyFor(title,artist),'audius',id,title,artist,'','synth pop','',180,
      `https://api.audius.co/v1/tracks/${id}/stream?app_name=personal-music-mix`,`https://api.audius.co/v1/tracks/${id}`,null,JSON.stringify([seedKey]),'2026-09-16T10:00:00.000Z',null);
  sqlite.prepare('INSERT INTO music_candidate_origins(candidate_key,seed_track_key,relationship) VALUES(?,?,?)').run(`audius:${id}`,seedKey,'direct');
}

test('refresh consumes 50 fresh recordings atomically and retries return the identical stored batch',async()=>{
  const{db,sqlite}=database();const now=Date.parse('2026-09-16T12:00:00Z');const seed=addSeed(sqlite,now);
  for(let i=0;i<110;i++) addAudius(sqlite,i,seed.track_key);
  const first=await refreshFreshMix(db,{request_id:'refresh_request_000001'},{now:()=>now});
  assert.equal(first.httpStatus,200);assert.equal(first.body.status,'completed',JSON.stringify(first.body));assert.equal(first.body.recommendations.length,FRESH_MIX_SIZE);
  assert.equal(new Set(first.body.recommendations.map(item=>item.track.recording_key)).size,FRESH_MIX_SIZE);
  assert.ok(first.body.recommendations.every(item=>item.track.provider==='audius'&&item.reasons.length>0));
  const replay=await refreshFreshMix(db,{request_id:'refresh_request_000001'},{now:()=>now+1000});
  assert.deepEqual(replay.body,first.body);
  assert.equal(sqlite.prepare('SELECT COUNT(*) AS count FROM music_served').get().count,50);
  const second=await refreshFreshMix(db,{request_id:'refresh_request_000002'},{now:()=>now+2000});
  assert.equal(second.body.status,'completed');assert.equal(second.body.recommendations.length,50);
  assert.equal(new Set([...first.body.recommendations,...second.body.recommendations].map(item=>item.track.recording_key)).size,100);
  const previousPlan=sqlite.prepare("SELECT payload FROM music_state WHERE key='playlist'").get().payload;
  const exhausted=await refreshFreshMix(db,{request_id:'refresh_request_000003'},{now:()=>now+3000});
  assert.equal(exhausted.body.status,'unavailable');assert.equal(exhausted.body.available_count,10);
  assert.equal(sqlite.prepare('SELECT COUNT(*) AS count FROM music_served').get().count,100);
  assert.equal(sqlite.prepare("SELECT payload FROM music_state WHERE key='playlist'").get().payload,previousPlan);
  sqlite.close();
});

test('YouTube discovery and Audius candidates share one cross-provider recording exclusion',async()=>{
  const{db,sqlite}=database();const now=Date.parse('2026-09-16T12:00:00Z');const seed=addSeed(sqlite,now);
  const expiry=now/1000+3600;
  for(let i=0;i<26;i++) {
    const title=i===0?'Shared Recording':'YouTube Song '+i;
    const artist=i===0?'Shared Artist':'YouTube Artist '+i;
    const videoId=String(i+1).padStart(11,'0');
    const row={track_key:`video:${videoId}`,title,artist,video_id:videoId,provider:'youtube',source:'favorite_discovery',play_count:0,
      recording_key:recordingKeyFor(title,artist),discovery_seeds:[{track_key:seed.track_key,title:seed.title,expires_at:expiry}]};
    sqlite.prepare('INSERT INTO music_library(track_key,payload) VALUES(?,?)').run(row.track_key,JSON.stringify(row));
  }
  for(let i=0;i<25;i++) addAudius(sqlite,i,seed.track_key);
  const duplicateId='shared_audio_01';
  sqlite.prepare(`INSERT INTO music_candidates(candidate_key,recording_key,provider,provider_track_id,title,artist,album,genre,mood,duration_seconds,audio_url,provider_url,seed_keys,discovered_at)
    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)`).run(`audius:${duplicateId}`,recordingKeyFor('Shared Recording','Shared Artist'),'audius',duplicateId,'Shared Recording','Shared Artist','','synth pop','',180,
      `https://api.audius.co/v1/tracks/${duplicateId}/stream?app_name=personal-music-mix`,`https://api.audius.co/v1/tracks/${duplicateId}`,'[]','2026-09-16T10:00:00Z');
  sqlite.prepare('INSERT INTO music_candidate_origins(candidate_key,seed_track_key,relationship) VALUES(?,?,?)').run(`audius:${duplicateId}`,seed.track_key,'direct');
  const result=await refreshFreshMix(db,{request_id:'refresh_mixed_sources_0001'},{now:()=>now});
  assert.equal(result.body.status,'completed');assert.equal(result.body.recommendations.length,50);
  assert.ok(result.body.recommendations.some(item=>item.track.provider==='youtube'));
  assert.ok(result.body.recommendations.some(item=>item.track.provider==='audius'));
  assert.equal(result.body.recommendations.filter(item=>item.track.title==='Shared Recording').length,1);
  assert.equal(new Set(result.body.recommendations.map(item=>item.track.recording_key)).size,50);
  sqlite.close();
});

test('concurrent refresh IDs reserve distinct batches; concurrent retries share one idempotency result',async()=>{
  const{db,sqlite}=database();const now=Date.parse('2026-09-16T12:00:00Z');const seed=addSeed(sqlite,now);
  for(let i=0;i<200;i++) addAudius(sqlite,i,seed.track_key);
  const [first,second]=await Promise.all([
    refreshFreshMix(db,{request_id:'concurrent_request_0001'},{now:()=>now}),
    refreshFreshMix(db,{request_id:'concurrent_request_0002'},{now:()=>now}),
  ]);
  assert.equal(first.body.status,'completed');assert.equal(second.body.status,'completed');
  assert.equal(new Set([...first.body.recommendations,...second.body.recommendations].map(item=>item.track.recording_key)).size,100);
  const duplicateCalls=await Promise.all([
    refreshFreshMix(db,{request_id:'concurrent_same_request_0001'},{now:()=>now+1000}),
    refreshFreshMix(db,{request_id:'concurrent_same_request_0001'},{now:()=>now+1000}),
  ]);
  const completed=duplicateCalls.find(item=>item.body.status==='completed');
  assert.ok(completed);
  const replay=await refreshFreshMix(db,{request_id:'concurrent_same_request_0001'},{now:()=>now+2000});
  assert.deepEqual(replay.body,completed.body);
  assert.equal(sqlite.prepare('SELECT COUNT(*) AS count FROM music_served').get().count,150);
  sqlite.close();
});

test('ten consecutive production-sized refreshes return 50 distinct recordings each within ten seconds',async()=>{
  const{db,sqlite}=database();const now=Date.parse('2026-09-16T12:00:00Z');const seed=addSeed(sqlite,now);
  for(let i=0;i<500;i++) addAudius(sqlite,i,seed.track_key);
  const allRecordings=new Set();const timings=[];
  for(let batch=0;batch<10;batch++) {
    const started=performance.now();
    const result=await refreshFreshMix(db,{request_id:`perf_refresh_request_${String(batch).padStart(3,'0')}`},{now:()=>now+batch*1000});
    const elapsed=performance.now()-started;timings.push(elapsed);
    assert.equal(result.body.status,'completed',JSON.stringify(result.body));
    assert.equal(result.body.recommendations.length,50);
    const recordings=result.body.recommendations.map(item=>item.track.recording_key);
    assert.equal(new Set(recordings).size,50,'a batch contains duplicate recordings');
    for(const recording of recordings) {assert.ok(!allRecordings.has(recording),'a previous batch recording was reused');allRecordings.add(recording);}
    assert.ok(elapsed<10000,`refresh ${batch+1} took ${elapsed.toFixed(1)}ms`);
  }
  assert.equal(allRecordings.size,500);
  assert.equal(sqlite.prepare('SELECT COUNT(*) AS count FROM music_served').get().count,500);
  sqlite.close();
});
