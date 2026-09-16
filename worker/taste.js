import { recordingKeyFor } from './domain.js';

const fold = value => String(value || '').normalize('NFKC').trim().toLocaleLowerCase('en-US');
const genresOf = value => [...new Set(String(value || '').split(/[·,;/|]+/).map(item=>fold(item)).filter(Boolean))];
const usableTimestamp = value => {
  const parsed=Date.parse(value || '');
  return Number.isFinite(parsed) ? parsed : null;
};

function feedbackSignals(events, now) {
  let likes=0, dislikes=0, completions=0, bestProgress=0, strongestSkip=0;
  for(const event of events) {
    const created=usableTimestamp(event.created_at);
    if(created && now-created>365*86400000) continue;
    if(event.event==='like') likes++;
    if(event.event==='dislike') dislikes++;
    if(event.event==='completed') completions++;
    if(event.event==='play_progress') {
      const duration=Number(event.duration_seconds);
      const progress=duration>0 ? Math.min(1,Number(event.listened_seconds||0)/duration) : 0;
      bestProgress=Math.max(bestProgress,progress);
    }
    if(event.event==='skipped') {
      const duration=Number(event.duration_seconds);
      const progress=duration>0 ? Math.min(1,Number(event.listened_seconds||0)/duration) : 0;
      if(progress<.65) strongestSkip=Math.max(strongestSkip,1-progress);
    }
  }
  return {likes,dislikes,completions,bestProgress,strongestSkip};
}

function seedWeight(row, favorites, events, now) {
  const signals=feedbackSignals(events,now);
  const isFavorite=favorites.has(row.track_key)||Boolean(row.local_favorite)||Number(row.liked_count)>0||Number(row.like_events)>0||signals.likes>0;
  const playCount=Math.max(0,Number(row.play_count)||0);
  const played=usableTimestamp(row.latest_played_at);
  const feedbackAt=Math.max(0,...events.map(event=>usableTimestamp(event.created_at)||0));
  const latestEvidence=Math.max(played||0,feedbackAt);
  const recency=latestEvidence ? Math.exp(-Math.max(0,now-latestEvidence)/(60*86400000)) : 0;
  const history=Math.min(4,Math.log1p(playCount)*1.1);
  const positive=Math.min(5,signals.likes*3+signals.completions*1.2+Math.max(0,signals.bestProgress-.2)*1.4);
  const negative=Math.min(4,signals.strongestSkip*2.4);
  return {isFavorite,playCount,recency,weight:Math.max(.15,(isFavorite?5:0)+history+recency+positive-negative)};
}

function feedbackByRecording(events) {
  const map=new Map();
  for(const event of events||[]) {
    const key=String(event.recording_key||'');
    if(!key) continue;
    const list=map.get(key)||[];list.push(event);map.set(key,list);
  }
  return map;
}

function originMap(origins) {
  const map=new Map();
  for(const item of origins||[]) {
    const list=map.get(item.candidate_key)||[];
    list.push({seed_track_key:item.seed_track_key,relationship:item.relationship==='similar_artist'?'similar_artist':'direct'});
    map.set(item.candidate_key,list);
  }
  return map;
}

function selectDiverse(pool, count, selected, artistCounts) {
  const available=pool.filter(item=>!selected.has(item.track.candidate_key));
  const firstPass=available.filter(item=>(artistCounts.get(fold(item.track.artist))||0)<2);
  const chosen=firstPass.slice(0,count);
  if(chosen.length<count) chosen.push(...available.filter(item=>!chosen.includes(item)).slice(0,count-chosen.length));
  for(const item of chosen) {
    selected.add(item.track.candidate_key);
    const artist=fold(item.track.artist);
    artistCounts.set(artist,(artistCounts.get(artist)||0)+1);
  }
  return chosen;
}

