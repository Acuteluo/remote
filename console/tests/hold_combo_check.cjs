const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
const src=fs.readFileSync(path.join(__dirname,'../static/vnc.js'),'utf8');
const start=src.indexOf("b.addEventListener('click', async () => {");
const brace=src.indexOf('{',start);let n=1,end=brace+1;
for(;n;end++){if(src[end]==='{')n++;if(src[end]==='}')n--;}
const body=src.slice(brace+1,end-1);
function context(){
 const a=[],b=[],waits=[];const first={sendKey(...args){a.push(args)}},second={sendKey(...args){b.push(args)}};
 const c={rfb:first,connected:true,b:{dataset:{combo:'alt+tab'}},MODS:[{key:'alt',keysym:1,code:'Alt'}],SPECIAL:{tab:[2,'Tab']},HOLD_COMBO:{'alt+tab':900},holdState:{combo:null,target:null,token:0,timer:0},clearMods(){},canSend(){return c.connected},sleep:()=>new Promise(r=>waits.push(r)),clearTimeout(){},setTimeout(){return 1},releaseHold(){c.holdState.combo=null;c.holdState.target=null;c.holdState.token++},keysymFor(){return 0}};
 vm.createContext(c);vm.runInContext('async function action(){'+body+'}',c);return{c,a,b,waits,second};
}
(async()=>{
 let x=context(),p=x.c.action();assert.equal(x.a.length,1);x.c.rfb=x.second;x.c.releaseHold();x.waits.shift()();await p;assert.equal(x.b.length,0);
 x=context();p=x.c.action();x.waits.shift()();await new Promise(setImmediate);assert.equal(x.a.length,2);x.c.rfb=x.second;x.c.releaseHold();x.waits.shift()();await p;assert.equal(x.b.length,0);
 console.log('PASS Alt+Tab never continues an old action on a replacement connection');
})().catch(e=>{console.error(e);process.exit(1)});
