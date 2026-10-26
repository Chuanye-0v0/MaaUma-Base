import esbuild from 'esbuild';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {fileURLToPath} from 'node:url';
const here=path.dirname(fileURLToPath(import.meta.url));
const manifest=JSON.parse(fs.readFileSync(path.join(here,'source-manifest.json'),'utf8'));
const sha=file=>crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
for(const row of manifest.files){if(sha(path.join(here,'vendor/kachi-uma-tools',row.path))!==row.sha256)throw new Error('Source leaf changed: '+row.path);}
const results=[];
for(const ruleset of ['jp','global']){
 const redirect={name:'selected-game-data',setup(build){build.onResolve({filter:/\/data\/(?:course_data|skill_data|skillnames|tracknames)\.json$/},args=>{
  const name=path.basename(args.path);return{path:path.join(here,'vendor/kachi-uma-tools',ruleset==='global'?'umalator-global':'uma-skill-tools/data',name)};
 });}};
 for(const browser of [false,true]){
  const outfile=path.join(here,'dist',`simulation-${ruleset}${browser?'.worker.js':'.cjs'}`);
  const plugins=[redirect];
  if(browser)plugins.push({name:'browser-assert',setup(build){build.onResolve({filter:/^node:assert$/},()=>({path:'assert',namespace:'reference-assert'}));
   build.onLoad({filter:/.*/,namespace:'reference-assert'},()=>({contents:'export function strict(value,message){if(!value)throw new Error(message||"Reference model assertion failed");}'}));}});
  const result=await esbuild.build({entryPoints:[path.join(here,browser?'worker-entry.ts':'node-entry.ts')],bundle:true,
   platform:browser?'browser':'node',format:browser?'iife':'cjs',target:'es2022',outfile,minify:false,metafile:true,
   define:{CC_GLOBAL:String(ruleset==='global'),CC_DEBUG:'false',__RULESET__:JSON.stringify(ruleset)},plugins,
   banner:{js:'/* GPL-3.0-or-later. Contains kachi-dev/uma-tools@'+manifest.commit+'. Source and patches accompany this build. */'}});
  results.push({ruleset,browser,outfile,sha256:sha(outfile),bytes:fs.statSync(outfile).size,source_inputs:Object.keys(result.metafile.inputs)});
 }
}
fs.writeFileSync(path.join(here,'build-manifest.json'),JSON.stringify({ok:true,source_manifest_sha256:sha(path.join(here,'source-manifest.json')),
 source_commit:manifest.commit,node:process.version,esbuild:esbuild.version,package_lock_sha256:sha(path.join(here,'package-lock.json')),
 adapter_sha256:sha(path.join(here,'adapter.ts')),builder_sha256:sha(fileURLToPath(import.meta.url)),
 node_entry_sha256:sha(path.join(here,'node-entry.ts')),worker_entry_sha256:sha(path.join(here,'worker-entry.ts')),
 typescript_full_typecheck:false,transpilation_source_build_verified:true,
 untrusted_install_scripts_executed:0,outputs:results},null,2)+'\n');
console.log(JSON.stringify({ok:true,outputs:results.map(r=>({ruleset:r.ruleset,browser:r.browser,bytes:r.bytes}))}));
