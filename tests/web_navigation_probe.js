const fs=require('node:fs'),vm=require('node:vm');
const html=fs.readFileSync(process.argv[2],'utf8');
const scripts=[...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)];
let code=scripts.at(-1)[1].replace("window.addEventListener('hashchange',route);route();status();setInterval(status,5000);",'');
const elements=new Map();
function el(s){if(!elements.has(s))elements.set(s,{innerHTML:'',textContent:'',className:'',classList:{toggle(){},add(){},remove(){}},style:{},dataset:{},matches:()=>false});return elements.get(s)}
const pending=[],errors=[],timers=new Map();let seq=0;
const context=vm.createContext({AbortController,DOMException,URLSearchParams,
 document:{querySelector:el,querySelectorAll:()=>[],body:{dataset:{},classList:{toggle(){}}},activeElement:el('active'),addEventListener(){},hidden:false,createElement:()=>({})},
 location:{hash:'#cockpit',search:'',replace(h){this.hash=h}},
 localStorage:{getItem:()=>null,setItem(){}},window:{addEventListener(){}},
 console:{error(e){errors.push(e)}},fetch:(path,opt)=>new Promise(resolve=>pending.push({path,opt,resolve})),
 setInterval(fn){const id=++seq;timers.set(id,fn);return id},clearInterval(id){timers.delete(id)},setTimeout,clearTimeout});
vm.runInContext(code,context);
const run=code=>vm.runInContext(code,context);
const flush=async()=>{for(let i=0;i<8;i++)await Promise.resolve()};
function respond(p,data){p.resolve({status:200,ok:true,json:async()=>data})}
(async()=>{
 run("PAGES.cockpit=[async()=>{$('#app').innerHTML='cockpit'},1000];PAGES.feed=[async()=>{$('#app').innerHTML='feed'},1000];route();");
 // A → B → A：即便浏览器已收到响应、abort 无效，旧请求也不得覆盖新视图。
 run("var old=api('/fixture').then(()=>{if(page==='cockpit')$('#app').innerHTML='STALE'}).catch(()=>{});");
 const a=pending.shift();
 run("location.hash='#feed';route();location.hash='#cockpit';route();");
 await flush();respond(a,{});await flush();
 console.log('ABA_STALE_WRITE='+String(el('#app').innerHTML==='STALE'));
 console.log('OLD_GET_ABORTED='+String(a.opt.signal?.aborted===true));
 // 尚未返回的旧页请求与新页不同名时也不得刷新旧内容。
 run("var old2=api('/fixture2').then(()=>{$('#app').innerHTML='OLD'}).catch(()=>{});");
 const b=pending.shift();run("location.hash='#feed';route();");await flush();respond(b,{});await flush();
 console.log('CROSS_PAGE_STALE_WRITE='+String(el('#app').innerHTML==='OLD'));
 // 首次加载期间多个自动刷新 tick 只保留一次在途渲染。
 run("var count=0,release;PAGES.feed=[async()=>{count++;await new Promise(r=>release=r)},1000];render(true);render(false);render(false);");
 console.log('OVERLAPPING_RENDER_CALLS='+run('count'));run('release()');await flush();
 // 顶栏状态不随页面取消；写操作不取消。
 run("var bg=api('/api/status',{background:true});var action=post('/fixture/action',{});");
 const bg=pending.shift(),post=pending.shift();run("location.hash='#cockpit';route();");
 respond(bg,{});respond(post,{});await flush();
 console.log('BACKGROUND_CANCELLED='+String(Boolean(bg.opt.signal?.aborted)));
 console.log('POST_CANCELLED='+String(Boolean(post.opt.signal?.aborted)));
 console.log('UNEXPECTED_ERRORS='+errors.length);
})().catch(e=>{console.error(e);process.exitCode=1});
