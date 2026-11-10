"""Демо-режим мини-приложений: страницы /me?demo=1 и /app?demo=1 показывают интерфейс с выдуманными данными.
Ничего реального не затрагивается: запросы к /api/ подменяются в браузере, на сервер они не уходят."""

_COMMON = r"""
var now=Math.floor(Date.now()/1000),DAY=86400;
function J(o,st){return Promise.resolve(new Response(JSON.stringify(o),{status:st||200,headers:{'Content-Type':'application/json'}}))}
function E(m,st){return Promise.resolve(new Response(m,{status:st||400}))}
var realFetch=window.fetch;
function badge(){var b=document.createElement('div');b.textContent='ДЕМО · данные выдуманы';
 b.style.cssText='position:fixed;top:8px;right:8px;z-index:99999;background:#c6ff1a;color:#0a1200;font:700 11px -apple-system,system-ui,sans-serif;padding:5px 10px;border-radius:99px;pointer-events:none;opacity:.92';
 (document.body?document.body.appendChild(b):document.addEventListener('DOMContentLoaded',function(){document.body.appendChild(b)}))}
badge();
window.fetch=function(url,opt){
  if(typeof url==='string'&&url.indexOf('/api/')===0){
    opt=opt||{};var u=new URL(url,location.href),body={};try{body=opt.body?JSON.parse(opt.body):{}}catch(e){}
    return new Promise(function(res){setTimeout(function(){res(route(u.pathname,u.searchParams,(opt.method||'GET').toUpperCase(),body))},180)}).then(function(p){return p});
  }
  return realFetch.apply(this,arguments)};
"""

_CLIENT = r"""
var K=[{n:1,devices:[{name:'iOS iPhone 17 Pro Max 26.4.1'}]},{n:2,devices:[{name:'Windows PC 11'}]}];
var PLANS=[{key:'m1',name:'1 месяц',days:30,price:249,base:249,label:'',per_month:249,save:0},
 {key:'m3',name:'3 месяца',days:90,price:649,base:747,label:'-13%',per_month:216,save:13},
 {key:'m12',name:'12 месяцев',days:365,price:1990,base:2988,label:'-33%',per_month:166,save:33}];
var ME={id:777000,name:'Алекс',banned:false,until:now+23*DAY,days:23,active:true,trial_available:false,trial_days:5,balance:120,
 bot:'nqyro_bot',support:'voidpulse1337',per_device:true,plans:PLANS,best:'m12',need_email:false,email:'demo@mail.ru',ref_link:'https://t.me/nqyro_bot?start=ref_777000',
 methods:['sbp','card'],slot:{price:99,days:30,max:3,active:0,can:true},ar:{on:true,card:'•• 4242',days:2},disc:'−35% на первую покупку',limit:5,created:now-40*DAY,ref_percent:20,
 topup:{min:50,max:5000,bonus:5}};
var MSGS=[];
function conn(){return {active:true,per_device:true,allowed:5,used:K.reduce(function(a,k){return a+Math.max(1,k.devices.length)},0),can_add:K.length<5,
 happ_dl:'https://www.happ.su',incy_dl:'https://incy.cc',
 keys:K.map(function(k){var s='https://sub.nqyrovpn.site/demo'+k.n;return {n:k.n,sub:s,status:'ACTIVE',devices:k.devices,happ:'https://t.me/nqyro_bot',incy:'https://t.me/nqyro_bot',deep_happ:'happ://add/'+s,deep_incy:'incy://import/'+s}})}}
function route(p,q,m,b){
  if(p==='/api/me')return J(ME);
  if(p==='/api/me/connect')return J(conn());
  if(p==='/api/me/device/add'){var n=K.reduce(function(a,k){return Math.max(a,k.n)},0)+1;K.push({n:n,devices:[]});return J({n:n})}
  var o=p.match(/^\/api\/me\/device\/(\d+)\/off$/);if(o){var n2=+o[1];K=K.filter(function(k){return k.n!==n2});if(!K.length)K=[{n:1,devices:[]}];return J({ok:true,n:n2,split:0})}
  if(p==='/api/me/email')return J({ok:true});
  if(p==='/api/me/trial')return J({until:now+5*DAY});
  if(p==='/api/me/pay'||p==='/api/me/topup')return J({dev:true,pid:1});
  if(p==='/api/me/promo'){return (b.code||'').toUpperCase()==='DEMO'?J({status:'ok',days:7,until:now+30*DAY}):E('Такого кода нет. Попробуй DEMO.',400)}
  if(p==='/api/me/autorenew'){ME.ar.on=!!b.on;return J({ar:ME.ar})}
  if(p==='/api/me/history')return J({payments:[{ts:now-8*DAY,amount:649,plan:'3 месяца',method:'sbp'},{ts:now-98*DAY,amount:249,plan:'1 месяц',method:'card'},{ts:now-130*DAY,amount:120,plan:'Пополнение',method:'sbp'}]});
  if(p==='/api/me/friends')return J({invited:6,earned:1240,balance:ME.balance,percent:20,ref_link:ME.ref_link,
    wd:{avail:840,held:200,min:300,hold_days:7,pending:null,cool_until:0,blocked:false},methods:[{key:'card',name:'Карта'},{key:'sbp',name:'СБП'},{key:'usdt',name:'USDT TRC20'}],
    payouts:[{id:3,amount:500,method:'Карта',status:'paid',ts:now-20*DAY}]});
  if(p==='/api/me/withdraw')return J({id:4,amount:+b.amount||0,to:'карта •• 4242'});
  if(p==='/api/me/support'){if(m==='POST'){MSGS.push({staff:false,text:b.text||'',ts:now});MSGS.push({staff:true,text:'Привет! Это демо-ответ поддержки. Мы на связи.',ts:now+30})}return J({ticket:MSGS.length?1:null,messages:MSGS,support:'voidpulse1337'})}
  if(p==='/api/me/promo'){var c=(b.code||'').toUpperCase();if(c==='NQYRO5')return J({status:'ok',days:5,until:now+28*DAY});return E('Такого кода нет. В демо работает код NQYRO5.',400)}
  if(p==='/api/me/friends')return J({invited:7,earned:840,balance:120,percent:20,ref_link:ME.ref_link});
  if(p==='/api/me/history')return J({payments:[{ts:now-8*DAY,amount:649,plan:'3 месяца',method:'sbp'},{ts:now-98*DAY,amount:249,plan:'1 месяц',method:'card'},{ts:now-130*DAY,amount:300,plan:'Пополнение',method:'sbp'}]});
  if(p==='/api/me/topup')return J({dev:true,pid:2});
  if(p==='/api/me/autorenew'){ME.ar.on=!!b.on;return J({ar:ME.ar})}
  if(p==='/api/me/support'){if(m==='POST'){TK.push({staff:false,text:b.text,ts:now})}return J({ticket:41,messages:TK,support:'voidpulse1337'})}
  if(/^\/api\/me\/payment\/\d+$/.test(p))return J({status:'paid',until:now+53*DAY});
  return E('Демо: такого запроса нет',404)}
"""

