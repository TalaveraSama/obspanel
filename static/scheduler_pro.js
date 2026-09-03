/* TVPlayout 13.0 PRO - Scheduler / Poster editor / Next editor */
(function(){
  const $=(s)=>document.querySelector(s);
  const esc=(x)=>String(x??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\"/g,'&quot;').replace(/'/g,'&#39;');
  let posterModal=null, nextModal=null, searchTimer=null;

  function ensurePosterModal(){
    if(posterModal) return posterModal;
    posterModal=document.createElement('div');
    posterModal.className='modalWrap';
    posterModal.id='posterPickerModal';
    posterModal.style.display='none';
    posterModal.innerHTML=`<div class="modalBox poster-picker">
      <button class="modalClose" id="posterClose">×</button>
      <span class="eyebrow">TMDB · CORRECCIÓN MANUAL</span>
      <h2>🖼️ Seleccionar poster correcto</h2>
      <div id="posterCurrent" class="poster-current"><div><strong>Selecciona una película</strong><small>Busca por título y elige el resultado correcto.</small></div></div>
      <div class="poster-search"><input id="posterQuery" placeholder="Buscar en TMDB..." autocomplete="off"><button id="posterSearchBtn">🔎 BUSCAR</button></div>
      <div id="posterResults" class="poster-results"><div class="info">Escribe el título y pulsa BUSCAR.</div></div>
    </div>`;
    document.body.appendChild(posterModal);
    $('#posterClose').onclick=()=>posterModal.style.display='none';
    $('#posterSearchBtn').onclick=()=>searchPoster($('#posterQuery').value);
    $('#posterQuery').addEventListener('keydown',e=>{if(e.key==='Enter')searchPoster($('#posterQuery').value)});
    posterModal.addEventListener('click',e=>{if(e.target===posterModal)posterModal.style.display='none'});
    return posterModal;
  }

  async function searchPoster(q){
    q=String(q||'').trim(); if(!q)return;
    const box=$('#posterResults'); box.innerHTML='<div class="info">🔎 Buscando en TMDB...</div>';
    try{
      const r=await fetch('/api/tmdb/search?q='+encodeURIComponent(q),{cache:'no-store'});
      const d=await r.json();
      if(!r.ok) throw Error(d.error||'No se pudo buscar TMDB');
      const results=d.results||[];
      if(!results.length){box.innerHTML='<div class="info">No se encontraron resultados. Prueba con el título original.</div>';return}
      box.innerHTML=results.map(x=>{
        const img=x.poster_path?'https://image.tmdb.org/t/p/w342'+x.poster_path:'';
        const year=(x.release_date||'').slice(0,4);
        return `<button type="button" class="poster-result" data-tmdb-id="${x.id}">
          ${img?`<img src="${img}" loading="lazy" alt="">`:'<div style="height:225px;display:grid;place-items:center;color:#6d879e">SIN POSTER</div>'}
          <div class="pr-title">${esc(x.title||x.original_title||'Sin título')}</div>
          <div class="pr-year">${esc(year||'Año desconocido')} · TMDB ${x.id}</div>
          <div class="pr-original">${esc(x.original_title||'')}</div>
        </button>`;
      }).join('');
      box.querySelectorAll('.poster-result').forEach(btn=>btn.onclick=()=>selectPoster(+btn.dataset.tmdbId));
    }catch(e){box.innerHTML='<div class="error">'+esc(e.message)+'</div>'}
  }

  async function openPosterEditor(scheduleId,title){
    ensurePosterModal();
    posterModal.dataset.scheduleId=scheduleId;
    $('#posterQuery').value=title||'';
    $('#posterCurrent').innerHTML=`<div><strong>${esc(title||'Película')}</strong><small>Evento #${scheduleId}. Puedes buscar otro resultado de TMDB.</small></div>`;
    $('#posterResults').innerHTML='<div class="info">Cargando metadata actual...</div>';
    posterModal.style.display='flex';
    try{
      const r=await fetch('/api/tmdb/schedule-assets?ids='+encodeURIComponent(scheduleId),{cache:'no-store'}); const d=await r.json();
      const a=(d.items||[])[0];
      if(a && a.poster_url){$('#posterCurrent').innerHTML=`<img src="${esc(a.poster_url)}" alt=""><div><strong>${esc(a.tmdb_title||title)}</strong><small>TMDB ID: ${esc(a.tmdb_id||'—')} · Poster actual</small></div>`}
    }catch(e){}
  }

  async function selectPoster(tmdbId){
    const sid=+(posterModal?.dataset.scheduleId||0); if(!sid)return;
    const box=$('#posterResults'); box.innerHTML='<div class="info">💾 Guardando poster y metadata...</div>';
    try{
      const r=await fetch('/api/tmdb/select',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({schedule_id:sid,tmdb_id:tmdbId})});
      const d=await r.json(); if(!r.ok||!d.ok)throw Error(d.error||'No se pudo guardar');
      $('#posterCurrent').innerHTML=`<img src="${esc(d.poster_url||'')}" alt=""><div><strong>${esc(d.title||'Actualizado')}</strong><small>TMDB ID: ${esc(d.tmdb_id||tmdbId)} · Guardado localmente</small></div>`;
      box.innerHTML='<div class="info">✓ Poster corregido. Actualizando Scheduler...</div>';
      if(window.loadSchedule) await window.loadSchedule(window.__schedulerPage||1);
      setTimeout(()=>posterModal.style.display='none',700);
    }catch(e){box.innerHTML='<div class="error">'+esc(e.message)+'</div>'}
  }

  async function enhanceSchedule(){
    const table=$('#scheduleTable'); if(!table)return;
    table.classList.add('scheduler-pro-table');
    const head=table.querySelector('thead tr');
    if(head && !head.querySelector('.poster-head')){const th=document.createElement('th');th.className='poster-head';th.textContent='POSTER';head.insertBefore(th,head.children[1]);}
    const rows=[...table.querySelectorAll('tbody tr[data-id]')];
    const ids=rows.map(r=>r.dataset.id).filter(Boolean);
    if(!ids.length)return;
    try{
      const r=await fetch('/api/tmdb/schedule-assets?ids='+encodeURIComponent(ids.join(',')),{cache:'no-store'});
      const d=await r.json(); const map={};(d.items||[]).forEach(x=>map[String(x.schedule_id)]=x);
      rows.forEach(tr=>{
        if(tr.querySelector('.poster-cell'))return;
        const id=String(tr.dataset.id), a=map[id]||{}, cells=tr.children;
        const pc=document.createElement('td');pc.className='poster-cell';
        pc.innerHTML=a.poster_url?`<img class="sched-poster" loading="lazy" src="${esc(a.poster_url)}" alt=""><button class="poster-edit" type="button">🖼 EDITAR</button>`:`<img class="sched-poster missing" src="/static/no-poster.svg" alt=""><button class="poster-edit" type="button">🖼 ELEGIR</button>`;
        pc.querySelector('button').onclick=()=>openPosterEditor(id,cells[1]?.querySelector('b')?.textContent||'');
        tr.insertBefore(pc,cells[1]);
        const titleCell=tr.children[2]; if(titleCell)titleCell.classList.add('title-cell');
      });
    }catch(e){console.debug('scheduler assets',e)}
  }

  function upgradeScheduler(){
    if(window.TV_TAB!=='scheduler')return;
    const originalLoad=window.loadSchedule;
    if(originalLoad && !originalLoad.__pro){
      const wrapped=async function(page=1){window.__schedulerPage=page;const r=await originalLoad(page);await enhanceSchedule();return r};
      wrapped.__pro=true; window.loadSchedule=wrapped;
      const originalSearch=window.scheduleSearch;
      if(originalSearch && !originalSearch.__pro){
        const ss=function(v){clearTimeout(searchTimer);searchTimer=setTimeout(()=>originalSearch(v),220)};ss.__pro=true;window.scheduleSearch=ss;
      }
      const tb=$('.card:has(#scheduleTable) .libraryToolbar');
      if(tb && !tb.querySelector('.schedule-clear')){
        const b=document.createElement('button');b.className='schedule-clear';b.textContent='✕ LIMPIAR FILTRO';b.onclick=()=>{$('#schedSearch').value='';$('#schedDay').value='';scheduleQ='';window.loadSchedule(1)};tb.appendChild(b);tb.classList.add('scheduler-pro-toolbar');
      }
      window.loadSchedule(1);
    }
  }

  function ensureNextModal(){
    if(nextModal)return nextModal;
    nextModal=document.createElement('div');nextModal.className='modalWrap';nextModal.style.display='none';nextModal.id='nextEditModal';
    nextModal.innerHTML=`<div class="modalBox next-edit-modal"><button class="modalClose" id="nextClose">×</button><span class="eyebrow">NEXT · PREPARACIÓN ANTICIPADA</span><h2>✏️ Editar siguiente evento</h2><div id="nextEditBody"></div></div>`;
    document.body.appendChild(nextModal);$('#nextClose').onclick=()=>nextModal.style.display='none';nextModal.addEventListener('click',e=>{if(e.target===nextModal)nextModal.style.display='none'});
    return nextModal;
  }

  async function editNext(){
    const n=window.__nextEvent;if(!n)return;
    ensureNextModal();
    const body=$('#nextEditBody'); body.innerHTML='<div class="info">Cargando pistas...</div>'; nextModal.style.display='flex';
    try{
      const r=await fetch('/api/media/'+n.media_id,{cache:'no-store'});const m=await r.json();
      const aud=JSON.parse(m.audio_json||'[]'),sub=JSON.parse(m.subs_json||'[]');
      body.innerHTML=`<div class="poster-current" id="nextPosterBox"><div><strong>${esc(n.title)}</strong><small>${esc(n.start_at)} · ${(n.duration/60).toFixed(1)} min</small></div></div>
      <div class="next-edit-grid"><label class="full">Inicio<input id="nextStart" type="datetime-local" value="${esc(n.start_at.slice(0,16))}"></label><label>Audio<select id="nextAudio">${aud.map(x=>`<option value="${x.ordinal}" ${x.ordinal==n.audio_index?'selected':''}>${esc((x.language||'und').toUpperCase())}${x.title?' — '+esc(x.title):''}</option>`).join('')||'<option value="0">DEFAULT</option>'}</select></label><label>Subtítulo<select id="nextSub"><option value="-1">Ninguno</option>${sub.map(x=>`<option value="${x.ordinal}" ${x.ordinal==n.subtitle_index?'selected':''}>${esc((x.language||'und').toUpperCase())}${x.title?' — '+esc(x.title):''}${x.external?' (SRT)':''}</option>`).join('')}</select></label><label>Tipo<select id="nextKind"><option ${n.kind==='PROGRAM'?'selected':''}>PROGRAM</option><option ${n.kind==='COMMERCIAL'?'selected':''}>COMMERCIAL</option><option ${n.kind==='PROMO'?'selected':''}>PROMO</option></select></label><div><button id="nextSave">💾 GUARDAR NEXT</button></div></div><div id="nextMsg" class="info" style="display:none"></div>`;
      try{const ar=await fetch('/api/tmdb/schedule-assets?ids='+n.id);const ad=await ar.json();const a=(ad.items||[])[0];if(a?.poster_url)$('#nextPosterBox').insertAdjacentHTML('afterbegin',`<img src="${esc(a.poster_url)}" style="width:70px;height:96px;object-fit:cover;border-radius:6px" alt="">`)}catch(e){}
      $('#nextSave').onclick=async()=>{const msg=$('#nextMsg');msg.style.display='block';msg.textContent='Guardando...';try{const rr=await fetch('/api/schedule/update',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({sid:n.id,start_at:$('#nextStart').value,audio_index:+$('#nextAudio').value,subtitle_index:+$('#nextSub').value,kind:$('#nextKind').value})});const dd=await rr.json();if(!dd.ok)throw Error(dd.error||'No se pudo guardar');msg.textContent=dd.live?'✓ Actualizado en vivo.':'✓ NEXT actualizado antes de salir al aire.';if(window.refreshPlayout)await window.refreshPlayout();setTimeout(()=>nextModal.style.display='none',800)}catch(e){msg.textContent='✕ '+e.message}};
    }catch(e){body.innerHTML='<div class="error">'+esc(e.message)+'</div>'}
  }

  function upgradeNext(){
    if(window.TV_TAB!=='playout')return;
    window.openNextEditor=editNext;
    const next=document.querySelector('.nextHero');if(next)next.classList.add('next-pro');
    if(window.__nextEvent)editNextPoster(window.__nextEvent);
  }
  async function editNextPoster(n){try{const r=await fetch('/api/tmdb/schedule-assets?ids='+n.id);const d=await r.json();const a=(d.items||[])[0];if(a?.poster_url){const h=$('#nextTitle');if(h&&!h.parentElement.querySelector('.next-poster'))h.insertAdjacentHTML('afterend',`<img class="next-poster" src="${esc(a.poster_url)}" alt="">`)}}catch(e){}}

  function start(){
    upgradeScheduler();
    upgradeNext();
    if(window.TV_TAB==='playout'){
      const old=window.refreshPlayout;
      if(old&&!old.__pro){const f=async()=>{const r=await old();upgradeNext();return r};f.__pro=true;window.refreshPlayout=f;}
    }
  }
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',start);else start();
})();
