const fs=require('fs'),vm=require('vm'),assert=require('assert');
const src=fs.readFileSync(require('path').join(__dirname, '../static/vnc.js'),'utf8');
function fn(name){const start=src.indexOf('function '+name+'('),brace=src.indexOf('{',start);let depth=1,end=brace+1;for(;depth;end++){if(src[end]==='{')depth++;if(src[end]==='}')depth--;}return src.slice(start,end);}
(async()=>{
 const trace=[],pending=[];const c={typeBuf:[],typeBusy:false,typeTimer:0,selfClipUntil:0,S:{typeMode:'auto',typeBatch:80},MODS:[],modsActive:{},SPECIAL:{enter:[1,'Enter'],backspace:[2,'BackSpace']},rfb:{sendKey(k,code,down){if(down)trace.push(code)}},connected:true,canSend:()=>true,clearMods(){},releaseHold(){},clearInputAlert(){},showTypeErr(s){trace.push('error')},AbortController,setTimeout:()=>1,clearTimeout(){},Date,fetch(u,args){trace.push(JSON.parse(args.body).text);return new Promise(r=>pending.push(r))}};
 vm.createContext(c);vm.runInContext(fn('flushType')+fn('queueType')+fn('sendSpecial'),c);
 c.queueType('first');c.sendSpecial('enter');c.queueType('next');c.sendSpecial('enter');
 assert.deepEqual(trace,['first']);pending.shift()({status:200,json:async()=>({ok:true})});await new Promise(setImmediate);
 assert.deepEqual(trace,['first','Enter','next']);pending.shift()({status:200,json:async()=>({ok:true})});await new Promise(setImmediate);
 assert.deepEqual(trace,['first','Enter','next','Enter']);
 c.queueType('failed');c.sendSpecial('enter');pending.shift()({status:200,json:async()=>({ok:false,err:'unknown'})});await new Promise(setImmediate);
 assert.equal(trace.at(-1),'error');assert.equal(c.typeBuf.length,0);
 console.log('PASS delayed text precedes Enter; following text keeps order; failure stops queued commands');
})().catch(e=>{console.error(e);process.exit(1)});
