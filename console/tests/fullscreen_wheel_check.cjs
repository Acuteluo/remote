const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
const src=fs.readFileSync(path.join(__dirname,'../static/vnc.js'),'utf8');
function fn(name){const start=src.indexOf('function '+name+'('),brace=src.indexOf('{',start);let depth=1,end=brace+1;for(;depth;end++){if(src[end]==='{')depth++;if(src[end]==='}')depth--;}return src.slice(start,end);}
const calls=[];const c={ready:()=>true,fsOn:()=>true,lastFbPointer:{x:321,y:456},mask:0,WHEEL_UP:8,WHEEL_DOWN:16,WHEEL_LEFT:32,WHEEL_RIGHT:64,rfb:{_sock:{}},RFB:{messages:{pointerEvent(s,x,y,m){calls.push([x,y,m])}}},sendPointer(){throw Error('fullscreen must not recalculate CSS coordinates')}};
vm.createContext(c);vm.runInContext(fn('wheelStep'),c);
c.wheelStep(-1,0);c.wheelStep(1,0);assert.deepEqual(calls,[[321,456,8],[321,456,0],[321,456,16],[321,456,0]]);
c.lastFbPointer=null;c.wheelStep(1,0);assert.equal(calls.length,4);
console.log('PASS fullscreen wheel preserves framebuffer position across layout changes');
