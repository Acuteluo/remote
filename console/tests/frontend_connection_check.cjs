const fs=require('fs'),vm=require('vm'),assert=require('assert');
function connectSource(path){const src=fs.readFileSync(path,'utf8');const start=src.indexOf('function connect()'),brace=src.indexOf('{',start);let n=1,end=brace+1;for(;n;end++){if(src[end]==='{')n++;if(src[end]==='}')n--;}return src.slice(start,end);}
for(const name of ['home','term']){
 const sockets=[],timers=[];
 class WS{constructor(){this.readyState=0;sockets.push(this)}close(){this.readyState=3}}
 const c={ws:null,reconnectTimer:0,WebSocket:WS,location:{protocol:'http:',host:'localhost'},document:{hidden:false},statusEl:{},$:()=>({}),render(){},term:{focus(){},write(){}},doFit(){},clearTimeout(){},setTimeout(f){timers.push(f);return timers.length}};
 vm.createContext(c);vm.runInContext(connectSource(require('path').join(__dirname, '../static', name+'.js')),c);
 c.connect();const old=sockets[0],queuedClose=old.onclose;
 if(name==='home')c.ws=null;
 c.connect();queuedClose();assert.equal(c.ws,sockets[1]);assert.equal(timers.length,0);
 sockets[1].onclose();assert.equal(timers.length,1);
 console.log('PASS '+name+' ignores stale close events and reconnects current socket');
}
