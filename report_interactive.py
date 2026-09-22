"""Offline PNG hover metadata and standalone report controls."""
import base64
import json
import math


def panel(fig, ax, kind, values=None):
    """Call after savefig: axes layout is final; image origin is upper-left."""
    bbox=ax.get_position()
    result=dict(kind=kind,box=[bbox.x0,1-bbox.y1,bbox.width,bbox.height],xlim=list(ax.get_xlim()),ylim=list(ax.get_ylim()))
    if values is not None:
        import numpy as np
        values=np.asarray(values)
        sy=max(1,math.ceil(values.shape[0]/512));sx=max(1,math.ceil(values.shape[1]/512))
        sample=values[::sy,::sx]
        packed=np.round(np.clip(sample,-3276,3276)*10).astype('<i2')
        result['levels']=dict(rows=sample.shape[0],cols=sample.shape[1],row_step=sy,col_step=sx,
                              full_rows=values.shape[0],full_cols=values.shape[1],data=base64.b64encode(packed.tobytes()).decode())
    return result


def inject(page, plots, events, bands, center):
    # Escape '<' so a downloaded/reference string cannot close the script element.
    payload=json.dumps(dict(plots=plots,events=[dict(id=e['id'],start=e['start_s'],end=e['end_s'],low=e['low_offset_hz'],high=e['high_offset_hz'],peak=e.get('peak_time_s',(e['start_s']+e['end_s'])/2),offset=e.get('center_offset_hz',(e['low_offset_hz']+e['high_offset_hz'])/2)) for e in events],bands=bands,center=center),separators=(',',':')).replace('<','\\u003c')
    return page+STYLE+'<script id="inspection-data" type="application/json">'+payload+'</script>'+SCRIPT

