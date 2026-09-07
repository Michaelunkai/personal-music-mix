import { test } from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { readFileSync } from 'node:fs';
import worker from '../dist/server/index.js';

function database() {
  const sqlite = new DatabaseSync(':memory:');
  sqlite.exec(readFileSync(new URL('../drizzle/0000_lively_gressill.sql', import.meta.url), 'utf8'));
  const prepare = (sql, args=[]) => ({
    bind(...values) { return prepare(sql,values); },
    async first() { return sqlite.prepare(sql).get(...args) || null; },
    async all() { return { results: sqlite.prepare(sql).all(...args) }; },
    async run() { sqlite.prepare(sql).run(...args); return {success:true}; },
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
  await call('/api/favorites',{...favorite,liked:false});
  assert.deepEqual((await (await call('/api/favorites')).json()).track_keys,[]);
  assert.equal((await call('/api/favorites',favorite,{Origin:'https://untrusted.example'})).status,403);
  assert.equal((await call('/api/favorites',{track_key:'missing',liked:true})).status,404);
  assert.equal((await call('/api/sync/import',{tracks:[]})).status,422);
  assert.equal((await (await call('/api/health')).json()).database.tracks,2);
  const page=await call('/'); assert.equal(page.status,200);
  assert.equal(page.headers.get('Referrer-Policy'),'strict-origin-when-cross-origin');
  assert.match(await page.text(),/data-view="favorites"/);
});
