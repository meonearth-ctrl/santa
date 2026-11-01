/* ---------- Santa transcripts: the Mac's offline Whisper output (.json / .srt), tagged to seat numbers ----------
   Who is speaking comes from three cues: a candidate saying their number ("Number four."), saying their name ("My name is Rose…",
   matched to the group's list), or the panel calling a number ("Candidate number two, …") just before someone else answers.
   Video never comes here: only the text. */
const NUMW={one:1,two:2,three:3,four:4,five:5,six:6,seven:7,eight:8,nine:9,ten:10,eleven:11,twelve:12};
const NUMRX='(\\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)';
const CUE_RX=[
  new RegExp("\\b(?:i am|i'm|im|this is|my number is)\\s+(?:candidate\\s+)?number\\s*"+NUMRX+"\\b"),
  new RegExp("\\b(?:i am|i'm|im|this is)\\s+candidate\\s+"+NUMRX+"\\b"),
  new RegExp("\\b(?:candidate|number)\\s+(?:number\\s+)?"+NUMRX+"\\s*(?:here|speaking)\\b")];
const LEAD_RX=new RegExp("^(?:(?:hi|hello|good (?:morning|afternoon|evening|day))[\\s,.!]*(?:everyone|all|sir|ma'am|maam|po|panel)?[\\s,.!]*)?(?:candidate\\s+)?number\\s*"+NUMRX+"[\\s,.!]*$");
const CALL_RX=new RegExp("\\b(?:candidate\\s+(?:number\\s+)?|number\\s+)"+NUMRX+"\\b");
function numOf(w){w=String(w).toLowerCase();return NUMW[w]||+w||null}
function low(t){return String(t||'').toLowerCase().replace(/[“”"]/g,'').trim()}
function selfCue(text){
  const t=low(text);
  for(const rx of CUE_RX){const m=t.match(rx); if(m) return numOf(m[1])}
  const m=t.match(LEAD_RX); return m?numOf(m[1]):null; // a bare "Number four." said by the candidate
}
/* the panel addressing a seat: "Candidate number two, why…", "Number five, I want you to start" */
function callCue(text){const t=low(text); if(selfCue(t)) return null; const all=[...t.matchAll(new RegExp(CALL_RX.source,'g'))]; return all.length===1?numOf(all[0][1]):null}
function lev(a,b){const m=a.length,n=b.length;if(!m||!n)return Math.max(m,n);let p=Array.from({length:n+1},(_,j)=>j);
  for(let i=1;i<=m;i++){const c=[i];for(let j=1;j<=n;j++)c[j]=Math.min(p[j]+1,c[j-1]+1,p[j-1]+(a[i-1]===b[j-1]?0:1));p=c}return p[n]}
const NAME_STOP=new Set(['the','and','years','year','old','currently','from','here','working','work','your','you','call','sir','maam','just','also','been','have','graduate','graduated','newly','hired','manager','restaurant','assistant','supervisor','candidate','number']);
/* "My name is Rose Ancineta" -> seat whose listed name matches best (Whisper spellings vary, so near matches count) */
function nameCue(text,cands){
  const t=low(text); const m=t.match(/\b(?:my name is|my name's|you can call me|call me|i am|i'm)\s+([a-z][a-z' .-]{2,50})/); if(!m) return null;
  const words=m[1].split(/[^a-z']+/).filter(w=>w.length>=3&&!NAME_STOP.has(w)).slice(0,4); if(!words.length) return null;
  const sc=cands.map(c=>{const toks=String(c.name||'').toLowerCase().split(/[^a-z']+/).filter(w=>w.length>=3);
    let best=0; words.forEach(w=>toks.forEach(k=>{let s=1-lev(w,k)/Math.max(w.length,k.length); if(Math.min(w.length,k.length)>=4&&(k.includes(w)||w.includes(k))) s=Math.max(s,0.85); if(s>best) best=s})); return {no:c.no,s:best}}).sort((a,b)=>b.s-a.s);
  return sc.length&&sc[0].s>=0.78&&(!sc[1]||sc[0].s-sc[1].s>=0.12)?sc[0].no:null;
}
function srtSec(t){const m=String(t).trim().match(/(\d+):(\d+):(\d+)[,.](\d+)/);return m?(+m[1])*3600+(+m[2])*60+(+m[3])+(+m[4])/1000:0}
function parseSanta(name,text){
  if(/\.json$/i.test(name)||/^\s*\{/.test(text)){
    const j=JSON.parse(text); if(!Array.isArray(j.segments)) throw new Error('This JSON has no segments. Use the .json Santa writes to 03_Transcripts');
    return {source:j.source||name,model:j.model||'',engine:j.engine||'',secs:j.audio_seconds||null,
      segs:j.segments.map(x=>({a:+(+x.start||0).toFixed(2),z:+(+x.end||0).toFixed(2),t:String(x.text||'').trim(),s:x.speaker?String(x.speaker):''})).filter(x=>x.t)};
  }
  const segs=[]; String(text).replace(/\r/g,'').split(/\n\s*\n/).forEach(b=>{const L=b.split('\n');const i=L.findIndex(l=>/-->/.test(l)); if(i<0) return;
    const [a,z]=L[i].split('-->').map(srtSec); let t=L.slice(i+1).join(' ').trim(), s=''; const m=t.match(/^\[([^\]]+)\]\s*/); if(m){s=m[1];t=t.slice(m[0].length)}
    if(t) segs.push({a:+a.toFixed(2),z:+z.toFixed(2),t,s})});
  if(!segs.length) throw new Error('No subtitles found in this file');
  return {source:name.replace(/\.srt$/i,''),model:'',engine:'',secs:segs[segs.length-1].z,segs};
}
/* First pass: what each segment says about who is talking. */
function cuesOf(segs,cands){
  const nos=cands.map(c=>c.no);
  return segs.map(g=>{const n=selfCue(g.t); if(n&&nos.includes(n)) return {k:'num',n};
    const nm=nameCue(g.t,cands); if(nm) return {k:'name',n:nm};
    const c=callCue(g.t); if(c&&nos.includes(c)) return {k:'call',n:c};
    return null});
}
/* speaker label -> seat number or 'panel' (only when Santa separated speakers) */
function autoMap(segs,cands){
  const cu=cuesOf(segs,cands), v={}, talk={};
  const vote=(sp,key,w)=>{const o=v[sp]=v[sp]||{};o[key]=(o[key]||0)+w};
  segs.forEach((g,i)=>{if(!g.s) return; talk[g.s]=(talk[g.s]||0)+(g.z-g.a); const c=cu[i]; if(!c) return;
    if(c.k==='call'){vote(g.s,'panel',1); const nx=segs.slice(i+1,i+4).find(x=>x.s&&x.s!==g.s&&x.a-g.z<15); if(nx) vote(nx.s,c.n,0.5)}
    else vote(g.s,c.n,1)});
  const map={};
  Object.entries(v).forEach(([sp,o])=>{const seats=Object.entries(o).filter(([k])=>k!=='panel'); const sv=seats.reduce((a,[,x])=>a+x,0), pv=o.panel||0;
    if(pv>=2&&pv>sv){map[sp]='panel';return}
    if(!seats.length) return; const best=seats.sort((a,b)=>b[1]-a[1])[0]; if(best[1]>=1&&best[1]/sv>=0.6&&best[1]>pv) map[sp]=+best[0]});
  const top=Object.entries(talk).filter(([sp])=>map[sp]==null&&!(v[sp]&&Object.keys(v[sp]).some(k=>k!=='panel'))).sort((a,b)=>b[1]-a[1])[0];
  if(top&&!Object.values(map).includes('panel')) map[top[0]]='panel';
  return map;
}
/* Second pass, per segment: seat number, 'panel' or null, and how it was decided ('set' by hand, 'speaker', 'num', 'name',
   'called' = first answer after the panel called that number, 'carry' = the same voice going on within 20 s). */
const CARRY=20;
function tagSegs(T,cands){
  const map=T.map||{}, over=T.over||{}; const cu=cuesOf(T.segs||[],cands); let cur=null, curAt=-1e9, curSp='', call=null;
  return (T.segs||[]).map((g,i)=>{
    let n=null, how=''; const c=cu[i];
    if(over[i]!=null&&over[i]!==''){n=over[i]==='panel'?'panel':+over[i];how='set'}
    else if(g.s&&map[g.s]!=null&&map[g.s]!==''){n=map[g.s]==='panel'?'panel':+map[g.s];how='speaker'}
    else if(c&&c.k!=='call'){n=c.n;how=c.k}
    else if(c&&c.k==='call'){n='panel';how='call'}
    else if(call&&g.a-call.at<=30&&(!g.s||g.s!==call.s)){n=call.n;how='called'}
    else if(cur!=null&&g.a-curAt<=CARRY&&(!g.s||g.s===curSp)){n=cur;how='carry'}
    if(c&&c.k==='call'&&how!=='set') call={n:c.n,s:g.s,at:g.z}; else if(typeof n==='number') call=null;
    if(typeof n==='number'){cur=n;curAt=g.z;curSp=g.s} else if(n==='panel'){cur=null}
    return {n,how};
  });
}
if(typeof module!=='undefined') module.exports={selfCue,nameCue,callCue,parseSanta,autoMap,tagSegs,cuesOf};
