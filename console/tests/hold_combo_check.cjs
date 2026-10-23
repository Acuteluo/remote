const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
const src=fs.readFileSync(path.join(__dirname,'../static/vnc.js'),'utf8');
function block(start){const brace=src.indexOf('{',start);let n=1,end=brace+1;for(;n;end++){if(src[end]==='{')n++;if(src[end]==='}')n--;}return src.slice(brace+1,end-1)}
const body=block(src.indexOf("b.addEventListener('click', async () => {"));
function context(){
 const a=[],b=[],waits=[],requests=[],errors=[];const first={sendKey(...args){a.push(args)}},second={sendKey(...args){b.push(args)}};
 const c={rfb:first,connected:true,b:{dataset:{combo:'alt+tab'}},MODS:[{key:'alt',keysym:1,code:'Alt'}],SPECIAL:{tab:[2,'Tab']},HOLD_COMBO:{'alt+tab':900},holdState:{combo:null,target:null,token:0,timer:0},typeBuf:[],typeBusy:false,typeTimer:0,selfClipUntil:0,S:{typeMode:'auto'},AbortController,Promise,Date,clearInputAlert(){},showTypeErr(s){errors.push(s)},fetch(url,args){requests.push(JSON.parse(args.body));return new Promise(r=>requests.resolve=r)},clearMods(){},canSend(){return c.connected},sleep:()=>new Promise(r=>waits.push(r)),clearTimeout(){},setTimeout(){return 1},releaseHold(){if(c.holdState.target===c.rfb&&c.holdState.combo)c.rfb.sendKey(1,'Alt',false);c.holdState.combo=null;c.holdState.target=null;c.holdState.token++},keysymFor(){return 0}};
 vm.createContext(c);vm.runInContext('function flushType(){'+block(src.indexOf('function flushType('))+'}\nasync function action(){'+body+'}',c);return{c,a,b,waits,second,requests,errors};
}
const tick=()=>new Promise(setImmediate);
(async()=>{
 let x=context();await x.c.action();assert.equal(x.a.length,1);x.c.rfb=x.second;x.c.releaseHold();x.waits.shift()();await tick();assert.equal(x.b.length,0);assert.equal(x.errors.length,1);
 x=context();await x.c.action();x.waits.shift()();await tick();assert.equal(x.a.length,2);x.c.rfb=x.second;x.c.releaseHold();x.waits.shift()();await tick();assert.equal(x.b.length,0);
 x=context();x.c.typeBuf.push('before');x.c.flushType();await x.c.action();x.c.typeBuf.push('after');x.c.flushType();assert.equal(x.a.length,0);assert.equal(x.requests.length,1);
 x.requests.resolve({status:200,json:async()=>({ok:true})});await tick();assert.equal(x.a.length,1);assert.equal(x.requests.length,1);x.waits.shift()();await tick();x.waits.shift()();await tick();assert.equal(x.requests.length,2);assert.equal(x.requests[1].text,'after');assert.equal(x.a.at(-1)[2],false);assert.equal(x.c.holdState.combo,null);
 x.requests.resolve({status:200,json:async()=>({ok:true})});await tick();
 x=context();vm.runInContext('function releaseHold(){'+block(src.indexOf('function releaseHold('))+'}',x.c);Object.assign(x.c.holdState,{combo:'alt+tab',target:x.c.rfb,key:[2,'Tab']});x.c.releaseHold();assert.deepEqual(x.a,[[2,'Tab',false],[1,'Alt',false]]);
 console.log('PASS Alt+Tab respects text order, releases Alt before following text, and rejects replacement connections');
})().catch(e=>{console.error(e);process.exit(1)});
