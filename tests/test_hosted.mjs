import { test } from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { readFileSync } from 'node:fs';
import worker from '../dist/server/index.js';
import {normalizeImport,rankTracks} from '../worker/domain.js';

function database() {
  const sqlite = new DatabaseSync(':memory:');
  sqlite.exec(readFileSync(new URL('../drizzle/0000_lively_gressill.sql', import.meta.url), 'utf8'));
  sqlite.exec(readFileSync(new URL('../drizzle/0001_durable_served_ledger.sql', import.meta.url), 'utf8'));
  const prepare = (sql, args=[]) => ({
    bind(...values) { return prepare(sql,values); },
    async first() { return sqlite.prepare(sql).get(...args) || null; },
    async all() { return { results: sqlite.prepare(sql).all(...args) }; },
    async run() { const result=sqlite.prepare(sql).run(...args); return {success:true,meta:{changes:Number(result.changes || 0)}}; },
  });
  return {prepare,async batch(items) {sqlite.exec('BEGIN');try {const results=[];for(const item of items)results.push(await item.run());sqlite.exec('COMMIT');return results;}catch(e){sqlite.exec('ROLLBACK');throw e;}}};
}

test('built hosted app persists import, favorites, refresh and full library', async () => {
  const env = {DB:database()};
  const call = async (path,body,headers={}) => worker.fetch(new Request(`https://example.chatgpt.site${path}`,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json',...headers},body:JSON.stringify(body)}),env);
  const tracks=['Alpha','Zebra'].map((title,i)=>({track_key:`video:${i?'bbbbbbbbbbb':'aaaaaaaaaaa'}`,title,artist:'Artist',video_id:i?'bbbbbbbbbbb':'aaaaaaaaaaa',play_count:1,liked_count:0}));
  assert.equal((await (await call('/api/health')).json()).database.tracks,0);
  assert.equal((await call('/api/sync/import',{tracks})).status,200);
  const favorite={track_key:tracks[1].track_key,liked:true};
  assert.equal((await (await call('/api/favorites',favorite)).json()).saved,true);
  assert.equal((await (await call('/api/recommendations')).json()).items[0].track.title,'Zebra');
  // Repeated bridge imports must not erase a favorite created on the website.
  await call('/api/sync/import',{tracks});
  assert.deepEqual((await (await call('/api/favorites')).json()).track_keys,[tracks[1].track_key]);
  await call('/api/scan',{});
  assert.equal((await (await call('/api/playlists/latest')).json()).plan.requested_count,2);
  assert.equal((await (await call('/api/library')).json()).count,2);
  await call('/api/playlists/preview',{name:'Evening favorites'});
  await call('/api/scan',{});
  assert.equal((await (await call('/api/playlists/latest')).json()).plan.name,'Evening favorites');
  await call('/api/favorites',{...favorite,liked:false});
  assert.deepEqual((await (await call('/api/favorites')).json()).track_keys,[]);
  assert.equal((await call('/api/favorites',favorite,{Origin:'https://untrusted.example'})).status,403);
  assert.equal((await call('/api/favorites',{track_key:'missing',liked:true})).status,404);
  assert.equal((await call('/api/sync/import',{tracks:[]})).status,422);
  assert.equal((await (await call('/api/health')).json()).database.tracks,2);
  const page=await call('/'); assert.equal(page.status,200);
  assert.equal(page.headers.get('Referrer-Policy'),'strict-origin-when-cross-origin');
  assert.match(await page.text(),/data-view="favorites"/);
  // Later local choices sync; an older import cannot overwrite the cloud choice.
  const dated = {...tracks[0],local_favorite:true,local_favorite_updated_at:'2020-01-01T00:00:00Z'};
  await call('/api/sync/import',{tracks:[dated]});
  assert.deepEqual((await (await call('/api/favorites')).json()).track_keys,[dated.track_key]);
  await call('/api/favorites',{track_key:dated.track_key,liked:false});
  await call('/api/sync/import',{tracks:[dated]});
  assert.deepEqual((await (await call('/api/favorites')).json()).track_keys,[]);
  dated.local_favorite_updated_at='2090-01-01T00:00:00Z';
  await call('/api/sync/import',{tracks:[dated]});
  assert.deepEqual((await (await call('/api/favorites')).json()).track_keys,[dated.track_key]);
  await call('/api/favorites',{track_key:dated.track_key,liked:false});
  await call('/api/sync/import',{tracks:[dated]});
  assert.deepEqual((await (await call('/api/favorites')).json()).track_keys,[], 'A website action wins even when the local clock is ahead');
  dated.local_favorite=false; dated.local_favorite_updated_at='2090-01-01T00:00:01Z';
  await call('/api/sync/import',{tracks:[dated]});
  assert.deepEqual((await (await call('/api/favorites')).json()).track_keys,[]);
});

test('fresh discoveries get a reserved share and disappear when unsupported', () => {
  const rows=normalizeImport({tracks:[...Array.from({length:30},(_,i)=>({track_key:'seed'+i,title:'Saved '+i,artist:'A',play_count:100,liked_count:1})),
    ...Array.from({length:8},(_,i)=>({track_key:'new'+i,title:'New '+i,artist:'B',video_id:'b'.repeat(11),source:'favorite_discovery',discovery_seeds:[{track_key:'seed0',title:'My favorite',expires_at:Date.now()/1000+300}]}))]});
  const items=rankTracks(rows,new Set());
  assert.equal(items.length,20);
  assert.equal(items.filter(row=>row.source==='favorite_discovery').length,6);
  assert.ok(items.filter(row=>row.source==='favorite_discovery').every(row=>row.reasons.includes('recommended from your favorite: My favorite') && !row.reasons.some(reason=>reason.includes('history'))));
  rows[0].liked_count=0;
  assert.equal(rankTracks(rows,new Set()).filter(row=>row.source==='favorite_discovery').length,0);
  assert.ok(rankTracks(rows,new Set(['new0']),100).some(row=>row.track.track_key==='new0'));
});

test('hosted refresh queues discovery and only acknowledged delivery clears pending',async()=>{
  const env={DB:database()};
  const call=(path,payload)=>worker.fetch(new Request('https://test.chatgpt.site'+path,payload===undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}),env);
  const seed={track_key:'seed',title:'Seed',artist:'A',video_id:'a'.repeat(11),play_count:10};
  const candidate={track_key:'new',title:'Discovery',artist:'B',video_id:'b'.repeat(11),source:'favorite_discovery',discovery_seeds:[{track_key:'seed',title:'Seed',expires_at:Date.now()/1000+300}]};
  await call('/api/sync/import',{tracks:[seed]});
  await call('/api/favorites',{track_key:'seed',liked:true});
  await call('/api/sync/import',{tracks:[seed,candidate]});
  assert.equal((await (await call('/api/recommendations')).json()).count,2);
  const activePreview=await (await call('/api/playlists/preview',{name:'Before expiry'})).json();
  assert.deepEqual(activePreview.plan.items.map(track=>track.track_key),['new']);
  const originalNow=Date.now;
  try {
    Date.now=()=>originalNow()+600000;
    const expiredPreview=await (await call('/api/playlists/preview',{name:'After expiry'})).json();
    assert.equal(expiredPreview.plan.items.length,0);
    assert.equal(expiredPreview.plan.requested_count,0);
    assert.equal(expiredPreview.plan.name,'After expiry');
  } finally {Date.now=originalNow;}
  await call('/api/scan',{});
  const first=(await (await call('/api/favorites')).json()).discovery_request;
  assert.ok(first.id);
  assert.equal((await (await call('/api/connection')).json()).discovery.pending,true);
  await call('/api/sync/heartbeat',{discovery:{state:'temporarily_unavailable',request_id:null,candidate_count:1,seed_count:1,checked_at:new Date().toISOString()}});
  const retrying=await (await call('/api/connection')).json();
  assert.equal(retrying.discovery.pending,true);
  assert.match(retrying.message,/temporarily unavailable/);
  await call('/api/sync/heartbeat',{discovery:{state:'updated',request_id:first.id,candidate_count:1,seed_count:1,checked_at:new Date().toISOString()}});
  const connection=await (await call('/api/connection')).json();
  assert.equal(connection.companion.online,true);
  assert.equal(connection.browser_bridge.ready,false);
  assert.equal(connection.discovery.pending,false);
  await call('/api/scan',{});
  assert.notEqual((await (await call('/api/favorites')).json()).discovery_request.id,first.id);
  await call('/api/favorites',{track_key:'seed',liked:false});
  assert.equal((await (await call('/api/recommendations')).json()).count,1);
  await call('/api/favorites',{track_key:'seed',liked:true});
  candidate.discovery_seeds[0].expires_at=Date.now()/1000-1;
  await call('/api/sync/import',{tracks:[candidate]});
  assert.equal((await (await call('/api/recommendations')).json()).count,1);
  assert.equal((await (await call('/api/playlists/latest')).json()).plan.requested_count,1);
});

test('chunked hosted imports rebuild once after the final chunk', async()=>{
  const env={DB:database()};
  const call=(path,body)=>worker.fetch(new Request(`https://chunks.chatgpt.site${path}`,body===undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),env);
  const seed={track_key:'seed',title:'Most Played',artist:'A',video_id:'a'.repeat(11),play_count:20,liked_count:0};
  const candidate={track_key:'candidate',title:'Fresh Candidate',artist:'B',video_id:'b'.repeat(11),play_count:0,liked_count:0,source:'favorite_discovery',discovery_seeds:[{track_key:'seed',title:'Most Played',seed_kind:'most_listened',play_count:20,liked:false,expires_at:Date.now()/1000+3600}]};
  const first=await (await call('/api/sync/import',{tracks:[seed],defer_rebuild:true})).json();
  assert.equal(first.status,'completed');
  assert.equal(first.rebuild_deferred,true);
  assert.equal((await (await call('/api/recommendations?unheard_only=1')).json()).count,0);
  const final=await (await call('/api/sync/import',{tracks:[candidate],defer_rebuild:false})).json();
  assert.equal(final.status,'completed');
  const fresh=await (await call('/api/recommendations?unheard_only=1')).json();
  assert.deepEqual(fresh.items.map(item=>item.track.track_key),['candidate']);
  assert.equal((await (await call('/api/playlists/latest?unheard_only=1')).json()).plan.requested_count,1);
});

test('favorite artists influence other library songs and zero-play favorites are valid', async () => {
  const {rankTracks}=await import('../worker/domain.js');
  const rows=[{track_key:'a',title:'Favorite',artist:'A',play_count:0,liked_count:0},{track_key:'b',title:'Another A',artist:'A',play_count:1,liked_count:0},{track_key:'c',title:'Another C',artist:'C',play_count:1,liked_count:0}];
  const plain=rankTracks(rows,new Set());
  const favored=rankTracks(rows,new Set(['a']));
  assert.ok(favored.find(row=>row.track.track_key==='b').score > plain.find(row=>row.track.track_key==='b').score || favored.find(row=>row.track.track_key==='b').score > favored.find(row=>row.track.track_key==='c').score);
  assert.ok(Number.isFinite(rankTracks([rows[0]],new Set(['a']))[0].score));
});

test('fresh dashboard mode only returns unseen playable discoveries and clears an exhausted batch', async () => {
  const env={DB:database()};
  const request = (path,body) => worker.fetch(new Request(`https://fresh.chatgpt.site${path}`, {
    ...(body === undefined ? {} : {method:'POST',body:JSON.stringify(body)}),
    headers:{'Content-Type':'application/json','X-Mix-Mode':'fresh'},
  }),env);
  const expiry=Date.now()/1000+3600;
  const seed={track_key:'seed-top',title:'Most Played',artist:'A',video_id:'aaaaaaaaaaa',play_count:40,liked_count:0};
  const candidate=(key,id)=>({track_key:key,title:key,artist:'A',video_id:id,play_count:0,liked_count:0,source:'favorite_discovery',discovery_seeds:[{track_key:seed.track_key,title:seed.title,seed_kind:'most_listened',play_count:40,liked:false,expires_at:expiry}]});
  await request('/api/sync/import',{tracks:[seed,candidate('new-one','bbbbbbbbbbb'),candidate('new-two','ccccccccccc')]});
  const first=await (await request('/api/recommendations')).json();
  assert.deepEqual(first.items.map(item=>item.track.track_key),['new-one','new-two']);
  assert.ok(first.items.every(item=>item.track.play_count===0 && item.reasons.some(reason=>reason.includes('listen to Most Played often'))));
  const exhausted=await (await request('/api/scan',{})).json();
  assert.deepEqual(exhausted.recommendations,[]);
  assert.equal(exhausted.preserved_previous_mix,false);
  assert.equal(exhausted.playlist_preview.requested_count,0);
  assert.deepEqual(exhausted.playlist_preview.items,[]);
  assert.match(exhausted.run.message,/No new unseen songs are available yet/);
  const afterExhaustion=await (await request('/api/recommendations')).json();
  assert.equal(afterExhaustion.count,0);
  assert.deepEqual(afterExhaustion.items,[]);
  await request('/api/sync/import',{tracks:[candidate('new-three','ddddddddddd')]});
  const second=await (await request('/api/recommendations')).json();
  assert.deepEqual(second.items.map(item=>item.track.track_key),['new-three']);
  const firstKeys=new Set(first.items.map(item=>item.track.track_key));
  assert.ok(second.items.every(item=>!firstKeys.has(item.track.track_key)));
});

test('refresh replaces a full fresh mix with an empty visible batch when no candidates arrive', async () => {
  const env={DB:database()};
  const request=(path,body)=>worker.fetch(new Request(`https://preserve.chatgpt.site${path}`,{
    ...(body===undefined?{}:{method:'POST',body:JSON.stringify(body)}),
    headers:{'Content-Type':'application/json','X-Mix-Mode':'fresh'},
  }),env);
  const expiry=Date.now()/1000+3600;
  const seed={track_key:'preserve-seed',title:'Most Played Seed',artist:'Signal Artist',video_id:'aaaaaaaaaaa',play_count:50,liked_count:0};
  const candidates=Array.from({length:20},(_,index)=>({
    track_key:`preserve-${index}`,
    title:`Fresh Candidate ${index}`,
    artist:`Discovery Artist ${index%2}`,
    video_id:String(index).padStart(11,'0'),
    play_count:0,
    liked_count:0,
    source:'favorite_discovery',
    discovery_seeds:[{track_key:seed.track_key,title:seed.title,seed_kind:'most_listened',play_count:seed.play_count,liked:false,expires_at:expiry}],
  }));
  const imported=await (await request('/api/sync/import',{tracks:[seed,...candidates]})).json();
  assert.equal(imported.status,'completed');
  assert.equal(imported.recommendations.length,20);
  const firstKeys=new Set(imported.recommendations.map(item=>item.track.track_key));
  assert.equal(firstKeys.size,20);

  const refreshed=await (await request('/api/scan',{})).json();
  assert.deepEqual(refreshed.recommendations,[]);
  assert.equal(refreshed.preserved_previous_mix,false);
  assert.equal(refreshed.playlist_preview.requested_count,0);
  assert.deepEqual(refreshed.playlist_preview.items,[]);
  assert.match(refreshed.run.message,/No new unseen songs are available yet/);

  const recommendations=await (await request('/api/recommendations')).json();
  assert.equal(recommendations.count,0);
  assert.deepEqual(recommendations.items,[]);
  const latest=await (await request('/api/playlists/latest')).json();
  assert.equal(latest.plan.requested_count,0);
  assert.deepEqual(latest.plan.items,[]);
  assert.ok([...firstKeys].every(key=>!latest.plan.items.some(item=>item.track_key===key)));
});

test('refresh replaces a full fresh mix with a smaller unseen batch without overlap', async () => {
  const env={DB:database()};
  const request=(path,body)=>worker.fetch(new Request(`https://favorite-preserve.chatgpt.site${path}`,{
    ...(body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),
    headers:{'Content-Type':'application/json','X-Mix-Mode':'fresh'},
  }),env);
  const expiry=Date.now()/1000+3600;
  const favoriteSeed={track_key:'favorite-root',title:'Pinned Favorite',artist:'Favorite Artist',video_id:'fffffffffff',play_count:0,liked_count:1};
  const favoriteItems=Array.from({length:20},(_,index)=>({track_key:`favorite-${index}`,title:`Favorite discovery ${index}`,artist:'Discovery Artist',video_id:String(index).padStart(11,'0'),play_count:0,liked_count:0,source:'favorite_discovery',discovery_seeds:[{track_key:favoriteSeed.track_key,title:favoriteSeed.title,seed_kind:'favorite',play_count:0,liked:true,expires_at:expiry}]}));
  const first=await (await request('/api/sync/import',{tracks:[favoriteSeed,...favoriteItems]})).json();
  assert.equal(first.recommendations.length,20);
  const listeningSeed={track_key:'listening-root',title:'Most Played',artist:'Listening Artist',video_id:'ggggggggggg',play_count:50,liked_count:0};
  const listeningItems=Array.from({length:9},(_,index)=>({track_key:`listening-${index}`,title:`Listening discovery ${index}`,artist:'Other Artist',video_id:String(index+30).padStart(11,'0'),play_count:0,liked_count:0,source:'favorite_discovery',discovery_seeds:[{track_key:listeningSeed.track_key,title:listeningSeed.title,seed_kind:'most_listened',play_count:50,liked:false,expires_at:expiry}]}));
  const second=await (await request('/api/sync/import',{tracks:[listeningSeed,...listeningItems]})).json();
  const firstKeys=new Set(first.recommendations.map(item=>item.track.track_key));
  const secondKeys=new Set(second.recommendations.map(item=>item.track.track_key));
  assert.equal(firstKeys.size,20);
  assert.equal(second.preserved_previous_mix,false);
  assert.equal(second.recommendations.length,listeningItems.length);
  assert.deepEqual([...secondKeys].sort(),listeningItems.map(item=>item.track_key).sort());
  assert.equal(second.playlist_preview.requested_count,listeningItems.length);
  assert.deepEqual(new Set(second.playlist_preview.items.map(item=>item.track_key)),secondKeys);
  assert.ok([...secondKeys].every(key=>!firstKeys.has(key)));
  assert.match(second.run.message,/9 songs are new/);
});

test('repeated fresh refreshes consume a finite pool without recycling and end empty', async () => {
  const env={DB:database()};
  const request=(path,body)=>worker.fetch(new Request(`https://finite-pool.chatgpt.site${path}`,
    body===undefined ? {headers:{'X-Mix-Mode':'fresh'}} : {method:'POST',body:JSON.stringify(body),headers:{'Content-Type':'application/json','X-Mix-Mode':'fresh'}}),env);
  const expiry=Date.now()/1000+3600;
  const seed={track_key:'finite-seed',title:'Most Played Seed',artist:'Signal Artist',video_id:'aaaaaaaaaaa',play_count:50,liked_count:0};
  const candidates=Array.from({length:45},(_,index)=>({
    track_key:`finite-${index}`,
    title:`Finite Candidate ${index}`,
    artist:'Discovery Artist',
    video_id:String(index+100).padStart(11,'0'),
    play_count:0,
    liked_count:0,
    source:'favorite_discovery',
    discovery_seeds:[{track_key:seed.track_key,title:seed.title,seed_kind:'most_listened',play_count:seed.play_count,liked:false,expires_at:expiry}],
  }));
  const imported=await (await request('/api/sync/import',{tracks:[seed,...candidates],defer_rebuild:true})).json();
  assert.equal(imported.status,'completed');
  assert.equal(imported.rebuild_deferred,true);

  const batches=[];
  const seen=new Set();
  for(let refreshIndex=0;refreshIndex<4;refreshIndex++) {
    const refreshed=await (await request('/api/scan',{})).json();
    const keys=refreshed.recommendations.map(item=>item.track.track_key);
    assert.equal(new Set(keys).size,keys.length);
    assert.ok(keys.every(key=>!seen.has(key)),`refresh ${refreshIndex+1} recycled a track`);
    assert.equal(refreshed.preserved_previous_mix,false);
    assert.deepEqual(refreshed.playlist_preview.items.map(item=>item.track_key),keys);
    keys.forEach(key=>seen.add(key));
    batches.push(keys);
  }

  assert.deepEqual(batches.map(batch=>batch.length),[20,20,5,0]);
  assert.equal(seen.size,candidates.length);
  assert.deepEqual(batches[3],[]);
  const finalRefresh=await (await request('/api/recommendations')).json();
  assert.equal(finalRefresh.count,0);
  assert.deepEqual(finalRefresh.items,[]);
});

test('fresh refreshes deduplicate video IDs and reject timestamp-heard songs', async () => {
  const env={DB:database()};
  const request=(path,body)=>worker.fetch(new Request(`https://video-identity.chatgpt.site${path}`,{
    ...(body===undefined?{}:{method:'POST',body:JSON.stringify(body)}),
    headers:{'Content-Type':'application/json','X-Mix-Mode':'fresh'},
  }),env);
  const expiry=Date.now()/1000+3600;
  const seed={track_key:'identity-seed',title:'Most Played Seed',artist:'Signal Artist',video_id:'aaaaaaaaaaa',play_count:50,liked_count:0};
  const discovery=(key,title,videoId,extra={})=>({track_key:key,title,artist:'Discovery Artist',video_id:videoId,play_count:0,liked_count:0,source:'favorite_discovery',discovery_seeds:[{track_key:seed.track_key,title:seed.title,seed_kind:'most_listened',play_count:seed.play_count,liked:false,expires_at:expiry}],...extra});
  await request('/api/sync/import',{tracks:[seed,
    discovery('duplicate-a','Duplicate A','bbbbbbbbbbb'),
    discovery('duplicate-b','Duplicate B','bbbbbbbbbbb'),
    discovery('timestamp-heard','Timestamp Heard','ccccccccccc',{latest_played_at:new Date().toISOString()}),
    discovery('malformed-timestamp','Malformed Timestamp','eeeeeeeeeee',{latest_played_at:'not-a-date'}),
    {track_key:'related-song',title:'Related Song',artist:'Related Artist',video_id:'ddddddddddd',play_count:0,liked_count:0,source:'related'},
  ],defer_rebuild:true});
  const first=await (await request('/api/scan',{})).json();
  assert.deepEqual(first.recommendations.map(item=>item.track.track_key),['duplicate-a','related-song']);
  assert.equal(new Set(first.recommendations.map(item=>item.track.video_id)).size,first.recommendations.length);
  const visible=await (await request('/api/recommendations')).json();
  assert.deepEqual(visible.items.map(item=>item.track.track_key),['duplicate-a','related-song']);
  const second=await (await request('/api/scan',{})).json();
  assert.deepEqual(second.recommendations,[]);
});

test('concurrent fresh refreshes reserve disjoint batches', async () => {
  const env={DB:database()};
  const request=(path,body)=>worker.fetch(new Request(`https://concurrent.chatgpt.site${path}`,{
    ...(body===undefined?{}:{method:'POST',body:JSON.stringify(body)}),
    headers:{'Content-Type':'application/json','X-Mix-Mode':'fresh'},
  }),env);
  const expiry=Date.now()/1000+3600;
  const seed={track_key:'concurrent-seed',title:'Most Played Seed',artist:'Signal Artist',video_id:'aaaaaaaaaaa',play_count:50,liked_count:0};
  const candidates=Array.from({length:40},(_,index)=>({track_key:`concurrent-${index}`,title:`Concurrent Candidate ${index}`,artist:'Discovery Artist',video_id:String(index+100).padStart(11,'0'),play_count:0,liked_count:0,source:'favorite_discovery',discovery_seeds:[{track_key:seed.track_key,title:seed.title,seed_kind:'most_listened',play_count:seed.play_count,liked:false,expires_at:expiry}]}));
  await request('/api/sync/import',{tracks:[seed,...candidates],defer_rebuild:true});
  const responses=await Promise.all([request('/api/scan',{}),request('/api/scan',{})]);
  const batches=await Promise.all(responses.map(response=>response.json()));
  const keys=batches.map(batch=>batch.recommendations.map(item=>item.track.track_key));
  assert.ok(keys.every(batch=>batch.length>0));
  assert.equal(keys[0].filter(key=>keys[1].includes(key)).length,0);
  assert.equal(new Set(keys.flat()).size,40);
});

test('served-key synchronization does not evict older exclusions', async () => {
  const env={DB:database()};
  const request=(path,body)=>worker.fetch(new Request(`https://ledger-capacity.chatgpt.site${path}`,{
    method:'POST',body:JSON.stringify(body),headers:{'Content-Type':'application/json','X-Mix-Mode':'fresh'},
  }),env);
  const older=Array.from({length:100000},(_,index)=>`served-${index}`);
  assert.equal((await (await request('/api/sync/ledger',{served_keys:older})).json()).served_keys,100000);
  assert.equal((await (await request('/api/sync/ledger',{served_keys:['newest-served']})).json()).served_keys,100001);
});

test('hosted served ledger excludes songs already shown by the local publisher', async () => {
  const env={DB:database()};
  const request=(path,body)=>worker.fetch(new Request(`https://ledger.chatgpt.site${path}`,{
    ...(body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),
    headers:{'Content-Type':'application/json','X-Mix-Mode':'fresh'},
  }),env);
  const expiry=Date.now()/1000+3600;
  const seed={track_key:'ledger-seed',title:'Most Played',artist:'A',video_id:'aaaaaaaaaaa',play_count:20,liked_count:0};
  const candidate=(key,id)=>({track_key:key,title:key,artist:'B',video_id:id,play_count:0,liked_count:0,source:'favorite_discovery',discovery_seeds:[{track_key:seed.track_key,title:seed.title,seed_kind:'most_listened',play_count:20,liked:false,expires_at:expiry}]});
  await request('/api/sync/import',{tracks:[seed,candidate('already-local','bbbbbbbbbbb'),candidate('still-fresh','ccccccccccc')],defer_rebuild:true});
  const ledger=await (await request('/api/sync/ledger',{served_keys:['already-local']})).json();
  assert.equal(ledger.status,'completed');
  const rebuilt=await (await request('/api/sync/import',{tracks:[seed,candidate('already-local','bbbbbbbbbbb'),candidate('still-fresh','ccccccccccc')]})).json();
  assert.deepEqual(rebuilt.recommendations.map(item=>item.track.track_key),['still-fresh']);
});

test('fresh recommendations prioritize explicit favorite seeds', async () => {
  const env={DB:database()};
  const request=(path,body)=>worker.fetch(new Request(`https://favorite.chatgpt.site${path}`,{
    ...(body===undefined?{}:{method:'POST',body:JSON.stringify(body)}),
    headers:{'Content-Type':'application/json','X-Mix-Mode':'fresh'},
  }),env);
  const expiry=Date.now()/1000+3600;
  const favoriteSeed={track_key:'favorite-seed',title:'Pinned Favorite',artist:'Fav Artist',video_id:'fffffffffff',play_count:0,liked_count:1};
  const listenedSeed={track_key:'listened-seed',title:'Most Played',artist:'Played Artist',video_id:'ggggggggggg',play_count:40,liked_count:0};
  const candidate=(key,id,seed,kind,liked=false)=>({track_key:key,title:key,artist:'Discovery Artist',video_id:id,play_count:0,liked_count:0,source:'favorite_discovery',discovery_seeds:[{track_key:seed.track_key,title:seed.title,seed_kind:kind,play_count:seed.play_count,liked,expires_at:expiry}]});
  await request('/api/sync/import',{tracks:[favoriteSeed,listenedSeed,candidate('from-favorite','hhhhhhhhhhh',favoriteSeed,'favorite',true),candidate('from-history','iiiiiiiiiii',listenedSeed,'most_listened')]});
  const fresh=await (await request('/api/recommendations')).json();
  assert.equal(fresh.items[0].track.track_key,'from-favorite');
  assert.ok(fresh.items[0].reasons.includes('recommended from your favorite: Pinned Favorite'));
  assert.ok(fresh.items.every(item=>item.track.play_count===0 && item.source==='favorite_discovery'));
});

test('hosted fresh mix keeps candidates from rotated listening seeds', async () => {
  const env={DB:database()};
  const request=(path,body)=>worker.fetch(new Request(`https://rotated.chatgpt.site${path}`,{
    ...(body===undefined?{}:{method:'POST',body:JSON.stringify(body)}),
    headers:{'Content-Type':'application/json','X-Mix-Mode':'fresh'},
  }),env);
  const expiry=Date.now()/1000+3600;
  const seeds=Array.from({length:10},(_,index)=>({
    track_key:`seed-${index}`,
    title:`Top seed ${index}`,
    artist:'Top artist',
    video_id:String(index).padStart(11,'0'),
    play_count:10,
    liked_count:0,
  }));
  const rotated={track_key:'seed-rotated',title:'Rotated seed',artist:'Rotated artist',video_id:'99999999999',play_count:1,liked_count:0};
  const candidate={track_key:'candidate-rotated',title:'Rotated discovery',artist:'Discovery artist',video_id:'88888888888',play_count:0,liked_count:0,source:'favorite_discovery',discovery_seeds:[{track_key:rotated.track_key,title:rotated.title,seed_kind:'most_listened',play_count:1,liked:false,expires_at:expiry}]};
  await request('/api/sync/import',{tracks:[...seeds,rotated,candidate]});
  const fresh=await (await request('/api/recommendations')).json();
  assert.deepEqual(fresh.items.map(item=>item.track.track_key),[candidate.track_key]);
  assert.ok(fresh.items[0].reasons.some(reason=>reason.includes('Rotated seed')));
});
