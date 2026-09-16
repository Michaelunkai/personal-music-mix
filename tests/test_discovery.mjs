import { test } from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { readFileSync } from 'node:fs';
import { catalogStatus, CATALOG_REFILL_THRESHOLD, CATALOG_RESERVE_TARGET, refillCatalog } from '../worker/discovery.js';
import { recordingKeyFor } from '../worker/domain.js';

function database(rows) {
  const sqlite=new DatabaseSync(':memory:');
  for(const name of ['0000_lively_gressill.sql','0001_durable_served_ledger.sql','0002_thick_praxagora.sql','0003_curly_xorn.sql','0004_conscious_boom_boom.sql']) {
    sqlite.exec(readFileSync(new URL(`../drizzle/${name}`,import.meta.url),'utf8').split('--> statement-breakpoint').join('\n'));
  }
  for(const row of rows) sqlite.prepare('INSERT INTO music_library(track_key,payload) VALUES(?,?)').run(row.track_key,JSON.stringify(row));
  const prepare=(sql,args=[])=>({
    bind(...values){return prepare(sql,values);},
    async first(){return sqlite.prepare(sql).get(...args)||null;},
    async all(){return{results:sqlite.prepare(sql).all(...args)};},
    async run(){const result=sqlite.prepare(sql).run(...args);return{success:true,meta:{changes:Number(result.changes||0)}};},
  });
  const db={prepare,async batch(items){sqlite.exec('BEGIN');try{const results=[];for(const item of items)results.push(await item.run());sqlite.exec('COMMIT');return results;}catch(error){sqlite.exec('ROLLBACK');throw error;}}};
  return{db,sqlite};
}

function providerFetch({gate}={}) {
  const requests=[];
  const fetchImpl=async(url,options={})=>{
    const parsed=new URL(url); requests.push({host:parsed.host,path:parsed.pathname,query:parsed.searchParams.get('query')||'',userAgent:options.headers?.['User-Agent']});
    if(parsed.host==='musicbrainz.org') return {ok:true,status:200,json:async()=>({artists:[{id:'11111111-1111-4111-8111-111111111111',name:parsed.searchParams.get('query')?.replace(/^artist:"|"$/g,''),tags:[{name:'synth pop',count:12}]} ]})};
    if(parsed.host==='api.listenbrainz.org') return {ok:true,status:200,json:async()=>({payload:{artist_name:'Signal Artist',artist_map:{
      '11111111-1111-4111-8111-111111111111':{artist_name:'Signal Artist'},
      '22222222-2222-4222-8222-222222222222':{artist_name:'Neighbor Artist'},
    }}})};
    if(parsed.host==='api.audius.co') {
      if(gate) await gate;
      const artist=parsed.searchParams.get('query');
      const id=artist==='Neighbor Artist'?'neighbor_123':'signal_123';
      const title=artist==='Neighbor Artist'?'Moon Return':'Afterlight';
      return {ok:true,status:200,json:async()=>({data:[{id,title,user:{name:artist},genre:'Electronic',mood:'Reflective',duration:182,is_streamable:true,artwork:{'150x150':'https://api.audius.co/cover.jpg'}}]})};
    }
    throw new Error(`Unexpected provider URL ${url}`);
  };
  return{fetchImpl,requests};
}

test('hosted replenishment discovers full Audius tracks from taste and ListenBrainz artist relationships',async()=>{
  const{db,sqlite}=database([{track_key:'video:seed0000001',title:'A song I know',artist:'Signal Artist',play_count:5,liked_count:0,latest_played_at:'2026-09-15T08:00:00Z'}]);
  const provider=providerFetch();let clock=Date.parse('2026-09-16T10:00:00.000Z');
  const result=await refillCatalog(db,{fetchImpl:provider.fetchImpl,now:()=>clock,wait:async ms=>{clock+=ms;},maxSeeds:1});
  assert.equal(result.started,true);
  assert.equal(result.state,'ready');
  assert.equal(result.saved_count,2);
  assert.equal((await catalogStatus(db,clock)).available,2);
  assert.ok(provider.requests.some(row=>row.host==='musicbrainz.org'&&row.userAgent?.includes('personal-music-mix')));
  assert.ok(provider.requests.some(row=>row.host==='api.listenbrainz.org'));
  const rows=sqlite.prepare('SELECT candidate_key,title,artist,genre,seed_keys,audio_url FROM music_candidates ORDER BY candidate_key').all();
  assert.deepEqual(rows.map(row=>row.artist).sort(),['Neighbor Artist','Signal Artist']);
  assert.ok(rows.every(row=>row.seed_keys==='["video:seed0000001"]'&&row.audio_url.startsWith('https://api.audius.co/v1/tracks/')));
  assert.ok(rows.find(row=>row.artist==='Signal Artist').genre.includes('synth pop'));
  const origins=sqlite.prepare('SELECT o.seed_track_key,o.relationship,c.artist FROM music_candidate_origins o JOIN music_candidates c USING(candidate_key)').all();
  assert.ok(origins.some(row=>row.artist==='Signal Artist'&&row.relationship==='direct'));
  assert.ok(origins.some(row=>row.artist==='Neighbor Artist'&&row.relationship==='similar_artist'));
  assert.equal(result.target,CATALOG_RESERVE_TARGET);
  assert.equal(result.refill_threshold,CATALOG_REFILL_THRESHOLD);
  sqlite.close();
});

