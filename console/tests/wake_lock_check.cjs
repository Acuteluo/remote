const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
const src=fs.readFileSync(path.join(__dirname,'../static/vnc.js'),'utf8');
const code=src.slice(src.indexOf('let wakeLock = null;'),src.indexOf('\napplyAwake();'));
(async()=>{let requested=0;const pending=[],alerts=[];const c={navigator:{wakeLock:{request(){requested++;return new Promise(r=>pending.push(r))}}},document:{visibilityState:'visible'},S:{keepAwake:true},showInputAlert(s){alerts.push(s)}};vm.createContext(c);vm.runInContext(code,c);
function lock(){return{released:0,listeners:[],async release(){this.released++},addEventListener(t,f){this.listeners.push(f)}}}
let p=c.applyAwake(),second=c.applyAwake();assert.equal(requested,1);c.S.keepAwake=false;await c.applyAwake();const late=lock();pending.shift()(late);await Promise.all([p,second]);assert.equal(late.released,1);
c.S.keepAwake=true;p=c.applyAwake();const old=lock();pending.shift()(old);await p;c.S.keepAwake=false;await c.applyAwake();assert.equal(old.released,1);
c.S.keepAwake=true;p=c.applyAwake();const current=lock();pending.shift()(current);await p;old.listeners[0]();await c.applyAwake();assert.equal(requested,3);c.S.keepAwake=false;await c.applyAwake();assert.equal(current.released,1);
c.S.keepAwake=true;p=c.applyAwake();c.document.visibilityState='hidden';const hidden=lock();pending.shift()(hidden);await p;assert.equal(hidden.released,1);
console.log('PASS concurrent requests share one lock; disabled/hidden late locks release; stale release preserves current lock');})().catch(e=>{console.error(e);process.exit(1)});
