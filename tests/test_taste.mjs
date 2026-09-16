import { test } from 'node:test';
import assert from 'node:assert/strict';
import { recordingKeyFor } from '../worker/domain.js';
import { rankMusicCandidates } from '../worker/taste.js';

const stream=(id,title,artist,genre='indie rock')=>({
  candidate_key:`audius:${id}`,recording_key:recordingKeyFor(title,artist),provider:'audius',provider_track_id:id,
  title,artist,genre,audio_url:`https://api.audius.co/v1/tracks/${id}/stream?app_name=personal-music-mix`,
});

test('ranking uses observed plays, favorites, listening feedback, artist and genre matches',()=>{
  const seeds=[
    {track_key:'seed:favorite',title:'Night Signal',artist:'Favorite Artist',play_count:12,latest_played_at:'2026-09-15T10:00:00Z',genre:'dream pop',local_favorite:true},
    {track_key:'seed:completed',title:'Long Repeat',artist:'Repeat Artist',play_count:1,latest_played_at:'2026-09-15T10:00:00Z',genre:'indie rock'},
  ];
  const candidates=[stream('favtrack_01','Blue Hour','Favorite Artist','dream pop'),stream('completed_01','Low Tide','Other Artist','indie rock')];
  const origins=[
    {candidate_key:candidates[0].candidate_key,seed_track_key:seeds[0].track_key,relationship:'direct'},
    {candidate_key:candidates[1].candidate_key,seed_track_key:seeds[1].track_key,relationship:'direct'},
  ];
  const baseline=rankMusicCandidates({seeds,favorites:new Set(),candidates,origins,now:Date.parse('2026-09-16T10:00:00Z')});
  assert.equal(baseline[0].track.title,'Blue Hour');
  assert.ok(baseline[0].reasons.some(reason=>reason.includes('Favorite Artist')));
  const completed=rankMusicCandidates({seeds,favorites:new Set(),candidates,origins,now:Date.parse('2026-09-16T10:00:00Z'),feedback:[{
    event_id:'event_completed_0001',track_key:seeds[1].track_key,recording_key:recordingKeyFor(seeds[1].title,seeds[1].artist),provider:'youtube',event:'completed',listened_seconds:190,duration_seconds:190,created_at:'2026-09-16T09:00:00Z',
  }]});
  const lowTide=completed.find(item=>item.track.title==='Low Tide');
  assert.ok(lowTide.score>baseline.find(item=>item.track.title==='Low Tide').score);
  assert.ok(lowTide.reasons.some(reason=>reason.includes('Long Repeat')));
});

test('similar-artist discoveries reserve one fifth, explanations cite lineage, and recording duplicates are removed',()=>{
  const seeds=[{track_key:'seed:root',title:'Roots',artist:'Root Artist',play_count:20,genre:'synth pop',latest_played_at:'2026-09-15T10:00:00Z'}];
  const candidates=[];const origins=[];
  for(let i=0;i<40;i++) {
    const track=stream(`direct_${String(i).padStart(4,'0')}`,`Direct Song ${i}`,`Direct Artist ${i}`,'synth pop');
    candidates.push(track);origins.push({candidate_key:track.candidate_key,seed_track_key:seeds[0].track_key,relationship:'direct'});
  }
  for(let i=0;i<10;i++) {
    const track=stream(`related_${String(i).padStart(4,'0')}`,`Related Song ${i}`,`Related Artist ${i}`,'synth pop');
    candidates.push(track);origins.push({candidate_key:track.candidate_key,seed_track_key:seeds[0].track_key,relationship:'similar_artist'});
  }
  candidates.push(stream('duplicate_001','Direct Song 0','Direct Artist 0'));
  const mix=rankMusicCandidates({seeds,candidates,origins,limit:50,now:Date.parse('2026-09-16T10:00:00Z')});
  assert.equal(mix.length,50);
  assert.equal(new Set(mix.map(item=>item.recording_key)).size,50);
  assert.equal(mix.filter(item=>item.related).length,10);
  assert.ok(mix.filter(item=>item.related).every(item=>item.reasons.some(reason=>reason.includes('similar-artist'))));
  assert.ok(mix.some(item=>item.reasons.some(reason=>reason.includes('synth pop'))));
});

test('served songs, library recordings, explicit dislikes and early-skip signals suppress recommendations',()=>{
  const seeds=[
    {track_key:'seed:root',title:'Roots',artist:'Root Artist',play_count:20,genre:'synth pop'},
    {track_key:'seed:skipped',title:'Skipped Track',artist:'Skip Artist',play_count:4,genre:'synth pop'},
  ];
  const candidates=[stream('skipartist_001','Another Track','Skip Artist'),stream('newartist_001','New Track','New Artist')];
  const origins=candidates.map(track=>({candidate_key:track.candidate_key,seed_track_key:seeds[0].track_key,relationship:'direct'}));
  const now=Date.parse('2026-09-16T10:00:00Z');
  const baseline=rankMusicCandidates({seeds,candidates,origins,now});
  const skipped=rankMusicCandidates({seeds,candidates,origins,now,feedback:[{
    event_id:'event_skip_0000001',track_key:seeds[1].track_key,recording_key:recordingKeyFor(seeds[1].title,seeds[1].artist),provider:'youtube',event:'skipped',listened_seconds:5,duration_seconds:180,created_at:'2026-09-16T09:00:00Z',
  }]});
  assert.ok(skipped.find(item=>item.track.title==='Another Track').score<baseline.find(item=>item.track.title==='Another Track').score);
  const rejected=stream('rejected_01','Rejected Song','No Thanks');
  const disliked=rankMusicCandidates({seeds,candidates:[...candidates,rejected],origins:[...origins,{candidate_key:rejected.candidate_key,seed_track_key:seeds[0].track_key,relationship:'direct'}],now,feedback:[{
    event_id:'event_dislike_00001',track_key:rejected.candidate_key,recording_key:rejected.recording_key,provider:'audius',event:'dislike',listened_seconds:0,duration_seconds:180,created_at:'2026-09-16T09:00:00Z',
  }],excludeRecordingKeys:[candidates[1].recording_key]});
  assert.ok(!disliked.some(item=>item.track.title==='Rejected Song'));
  assert.ok(!disliked.some(item=>item.track.title==='New Track'));
});
