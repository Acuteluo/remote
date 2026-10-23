const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
const src=fs.readFileSync(path.join(__dirname,'../static/vnc.js'),'utf8');
const start=src.indexOf('async function pingOnce()'),end=src.indexOf('\nfunction startPing()',start);
(async()=>{const alerts=[],el={dataset:{kind:'input'}};const c={pingBusy:false,performance:{now:()=>10},viewerId:'one',connected:true,pingEma:0,streamRtt:null,autoSaver:false,S:{showPing:true},Error,TypeError,Date,fetchAbort:async()=>({status:401}),updateAutoSaver(){},setStatus(){},showInputAlert(s,k){alerts.push(s);el.dataset.kind=k},clearInputAlert(){alerts.push('cleared')},renderPing(){},$:()=>el,connect(){}};
vm.createContext(c);vm.runInContext(src.slice(start,end),c);await c.pingOnce();assert.match(alerts[0],/登录已失效/);assert.equal(c.pingBusy,false);
c.fetchAbort=async()=>{const e=new Error();e.name='AbortError';throw e};await c.pingOnce();assert.match(alerts.at(-1),/10秒/);
c.fetchAbort=async()=>({ok:true,status:200,json:async()=>({active:true,rttMs:50})});await c.pingOnce();assert.equal(alerts.at(-1),'cleared');assert.equal(c.streamRtt,50);
console.log('PASS authentication/timeout errors are explicit and network recovery clears its alert');})().catch(e=>{console.error(e);process.exit(1)});
