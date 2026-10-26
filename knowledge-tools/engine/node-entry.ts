import fs from 'node:fs';
import {dispatch} from './adapter';
const raw=fs.readFileSync(0,'utf8');
try{process.stdout.write(JSON.stringify(dispatch(JSON.parse(raw)))+'\n');}
catch(error){process.stdout.write(JSON.stringify({ok:false,error:'invalid_simulation_request',message:String(error)})+'\n');process.exitCode=1;}