_ADMIN = r"""
var U=[{id:777000,username:'alex_v',name:'Алекс',until:now+23*DAY,banned:false,balance:120},
 {id:777001,username:'masha',name:'Маша',until:now+4*DAY,banned:false,balance:0},
 {id:777002,username:null,name:'Игорь',until:now-6*DAY,banned:false,balance:50},
 {id:777003,username:'dima_k',name:'Дима',until:now+87*DAY,banned:false,balance:0},
 {id:777004,username:'spam_acc',name:'Тест',until:0,banned:true,balance:0},
 {id:777005,username:'nastya',name:'Настя',until:now+12*DAY,banned:false,balance:300}];
function keys(){return [{n:1,status:'ACTIVE',devices:[{name:'iOS iPhone 17 Pro Max 26.4.1'},{name:'Android Pixel 8 15'}]},{n:2,status:'ACTIVE',devices:[{name:'Windows PC 11'}]},{n:3,status:'ACTIVE',devices:[]}]}
var chart=[];for(var i=13;i>=0;i--){var d=new Date((now-i*DAY)*1000);chart.push([d.toISOString().slice(0,10),[249,0,649,498,249,1990,249,747,249,0,249,498,649,249][13-i]])}
function sub(n){return 'https://sub.nqyrovpn.site/demo'+n}
function route(p,q,m,b){
  if(p==='/api/app/overview')return J({stats:{users:412,active:238,new_day:9,never:41,banned:6,payers:167,pay_sum:98450,pay_n:386,balances:5240},
    profit:{d1:{revenue:1245,refunds:0,costs:210,pay_n:5},d7:{revenue:9880,refunds:249,costs:1470,pay_n:38},d30:{revenue:41230,refunds:747,costs:6300,pay_n:158}},
    chart:chart,expiring:7,forecast:{payers:14,potential:5120}});
  if(p==='/api/app/users'){var s=(q.get('q')||'').toLowerCase();return J({users:U.filter(function(u){return !s||String(u.id).indexOf(s)>=0||(u.username||'').toLowerCase().indexOf(s)>=0||(u.name||'').toLowerCase().indexOf(s)>=0})})}
  var o=p.match(/^\/api\/app\/user\/(\d+)$/);
  if(o){var u=U.filter(function(x){return x.id===+o[1]})[0];if(!u)return E('no such user',404);
    return J({user:u,src:'tiktok',ref:null,created:now-40*DAY,trial:true,pay_n:4,pay_sum:1544,
      payments:[{ts:now-8*DAY,amount:649,plan:'3 месяца',method:'sbp'},{ts:now-98*DAY,amount:249,plan:'1 месяц',method:'card'},{ts:now-130*DAY,amount:249,plan:'1 месяц',method:'balance'}],
      per_device:true,devices:keys(),devices_err:null})}
  var g=p.match(/^\/api\/app\/user\/(\d+)\/give$/);if(g)return J({until:now+(b.days||30)*DAY,panel_error:null});
  var bn=p.match(/^\/api\/app\/user\/(\d+)\/ban$/);if(bn){var uu=U.filter(function(x){return x.id===+bn[1]})[0];if(uu)uu.banned=!!b.flag;return J({banned:!!b.flag,panel_error:null})}
  var ri=p.match(/^\/api\/app\/user\/(\d+)\/key\/(\d+)\/reissue$/);
  if(ri){var s2=sub(ri[2]);return J({n:+ri[2],sub:s2,happ:'https://bot.nqyrovpn.site/open/happ?sub='+encodeURIComponent(s2),incy:'https://bot.nqyrovpn.site/open/incy?sub='+encodeURIComponent(s2),deep_happ:'happ://add/'+s2,deep_incy:'incy://import/'+s2})}
  if(/^\/api\/app\/user\/\d+\/key\/\d+\/(send|off)$/.test(p))return J({ok:true});
  if(p==='/api/app/nodes')return J({nodes:[{name:'NL-Amsterdam-1',country:'NL',connected:true,xray:true,online:112,disabled:false},{name:'DE-Frankfurt-1',country:'DE',connected:true,xray:true,online:84,disabled:false},
    {name:'FI-Helsinki-1',country:'FI',connected:true,xray:false,online:0,disabled:false},{name:'PL-Warsaw-1',country:'PL',connected:false,xray:false,online:0,disabled:true}]});
  if(p==='/api/app/payments')return J({payments:U.slice(0,5).map(function(u,i){return {id:900+i,ts:now-i*3600*5,amount:[249,649,249,1990,499][i],plan:['1 месяц','3 месяца','1 месяц','12 месяцев','Устройство'][i],method:['sbp','card','sbp','card','sbp'][i],user:u.id,username:u.username,name:u.name}})});
  if(p==='/api/app/log')return J({log:[{ts:now-600,action:'give',target:'777001',details:'+7 дн. (мини-апп)',admin:'owner'},{ts:now-4000,action:'reissue',target:'777000',details:'устройство 1 (мини-апп)',admin:'owner'},{ts:now-9000,action:'ban',target:'777004',details:'мини-апп',admin:'owner'}]});
  return E('Демо: такого запроса нет',404)}
"""

# вне Telegram окна подтверждений и ссылки у Telegram.WebApp не работают: заменяем их обычными браузерными
_TG = r"""
try{var W=window.Telegram&&window.Telegram.WebApp;if(W){
 W.showConfirm=function(t,cb){cb(window.confirm(t))};W.showAlert=function(t,cb){window.alert(t);cb&&cb()};
 W.openLink=function(u){window.open(u,'_blank')};W.openTelegramLink=function(u){window.open(u,'_blank')};W.close=function(){};}}catch(e){}
"""


def inject(html: str, kind: str) -> str:
    """Вставляет демо-скрипт после загрузки telegram-web-app.js (чтобы подправить его методы). kind: 'client' или 'admin'."""
    js = "<script>(function(){" + _TG + _COMMON + (_CLIENT if kind == "client" else _ADMIN) + "})();</script>"
    mark = 'telegram-web-app.js"></script>'
    if mark in html:
        return html.replace(mark, mark + js, 1)
    return html.replace("<head>", "<head>" + js, 1)