test('catalog excludes titles already in the library and never admits a preview or gated stream',async()=>{
  const{db,sqlite}=database([{track_key:'video:seed0000001',title:'A song I know',artist:'Signal Artist',play_count:5},{track_key:'video:known0000001',title:'Afterlight',artist:'Signal Artist',play_count:0}]);
  const provider=providerFetch();
  const result=await refillCatalog(db,{fetchImpl:provider.fetchImpl,now:()=>Date.parse('2026-09-16T10:00:00Z'),wait:async()=>{},maxSeeds:1});
  assert.equal(result.saved_count,1);
  assert.equal(sqlite.prepare('SELECT COUNT(*) AS count FROM music_candidates WHERE title=?').get('Afterlight').count,0);
  sqlite.close();
});

test('refill continues when the reserve is above its low-water mark but below target',async()=>{
  const{db,sqlite}=database([{track_key:'video:seed0000001',title:'A song I know',artist:'Signal Artist',play_count:5}]);
  const insert=sqlite.prepare(`INSERT INTO music_candidates(candidate_key,recording_key,provider,provider_track_id,title,artist,audio_url,provider_url,seed_keys,discovered_at)
    VALUES(?,?,?,?,?,?,?,?,?,?)`);
  for(let i=0;i<201;i++) insert.run(`audius:existing${i}`,`recording:existing:${i}`,'audius',`existing${i}`,`Existing ${i}`,'Existing Artist',
    `https://api.audius.co/v1/tracks/existing${i}/stream?app_name=personal-music-mix`,`https://api.audius.co/v1/tracks/existing${i}`,'[]','2026-09-16T10:00:00.000Z');
  const provider=providerFetch();
  const clock=Date.parse('2026-09-16T10:00:00Z');
  const result=await refillCatalog(db,{fetchImpl:provider.fetchImpl,now:()=>clock,wait:async()=>{},maxSeeds:1});
  assert.equal(result.started,true);
  assert.equal(result.saved_count,2);
  assert.equal(result.available,203);
  sqlite.close();
});

test('explicit feedback on a provider track becomes a future discovery seed',async()=>{
  const{db,sqlite}=database([{track_key:'video:seed0000001',title:'A song I know',artist:'Signal Artist',play_count:5}]);
  const recordingKey=recordingKeyFor('Fresh Favorite','Audius Artist');
  sqlite.prepare(`INSERT INTO music_candidates(candidate_key,recording_key,provider,provider_track_id,title,artist,album,genre,mood,audio_url,provider_url,seed_keys,discovered_at)
    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)`).run('audius:freshfavorite01',recordingKey,'audius','freshfavorite01','Fresh Favorite','Audius Artist','','indie rock','',
      'https://api.audius.co/v1/tracks/freshfavorite01/stream?app_name=personal-music-mix','https://api.audius.co/v1/tracks/freshfavorite01','[]','2026-09-16T10:00:00.000Z');
  sqlite.prepare(`INSERT INTO music_feedback(event_id,track_key,recording_key,provider,event,listened_seconds,duration_seconds,created_at)
    VALUES(?,?,?,?,?,?,?,?)`).run('event_favorite_000001','audius:freshfavorite01',recordingKey,'audius','like',0,180,'2026-09-16T10:00:00.000Z');
  const provider=providerFetch();
  const result=await refillCatalog(db,{fetchImpl:provider.fetchImpl,now:()=>Date.parse('2026-09-16T10:01:00Z'),wait:async()=>{},maxSeeds:1});
  assert.equal(result.started,true);
  assert.ok(provider.requests.some(row=>row.host==='api.audius.co'&&row.query==='Audius Artist'));
  sqlite.close();
});

test('provider cooldown avoids repeated failed lookups and reserves refill work across callers',async()=>{
  const{db,sqlite}=database([{track_key:'video:seed0000001',title:'A song I know',artist:'Signal Artist',play_count:5}]);
  const instant=Date.parse('2026-09-16T10:00:00Z');let entered;const enteredPromise=new Promise(resolve=>{entered=resolve;});let release;const gate=new Promise(resolve=>{release=resolve;});
  const provider=providerFetch({gate});
  const first=refillCatalog(db,{fetchImpl:async(...args)=>{entered();return provider.fetchImpl(...args);},now:()=>instant,wait:async()=>{},maxSeeds:1});
  await enteredPromise;
  const second=await refillCatalog(db,{fetchImpl:provider.fetchImpl,now:()=>instant,wait:async()=>{},maxSeeds:1});
  assert.equal(second.started,false);
  release();
  const done=await first;
  assert.equal(done.started,true);
  assert.equal(provider.requests.filter(row=>row.host==='musicbrainz.org').length,1);
  const offline=async()=>({ok:false,status:503,json:async()=>({})});
  const failed=await refillCatalog(db,{fetchImpl:offline,now:()=>instant+30000,wait:async()=>{},maxSeeds:1});
  assert.equal(failed.started,true);
  const suppressed=await refillCatalog(db,{fetchImpl:offline,now:()=>instant+30001,wait:async()=>{},maxSeeds:1});
  assert.equal(suppressed.started,false);
  sqlite.close();
});
