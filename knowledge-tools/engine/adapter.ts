import { RaceSolverBuilder,buildBaseStats,buildAdjustedStats,parseGroundCondition } from './vendor/kachi-uma-tools/uma-skill-tools/RaceSolverBuilder';
import { PosKeepMode } from './vendor/kachi-uma-tools/uma-skill-tools/RaceSolver';
import skills from './vendor/kachi-uma-tools/uma-skill-tools/data/skill_data.json';
import skillnames from './vendor/kachi-uma-tools/uma-skill-tools/data/skillnames.json';
import courses from './vendor/kachi-uma-tools/uma-skill-tools/data/course_data.json';
import tracknames from './vendor/kachi-uma-tools/uma-skill-tools/data/tracknames.json';

declare const __RULESET__: string;
const MODEL_COMMIT='4401e25987d9c78707f2d3f8a9dcd02c1c56529c';
const trackCn:Record<string,string>={'10001':'札幌','10002':'函馆','10003':'新潟','10004':'福岛','10005':'中山','10006':'东京',
 '10007':'中京','10008':'京都','10009':'阪神','10010':'小仓','10101':'大井','10103':'川崎','10104':'船桥','10105':'盛冈','10201':'隆尚'};
const baseAssumptions=['参考机制模型，不是完整马群胜率模拟。','位置、排名及对手相关技能采用该引擎的近似条件。',
 '五维按本引擎的突破1200折算、心情、适性及赛道修正处理。','马身为在先到终点一方的结束时刻，双方距离差除以2.5米。'];
function metadata(args:any){
 const requested=String(args.server||args.requested_server||__RULESET__);
 const reference=!['jp','global','en','ja'].includes(requested.toLowerCase());
 return {model:'kachi-reference-source',model_commit:MODEL_COMMIT,data_commit:MODEL_COMMIT,data_snapshot:`kachi-${MODEL_COMMIT.slice(0,8)}-${__RULESET__}`,engine_ruleset:__RULESET__,
  requested_server:requested,data_scope:__RULESET__,status:reference?'reference_for_requested_server':'reference_model',
  cn_calibrated:false,tw_calibrated:false,source_built:true,full_field_win_probability_modeled:false,
  assumptions:[...(reference?[`请求服别${requested}目前使用明确标注的${__RULESET__}参考机制，尚未完成该服校准。`]:[]),...baseAssumptions]};
}
function skillVariant(id:string,value:any){
 if(id.startsWith('9')&&id.length>=6)return'inherited_unique';
 if(value.rarity>=3&&value.rarity<=5)return'native_unique';
 if(value.rarity===6)return'evolution';
 return'normal';
}
function skillEntry(id:string){
 const v=(skills as any)[id];const names=(skillnames as any)[id]||[];
 return {db_id:id,skill_id:id,name:names[0]||names[1]||id,name_ja:names[0]||null,name_en:names[1]||null,
  variant:skillVariant(id,v),rarity:v.rarity,source:__RULESET__+' game-data snapshot in kachi fork',source_commit:MODEL_COMMIT,
  ruleset:__RULESET__,simulatable_record:true,mechanics:v.alternatives};
}
function skillCatalog(args:any){
 const query=String(args.query||'').toLowerCase();const ids=args.ids?new Set(args.ids.map(String)):null;
 const limit=Math.min(500,Math.max(1,Number(args.limit)||40));
 const found=Object.keys(skills).filter(id=>(!ids||ids.has(id))&&(!query||[id,...((skillnames as any)[id]||[])].join(' ').toLowerCase().includes(query)));
 return {ok:true,action:'simulation.skills',result:{skills:found.slice(0,limit).map(skillEntry),total:found.length,limit},metadata:metadata(args)};
}
function courseEntry(id:string){
 const c=(courses as any)[id];const names=(tracknames as any)[String(c.raceTrackId)]||[];
 const venue=trackCn[String(c.raceTrackId)]||names[0]||names[1]||String(c.raceTrackId);
 return {course_id:id,venue,venue_ja:names[0]||null,venue_en:names[1]||null,distance:c.distance,
  surface:c.surface===1?'turf':'dirt',surface_label:c.surface===1?'草地':'泥地',
  direction:c.turn===1?'clockwise':c.turn===2?'counterclockwise':'straight',
  direction_label:c.turn===1?'顺时针':c.turn===2?'逆时针':'直线',ruleset:__RULESET__,
  name:`${venue} ${c.surface===1?'草地':'泥地'}${c.distance}米 ${c.turn===1?'顺时针':c.turn===2?'逆时针':'直线'}`,
  source_commit:MODEL_COMMIT};
}
function courseCatalog(args:any){
 const original=String(args.query||'').toLowerCase();
 const eventAlias=/菊花[赏賞]|kikuka\s*sho/.test(original);
 const query=original.replace(/菊花[赏賞]|kikuka\s*sho/g,'10810')
  .replace(/(\d+(?:\.\d+)?)\s*(?:km|公里|千米)/g,(_all,n)=>String(Number(n)*1000))
  .replace(/(\d+)\s*(?:米|m\b|メートル|meters?\b)/g,'$1 ').replace(/(\d+)/g,' $1 ');
 const tokens=query.split(/[\s,，/·]+/).filter(Boolean);
 const values=Object.keys(courses).map(courseEntry).filter(c=>tokens.every(token=>{
  if(/^\d+$/.test(token))return String(c.distance)===token||c.course_id===token;
  if(['turf','芝','草地'].includes(token))return c.surface==='turf';
  if(['dirt','ダート','泥地'].includes(token))return c.surface==='dirt';
  if(['clockwise','顺时针','右回','右回り'].includes(token))return c.direction==='clockwise';
  if(['counterclockwise','逆时针','左回','左回り'].includes(token))return c.direction==='counterclockwise';
  if(['straight','直线','直線'].includes(token))return c.direction==='straight';
  return[c.name,c.venue_ja,c.venue_en,c.course_id].join(' ').toLowerCase().includes(token);
 }));
 const meta=metadata(args);
 if(eventAlias)meta.assumptions.push('菊花赏别名仅匹配京都草地3000米的赛道几何入口，未推定具体届次、天气或赛期。');
 return {ok:true,action:'simulation.courses',result:{courses:values,total:values.length,query_tokens:tokens,
  alias_geometry_only:eventAlias},metadata:meta};
}
const strategies:Record<string,string>={'逃':'Nige','逃马':'Nige','逃げ':'Nige','front':'Nige','nige':'Nige',
 '先':'Senkou','先行':'Senkou','pace':'Senkou','senko':'Senkou','senkou':'Senkou','差':'Sasi','差行':'Sasi','差し':'Sasi','late':'Sasi','sashi':'Sasi','sasi':'Sasi',
 '追':'Oikomi','追马':'Oikomi','追入':'Oikomi','追込':'Oikomi','追込み':'Oikomi','end':'Oikomi','oikomi':'Oikomi','大逃':'Oonige','oonige':'Oonige'};
