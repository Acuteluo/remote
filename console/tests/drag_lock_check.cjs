const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
const src=fs.readFileSync(path.join(__dirname,'../static/vnc.js'),'utf8');
function body(start){const b=src.indexOf('{',start);let d=1,e=b+1;for(;d;e++){if(src[e]==='{')d++;if(src[e]==='}')d--;}return src.slice(b+1,e-1)}
const events=[],attrs={},btn={textContent:'',classList:{toggle(k,v){btn.on=v}},setAttribute(k,v){attrs[k]=v}};
const c={BTN_LEFT:1,BTN_RIGHT:4,mask:0,dragLock:0,tapDrag:false,lastTapAt:123,Map,$:()=>btn,sendPointer:m=>events.push(m)};
vm.createContext(c);
for(const name of ['press','release','click','syncDragLockUI','resetDragLock'])vm.runInContext('function '+name+'('+({press:'b',release:'b',click:'b',syncDragLockUI:'',resetDragLock:'sendRelease'}[name])+'){'+body(src.indexOf('function '+name+'('))+'}',c);
vm.runInContext('function cycle(){'+body(src.indexOf("$('pad-drag').addEventListener('click', () => {"))+'}',c);
c.syncDragLockUI();assert.equal(btn.textContent,'不拖动');assert.equal(btn.on,false);
c.cycle();assert.equal(c.mask,1);assert.equal(btn.textContent,'左键锁定');assert.equal(btn.on,true);assert.equal(attrs['aria-pressed'],'true');
c.cycle();assert.equal(c.mask,4);assert.equal(btn.textContent,'右键锁定');assert.deepEqual(events,[1,0,4]);
c.click(4);assert.equal(c.mask,4);assert.deepEqual(events,[1,0,4]);
c.click(1);assert.equal(c.mask,4);assert.deepEqual(events.slice(-2),[5,4]);
c.cycle();assert.equal(c.mask,0);assert.equal(btn.on,false);assert.equal(btn.textContent,'不拖动');assert.equal(c.lastTapAt,0);
c.cycle();c.resetDragLock(true);assert.equal(events.at(-1),0);assert.equal(c.dragLock,0);assert.equal(btn.on,false);
c.tapDrag=true;c.mask=1;c.cycle();assert.equal(c.tapDrag,false);assert.deepEqual(events.slice(-2),[0,1]);
c.resetDragLock(false);assert.equal(c.mask,0);assert.equal(btn.on,false);
console.log('PASS left/right/off cycle, release order, held-button clicks, gesture cleanup and disconnect reset');
