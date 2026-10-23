const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
const src=fs.readFileSync(path.join(__dirname,'../static/vnc.js'),'utf8');
const code=src.slice(src.indexOf('const settingQueues ='),src.indexOf('\nloadServerConfig();'));
(async()=>{const pending=[],calls=[],alerts=[];const c={Map,Promise,Error,S:{typeMode:'auto',typeBatch:80},saveS(){},applyTypeUI(){},showInputAlert(s){alerts.push(s)},clipApi(p,b){calls.push([p,b]);return new Promise(r=>pending.push(r))}};vm.createContext(c);vm.runInContext(code,c);
const load=c.loadServerConfig();c.saveCfg({typeMode:'clipboard'});await new Promise(setImmediate);assert.equal(calls.length,2);pending.shift()({ok:true,config:{typeMode:'xdotool'}});await load;assert.equal(c.S.typeMode,'auto');
c.saveCfg({typeMode:'auto'});await new Promise(setImmediate);assert.equal(calls.length,2);pending.shift()({ok:true});await new Promise(setImmediate);assert.equal(calls.length,3);pending.shift()({ok:false,err:'disk full'});await new Promise(setImmediate);assert.match(alerts.at(-1),/disk full/);
c.saveCfg({typeBatch:80});await new Promise(setImmediate);assert.equal(calls.length,4);pending.shift()({ok:true});await new Promise(setImmediate);
assert(src.indexOf('let wakeLock = null') < src.indexOf('\napplyAwake();'));
console.log('PASS stale settings load ignored, saves serialized, errors visible, wake lock initialized before use');})().catch(e=>{console.error(e);process.exit(1)});