function statsSummary(values:number[]){
 const a=[...values].sort((x,y)=>x-y);const n=a.length;const mid=Math.floor(n/2);
 return {mean:a.reduce((x,y)=>x+y,0)/n,median:n%2?a[mid]:(a[mid-1]+a[mid])/2,min:a[0],max:a[n-1]};
}
function inputHorse(args:any,defaults:string[]){
 const ref:any={speed:1200,stamina:1200,power:1000,guts:800,wisdom:1000};const stats=args.stats||{};const result:any={};
 for(const key of Object.keys(ref)){
  const given=stats[key];if(given===undefined||given===null||given===''){result[key]=ref[key];defaults.push(`参考${key}=${ref[key]}`);}
  else{const n=Number(given);if(!Number.isFinite(n)||n<1)throw new Error(`五维${key}需要正数。`);result[key]=n;}
 }
 const style=String(args.strategy||'先行');if(!args.strategy)defaults.push('参考跑法=先行');
 result.strategy=strategies[style.toLowerCase()]||style;
 result.distanceAptitude=args.distance_aptitude||'A';result.surfaceAptitude=args.surface_aptitude||'A';result.strategyAptitude=args.strategy_aptitude||'A';
 if(!args.distance_aptitude||!args.surface_aptitude||!args.strategy_aptitude)defaults.push('未填写的距离/场地/跑法适性参考A。');
 const moods:any={'绝好调':2,'绝佳':2,'好调':1,'上佳':1,'普通':0,'normal':0,'不调':-1,'不佳':-1,'绝不调':-2,'极差':-2};
 result.mood=typeof args.mood==='string'&&args.mood in moods?moods[args.mood]:Number(args.mood??0);
 if(args.mood===undefined)defaults.push('参考心情=普通');
 return result;
}
function makeBuilder(nsamples:number,course:any,horse:any,ids:string[],args:any,onActivate:any){
 const b=new RaceSolverBuilder(nsamples).seed(Number(args.seed??20261025)).course(course).horse(horse)
  .ground(args.ground||'good').weather(args.weather||'sunny').season(args.season||'autumn').time(args.time||'midday')
  .order(Number(args.order_min??1),Number(args.order_max??args.field_size??9)).numUmas(Number(args.field_size??9))
  .posKeepMode(args.position_keep==='approximate'?PosKeepMode.Approximate:PosKeepMode.None).mode('compare')
  .skillWisdomCheck(args.skill_activation_checks!==false).rushedKakari(args.rushed!==false)
  .competeFight(false).leadCompetition(false).laneMovement(false);
 for(const id of ids)b.addSkill(id);
 if(__RULESET__==='jp'&&args.post_1200_mechanics!==false)b.withAsiwotameru().withStaminaSyoubu();
 b.onSkillActivate(onActivate);return b;
}
function racePair(base:any,variant:any,distance:number){
 const dt=1/30;let t=0,firstGap:number|null=null;let timeA:number|null=null,timeB:number|null=null;
 let hpA:number|null=null,hpB:number|null=null;let spurtA=false,spurtB=false;
 for(let step=0;step<120000&&(timeA===null||timeB===null);step++){
  const pa=base.pos,pb=variant.pos;
  if(timeA===null)base.step(dt);if(timeB===null)variant.step(dt);
  const xa=base.pos,xb=variant.pos;
  if(![xa,xb,base.currentSpeed,variant.currentSpeed].every(Number.isFinite))throw new Error('该技能/机制产生了无效数值，当前模型不支持此组合。');
  const fa=timeA===null&&xa>=distance?(distance-pa)/(xa-pa):Infinity;
  const fb=timeB===null&&xb>=distance?(distance-pb)/(xb-pb):Infinity;
  const f=Math.min(fa,fb);
  if(firstGap===null&&Number.isFinite(f))firstGap=((pb+(xb-pb)*f)-(pa+(xa-pa)*f))/2.5;
  if(Number.isFinite(fa)){timeA=t+dt*fa;hpA=base.hp.hp;spurtA=!!base.hp.isMaxSpurt?.();}
  if(Number.isFinite(fb)){timeB=t+dt*fb;hpB=variant.hp.hp;spurtB=!!variant.hp.isMaxSpurt?.();}
  t+=dt;
 }
 base.cleanup();variant.cleanup();
 if(timeA===null||timeB===null)throw new Error('当前参考模拟未能结束。');
 return {gain:firstGap||0,time_a:timeA,time_b:timeB,hp_a:hpA!,hp_b:hpB!,spurt_a:spurtA,spurt_b:spurtB};
}
function run(args:any){
 const id=String(args.course_id||'');const course=(courses as any)[id];
 if(!course)return{ok:true,action:'simulation.run',result:{status:'needs_course',courses:Object.keys(courses).slice(0,8).map(courseEntry),message:'请先选择赛道；资料查询仍可使用。'},metadata:metadata(args)};
 const assumptions:string[]=[];const horse=inputHorse(args,assumptions);
 const requested=Number(args.samples)||64;const samples=Math.min(1000,Math.max(1,Math.floor(requested)));
 if(samples!==requested)assumptions.push(`样本数调整为${samples}以控制本地计算时间。`);
 if(!args.ground||!args.weather||!args.season||!args.field_size)assumptions.push('未填写条件采用参考良场、晴、秋季和9人；没有完整对手队伍建模。');
 const sourceSkills=Array.isArray(args.skills)?args.skills:String(args.skills||'').split(/[,，\s]+/).filter(Boolean);
 const all=sourceSkills.map((v:any)=>String(typeof v==='object'?v.db_id??v.id:v));const unavailable=all.filter((v:string)=>!(skills as any)[v]);
 const baseline=[...new Set(all.filter((v:string)=>(skills as any)[v]))];
 let compareId=String(args.compare_skill_id||'');if(compareId&&!(skills as any)[compareId])return{ok:true,action:'simulation.run',result:{status:'skill_not_in_snapshot',skill_id:compareId,message:'当前参考引擎快照没有这个技能。技能资料仍可独立查询。'},metadata:metadata(args)};
 const compared=compareId?[...new Set([...baseline,compareId])]:baseline;
 let activatedSamples=0,active=false;const activationExamples:any[]=[];
 const a=makeBuilder(samples,course,horse,baseline,args,()=>{}).build();
 const b=makeBuilder(samples,course,horse,compared,args,(state:any,skillId:string)=>{
  if(skillId!==compareId)return;active=true;
  if(activationExamples.length<5){
   const effects=(name:string)=>(state[name]||[]).filter((row:any)=>row.skillId===skillId).map((row:any)=>({modifier:row.modifier,duration_seconds:-row.durationTimer.t}));
   activationExamples.push({skill_id:skillId,position_m:state.pos,time_seconds:state.accumulatetime.t,
    current_speed:effects('activeCurrentSpeedSkills'),target_speed:effects('activeTargetSpeedSkills'),acceleration:effects('activeAccelSkills')});
  }
 }).build();
 const rows=[];
 for(let i=0;i<samples;i++){
  active=false;const left=a.next().value,right=b.next().value;
  rows.push(racePair(left,right,course.distance));if(active)activatedSamples++;
 }
 const baseResult={finish_time_seconds:statsSummary(rows.map(r=>r.time_a)),remaining_hp:statsSummary(rows.map(r=>r.hp_a)),
  full_spurt_rate:rows.filter(r=>r.spurt_a).length/samples,hp_positive_finish_rate:rows.filter(r=>r.hp_a>0).length/samples};
 const otherResult={finish_time_seconds:statsSummary(rows.map(r=>r.time_b)),remaining_hp:statsSummary(rows.map(r=>r.hp_b)),
  full_spurt_rate:rows.filter(r=>r.spurt_b).length/samples,hp_positive_finish_rate:rows.filter(r=>r.hp_b>0).length/samples};
 const meta=metadata(args);meta.assumptions.push(...assumptions);
 if(unavailable.length)meta.assumptions.push(`快照中没有的已学技能未纳入：${unavailable.join('、')}。`);
 meta.assumptions.push('固定步长1/30秒；冲刺率/剩余HP来自引擎HP模型，不能作为真实比赛胜率。');
 if(samples<50)meta.assumptions.push('当前样本较少，区间和均值可能不稳定；可增加样本再比较。');
 if(compareId&&skillVariant(compareId,(skills as any)[compareId])==='native_unique')meta.assumptions.push('固有技能采用该快照记录的基础效果；当前适配器尚未额外重算固有等级提升。');
 return {ok:true,action:'simulation.run',result:{status:assumptions.length?'reference_inputs':'reference_result',course:courseEntry(id),
  inputs:{stats:horse,effective_stats:buildAdjustedStats(buildBaseStats(horse,horse.mood),course,parseGroundCondition(args.ground||'good')),strategy:horse.strategy,skills:baseline,compare_skill_id:compareId||null,samples,seed:Number(args.seed??20261025),
   skill_activation_checks:args.skill_activation_checks!==false,position_keep:args.position_keep||'none',unique_level_requested:args.unique_level??1,unique_level_modeled:false,
   conditions:{ground:args.ground||'good',weather:args.weather||'sunny',season:args.season||'autumn',time:args.time||'midday',mood:horse.mood,field_size:Number(args.field_size??9)}},
  baseline:baseResult,comparison:compareId?otherResult:null,
  skill_delta:compareId?{skill:skillEntry(compareId),bashin:statsSummary(rows.map(r=>r.gain)),time_saved_seconds:statsSummary(rows.map(r=>r.time_a-r.time_b)),
   activated_samples:activatedSamples,activation_rate_observed:activatedSamples/samples,activation_examples:activationExamples,already_in_baseline:baseline.includes(compareId)}:null,
  finish_viability:{kind:'reference_hp_and_spurt_only',baseline_full_spurt_rate:baseResult.full_spurt_rate,
   comparison_full_spurt_rate:compareId?otherResult.full_spurt_rate:null,win_probability:null},unavailable_skills:unavailable},metadata:meta};
}
export function dispatch(args:any){
 try{
  const conditions:any={ground:{'良':'good','稍重':'yielding','重':'soft','不良':'heavy'},
   weather:{'晴':'sunny','阴':'cloudy','雨':'rainy','雪':'snowy','rain':'rainy','snow':'snowy'},
   season:{'春':'spring','夏':'summer','秋':'autumn','冬':'winter','樱花':'sakura'},
   time:{'上午':'morning','白天':'midday','中午':'midday','傍晚':'evening','夜间':'night'}};
  args={...args};for(const field of Object.keys(conditions))if(args[field]in conditions[field])args[field]=conditions[field][args[field]];
  switch(args.action||args.operation||args.method){
   case'simulation.courses':case'courses':return courseCatalog(args);
   case'simulation.skills':case'skills':return skillCatalog(args);
   case'simulation.run':case'run':return run(args);
   default:return{ok:false,error:'unknown_simulation_action'};
  }
 }catch(error){return{ok:false,error:'simulation_model_error',message:String(error instanceof Error?error.message:error),metadata:metadata(args)};}
}
