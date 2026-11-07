const out=[];
function apply(html,rules){return html.replace(/<(script|style)\b[\s\S]*?<\/\1\s*>|>([^<]+)</gi,(m,t,v)=>v===undefined?m:'>'+rules.reduce((s,r)=>s.split(r.find).join(r.replace||''),v)+'<');}
const cases=[
 ['inline markup','<p>name target</p>',[{find:'name',replace:'<em>new name</em>'}],{index:0,offset:5},{index:0,offset:9}],
 ['deleted preceding loose block','<div>name</div><p>target here</p>',[{find:'name',replace:''}],{index:1,offset:0},{index:0,offset:0}],
 ['deleted anchored block','<div>name</div><p>target here</p>',[{find:'name',replace:''}],{index:0,offset:2},{index:0,offset:0}],
 ['entity spelling','<p>&amp; target</p>',[{find:'amp',replace:'lt'}],{index:0,offset:2},{index:0,offset:2}],
 ['split into new paragraphs','<p>name target here</p>',[{find:'name',replace:'first</p><p>second'}],{index:0,offset:5},{index:1,offset:7}],
 ['inside rewritten paragraph','<p>name target here</p>',[{find:'name',replace:'first</p><p>second'}],{index:0,offset:2},{index:0,offset:0}],
 ['media after deletion','<div>name</div><p><img src=x></p><p>target</p>',[{find:'name',replace:''}],{index:1,offset:0},{index:0,offset:0}],
 ['table foster parenting','<table><tr><td>inside marker</td></tr>outside marker</table>',[{find:'inside',replace:'<em>inner</em>'}],{index:0,offset:3},{index:0,offset:3}],
 ['unicode entity and markup','<p>😀 name target</p>',[{find:'name',replace:'<b>é &amp; 名</b>'}],{index:0,offset:8},{index:0,offset:9}],
];
for(const [name,html,rules,start,expected] of cases){
 const p=new DOMParser(),a=readerBlocks.collect(p.parseFromString(html,'text/html').body),b=readerBlocks.collect(p.parseFromString(apply(html,rules),'text/html').body);
 const map=readerSourceAnchor.buildHtml?readerSourceAnchor.buildHtml(html,b,rules):readerSourceAnchor.build(a,b,rules);
 if(!map)throw Error(name+': no map');
 const actual=map.toDisplay(start);
 if(JSON.stringify(actual)!==JSON.stringify(expected))throw Error(name+': '+JSON.stringify(actual)+' != '+JSON.stringify(expected));
 out.push({name,actual,passed:true});
}
return out;