/** Rank durable, unseen provider candidates from observed listening evidence. */
export function rankMusicCandidates({seeds=[],favorites=new Set(),feedback=[],candidates=[],origins=[],seedGenresByArtist={},excludeRecordingKeys=[],now=Date.now(),limit=50}={}) {
  const allFeedback=feedbackByRecording(feedback);
  const suppressed=new Set((feedback||[]).filter(event=>event.event==='dislike').map(event=>event.recording_key));
  const excluded=new Set(excludeRecordingKeys);
  const seedByKey=new Map();
  const seedTaste=[];
  const genreWeights=new Map();
  const artistWeights=new Map();
  const artistPenalties=new Map();
  for(const row of seeds) {
    const key=String(row.track_key||'');
    const recordingKey=row.recording_key||recordingKeyFor(row.title,row.artist);
    if(!key||!recordingKey) continue;
    const events=allFeedback.get(recordingKey)||[];
    const signals=feedbackSignals(events,now);
    const profile=seedWeight(row,favorites,events,now);
    const disliked=events.some(event=>event.event==='dislike');
    const artist=fold(row.artist);
    const genres=[...genresOf(row.genre),...(Array.isArray(row.genres)?row.genres.flatMap(genresOf):[]),
      ...(Array.isArray(seedGenresByArtist[artist])?seedGenresByArtist[artist].flatMap(genresOf):[])];
    const seed={...row,track_key:key,recording_key:recordingKey,artist_fold:artist,genres:[...new Set(genres)],...profile,disliked};
    seedByKey.set(key,seed);
    if(disliked) {
      artistPenalties.set(artist,(artistPenalties.get(artist)||0)+5);
      continue;
    }
    if(signals.strongestSkip>0) artistPenalties.set(artist,(artistPenalties.get(artist)||0)+signals.strongestSkip*1.5);
    if(profile.playCount<=0&&!profile.isFavorite&&!events.some(event=>['play_progress','completed','like'].includes(event.event))) continue;
    seedTaste.push(seed);
    artistWeights.set(artist,(artistWeights.get(artist)||0)+profile.weight);
    for(const genre of seed.genres) genreWeights.set(genre,(genreWeights.get(genre)||0)+profile.weight);
  }
  seedTaste.sort((a,b)=>b.weight-a.weight||a.track_key.localeCompare(b.track_key));
  const activeSeeds=seedTaste.slice(0,100);
  const maxSeedWeight=Math.max(.1,...activeSeeds.map(seed=>seed.weight));
  const originsFor=originMap(origins);
  const seenRecordings=new Set();
  const ranked=[];
  for(const row of candidates) {
    const key=String(row.recording_key||recordingKeyFor(row.title,row.artist)||'');
    if(!key||seenRecordings.has(key)||excluded.has(key)||suppressed.has(key)) continue;
    const isPlayable=row.provider==='audius'
      ? /^https:\/\/api\.audius\.co\/v1\/tracks\/[A-Za-z0-9_-]+\/stream\?/.test(String(row.audio_url||''))
      : row.provider==='youtube'&&/^[A-Za-z0-9_-]{11}$/.test(String(row.video_id||''));
    if(!isPlayable) continue;
    seenRecordings.add(key);
    const candidateOrigins=originsFor.get(row.candidate_key)||[];
    const legacyKeys=(()=>{try{return JSON.parse(row.seed_keys||'[]')}catch{return[]}})();
    const refs=candidateOrigins.length?candidateOrigins:legacyKeys.map(seed_track_key=>({seed_track_key,relationship:'direct'}));
    const connected=[];
    for(const ref of refs) {
      const seed=seedByKey.get(ref.seed_track_key);
      if(seed&&!seed.disliked) connected.push({...ref,seed});
    }
    const candidateArtist=fold(row.artist);
    const sameArtist=activeSeeds.filter(seed=>seed.artist_fold&&seed.artist_fold===candidateArtist);
    const candidateGenres=genresOf(row.genre);
    const genreMatches=candidateGenres.filter(genre=>genreWeights.has(genre)).sort((a,b)=>(genreWeights.get(b)||0)-(genreWeights.get(a)||0));
    const artistSignal=Math.max(0,...sameArtist.map(seed=>seed.weight));
    const originSignal=Math.max(0,...connected.map(ref=>ref.seed.weight*(ref.relationship==='similar_artist'?.82:1)));
    const genreSignal=genreMatches.reduce((sum,genre)=>sum+(genreWeights.get(genre)||0),0);
    const totalGenreWeight=[...genreWeights.values()].reduce((sum,value)=>sum+value,0);
    const originScore=originSignal/maxSeedWeight;
    const artistScore=Math.min(1,artistSignal/maxSeedWeight);
    const genreScore=totalGenreWeight>0?Math.min(1,genreSignal/totalGenreWeight*4):0;
    const penalty=Math.min(.55,(artistPenalties.get(candidateArtist)||0)/Math.max(8,maxSeedWeight*2));
    const score=Math.max(0,.43*originScore+.33*artistScore+.24*genreScore-penalty);
    if(score<=0) continue;
    const direct=connected.some(ref=>ref.relationship==='direct');
    const related=connected.some(ref=>ref.relationship==='similar_artist')&&!direct;
    const bestConnection=[...connected].sort((a,b)=>b.seed.weight-a.seed.weight)[0];
    const reasons=[];
    if(related&&bestConnection) reasons.push(`Found through a similar-artist link for ${bestConnection.seed.title}`);
    else if(bestConnection) reasons.push(`Found from listening evidence for ${bestConnection.seed.title}`);
    if(sameArtist.length) reasons.push(`Artist matches your listening history: ${String(row.artist).slice(0,120)}`);
    if(genreMatches.length) reasons.push(`Shares styles linked to your listening: ${genreMatches.slice(0,3).join(', ')}`);
    if(connected.length>1) reasons.push(`Connected to ${connected.length} of your taste seeds`);
    ranked.push({track:row,score,related,reasons,recording_key:key});
  }
  ranked.sort((a,b)=>b.score-a.score||fold(a.track.artist).localeCompare(fold(b.track.artist))||fold(a.track.title).localeCompare(fold(b.track.title)));
  const target=Math.max(1,Math.min(200,Number(limit)||50));
  const relatedTarget=Math.ceil(target*.2);
  const related=ranked.filter(item=>item.related);
  const direct=ranked.filter(item=>!item.related);
  const selected=new Set(),artistCounts=new Map();
  const relatedChosen=selectDiverse(related,relatedTarget,selected,artistCounts);
  const directChosen=selectDiverse(direct,target-relatedTarget,selected,artistCounts);
  const chosenSet=new Set([...directChosen,...relatedChosen].map(item=>item.track.candidate_key));
  const chosen=[];
  let directIndex=0,relatedIndex=0;
  while(chosen.length<target&&(directIndex<directChosen.length||relatedIndex<relatedChosen.length)) {
    for(let slot=0;slot<4&&directIndex<directChosen.length&&chosen.length<target;slot++) chosen.push(directChosen[directIndex++]);
    if(relatedIndex<relatedChosen.length&&chosen.length<target) chosen.push(relatedChosen[relatedIndex++]);
    if(directIndex>=directChosen.length&&relatedIndex<relatedChosen.length&&chosen.length<target) chosen.push(relatedChosen[relatedIndex++]);
  }
  if(chosen.length<target) {
    chosen.push(...selectDiverse(ranked.filter(item=>!chosenSet.has(item.track.candidate_key)),target-chosen.length,selected,artistCounts));
  }
  return chosen.slice(0,target).map((item,index)=>({...item,rank:index+1}));
}