STYLE='''<style>
.event-id{background:transparent;border:0;color:#79dfff;text-decoration:underline;cursor:pointer;font:inherit;padding:5px}
tr.event-selected{background:#25475a;outline:2px solid #ffdb70;outline-offset:-2px}
.event-marker{position:absolute;z-index:2;transform:translateY(-50%);background:#10151e;color:#79dfff;border:1px solid #79dfff;border-radius:3px;cursor:pointer;font:bold 12px monospace;padding:2px 4px;line-height:1.2}
.event-marker[aria-pressed="true"]{background:#ffdb70;color:#10151e;border-color:#fff}
.event-marker:focus-visible,.event-id:focus-visible{outline:3px solid #fff}
.event-region{position:absolute;pointer-events:none;border:2px solid #ffdb70;background:#ffdb7025;box-sizing:border-box;min-width:6px;min-height:6px;z-index:1;transform:translate(-50%,-50%)}
.event-details{padding:12px;color:#ffe7a0;white-space:pre-wrap;line-height:1.6;border-top:1px solid #43516a}

.sort-column{font:inherit;font-weight:600;color:#e5edf8;background:transparent;border:0;padding:3px 0;cursor:pointer;text-align:left}
.sort-column:hover{color:#79dfff}.sort-column:focus-visible{outline:2px solid #79dfff;outline-offset:4px}
.sort-arrow{color:#79dfff;margin-left:6px;white-space:nowrap}

.viewer{margin:16px 0 28px;border:1px solid #43516a;border-radius:10px;overflow:hidden;background:#171f2b}
.view-tools{display:flex;gap:8px;align-items:center;padding:10px;flex-wrap:wrap}.view-tools button{background:#29364b;color:#eff7ff;border:1px solid #6a839f;border-radius:5px;padding:6px 12px;cursor:pointer}.view-tools button:focus-visible{outline:2px solid #7fe4ff}.view-tools span{color:#bcd0e6;font-size:13px}
.viewport{max-height:80vh;overflow:auto;position:relative;touch-action:pan-x pan-y}.plot-stage{position:relative;line-height:0;user-select:none}.plot-stage img{display:block;width:100%;height:auto;max-width:none}.plot-stage canvas{position:absolute;inset:0;width:100%;height:100%;pointer-events:none}
.readout{padding:10px 12px;min-height:3em;font:14px/1.5 ui-monospace,monospace;color:#bbf4ff;white-space:pre-wrap}.plot-stage{cursor:crosshair}
</style>'''
SCRIPT=r'''<script>
(()=>{'use strict';
const eventTable=document.querySelector('.scroll > table');
if(eventTable){
 const headers=Array.from(eventTable.rows[0].cells);
 const rows=Array.from(eventTable.rows).slice(1);
 const original=new Map(rows.map((row,i)=>[row,i]));
 const key=(row,col)=>{const text=row.cells[col].textContent.trim();
   if(col===2){const match=text.match(/^(\d+):(\d+):(\d+(?:\.\d+)?)/);return match?Number(match[1])*3600+Number(match[2])*60+Number(match[3]):null;}
   if([0,3,4,5].includes(col)){const value=parseFloat(text);return Number.isFinite(value)?value:null;}
   return text;
 };
 headers.forEach((header,col)=>{
   const label=header.textContent;const button=document.createElement('button');button.type='button';button.className='sort-column';
   button.append(document.createTextNode(label));const arrow=document.createElement('span');arrow.className='sort-arrow';arrow.setAttribute('aria-hidden','true');arrow.textContent='↕';button.append(arrow);
   header.textContent='';header.append(button);header.setAttribute('aria-sort','none');
   button.title=col===2?'Sort by event start time':'Sort by '+label;
   button.addEventListener('click',()=>{
     const ascending=header.getAttribute('aria-sort')!=='ascending';
     headers.forEach(h=>{h.setAttribute('aria-sort','none');h.querySelector('.sort-arrow').textContent='↕';});
     header.setAttribute('aria-sort',ascending?'ascending':'descending');arrow.textContent=ascending?'▲':'▼';
     rows.sort((a,b)=>{const x=key(a,col),y=key(b,col);let order;
       if(x===null||x==='')return y===null||y===''?original.get(a)-original.get(b):1;
       if(y===null||y==='')return -1;
       order=typeof x==='number'?x-y:x.localeCompare(y,undefined,{numeric:true,sensitivity:'base'});
       return (ascending?order:-order)||original.get(a)-original.get(b);
     });
     const parent=eventTable.rows[0].parentNode;rows.forEach(row=>parent.append(row));
   });
 });
}

const data=JSON.parse(document.getElementById('inspection-data').textContent);
const hms=(s)=>{let ms=Math.round(Math.abs(s)*1000);let h=Math.floor(ms/3600000);ms%=3600000;let m=Math.floor(ms/60000);ms%=60000;let sec=Math.floor(ms/1000);return (s<0?'-':'')+[h,m,sec].map(x=>String(x).padStart(2,'0')).join(':')+'.'+String(ms%1000).padStart(3,'0')};
let selectedEvent=null;
const eventRows=new Map();let overviewSelect=null;
if(eventTable)for(const row of Array.from(eventTable.rows).slice(1)){
 const id=Number(row.cells[0].textContent.trim());eventRows.set(id,row);
 const button=document.createElement('button');button.type='button';button.className='event-id';button.textContent=String(id);button.setAttribute('aria-label','Show event '+id+' on waterfall');
 row.cells[0].textContent='';row.cells[0].append(button);button.addEventListener('click',()=>selectEvent(id,true));
}
function selectEvent(id,reveal){selectedEvent=id;for(const [rowId,row] of eventRows)row.classList.toggle('event-selected',rowId===id);if(overviewSelect)overviewSelect(id,reveal);}
const decode=(p)=>{if(p.levels&&!p.levels.view){let raw=atob(p.levels.data);let bytes=Uint8Array.from(raw,x=>x.charCodeAt(0));p.levels.view=new DataView(bytes.buffer)}};
for(const img of document.querySelectorAll('img')){
const panels=data.plots[img.getAttribute('src')];if(!panels)continue;
const box=document.createElement('section');box.className='viewer';img.before(box);
const tools=document.createElement('div');tools.className='view-tools';box.append(tools);
const viewport=document.createElement('div');viewport.className='viewport';box.append(viewport);
const stage=document.createElement('div');stage.className='plot-stage';viewport.append(stage);stage.append(img);
const canvas=document.createElement('canvas');stage.append(canvas);
if(img.getAttribute('src')==='waterfall.png'){
 const p=panels.find(p=>p.kind==='waterfall');
 const position=(offset,time)=>{const x=data.center===null?offset/1000:(data.center+offset)/1e6;return {x:p.box[0]+(x-p.xlim[0])/(p.xlim[1]-p.xlim[0])*p.box[2],y:p.box[1]+(time-p.ylim[1])/(p.ylim[0]-p.ylim[1])*p.box[3]};};
 const region=document.createElement('div');region.className='event-region';region.hidden=true;stage.append(region);
 const details=document.createElement('div');details.className='event-details';details.setAttribute('aria-live','polite');details.textContent='Select an event ID in the table or a [number] on this waterfall.';box.append(details);
 const markers=new Map();
 for(const event of data.events){
  const point=position(event.offset??(event.low+event.high)/2,event.peak??(event.start+event.end)/2);
  const marker=document.createElement('button');marker.type='button';marker.className='event-marker';marker.textContent='['+event.id+']';marker.setAttribute('aria-label','Select event '+event.id);marker.setAttribute('aria-pressed','false');
  marker.style.left=(point.x*100)+'%';marker.style.top=(point.y*100)+'%';
  marker.addEventListener('pointermove',ev=>ev.stopPropagation());
  marker.addEventListener('click',ev=>{ev.stopPropagation();selectEvent(event.id,false)});stage.append(marker);markers.set(event.id,marker);
 }
 overviewSelect=(id,reveal)=>{
  const event=data.events.find(e=>e.id===id);if(!event)return;
  for(const [key,marker] of markers)marker.setAttribute('aria-pressed',String(key===id));
  const a=position(event.low,event.start),b=position(event.high,event.end);
  region.hidden=false;region.style.left=((a.x+b.x)*50)+'%';region.style.top=((a.y+b.y)*50)+'%';region.style.width=(Math.abs(b.x-a.x)*100)+'%';region.style.height=(Math.abs(b.y-a.y)*100)+'%';
  const row=eventRows.get(id);details.textContent='Selected event '+id+' — candidate activity, not an identification\n'+(row?Array.from(row.cells).slice(1,6).map(c=>c.textContent.trim()).join(' | '):hms(event.start)+'–'+hms(event.end));
  if(row&&row.cells[7])details.textContent+='\nFrequency references: '+row.cells[7].innerText;
  if(reveal){box.scrollIntoView({block:'start'});const point=position(event.offset??(event.low+event.high)/2,event.peak??(event.start+event.end)/2);viewport.scrollLeft=point.x*stage.clientWidth-viewport.clientWidth/2;viewport.scrollTop=point.y*img.clientHeight-viewport.clientHeight/2;}
 };
}
const output=document.createElement('div');output.className='readout';output.textContent='Hover over the plotted area for time, frequency and reference details.';box.append(output);
let zoom=1,pinned=false,last=null;
const zoomLabel=document.createElement('span');zoomLabel.textContent='100%';zoomLabel.setAttribute('aria-live','polite');tools.append(zoomLabel);
const hint=document.createElement('span');hint.textContent='Hover to inspect · click to pin · zoom, then scroll to pan';
const setZoom=(value)=>{zoom=Math.max(1,Math.min(8,value));stage.style.width=Math.round(viewport.clientWidth*zoom)+'px';zoomLabel.textContent=Math.round(zoom*100)+'%';if(zoom===1){viewport.scrollTop=0;viewport.scrollLeft=0}draw();};
for(const [label,action] of [['−',()=>setZoom(zoom/1.5)],['+',()=>setZoom(zoom*1.5)],['Reset',()=>{pinned=false;last=null;setZoom(1);output.textContent='Hover over the plotted area to inspect.'}]]){const button=document.createElement('button');button.type='button';button.textContent=label;button.setAttribute('aria-label',label==='+'?'Zoom in':label==='−'?'Zoom out':'Reset view');button.onclick=action;tools.append(button)}tools.append(hint);
function draw(){const w=stage.clientWidth,h=img.clientHeight;canvas.width=w;canvas.height=h;const ctx=canvas.getContext('2d');if(!last)return;ctx.strokeStyle='#00d5ff';ctx.lineWidth=1;ctx.setLineDash([5,4]);ctx.beginPath();ctx.moveTo(last.x*w,0);ctx.lineTo(last.x*w,h);ctx.moveTo(0,last.y*h);ctx.lineTo(w,last.y*h);ctx.stroke();}
function inspect(ev){const rect=img.getBoundingClientRect();const x=(ev.clientX-rect.left)/rect.width,y=(ev.clientY-rect.top)/rect.height;
const p=panels.find(p=>x>=p.box[0]&&x<=p.box[0]+p.box[2]&&y>=p.box[1]&&y<=p.box[1]+p.box[3]);
if(!p){if(!pinned){last=null;draw();output.textContent='Move onto the graph inside its axes.'}return;}
const u=(x-p.box[0])/p.box[2],v=(y-p.box[1])/p.box[3];
const xv=p.xlim[0]+u*(p.xlim[1]-p.xlim[0]);const yv=p.ylim[1]+v*(p.ylim[0]-p.ylim[1]);
last={x,y};draw();let lines=[];
if(p.kind==='trace'){lines.push('Time '+hms(xv)+' | Axis level '+yv.toFixed(1)+' dB/Hz (digital units)');}
else{const freq=data.center===null?xv*1000:xv*1e6;const offset=data.center===null?freq:freq-data.center;
lines.push((data.center===null?'Offset '+(offset/1000).toFixed(3)+' kHz':'Frequency '+(freq/1e6).toFixed(6)+' MHz | Offset '+(offset/1000).toFixed(3)+' kHz'));
if(p.kind==='waterfall'){lines.unshift('Time '+hms(yv)+' from recording start');decode(p);if(p.levels){let q=p.levels;let row=p.time_bin_s?Math.floor(Math.max(0,yv-(p.time_origin_s||0))/p.time_bin_s):Math.floor(v*q.full_rows);row=Math.min(q.rows-1,Math.max(0,Math.floor(row/q.row_step)));let col=Math.min(q.cols-1,Math.floor(u*q.full_cols/q.col_step));let level=q.view.getInt16(2*(row*q.cols+col),true)/10;lines.push('Approx. level '+level.toFixed(1)+' dB above reference band (sampled; not SNR)');}
let ids=data.events.filter(e=>yv>=e.start&&yv<=e.end&&offset>=e.low&&offset<=e.high).map(e=>'#'+e.id);if(ids.length)lines.push('Detected event '+ids.join(', '));}
if(data.center!==null){let bands=data.bands.filter(b=>freq>=b.low_hz&&freq<b.high_hz).map(b=>b.name);if(bands.length)lines.push('Band reference: '+bands.join('; ')+' — not identification');}
}
output.textContent=(pinned?'PINNED · ':'')+lines.join('\n');}
stage.addEventListener('pointermove',ev=>{if(!pinned)inspect(ev)});
stage.addEventListener('click',ev=>{pinned=!pinned;inspect(ev)});
stage.addEventListener('pointerleave',()=>{if(!pinned){last=null;draw()}});
img.addEventListener('load',draw);new ResizeObserver(draw).observe(img);
new ResizeObserver(()=>{stage.style.width=Math.round(viewport.clientWidth*zoom)+'px';draw()}).observe(viewport);
}
})();
</script>'''
