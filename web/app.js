let miUbic=null;
let msCat="todas";
const ORDEN=["heridos","rescate","desaparecidos","mascotas","ofrecimientos","donaciones","info","contacto"];
// Aterrizaje action-first (pedido de Gustavo): el visitante nuevo cae en "Lo que
// hace falta" (necesidades con barra y botón «Lo llevo»), no en el texto de Registro.
let activo="necesidades", q="", zonaSel="todas";
const DESAP_LOTE=20;
let desapMostrar=DESAP_LOTE;

function esc(s){return (s||"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));}
function tiempoDesde(iso){
  const d=new Date(iso),min=Math.round((new Date()-d)/60000);
  if(min<1)return"ahora"; if(min<60)return"hace "+min+" min";
  const h=Math.round(min/60); if(h<24)return"hace "+h+" h";
  return"hace "+Math.round(h/24)+" d";
}
function telDe(t){const m=String(t||"").match(/\+?\d[\d\s().\-]{6,}\d/);return m?m[0].replace(/[^\d+]/g,""):null;}
function coincide(txt){return !q || txt.toLowerCase().includes(q);}
function vacio(msg){return `<div class="vacio"><span class="em">🕊️</span>${esc(msg)}</div>`;}
function matchZona(x){return zonaSel==="todas" || (x.zona||"Otras zonas")===zonaSel;}
function itemsTab(){
  if(activo==="info"||activo==="apoya"||activo==="buscar"||activo==="emergencias"||activo==="sismos"||activo==="servicios"||activo==="situacion"||activo==="necesidades"||activo==="oferta"||activo==="directorio"||activo==="manos"||activo==="llamados"||activo==="pedir"||activo==="angeles") return [];
  if(activo==="desaparecidos") return DATOS.filter(m=>m.categoria==="desaparecidos");
  // La categoría "info" del canal navega con la clave "alertas": la clave "info"
  // ya la usa la sección Registro y chocaban.
  const k=activo==="alertas"?"info":activo;
  return DATOS.filter(m=>m.categoria===k);
}
function poblarZonas(items){
  const sel=document.getElementById("zona");
  const zonas=[...new Set(items.map(x=>x.zona||"Otras zonas"))]
    .sort((a,b)=> a==="Otras zonas"?1 : b==="Otras zonas"?-1 : a.localeCompare(b,"es"));
  sel.innerHTML=`<option value="todas">📍 Todas las zonas</option>`+
    zonas.map(z=>`<option value="${esc(z)}">${esc(z)}</option>`).join("");
  if(zonaSel!=="todas" && zonas.includes(zonaSel)) sel.value=zonaSel;
  else { zonaSel="todas"; sel.value="todas"; }
  sel.style.display = zonas.length>1 ? "" : "none";
}
function mapa(ubic){return ubic?`<a class="btn" target="_blank" rel="noopener" href="https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(ubic+" Venezuela")}">🗺️ Cómo llegar</a>`:"";}
function compartirBtn(texto,url){return `<button class="btn b-share" data-share="${esc(texto)}" data-url="${esc(url||"")}">💬 Compartir</button>`;}
function copiarBtn(url){return url?`<button class="btn" data-copiar="${esc(url)}">🔗 Copiar enlace</button>`:"";}
function denunBtn(tipo,id){return `<button class="btn b-report" data-denunciar="${tipo}:${id}">🚩 Denunciar</button>`;}

function listaTabs(){
  const t=[];
  if(LLAMADOS.length) t.push({key:"llamados",ico:"📣",nom:"Llamados de apoyo",n:LLAMADOS.length});
  if(PEDIDOS.length) t.push({key:"pedir",ico:"🆘",nom:"Pedidos de ayuda",n:PEDIDOS.length});
  if(DIRECTORIO.length) t.push({key:"directorio",ico:"🗺️",nom:"Red de Acopios y Centros de Ayuda",n:DIRECTORIO.length});
  if(ALIADOS.length) t.push({key:"manos",ico:"🤝",nom:"Manos Solidarias",n:ALIADOS.length});
  t.push({key:"info",ico:"📋",nom:"Registro"});
  if(OFERTA.length) t.push({key:"oferta",ico:"🎁",nom:"Disponible para entregar",n:OFERTA.length});
  if(NECESIDADES.length||SEGUIMIENTO.length) t.push({key:"necesidades",ico:"📥",nom:"Lo que hace falta",n:NECESIDADES.reduce((s,p)=>s+p.items.length,0)+SEGUIMIENTO.reduce((s,p)=>s+p.items.length,0)});
  // Todo lo que se alimenta del canal de emergencias viales va agrupado en un
  // solo desplegable "Ángeles de la Autopista" (categorías + Servicios + Situación).
  const sub=[];
  if(RESUMEN) sub.push({key:"situacion",ico:"📊",nom:"Situación"});
  ORDEN.forEach(k=>{const n=DATOS.filter(m=>m.categoria===k).length; if(n) sub.push({key:k==="info"?"alertas":k,ico:CATS[k].icono,nom:CATS[k].nombre,n});});
  if(SERVICIOS.length) sub.push({key:"servicios",ico:"🔌",nom:"Servicios",n:SERVICIOS.length});
  if(sub.length) t.push({key:"angeles",ico:"🚨",nom:"Ángeles de la Autopista",n:sub.reduce((s,x)=>s+(x.n||0),0),sub});
  t.push({key:"buscar",ico:"🔦",nom:"Buscar persona"});
  t.push({key:"emergencias",ico:"📞",nom:"Emergencias"});
  t.push({key:"sismos",ico:"🌎",nom:"Sismos"});
  t.push({key:"apoya",ico:"🤍",nom:"Apoya la plataforma"});
  return t;
}
function pintarTabs(){
  const cont=document.getElementById("secmenu");
  if(!cont) return;
  const T=listaTabs();
  if(!T.length){cont.innerHTML="";return;}
  const CORE=["llamados","pedir","info","directorio","manos","necesidades","oferta","angeles"];
  const planos=T.flatMap(t=>t.sub?t.sub:[t]);
  if(!activo || (!planos.some(t=>t.key===activo) && !CORE.includes(activo))) activo=T[0].key;
  const grupo=T.find(t=>t.sub && t.sub.some(s=>s.key===activo));
  const chip=(t,on,sub)=>`<button class="secchip${sub?" sub":""}${on?" on":""}" data-k="${t.key}">${t.ico} ${esc(t.nom)}${t.n!=null?` <span class="pill">${t.n}</span>`:""}</button>`;
  cont.innerHTML=T.map(t=>chip(t,(t.key===activo)||(!!grupo&&grupo.key===t.key),false)).join("")
    +(grupo?`<div class="subchips">`+grupo.sub.map(s=>chip(s,s.key===activo,true)).join("")+`</div>`:"");
  cont.querySelectorAll(".secchip[data-k]").forEach(el=>el.onclick=()=>{
    const k=el.dataset.k, g=T.find(t=>t.key===k && t.sub);
    irTab(g?g.sub[0].key:k);
  });
}

function tarjeta(m){
  const C=CATS[m.categoria]||{icono:"•",nombre:m.categoria};
  const tel=telDe(m.contacto);
  const fecha=`${tiempoDesde(m.tg_fecha)}${m.vigencia==="posible_desactualizado"?'<span class="vieja">posible viejo</span>':""}`;
  const sh=`${m.titulo}`+(m.ubicacion?` — 📍 ${m.ubicacion}`:"")+` — ${m.detalle}`+(m.contacto?` — 📞 ${m.contacto}`:"");
  // Si el mensaje original trae bastante más que el resumen (ej. una lista de
  // nombres), lo mostramos en un desplegable para no perder esa info.
  const _orig=(m.texto_original||"").trim();
  const completo=(_orig && _orig.length>(m.detalle||"").length+60)
    ? `<details class="completo"><summary>📋 Ver lista / mensaje completo</summary><div class="completo-txt">${esc(_orig)}</div></details>`
    : "";
  return `<div class="card">
    <div class="cab"><span class="ico">${C.icono}</span>
      <div><div class="cat">${esc(C.nombre)}</div><h3>${esc(m.titulo)}</h3></div></div>
    ${m.foto?`<img class="foto" src="${esc(m.foto)}" loading="lazy" alt="">`:""}
    ${m.ubicacion?`<div class="meta">📍 <b>${esc(m.ubicacion)}</b></div>`:""}
    <div class="det">${esc(m.detalle)}</div>${completo}
    ${m.necesidades?`<div class="meta">🧰 ${esc(m.necesidades)}</div>`:""}
    ${m.contacto&&tel?`<a class="btn-tel" href="tel:${tel}">📞 Llamar ${esc(m.contacto)}</a>`:(m.contacto?`<div class="meta">📞 ${esc(m.contacto)}</div>`:"")}
    <div class="acciones">${mapa(m.ubicacion)}${compartirBtn(sh,m.link)}</div>
    <div class="pie"><span>${fecha}</span>
      <a href="${esc(m.link)}" target="_blank" rel="noopener">ver original ↗</a></div>
  </div>`;
}
const DESAP_EST={buscando:["🟡 Buscando","e-buscando"],aparecio:["✅ Apareció","e-aparecio"],fallecida:["🕯️ Fallecida","e-fallecida"]};
function itemsDesap(){
  const ord={buscando:0,aparecio:1,fallecida:2};
  return DATOS.filter(m=>m.categoria==="desaparecidos" && matchZona(m) && coincide(m.titulo+" "+(m.ubicacion||"")+" "+m.detalle))
    .sort((a,b)=>(ord[a.estado_persona||"buscando"]||0)-(ord[b.estado_persona||"buscando"]||0));
}
function dpersonCard(m){
  const est=m.estado_persona||"buscando", e=DESAP_EST[est]||DESAP_EST.buscando;
  const tel=telDe(m.contacto);
  const sh=`🔎 SE BUSCA: ${m.titulo}`+(m.ubicacion?` — visto en ${m.ubicacion}`:"")+(m.detalle?` — ${m.detalle}`:"")+(m.contacto?` — 📞 ${m.contacto}`:"");
  const reportar=(est==="buscando")?`<button class="btn b-report" data-reportar="${m.tg_id}">✋ Reportar novedad</button>`:"";
  return `<div class="dperson${est!=="buscando"?" resuelto":""}">
    ${m.foto?`<img class="fimg" src="${esc(m.foto)}" loading="lazy" alt="">`:`<div class="fimg"></div>`}
    <div class="dbody"><span class="ebadge ${e[1]}">${e[0]}</span><h3>${esc(m.titulo)}</h3>
      ${m.ubicacion?`<div class="meta">📍 ${esc(m.ubicacion)}</div>`:""}
      ${m.detalle?`<div class="meta">${esc(m.detalle)}</div>`:""}
      ${est==="buscando"&&tel?`<a class="btn-tel" href="tel:${tel}">📞 Llamar</a>`:(m.contacto?`<div class="meta">📞 ${esc(m.contacto)}</div>`:"")}
      <div class="acciones">${compartirBtn(sh,m.link)}${reportar}</div>
      <div class="pie"><span>${tiempoDesde(m.tg_fecha)}</span>
        <a href="${esc(m.link)}" target="_blank" rel="noopener">ver ↗</a></div>
    </div></div>`;
}
function masDesapHTML(restantes){
  return restantes>0
    ? `<div id="mas-desap-wrap" style="text-align:center;margin:14px 0"><button class="btn" id="mas-desap" onclick="cargarMasDesap()">⬇️ Cargar ${Math.min(DESAP_LOTE,restantes)} más (quedan ${restantes})</button></div>`
    : "";
}
function renderDesap(){
  const items=itemsDesap();
  if(!items.length) return vacio("No hay reportes de desaparecidos.");
  return `<div class="galeria">`+items.slice(0,desapMostrar).map(dpersonCard).join("")+`</div>`+masDesapHTML(items.length-desapMostrar);
}
// Carga el siguiente lote AÑADIENDO tarjetas al final (no repinta las ya mostradas:
// con cientos en pantalla, un re-render completo se siente lento en teléfonos sencillos).
let _cargandoMas=false;
function cargarMasDesap(){
  if(_cargandoMas) return;
  const btn=document.getElementById("mas-desap"), gal=document.querySelector("#contenido .galeria");
  if(!btn||!gal){ desapMostrar+=DESAP_LOTE; render(); return; }
  _cargandoMas=true; btn.disabled=true;
  btn.innerHTML=`<span class="spin"></span> Cargando…`;
  setTimeout(()=>{ // deja que el spinner alcance a pintarse antes del trabajo de DOM
    _cargandoMas=false;
    if(activo!=="desaparecidos") return;
    const items=itemsDesap();
    const nuevos=items.slice(desapMostrar,desapMostrar+DESAP_LOTE);
    desapMostrar+=nuevos.length;
    gal.insertAdjacentHTML("beforeend",nuevos.map(dpersonCard).join(""));
    const restantes=items.length-desapMostrar;
    if(restantes>0){ btn.disabled=false; btn.innerHTML=`⬇️ Cargar ${Math.min(DESAP_LOTE,restantes)} más (quedan ${restantes})`; }
    else{ const w=document.getElementById("mas-desap-wrap"); if(w) w.remove(); }
  },60);
}
// Nota: NADA de scroll infinito a propósito (decisión 2026-07-04): en el celular hace
// que la página "no termine nunca". Cargar más tarjetas es SIEMPRE un toque explícito.
function renderCategoria(k){
  const items=DATOS.filter(m=>m.categoria===k && matchZona(m) && coincide(m.titulo+" "+(m.ubicacion||"")+" "+m.detalle+" "+(m.necesidades||"")));
  if(!items.length) return vacio("No hay nada que coincida.");
  return items.map(tarjeta).join("");
}
function renderEmergencias(){
  let html=`<div class="aviso">Números de referencia (base: redayudavenezuela.com). Verifica antes de llamar.</div>`;
  let hay=false;
  EMERGENCIAS.forEach(g=>{
    const items=g.items.filter(it=>coincide(it.nombre+" "+it.numeros.join(" ")+" "+g.grupo));
    if(!items.length) return;
    hay=true;
    html+=`<h3 class="grupo-titulo">${esc(g.grupo)}</h3>`;
    html+=items.map(it=>{
      const botones=it.numeros.map(n=>{
        const tel=String(n).replace(/[^0-9+*#]/g,"");
        return `<a class="btn-tel" href="tel:${tel}">📞 ${esc(n)}</a>`;
      }).join("");
      return `<div class="centro"><div class="info">
        <div class="cn">${esc(it.nombre)}</div>
        <div class="acciones">${botones}</div>
      </div></div>`;
    }).join("");
  });
  return hay ? html : vacio("Sin resultados.");
}
function statCard(num,label,color){
  return `<div class="stat" style="border-top-color:${color}"><div class="stat-n">${esc(String(num))}</div><div class="stat-l">${esc(label)}</div></div>`;
}
function renderSituacion(){
  const r=RESUMEN||{};
  let html=`<div class="aviso">📊 Panorama de la emergencia según lo recopilado del canal. Cifras <b>referenciales, no oficiales</b> — verifica.</div>`;
  html+=`<div class="stats">`+
    statCard(r.desaparecidos_buscando,"Desaparecidos en búsqueda","var(--desaparecidos)")+
    statCard(r.desaparecidos_resueltos,"Personas localizadas","var(--disponible)")+
    statCard(r.heridos,"Reportes de heridos","var(--heridos)")+
    statCard(r.rescate,"Pedidos de rescate","var(--rescate)")+
    (r.ofrecimientos?statCard(r.ofrecimientos,"Ofrecimientos de ayuda","var(--ofrecimientos)"):"")+
    (r.servicios_zonas?statCard(r.servicios_zonas,"Zonas con reporte de servicios","var(--info)"):"")+
  `</div>`;
  if(r.nuevos_24h) html+=`<div class="cambios">🆕 <b>${esc(String(r.nuevos_24h))}</b> actualizaciones nuevas en las últimas 24 h.</div>`;
  html+=`<div class="pie" style="justify-content:center;margin-top:14px">Actualizado: ${esc(r.actualizado||"")}</div>`;
  return html;
}
const PUNTO_ICO={acopio:"📦",refugio:"🏠",hospital:"🏥",cdi:"🩺",maternidad:"🤰",clinica:"➕",fundacion:"🏛",otro:"📍"};
const PUNTO_NOM={acopio:"Centro de acopio",refugio:"Refugio",hospital:"Hospital",cdi:"CDI",maternidad:"Maternidad",clinica:"Clínica",fundacion:"Fundación",otro:"Punto"};
// Orden y etiqueta de las secciones (encabezados) dentro de Requerimientos y Directorio.
const PUNTO_GRUPOS=[["acopio","📦 Centros de acopio"],["refugio","🏠 Refugios"],["hospital","🏥 Hospitales"],["cdi","🩺 CDI"],["maternidad","🤰 Maternidades"],["clinica","➕ Clínicas"],["fundacion","🏛 Fundaciones aliadas"],["otro","📍 Otros puntos"]];
// Agrupa los puntos por tipo, respetando PUNTO_GRUPOS; solo devuelve las secciones con datos.
function agruparPorTipo(items){
  const map={};
  items.forEach(p=>{const k=PUNTO_ICO[p.tipo_punto]?p.tipo_punto:"otro";(map[k]=map[k]||[]).push(p);});
  return PUNTO_GRUPOS.filter(([k])=>map[k]&&map[k].length).map(([k,lbl])=>[lbl,map[k]]);
}
const DIR_CAMPOS=[
  ["tipo_inmueble","🏚️ Inmueble"],["aforo","👥 Aforo"],["banos","🚻 Baños"],["duchas","🚿 Duchas"],
  ["cocina","🍳 Cocina"],["agua_potable","🚰 Agua potable"],["electricidad","⚡ Electricidad"],
  ["planta","🔋 Planta/generador"],["camas","🛏️ Camas disp."],["acceso_vehiculos","🚛 Acceso vehículos"],
  ["accesibilidad","♿ Accesibilidad"],["almacenamiento","📦 Almacenamiento"],
  ["refrigeracion","❄️ Refrigeración"],["cobertura","🗺️ Cobertura"],["areas","🎯 Atiende"],["horario","🕐 Horario"]];
function dirClase(v){
  const s=(v||"").toString().trim().toLowerCase();
  if(s==="sí"||s==="si") return "yes";
  if(s==="no") return "non";
  if(s.startsWith("no aplica")||s.startsWith("no posee")||s==="") return "naa";
  return "";
}
function dirIcono(v){const c=dirClase(v);return c==="yes"?"✅ ":c==="non"?"🚫 ":c==="naa"?"➖ ":"";}
function dirItem(p){
  const ico=PUNTO_ICO[p.tipo_punto]||"📍";
  const sub=[PUNTO_NOM[p.tipo_punto]||"Punto",p.ubicacion].filter(Boolean).map(esc).join(" · ");
  const abrir=q?" open":"";
  let grid="";
  DIR_CAMPOS.forEach(([k,lbl])=>{
    const v=(p[k]||"").toString().trim(); if(!v) return;
    const wide=(k==="almacenamiento")?" wide":"";
    grid+=`<div class="dir-f${wide}"><div class="dir-k">${lbl}</div><div class="dir-v ${dirClase(v)}">${dirIcono(v)}${esc(v)}</div></div>`;
  });
  if(p.ubicacion) grid+=`<div class="dir-f wide"><div class="dir-k">📍 Ubicación</div><div class="dir-v">${esc(p.ubicacion)}</div></div>`;
  const tel=telDe(p.contacto);
  const sh=`${ico} ${PUNTO_NOM[p.tipo_punto]||"Punto"}: ${p.punto}`+(p.ubicacion?` — 📍 ${p.ubicacion}`:"")+(p.contacto?` — 📞 ${p.contacto}`:"");
  return `<details class="centro nec-acc"${abrir}>
    <summary><span class="nec-tit">${ico} ${esc(p.punto)}</span><span class="nec-sub">${sub}</span></summary>
    <div class="nec-body">
      <div class="dir-grid">${grid}</div>
      ${p.responsable?`<div class="dir-resp"><div class="dir-k">🧑‍💼 Responsable</div><div class="dir-v">${esc(p.responsable)}</div></div>`:""}
      ${tel?`<a class="btn-tel" href="tel:${tel}" style="margin-top:8px">📞 Llamar ${esc(p.contacto)}</a>`:(p.contacto?`<div class="meta" style="margin-top:8px">📞 ${esc(p.contacto)}</div>`:"")}
      <div class="acciones">${mapaBtn(p)}${compartirBtn(sh, paginaPunto(p))}${copiarBtn(paginaPunto(p))}${denunBtn("centro",p.id)}</div>
    </div>
  </details>`;
}
function tieneGps(p){return p.lat!=null&&p.lon!=null&&p.lat!==""&&p.lon!==""&&!isNaN(+p.lat)&&!isNaN(+p.lon);}
function mapsUrl(p){return tieneGps(p)?`https://www.google.com/maps/dir/?api=1&destination=${p.lat},${p.lon}`:(p.ubicacion?`https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(p.ubicacion+" Venezuela")}`:SITE_URL);}
function mapaBtn(p){return (tieneGps(p)||p.ubicacion)?`<a class="btn" target="_blank" rel="noopener" href="${mapsUrl(p)}">🗺️ Cómo llegar</a>`:"";}
function renderDirectorio(){
  const items=DIRECTORIO.filter(p=>coincide(p.punto+" "+(p.tipo_punto||"")+" "+(p.tipo_inmueble||"")+" "+(p.ubicacion||"")+" "+(p.responsable||"")));
  if(!items.length) return vacio("Aún no hay puntos con ficha completa en el directorio.");
  const sinGps=items.filter(p=>!tieneGps(p)).length;
  let html=`<div class="aviso">🗺️ <b>Red de Acopios y Centros de Ayuda</b>: cada punto de la red de colaboradores en el mapa, con su ficha (qué tiene: agua, camas, electricidad…) completada por sus responsables vía el bot. Toca un pin o un punto para ver los detalles. <b>No es información oficial</b>, verifica.</div>`;
  html+=`<div id="mapa-red" class="mapa-red"></div>`+MAPA_LEYENDA;
  if(sinGps) html+=`<div class="aviso aviso-mini">📍 ${sinGps} punto${sinGps>1?"s":""} todavía sin ubicación en el mapa. Su responsable la comparte enviando <b>/ubicacion</b> al bot.</div>`;
  agruparPorTipo(items).forEach(([lbl,gr])=>{
    html+=`<h3 class="grupo-titulo">${lbl} · ${gr.length}</h3>`;
    html+=gr.map(dirItem).join("");
  });
  // Autoregistro: puntos DETECTADOS por el canal Emergencias Viales que aún no
  // tienen responsable en la red — cada uno con su botón de reclamo (el registro
  // llega pre-llenado con los datos que el canal ya dio).
  // APARCADO por Gustavo (2026-07-05): riesgo de suplantación en el reclamo abierto.
  // Para activarlo: RECLAMO_CANAL=true + backend con aprobación manual de reclamos.
  const RECLAMO_CANAL=false;
  const canal=RECLAMO_CANAL?CANAL.filter(c=>coincide(c.nombre+" "+(c.ubicacion||""))):[];
  if(canal.length){
    html+=`<h3 class="grupo-titulo">📡 Detectados en el canal · ${canal.length}</h3>`;
    html+=`<div class="aviso aviso-mini">Estos puntos aparecieron en el canal <b>Emergencias Viales</b> pero aún <b>no tienen responsable</b> en la red. ¿Manejas alguno? <b>Reclámalo</b>: confirmas sus datos en 2 minutos y la red te hace llegar justo lo que te falta.</div>`;
    html+=canal.map(canalItem).join("");
  }
  return html;
}
function canalItem(c){
  const est=c.estado==="disponible"?`<span class="urg" style="background:#e7f6f4;color:#0b5f59">🟢 Recibiendo</span>`
           :(c.estado==="lleno"?`<span class="urg" style="background:#fdeaea;color:#9b2226">🔴 Lleno</span>`:"");
  const url="/registrar.html?canal=1&tipo="+(c.salud?"hospital":"acopio")
    +"&punto="+encodeURIComponent(c.nombre)+"&ubicacion="+encodeURIComponent(c.ubicacion||"");
  return `<div class="centro">
    <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap"><b>${c.salud?"🏥":"📦"} ${esc(c.nombre)}</b>${est}<span class="meta">· ${c.reportes} reporte${c.reportes==1?"":"s"} del canal</span></div>
    ${c.ubicacion?`<div class="meta" style="margin-top:4px">📍 ${esc(c.ubicacion)}</div>`:""}
    <div class="meta" style="margin-top:2px">🕐 ${tiempoDesde(c.tg_fecha)}</div>
    <div class="acciones"><a class="btn b-llevar b-accweb" href="${url}">🏠 Es mi punto — reclamarlo</a></div>
  </div>`;
}
// Lazy-load de Leaflet (mapa) solo al abrir la pestaña; cachea la promesa.
let _leafletP=null;
function cargarLeaflet(){
  if(window.L) return Promise.resolve();
  if(_leafletP) return _leafletP;
  _leafletP=new Promise((res,rej)=>{
    const css=document.createElement("link");
    css.rel="stylesheet"; css.href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css";
    css.crossOrigin=""; document.head.appendChild(css);
    const s=document.createElement("script");
    s.src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js";
    s.crossOrigin=""; s.onload=res; s.onerror=rej; document.head.appendChild(s);
  });
  return _leafletP;
}
// --- Mapa compartido: capa de centros (navy) + capa de Manos Solidarias (teal) ---
const MS_ICO={comida:"🍲",herramientas:"🔦",transporte:"🚐",agua:"💧",carga:"🔌",salud:"🩹",hospedaje:"🛏️",remoto:"💻",estructural:"🏗️",oficios:"🛠️",otro:"📦"};
const MS_LABEL={comida:"Comida",herramientas:"Herramientas/linternas",transporte:"Transporte",agua:"Agua",carga:"Carga de teléfonos",salud:"Salud",hospedaje:"Hospedaje temporal",remoto:"Remoto / En Línea",estructural:"Estructural / Inspección",oficios:"Oficios",otro:"Otra ayuda"};
const MAPA_LEYENDA=`<div class="leyenda"><span><span class="leg-dot" style="background:#003C67"></span>Centros de la red</span><span><span class="leg-dot" style="background:#1E9C94"></span>Manos Solidarias</span></div>`;
function waLink(c){const d=telDe(c);if(!d)return"";let n=d.replace(/[^0-9]/g,"");if(d.startsWith("+"))return"https://wa.me/"+n;if(n.startsWith("0"))n="58"+n.slice(1);else if(!n.startsWith("58"))n="58"+n;return"https://wa.me/"+n;}
let _mapa=null;
function popupRed(p){
  const ico=PUNTO_ICO[p.tipo_punto]||"📍";
  const tel=telDe(p.contacto);
  const llegar=mapsUrl(p);
  return `<div class="pop"><b>${ico} ${esc(p.punto)}</b><br>`+
    `<span class="pop-sub">${esc(PUNTO_NOM[p.tipo_punto]||"Punto")}${p.ubicacion?" · "+esc(p.ubicacion):""}</span><br>`+
    (tel?`<a href="tel:${tel}">📞 ${esc(p.contacto)}</a> · `:"")+
    `<a href="${llegar}" target="_blank" rel="noopener">🗺️ Cómo llegar</a></div>`;
}
function popupMS(a){
  const ico=MS_ICO[a.categoria]||"🤝";
  const llegar=mapsUrl(a);
  return `<div class="pop"><b>${ico} ${esc(a.nombre)}</b><br>`+
    `<span class="pop-sub">${esc(MS_LABEL[a.categoria]||"Ayuda")}${a.zona?" · "+esc(a.zona):""}</span><br>`+
    `<span style="font-size:12px">${esc(a.descripcion||"")}</span><br>`+
    `<a href="#" onclick="abrirContactoMS(${a.id});return false">🤝 Solicitar contacto</a> · `+
    `<a href="${llegar}" target="_blank" rel="noopener">🗺️ Cómo llegar</a></div>`;
}
function pinIcon(emoji,color){return L.divIcon({className:"",html:`<div style="width:28px;height:28px;border-radius:50% 50% 50% 0;background:${color};transform:rotate(-45deg);border:2px solid #fff;box-shadow:0 1px 3px rgba(0,0,0,.4);display:flex;align-items:center;justify-content:center"><span style="transform:rotate(45deg);font-size:13px">${emoji}</span></div>`,iconSize:[28,28],iconAnchor:[14,26],popupAnchor:[0,-24]});}
function initMapa(id){
  const el=document.getElementById(id); if(!el) return;
  cargarLeaflet().then(()=>{
    const cont=document.getElementById(id); if(!cont||!window.L) return;
    if(_mapa){try{_mapa.remove();}catch(e){}_mapa=null;}
    const map=L.map(cont,{scrollWheelZoom:false}).setView([7.9,-66.0],6); // Venezuela
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",{maxZoom:19,attribution:'&copy; OpenStreetMap'}).addTo(map);
    const coords=[];
    DIRECTORIO.filter(tieneGps).forEach(p=>{
      L.marker([+p.lat,+p.lon],{icon:pinIcon(PUNTO_ICO[p.tipo_punto]||"📦","#003C67")}).addTo(map).bindPopup(popupRed(p));
      coords.push([+p.lat,+p.lon]);
    });
    ALIADOS.filter(tieneGps).forEach(a=>{
      L.marker([+a.lat,+a.lon],{icon:pinIcon(MS_ICO[a.categoria]||"🤝","#1E9C94")}).addTo(map).bindPopup(popupMS(a));
      coords.push([+a.lat,+a.lon]);
    });
    if(coords.length===1) map.setView(coords[0],14);
    else if(coords.length>1) map.fitBounds(coords,{padding:[40,40],maxZoom:13});
    _mapa=map;
    setTimeout(()=>map.invalidateSize(),120);
  }).catch(()=>{el.innerHTML='<div class="vacio"><span class="em">🗺️</span>No se pudo cargar el mapa. Revisa tu conexión.</div>';});
}
function renderManos(){
  let items=ALIADOS.filter(a=>coincide(a.nombre+" "+(a.categoria||"")+" "+(MS_LABEL[a.categoria]||"")+" "+(a.descripcion||"")+" "+(a.zona||"")));
  if(msCat!=="todas") items=items.filter(a=>a.categoria===msCat);
  let html=`<div class="aviso aviso-ms">🤝 <b>Manos Solidarias</b>: personas de la comunidad que ofrecen ayuda <b>gratis</b> (comida, transporte, salud, inspección estructural, ayuda en línea…). Toca un pin o una tarjeta para contactar. <b>No es información oficial</b>, verifica.</div>`;
  html+=`<div style="text-align:center;margin:8px 0 12px"><a class="btn" href="/ofrecer.html" style="background:#1E9C94;color:#fff;font-weight:800;border-color:#1E9C94">➕ Ofrecer mi ayuda</a></div>`;
  const cats=[...new Set(ALIADOS.map(a=>a.categoria))];
  html+=`<div class="ms-chips"><span class="ms-chip${msCat==="todas"?" on":""}" onclick="filtrarMS('todas')">Todos</span>`;
  cats.forEach(c=>{html+=`<span class="ms-chip${msCat===c?" on":""}" onclick="filtrarMS('${esc(c)}')">${MS_ICO[c]||"📦"} ${esc(MS_LABEL[c]||c)}</span>`;});
  html+=`</div>`;
  html+=`<div id="mapa-ms" class="mapa-red"></div>`+MAPA_LEYENDA;
  if(!items.length){html+=vacio("No hay aliados en esta categoría.");return html;}
  html+=items.map(msItem).join("");
  return html;
}
function msItem(a){
  const ico=MS_ICO[a.categoria]||"🤝";
  const sub=[MS_LABEL[a.categoria]||"Ayuda",a.zona].filter(Boolean).map(esc).join(" · ");
  const llegar=tieneGps(a)?`<a class="btn" target="_blank" rel="noopener" href="${mapsUrl(a)}">🗺️ Cómo llegar</a>`:"";
  const hor=a.horario?`<div class="meta" style="margin-top:6px">🕐 ${esc(a.horario)}</div>`:"";
  const dsc=(a.descripcion||"");
  const sh=`🤝 ${a.nombre} — ${MS_LABEL[a.categoria]||"ayuda"}: ${dsc.length>120?dsc.slice(0,119).trim()+"…":dsc}`+(a.zona?` (${a.zona})`:"")+" — pide su contacto en la web";
  const urlMS=paginaAliado(a);
  return `<div class="centro ms-card" id="ms-${a.id}">
    <div class="ms-top"><span class="ms-ico">${ico}</span><div><div class="ms-nom">${esc(a.nombre)}</div><div class="ms-sub">${sub}</div></div></div>
    <div class="ms-desc">${esc(a.descripcion||"")}</div>${hor}
    <div class="acciones"><button class="btn b-wa" data-contactar="${a.id}">🤝 Solicitar contacto</button>${llegar}${compartirBtn(sh, urlMS)}${copiarBtn(urlMS)}${denunBtn("aliado",a.id)}</div>
  </div>`;
}
function filtrarMS(c){msCat=c;render();}
function renderLlamados(){
  let items=LLAMADOS.filter(l=>coincide((l.sector||"")+" "+(l.mensaje||"")));
  let html=`<div class="aviso">📣 <b>Llamados de apoyo</b>: solicitudes que levantan nuestros <b>voluntarios</b> en terreno, de sectores que siguen en emergencia y con poca cobertura. Si puedes ayudar, <b>compártelos</b> para que la ayuda llegue. <b>No es información oficial</b>, verifica.</div>`;
  if(!items.length){html+=vacio("No hay llamados de apoyo activos ahora.");return html;}
  html+=items.map(llamadoItem).join("");
  return html;
}
function llamadoItem(l){
  const msj=(l.mensaje||"");
  const sh=`📣 Llamado de apoyo${l.sector?` — ${l.sector}`:""}: ${msj.length>140?msj.slice(0,139).trim()+"…":msj}`;
  const urlLla=paginaLlamado(l);
  const nresp=l.respuestas||0;
  let estado="";
  if(nresp>0){
    estado=`<span class="lla-resp">🤝 ${nresp} ${nresp==1?"persona respondiendo":"personas respondiendo"}</span>`;
  }else{
    const h=(Date.now()-new Date(l.creado_en).getTime())/3.6e6;
    if(h>6) estado=`<span class="lla-sin">⚠️ Aún sin respuesta — ¿puedes tú?</span>`;
  }
  const ayudarWeb=ACCION_LLA?`<button class="btn b-llevar b-accweb" onclick="llaAbrir(${l.id})">🙋 Puedo ayudar — aquí</button>`:"";
  const ayudar=ayudarWeb+(ACCION_LLA?accionBtn("🙋 Puedo ayudar","lla_"+l.id,
    "Hola 🙋 quiero ayudar con el llamado LL-"+l.id+(l.sector?" — "+l.sector:"")):"");
  return `<div class="centro lla-card" id="ll-${l.id}">
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
      <span style="font-size:22px">📣</span>
      <span style="font-weight:700;color:#005B67">${esc(l.sector||"Sector en emergencia")}</span>
    </div>
    ${l.foto?`<img class="foto" src="${esc(l.foto)}" loading="lazy" alt="">`:""}
    <div style="white-space:pre-wrap;line-height:1.45">${esc(l.mensaje||"")}</div>
    ${estado?`<div style="margin-top:8px">${estado}</div>`:""}
    <div class="meta" style="margin-top:6px">🕐 ${tiempoDesde(l.creado_en)}</div>
    <div class="acciones acc-lla">${ayudar}${compartirBtn(sh,urlLla)}${copiarBtn(urlLla)}</div>
  </div>`;
}
// Agrupación de insumos por categoría (para inventarios largos) + buscador interno.
const INV_CATS=[["medicament","💊 Medicamentos"],["farmac","💊 Medicamentos"],["insumo","🩹 Insumos médicos"],["curaci","🩹 Insumos médicos"],["medico","🩹 Insumos médicos"],["agua","💧 Agua y bebidas"],["bebida","💧 Agua y bebidas"],["aliment","🍚 Alimentos"],["comida","🍚 Alimentos"],["bebe","🍼 Bebés"],["panal","🍼 Bebés"],["higiene","🧼 Higiene y aseo"],["aseo","🧼 Higiene y aseo"],["limpieza","🧴 Limpieza"],["ropa","👕 Ropa"],["calzado","👟 Calzado"],["colchon","🛏️ Descanso"],["cobija","🛏️ Descanso"],["herramient","🛠️ Herramientas"]];
function _na(s){return (s||"").toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g,"");}
// La ropa NO se agrupa genérica: se subclasifica por público (niño/hombre/mujer/bebé),
// detectándolo en categoría + ítem + descripción (el dato puede venir en cualquiera).
const ROPA_LABELS=["👨 Ropa de hombre","👩 Ropa de mujer","🧒 Ropa de niños","🍼 Ropa de bebé","👕 Ropa unisex","👕 Ropa (varios)","👕 Ropa (sin especificar)"];
function ropaBucket(i){
  const txt=_na((i.categoria||"")+" "+(i.item||"")+" "+(i.detalle||""));
  if(txt.includes("bebe")||txt.includes("recien nac")) return "🍼 Ropa de bebé";
  const nino=txt.includes("nino")||txt.includes("nina")||txt.includes("infant")||txt.includes("nene");
  const h=txt.includes("hombre")||txt.includes("caballero")||txt.includes("masculin");
  const m=txt.includes("mujer")||txt.includes("dama")||txt.includes("femenin");
  if([nino,h,m].filter(Boolean).length>1) return "👕 Ropa (varios)";
  if(nino) return "🧒 Ropa de niños";
  if(h) return "👨 Ropa de hombre";
  if(m) return "👩 Ropa de mujer";
  if(txt.includes("unisex")) return "👕 Ropa unisex";
  return "👕 Ropa (sin especificar)";
}
function catLabel(i){
  const cat=_na(i.categoria||"");
  if(cat.includes("ropa")||cat.includes("vestiment")||cat.includes("prenda")) return ropaBucket(i);
  for(const kv of INV_CATS) if(cat.includes(kv[0])) return kv[1];
  return i.categoria ? "📦 "+i.categoria.charAt(0).toUpperCase()+i.categoria.slice(1) : "📦 Otros";
}
function agruparInsumos(items){
  const map={};
  items.forEach(i=>{const l=catLabel(i);(map[l]=map[l]||[]).push(i);});
  const orden=[...new Set(INV_CATS.map(x=>x[1])), ...ROPA_LABELS];
  return Object.keys(map).sort((a,b)=>{
    const ia=orden.indexOf(a), ib=orden.indexOf(b);
    if(ia!==-1||ib!==-1) return (ia===-1?99:ia)-(ib===-1?99:ib);
    return a.localeCompare(b,"es");
  }).map(k=>[k,map[k]]);
}
function invFila(i){
  const cant=(i.cantidad&&i.cantidad.trim())?esc(i.cantidad):"cant.: —";
  const desc=(i.detalle&&i.detalle.trim())?` · <span class="inv-desc">${esc(i.detalle)}</span>`:"";
  const txt=esc(_na((i.item||"")+" "+(i.cantidad||"")+" "+(i.detalle||"")));
  return `<div class="inv-fila" data-txt="${txt}">• <b>${esc(i.item)}</b> — ${cant}${desc}</div>`;
}
function cuerpoInsumos(items){
  if(!items.length) return "";
  const buscador=items.length>12?`<input class="inv-q" type="search" placeholder="🔎 Buscar en esta lista…" oninput="filtrarInv(this)">`:"";
  const grupos=agruparInsumos(items);
  // Cada categoría es un submenú COLAPSABLE (default cerrado; si hay una sola, abierta).
  let h="";
  grupos.forEach(([lbl,gr])=>{
    const abierto=grupos.length===1?" open":"";
    h+=`<details class="inv-cat"${abierto}><summary class="inv-grupo">${esc(lbl)} <span class="inv-n">(${gr.length})</span></summary>`;
    h+=`<div class="inv-cat-body">${gr.map(invFila).join("")}</div></details>`;
  });
  return buscador+`<div class="inv-lista">${h}</div>`;
}
function filtrarInv(inp){
  const t=_na(inp.value.trim()), body=inp.closest(".nec-body");
  body.querySelectorAll(".inv-cat").forEach(cat=>{
    let vis=0;
    cat.querySelectorAll(".inv-fila").forEach(f=>{
      const m=(!t||f.dataset.txt.includes(t)); f.style.display=m?"":"none"; if(m)vis++;
    });
    cat.style.display=vis?"":"none";
    cat.open=t?vis>0:false;   // al buscar abre las categorías con coincidencias; al limpiar, colapsa
  });
}
// Deep link: ancla estable por punto (usa punto_id; cae a slug del nombre si falta).
function pidDe(p){ return p.id!=null ? ("pt-"+p.id) : ("ptn-"+_na(p.punto||"").replace(/[^a-z0-9]+/g,"-").replace(/^-|-$/g,"")); }
function deepLink(tab,p){ return SITE_URL+"#"+tab+":"+pidDe(p); }
// Página compartible del punto (/p/pt-<id>.html, con vista previa propia en WhatsApp/redes).
// En seguimiento/directorio la identidad es punto_id (id ahí es de la necesidad/representante).
function pidPunto(p){return (p.punto_id!=null)?("pt-"+p.punto_id):pidDe(p);}
function paginaPunto(p){return SITE_URL+"/p/"+pidPunto(p)+".html";}
function paginaPedido(p){return SITE_URL+"/s/pd-"+p.id+".html";}
function paginaLlamado(l){return SITE_URL+"/s/ll-"+l.id+".html";}
function paginaAliado(a){return SITE_URL+"/m/ms-"+a.id+".html";}
// --- Acción desde el web → bot (deep link). Genérico: sirve para necesidades y, luego, llamados.
const BOT_USER="angelesapoyove_bot";
const ACCION_NEC=true;  // se activa cuando el bot maneje /start nec_<id> (Fase 5)
const ACCION_LLA=true;  // se activa cuando el bot maneje /start lla_<id> (Fase 5 llamados)
function botLink(payload){ return "https://t.me/"+BOT_USER+"?start="+encodeURIComponent(payload); }
// WhatsApp no tiene deep link con payload: se manda el texto prellenado con un código
// (LL-<id> / ND-<id>) que bot_wa.py reconoce al recibirlo. Mismo flujo que Telegram.
const WA_BOT_NUM="58412XXXXXXX";
function waBotLink(texto){ return "https://wa.me/"+WA_BOT_NUM+"?text="+encodeURIComponent(texto); }
function accionBtn(label,payload,waTexto){
  return `<a class="btn b-llevar b-accwa" target="_blank" rel="noopener" href="${waBotLink(waTexto)}">${label} · WhatsApp</a>`
       + `<a class="btn b-llevar" target="_blank" rel="noopener" href="${botLink(payload)}">${label} · Telegram</a>`;
}
// Compromiso INLINE desde la web (POST /api/comprometer) — tercera vía, sin salir
// de la página. La reserva vence sola (12 h) y el punto recibe el aviso por SU canal.
function _necDe(id){ for(const p of (SEGUIMIENTO||[])){ for(const it of (p.items||[])) if(it.id===id) return {it:it,p:p}; } return null; }
function cmpAbrir(id){
  const n=_necDe(id); if(!n) return;
  const meta=+n.it.meta||0;
  const falta=meta?Math.max(0,meta-(+n.it.recibido||0)-(+n.it.en_camino||0)):"";
  const old=document.getElementById("cmp-ovl"); if(old) old.remove();
  const d=document.createElement("div"); d.className="cmp-ovl"; d.id="cmp-ovl";
  d.innerHTML=`<div class="cmp-box"><button class="cmp-x" onclick="document.getElementById('cmp-ovl').remove()">✕</button>
    <h3>🚚 Voy a llevar: ${esc(n.it.item||"")}</h3>
    <div style="color:var(--suave);font-size:13px">${esc(n.p.punto||"")}${falta!==""?` · faltan ${falta}`:""}</div>
    <label>¿Cuánto llevas?</label><input id="cmp-cant" type="number" min="1" inputmode="numeric" placeholder="${falta||"cantidad"}">
    <label>Tu nombre</label><input id="cmp-nom" maxlength="80" placeholder="Nombre u organización">
    <label>Tu WhatsApp</label><input id="cmp-wa" maxlength="60" placeholder="Ej.: 0412 1234567">
    <div style="font-size:12px;color:#8a99ad;margin-top:6px">El punto recibe tu WhatsApp para coordinar. Si al final no puedes, la reserva se libera sola en 12 h.</div>
    <div class="cmp-err" id="cmp-err"></div>
    <button class="btn b-llevar" id="cmp-go" onclick="cmpEnviar(${id})">Confirmar compromiso</button></div>`;
  d.addEventListener("click",e=>{ if(e.target===d) d.remove(); });
  document.body.appendChild(d);
  setTimeout(()=>{const c=document.getElementById("cmp-cant"); if(c) c.focus();},50);
}
function cmpEnviar(id){
  const g=x=>document.getElementById(x), err=g("cmp-err"), btn=g("cmp-go");
  const body={necesidad_id:id,cantidad:(g("cmp-cant").value||"").trim(),nombre:(g("cmp-nom").value||"").trim(),contacto:(g("cmp-wa").value||"").trim()};
  if(!body.cantidad||!body.nombre||!body.contacto){ err.textContent="Completa cantidad, nombre y WhatsApp."; err.style.display="block"; return; }
  btn.disabled=true; btn.textContent="Enviando…";
  fetch("/api/comprometer",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)})
    .then(r=>r.json().catch(()=>({ok:false})))
    .then(d=>{
      if(!(d&&d.ok)) throw new Error((d&&d.error)||"No se pudo enviar");
      const c=d.centro||{};
      document.querySelector("#cmp-ovl .cmp-box").innerHTML=`<button class="cmp-x" onclick="document.getElementById('cmp-ovl').remove()">✕</button>
        <h3>✅ ¡Anotado! El punto ya fue avisado</h3>
        <div style="line-height:1.7;font-size:14.5px;margin-top:8px"><b>${esc(c.punto||"")}</b><br>${c.ubicacion?`📍 ${esc(c.ubicacion)}<br>`:""}${c.horario?`🕐 ${esc(c.horario)}<br>`:""}${c.contacto?`📞 ${esc(c.contacto)}`:""}</div>
        <div style="font-size:12.5px;color:#8a99ad;margin-top:10px">Lleva lo comprometido a esa dirección. Tu aporte ya cuenta como «en camino» (la web se actualiza en minutos).</div>`;
    })
    .catch(e=>{ err.textContent=(e.message||"No se pudo enviar")+" — puedes intentar de nuevo."; err.style.display="block"; btn.disabled=false; btn.textContent="Confirmar compromiso"; });
}
// Respuesta INLINE a un llamado de apoyo (POST /api/responder_llamado) — ayuda
// puntual sin registro: nombre + WhatsApp; quien levantó el llamado coordina.
function _llaDe(id){ for(const l of (LLAMADOS||[])) if(l.id===id) return l; return null; }
function llaAbrir(id){
  const l=_llaDe(id); if(!l) return;
  const old=document.getElementById("cmp-ovl"); if(old) old.remove();
  const d=document.createElement("div"); d.className="cmp-ovl"; d.id="cmp-ovl";
  d.innerHTML=`<div class="cmp-box"><button class="cmp-x" onclick="document.getElementById('cmp-ovl').remove()">✕</button>
    <h3>🙋 Responder al llamado</h3>
    <div style="color:var(--suave);font-size:13px">${esc(l.sector||"Sector en emergencia")}</div>
    <label>¿Con qué puedes ayudar?</label><input id="lla-msg" maxlength="300" placeholder="Ej.: llevo agua, pico y pala">
    <label>Tu nombre</label><input id="lla-nom" maxlength="80" placeholder="Nombre u organización">
    <label>Tu WhatsApp</label><input id="lla-wa" maxlength="60" placeholder="Ej.: 0412 1234567">
    <div style="font-size:12px;color:#8a99ad;margin-top:6px">Quien levantó el llamado recibe tu WhatsApp y coordina contigo. Tu número no se publica en la web.</div>
    <div class="cmp-err" id="lla-err"></div>
    <button class="btn b-llevar" id="lla-go" onclick="llaEnviar(${id})">Enviar mi respuesta</button></div>`;
  d.addEventListener("click",e=>{ if(e.target===d) d.remove(); });
  document.body.appendChild(d);
}
function llaEnviar(id){
  const g=x=>document.getElementById(x), err=g("lla-err"), btn=g("lla-go");
  const body={llamado_id:id,mensaje:(g("lla-msg").value||"").trim(),nombre:(g("lla-nom").value||"").trim(),contacto:(g("lla-wa").value||"").trim()};
  if(!body.nombre||!body.contacto){ err.textContent="Completa tu nombre y tu WhatsApp."; err.style.display="block"; return; }
  btn.disabled=true; btn.textContent="Enviando…";
  fetch("/api/responder_llamado",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)})
    .then(r=>r.json().catch(()=>({ok:false})))
    .then(d=>{
      if(!(d&&d.ok)) throw new Error((d&&d.error)||"No se pudo enviar");
      document.querySelector("#cmp-ovl .cmp-box").innerHTML=`<button class="cmp-x" onclick="document.getElementById('cmp-ovl').remove()">✕</button>
        <h3>✅ ¡Gracias! Tu respuesta ya llegó</h3>
        <div style="line-height:1.6;font-size:14.5px;margin-top:8px">Quien levantó el llamado recibió tu WhatsApp y te contactará para coordinar. 🤝</div>`;
    })
    .catch(e=>{ err.textContent=(e.message||"No se pudo enviar")+" — puedes intentar de nuevo."; err.style.display="block"; btn.disabled=false; btn.textContent="Enviar mi respuesta"; });
}
function urgChip(u){ u=(u||"media").toString().toLowerCase();
  if(u.indexOf("crit")===0) return `<span class="urg urg-crit">⚠ Crítico</span>`;
  if(u==="alta") return `<span class="urg urg-alta">🔥 Alta</span>`;
  return ""; }
function barra3(i){
  const meta=+i.meta||0, rec=+i.recibido||0, enc=+i.en_camino||0;
  const btnWeb=ACCION_NEC?`<button class="btn b-llevar b-accweb" onclick="cmpAbrir(${i.id})">🚚 Lo llevo — anotar aquí</button>`:"";
  const btn=btnWeb+(ACCION_NEC?accionBtn("🤝 Puedo llevar esto","nec_"+i.id,
    "Hola 🤝 puedo llevar "+(i.item||"insumos")+" — necesidad ND-"+i.id):"");
  if(!meta){
    return `<div class="nec3">${urgChip(i.urgencia)}<div class="nec3-foot"><span class="nec3-sinmeta">Cantidad por confirmar</span>${btn}</div></div>`;
  }
  const faltan=Math.max(0,meta-rec-enc), cub=faltan<=0;
  const pRec=Math.min(100,Math.round(rec/meta*100));
  const pEnc=Math.max(0,Math.min(100-pRec,Math.round(enc/meta*100)));
  const u=i.unidad?(" "+esc(i.unidad)):"";
  const foot=cub?`<span class="urg urg-ok">✓ Cubierto</span>`:btn;
  return `<div class="nec3">${cub?"":urgChip(i.urgencia)}
    <div class="nec3-bar"><span class="seg-rec" style="width:${pRec}%"></span><span class="seg-enc" style="width:${pEnc}%"></span></div>
    <div class="nec3-leg"><span><i class="d d-rec"></i>${rec} recibido${rec==1?"":"s"}</span><span><i class="d d-enc"></i>${enc} en camino</span><span><i class="d d-fal"></i>${faltan} faltan</span></div>
    <div class="nec3-foot"><span class="nec3-num">${cub?"Completo":("Faltan "+faltan)}<span class="nec3-meta"> / meta ${meta}${u}</span></span>${foot}</div>
  </div>`;
}
function necItem(p){
  const ico=PUNTO_ICO[p.tipo_punto]||"📍";
  const ni=p.items.length;
  const abrir=q?" open":"";
  const sub=(p.personas?`👥 ${p.personas} personas · `:"")+(ni?`${ni} ${ni==1?"requerimiento":"requerimientos"}`:"sin requerimientos aún");
  const _muestra=p.items.map(i=>i.item).slice(0,8).join(", ")+(ni>8?` y ${ni-8} más`:"");
  const sh=`${ico} ${p.punto}`+(p.personas?` — ${p.personas} personas`:"")+(ni?` necesita: ${_muestra}.`:"");
  const urlPunto=paginaPunto(p);
  return `<details id="${pidDe(p)}" class="centro nec-acc"${abrir}>
    <summary><span class="nec-tit">${ico} ${esc(p.punto)}</span><span class="nec-sub">${esc(sub)}</span></summary>
    <div class="nec-body">
      ${p.personas?`<div class="meta" style="font-size:15px;font-weight:800;color:var(--navy)">👥 ${esc(String(p.personas))} personas en este momento</div>`:""}
      ${cuerpoInsumos(p.items)}
      <div class="acciones">${compartirBtn(sh,urlPunto)}${copiarBtn(urlPunto)}</div>
      <div class="pie"><span>${tiempoDesde(p.actualizado)}</span></div>
    </div>
  </details>`;
}
function segBadge(e,m){
  if(e==="disponible") return `<span class="seg-badge seg-disp">✅ Disponible${m?` en ${esc(m)}`:" cerca"}</span>`;
  if(e==="coordinando") return `<span class="seg-badge seg-coord">🤝 Coordinando</span>`;
  return `<span class="seg-badge seg-busc">🔎 Buscando</span>`;
}
function segPunto(p){
  const ico=PUNTO_ICO[p.tipo_punto]||"📍";
  const abrir=q?" open":"";
  const lista=p.items.map(i=>`<div class="nec-it"><div class="nec-it-tit">• <b>${esc(i.item)}</b> ${segBadge(i.estado,i.match_punto)}</div>${barra3(i)}</div>`).join("");
  const sub=`${p.items.length} en seguimiento`;
  const ni=p.items.length;
  const _muestra=p.items.map(i=>i.item).slice(0,8).join(", ")+(ni>8?` y ${ni-8} más`:"");
  const sh=`${ico} ${p.punto} necesita: ${_muestra}.`;
  const urlPunto=paginaPunto(p);
  return `<details id="${pidPunto(p)}" class="centro nec-acc"${abrir}>
    <summary><span class="nec-tit">${ico} ${esc(p.punto)}</span><span class="nec-sub">${esc(sub)}</span></summary>
    <div class="nec-body">${lista}
      <div class="acciones">${compartirBtn(sh,urlPunto)}${copiarBtn(urlPunto)}</div>
      <div class="pie"><span>${tiempoDesde(p.actualizado)}</span></div></div>
  </details>`;
}
function renderNecesidades(){
  const f=p=>coincide(p.punto+" "+(p.tipo_punto||"")+" "+p.items.map(i=>i.item).join(" "));
  const seg=SEGUIMIENTO.filter(f);
  // Dedup: lo que ya está en seguimiento (con barra + botón) no se repite en la lista de texto libre.
  const segKeys=new Set();
  seg.forEach(p=>p.items.forEach(i=>segKeys.add(_na(p.punto)+"|"+_na(i.item))));
  const nec=NECESIDADES.filter(f)
    .map(p=>({...p, items:p.items.filter(i=>!segKeys.has(_na(p.punto)+"|"+_na(i.item)))}))
    .filter(p=>p.items.length||p.personas)
    .sort((a,b)=>(b.actualizado||"").localeCompare(a.actualizado||""));
  if(!seg.length && !nec.length) return vacio("Aún no hay necesidades reportadas por la red de colaboradores.");
  let html=`<div class="aviso">📥 <b>Lo que hace falta</b> ahora, por punto. Los badges <b>🔎 Buscando / ✅ Disponible / 🤝 Coordinando</b> indican necesidades <b>en seguimiento</b>: el sistema avisa solo a quien lo pidió en cuanto aparece en un acopio cercano. Se actualiza solo. <b>No es información oficial</b>, verifica.</div>`;
  // Agrupado por tipo de punto, mismas categorías que el mapa (acopio/refugio/
  // hospital/CDI/maternidad/clínica + otros). Dentro de cada grupo: primero las
  // necesidades en seguimiento, luego el resto.
  PUNTO_GRUPOS.forEach(([k,titulo])=>{
    const cond=p=>(PUNTO_ICO[p.tipo_punto]?p.tipo_punto:"otro")===k;
    const s=seg.filter(cond), n=nec.filter(cond);
    if(!s.length && !n.length) return;
    const cnt=s.reduce((a,p)=>a+p.items.length,0)+n.reduce((a,p)=>a+p.items.length,0);
    html+=`<h3 class="grupo-titulo">${titulo} · ${cnt}</h3>`;
    html+=s.map(segPunto).join("");
    html+=n.map(necItem).join("");
  });
  return html;
}
function ofItem(p){
  const ni=p.items.length;
  const abrir=q?" open":"";
  const sub=ni?`${ni} ${ni==1?"insumo disponible":"insumos disponibles"}`:"sin inventario aún";
  const _muestra=p.items.map(i=>i.item).slice(0,8).join(", ")+(ni>8?` y ${ni-8} más`:"");
  const sh=`📦 ${p.punto} (centro de acopio)`+(ni?` tiene disponible ${ni} ${ni==1?"insumo":"insumos"}: ${_muestra}.`:".");
  const urlPunto=paginaPunto(p);
  return `<details id="${pidDe(p)}" class="centro nec-acc"${abrir}>
    <summary><span class="nec-tit">📦 ${esc(p.punto)}</span><span class="nec-sub">${esc(sub)}</span></summary>
    <div class="nec-body">
      ${cuerpoInsumos(p.items)}
      <div class="acciones">${compartirBtn(sh,urlPunto)}${copiarBtn(urlPunto)}</div>
      <div class="pie"><span>${tiempoDesde(p.actualizado)}</span></div>
    </div>
  </details>`;
}
function renderOferta(){
  const items=OFERTA.filter(p=>coincide(p.punto+" "+p.items.map(i=>i.item).join(" "))).sort((a,b)=>(b.actualizado||"").localeCompare(a.actualizado||""));
  if(!items.length) return vacio("Aún no hay inventario publicado por los centros de acopio de la red.");
  let html=`<div class="aviso">🎁 Lo que cada <b>centro de acopio</b> de la red tiene <b>disponible para dar</b>, reportado por sus responsables. Toca un punto para ver el detalle, o usa el buscador. Se actualiza solo. <b>No es información oficial</b>, verifica.</div>`;
  agruparPorTipo(items).forEach(([lbl,gr])=>{
    html+=`<h3 class="grupo-titulo">${lbl} · ${gr.length}</h3>`;
    html+=gr.map(ofItem).join("");
  });
  return html;
}
const SERV_ICO={luz:"⚡",agua:"💧",gasolina:"⛽",internet:"🌐",telefonia:"📶",gas:"🔥",via:"🛣️",otro:"🔧"};
const SERV_NOM={luz:"Luz",agua:"Agua",gasolina:"Gasolina",internet:"Internet",telefonia:"Señal",gas:"Gas",via:"Vías",otro:"Otro"};
function servEstado(e){
  const t={ok:["✅ Funciona","s-ok"],falla:["⛔ Sin servicio","s-falla"],parcial:["⚠️ Intermitente","s-parcial"],desconocido:["❔ Sin dato","s-desc"]};
  return t[e]||t.desconocido;
}
function renderServicios(){
  const items=SERVICIOS.filter(s=>coincide((SERV_NOM[s.tipo]||s.tipo)+" "+(s.zona||"")+" "+(s.detalle||"")));
  if(!items.length) return vacio("No hay reportes de servicios todavía.");
  const zonas={};
  items.forEach(s=>{(zonas[s.zona]=zonas[s.zona]||[]).push(s);});
  let html=`<div class="aviso">🔌 Estado de servicios reportado en el canal (luz, agua, gasolina, señal, vías). Es referencial y puede cambiar — verifica.</div>`;
  const _ultZ=z=>zonas[z].reduce((m,s)=>(s.tg_fecha||"")>m?(s.tg_fecha||""):m,"");
  Object.keys(zonas).sort((a,b)=>_ultZ(b).localeCompare(_ultZ(a))).forEach(z=>{
    const lista=zonas[z];
    html+=`<div class="centro"><div class="info" style="width:100%">
      <div class="cn">📍 ${esc(z)}</div>
      <div class="serv-chips">`+lista.map(s=>{
        const e=servEstado(s.estado);
        return `<span class="serv-chip ${e[1]}">${SERV_ICO[s.tipo]||"🔧"} ${esc(SERV_NOM[s.tipo]||s.tipo)}: ${e[0]}</span>`;
      }).join("")+`</div>
      <div class="pie"><span>${tiempoDesde(lista[0].tg_fecha)}</span></div>
    </div></div>`;
  });
  return html;
}
function renderBuscar(){
  let html=`<div class="aviso">🔦 ¿Buscas a alguien que pudo ingresar a un hospital? Estas plataformas ciudadanas centralizan los listados de <b>hospitalizados, rescatados y desaparecidos</b> del terremoto y <b>se actualizan constantemente</b>. Abre cualquiera y busca por nombre o foto. <b>No es información oficial</b> — verifica.</div>`;
  const items=PLATAFORMAS.filter(p=>coincide(p.nombre+" "+p.desc));
  if(!items.length) return html+vacio("Ninguna plataforma coincide con tu búsqueda.");
  html+=items.map(p=>{
    const sh=`🔦 Busca personas en hospitales tras el terremoto en ${p.nombre}: ${p.url}`;
    return `<div class="centro"><div class="info">
      <div class="cn">${esc(p.nombre)}</div>
      <div class="meta">${esc(p.desc)}</div>
      <div class="acciones">
        <a class="btn b-share" target="_blank" rel="noopener" href="${esc(p.url)}">🔎 Abrir y buscar ↗</a>
        ${compartirBtn(sh,p.url)}
      </div>
    </div></div>`;
  }).join("");
  html+=`<div class="aviso" style="margin-top:16px">📂 ¿Buscas otra cosa (refugios, mascotas, transporte, daños estructurales)? Hay un directorio con más recursos del terremoto: <a href="https://enlacesterremotovenezuela.netlify.app/" target="_blank" rel="noopener" style="color:var(--navy);font-weight:800;text-decoration:underline">ver más recursos ↗</a>. Es un directorio externo — verifica cada enlace.</div>`;
  return html;
}
function sismoMag(s){ return s.mag!=null ? Number(s.mag).toFixed(1) : "?"; }
function distKm(a,b){
  const R=6371, dLat=(b.lat-a.lat)*Math.PI/180, dLon=(b.lon-a.lon)*Math.PI/180;
  const x=Math.sin(dLat/2)**2+Math.cos(a.lat*Math.PI/180)*Math.cos(b.lat*Math.PI/180)*Math.sin(dLon/2)**2;
  return Math.round(2*R*Math.asin(Math.sqrt(x)));
}
function intensidad(mag,d){ const s=(mag||0)-d/120; return s>=4.8?"fuerte":s>=3.8?"moderado":s>=2.8?"leve":"casi imperceptible"; }
function pedirUbicacion(){
  if(!navigator.geolocation) return;
  navigator.geolocation.getCurrentPosition(
    p=>{ miUbic={lat:p.coords.latitude,lon:p.coords.longitude}; if(activo==="sismos") render(); },
    ()=>{}, {timeout:8000, maximumAge:300000});
}
function sismoExtra(s){
  if(!miUbic || s.lat==null) return "";
  const d=distKm(miUbic,{lat:s.lat,lon:s.lon});
  return `<div class="meta">📍 a <b>${d} km</b> de ti · estimado: <b>${intensidad(s.mag,d)}</b></div>`;
}
function sismosHTML(fs){
  if(!fs.length) return vacio("Sin sismos registrados cerca de Venezuela en los últimos 3 días.");
  let html=`<div class="aviso">🌎 Sismos cerca de Venezuela (últimos 3 días · fuentes USGS + EMSC). <b>Es informativo, no es alerta temprana</b> — las réplicas muy pequeñas pueden no aparecer.</div>`;
  if(!miUbic) html+=`<button class="btn b-report" id="geo-btn" style="width:100%;justify-content:center;margin-bottom:12px">📍 Calcular distancia a mi ubicación</button>`;
  const s0=fs[0], m0=s0.mag, col0=m0>=5?"var(--lleno)":(m0>=4?"var(--rescate)":"var(--disponible)");
  html+=`<div class="card" style="border-left:5px solid ${col0}">
    <div class="cab"><span class="ico">🌎</span><div><div class="cat">Sismo más reciente</div><h3>Magnitud ${sismoMag(s0)}</h3></div></div>
    <div class="meta">📍 ${esc(s0.lugar||"(sin lugar)")}</div>
    ${sismoExtra(s0)}
    <div class="pie"><span>${tiempoDesde(s0.fecha)}</span><span>${esc(s0.fuente||"")}</span></div>
  </div>`;
  if(fs.length>1){
    html+=`<h3 class="grupo-titulo">Anteriores (${fs.length-1})</h3>`;
    html+=fs.slice(1).map(s=>{
      const m=s.mag, col=m>=5?"var(--lleno)":(m>=4?"var(--rescate)":"var(--disponible)");
      return `<div class="centro"><div class="info">
        <div class="cn">📍 ${esc(s.lugar||"(sin lugar)")}</div>
        ${sismoExtra(s)}
        <div class="meta">${tiempoDesde(s.fecha)} · ${esc(s.fuente||"")}</div>
      </div><span class="badge" style="color:${col};background:#f1f5f9">M ${sismoMag(s)}</span></div>`;
    }).join("");
  }
  return html;
}
function renderSismos(){ return sismosHTML(SISMOS); }
function renderAngeles(){
  // Hub del canal de emergencias viales: mismos submenús que el grupo del acordeón.
  const g=listaTabs().find(t=>t.sub);
  if(!g) return vacio("No hay reportes del canal todavía.");
  return `<div class="aviso">🚨 <b>Ángeles de la Autopista</b> — reportes en vivo recopilados automáticamente del canal de emergencias viales. Puede haber errores o datos viejos: <b>verifica antes de actuar</b>.</div>
    <div class="accesos" style="margin:12px 0 0;padding:0">`
    +g.sub.map(s=>`<button class="acceso ac-hub" onclick="irTab('${s.key}')"><span class="ac-ico">${s.ico}</span><span>${esc(s.nom)}${s.n!=null?` <span class="pill" style="background:rgba(0,0,0,.08);border-radius:999px;padding:1px 8px;font-size:12px">${s.n}</span>`:""}</span></button>`).join("")
    +`</div>`;
}
function irTab(k){
  activo=k;q="";zonaSel="todas";msCat="todas";desapMostrar=DESAP_LOTE;
  var qi=document.getElementById("q");if(qi)qi.value="";
  pintarTabs();render();
  const c=document.getElementById("contenido");
  c.classList.remove("fx-in"); void c.offsetWidth; c.classList.add("fx-in");
  const destino=document.getElementById("secmenu")||c;
  destino.scrollIntoView({behavior:"smooth",block:"start"});
}
function renderInfo(){
  const BOT="https://t.me/angelesapoyove_bot";
  const WA="https://wa.me/message/6TEVCZCHJ2TPN1";
  const paso=(n,t,d)=>`<div style="display:flex;gap:12px;margin:12px 0"><div style="flex:0 0 30px;height:30px;border-radius:50%;background:var(--navy);color:#fff;font-weight:800;display:flex;align-items:center;justify-content:center">${n}</div><div><b>${t}</b><div style="color:var(--suave);font-size:14px;margin-top:2px">${d}</div></div></div>`;
  const h2=(t)=>`<h2 style="color:var(--navy);font-size:20px;margin:28px 0 8px">${t}</h2>`;
  return `<div class="info-pagina" style="line-height:1.55;color:var(--tinta)">
    <h2 style="color:var(--navy);font-size:20px;margin:6px 0 8px">Súmate a la red</h2>
    <p>Todo se hace <b>gratis</b> y por la vía que prefieras: <b>WhatsApp, Telegram o directamente aquí en la web</b> — es la misma red por las 3 vías. Eliges tu camino según lo que puedas aportar:</p>
    <div style="display:grid;gap:12px;margin:12px 0">
      <div style="border:1px solid var(--linea);border-radius:14px;padding:14px 16px;background:var(--card)">
        <div style="font-weight:800;color:var(--navy)">🏠 Centros y colaboradores</div>
        <div style="color:var(--suave);font-size:14px;margin-top:4px">Si manejas o ayudas en un <b>centro de acopio, refugio u hospital</b>. Registra un punto nuevo o súmate a uno que ya existe como colaborador, sin repetir la ficha. Reportas lo que el punto tiene y lo que necesita.</div>
      </div>
      <div style="border:1px solid var(--linea);border-radius:14px;padding:14px 16px;background:var(--card)">
        <div style="font-weight:800;color:var(--navy)">🤝 Manos Solidarias</div>
        <div style="color:var(--suave);font-size:14px;margin-top:4px">Si ofreces ayuda <b>gratis</b> sin ser un centro fijo: comida, agua, transporte, carga de teléfonos, atención de salud, hospedaje. Apareces en el mapa para que te encuentren.</div>
      </div>
      <div style="border:1px solid var(--linea);border-radius:14px;padding:14px 16px;background:var(--card)">
        <div style="font-weight:800;color:var(--navy)">📣 Voluntariado</div>
        <div style="color:var(--suave);font-size:14px;margin-top:4px">Si estás <b>en terreno</b>. Levanta <b>llamados de apoyo</b> (un sector, un mensaje, una foto) y el sistema avisa en privado a los centros y Manos Solidarias que puedan cubrirlos. Tu contacto nunca se publica.</div>
      </div>
    </div>
    <div style="text-align:center;margin:16px 0 6px">
      <a class="btn" href="/registrar.html" style="background:var(--teal);color:#fff;font-weight:800;border-color:var(--teal)">🌐 Registrar mi centro aquí (2 min)</a>
      <a class="btn" href="${WA}" target="_blank" rel="noopener" style="background:#25D366;color:#fff;font-weight:800;border-color:#25D366">💬 Sumarme por WhatsApp</a>
      <a class="btn" href="${BOT}" target="_blank" rel="noopener" style="background:var(--navy);color:#fff;font-weight:800;border-color:var(--navy)">✈️ Sumarme por Telegram</a>
    </div>
    <p style="color:var(--suave);font-size:13.5px;text-align:center">🔒 Cédulas y datos personales <b>nunca se publican</b> · moderamos la red y retiramos contenido indebido.</p>
    <details style="border:1px solid var(--linea);border-radius:14px;background:var(--card);padding:4px 16px;margin:18px 0">
      <summary style="cursor:pointer;font-weight:800;color:var(--navy);padding:10px 0;font-size:15.5px">ℹ️ Quiénes somos, qué es esto y cómo funciona</summary>
      ${h2("Qué es")}
      <p>Cuando ocurre una emergencia, la solidaridad aparece rápido — pero desordenada. Unos centros se llenan de donaciones que ya no necesitan; a otros, a pocas cuadras, les falta lo básico. Nadie tiene el panorama completo.</p>
      <p><b>Red de Acopio y Refugio</b> es un registro vivo y abierto de los puntos que atienden la emergencia en Venezuela. Cada centro de acopio, refugio y hospital aparece con su ubicación, su capacidad, lo que tiene disponible y lo que necesita. Así, quien quiere ayudar sabe exactamente a dónde ir.</p>
      ${h2("Cómo funciona")}
      ${paso(1,"El centro se registra","Aquí en la web, por WhatsApp o por Telegram: una ficha rápida con dónde está, qué tipo de punto es y a quién contactar. Toma unos 2 minutos.")}
      ${paso(2,"Reporta lo que tiene y lo que necesita","Por texto, foto, nota de voz o un archivo. El sistema lo organiza solo y lo mantiene al día.")}
      ${paso(3,"Todo aparece en el mapa público","Ordenado, buscable y sin datos personales sensibles. Solo lo que sirve para coordinar.")}
      ${paso(4,"La ayuda llega a su destino","Donantes, voluntarios y otros centros encuentran al instante dónde hace falta lo que pueden dar — y van directo.")}
      ${h2("Para quién es")}
      <ul style="margin:8px 0 0;padding-left:18px">
        <li style="margin:6px 0">🤝 <b>Si quieres ayudar:</b> mira en tiempo real qué necesita cada centro cerca de ti antes de salir a donar.</li>
        <li style="margin:6px 0">📦 <b>Si manejas un centro de acopio:</b> gana visibilidad y recibe justo lo que te falta, no lo que sobra.</li>
        <li style="margin:6px 0">🏠 <b>Si coordinas un refugio:</b> reporta tu capacidad y tus necesidades en un solo lugar.</li>
        <li style="margin:6px 0">🏥 <b>Si atiendes en un hospital:</b> publica tus requerimientos de insumos para que lleguen más rápido.</li>
      </ul>
      <div class="aviso" style="margin-top:18px">🔒 <b>Tu información está protegida.</b> Las cédulas y los nombres de las personas afiliadas <b>nunca se publican</b>. En la web solo se muestra lo necesario para coordinar la ayuda: el centro, su ubicación y lo que necesita.</div>
      <div class="aviso">✅ <b>Transparencia.</b> Registrar un centro <b>siempre es gratis</b>. No redistribuimos donaciones de terceros: la ayuda va directa del donante al centro. Somos una <b>iniciativa ciudadana e independiente</b>, sin afiliación a grupos de rescate ni a organismos oficiales.</div>
      ${h2("Apoya la plataforma")}
      <p style="padding-bottom:10px">Red de Acopio y Refugio es gratis para todos y se mantiene con <b>aportes voluntarios</b> que cubren dominio, servidor y costos de operación. El dinero solo mantiene la plataforma funcionando: <b>no financiamos ni redistribuimos a los centros</b>. ¿Quieres apoyarla? Escríbenos a <a href="mailto:contacto@redacopioyrefugio.com" style="color:var(--navy);font-weight:700">contacto@redacopioyrefugio.com</a>.</p>
    </details>
    <div style="background:var(--navy);color:#fff;border-radius:16px;padding:20px 18px;margin:22px 0 6px;text-align:center">
      <div style="font-size:18px;font-weight:800">¿Manejas un centro, refugio o atiendes en un hospital?</div>
      <p style="margin:8px 0 14px;opacity:.95">Súmate a la red en 2 minutos, por la vía que prefieras.</p>
      <a class="btn" href="/registrar.html" style="background:#fff;color:var(--navy);font-weight:800;border-color:#fff">🌐 Registrarme aquí en la web</a>
      <a class="btn" href="${WA}" target="_blank" rel="noopener" style="background:#25D366;color:#fff;font-weight:800;border-color:#25D366">💬 Sumarme por WhatsApp</a>
      <a class="btn" href="${BOT}" target="_blank" rel="noopener" style="background:rgba(255,255,255,.14);color:#fff;font-weight:800;border-color:rgba(255,255,255,.5)">✈️ Sumarme por Telegram</a>
    </div>
  </div>`;
}
function renderApoya(){
  const MAIL="contacto@redacopioyrefugio.com";
  return `<div class="info-pagina" style="line-height:1.55;color:var(--tinta)">
    <div style="background:linear-gradient(135deg,var(--navy),var(--navy2));color:#fff;border-radius:16px;padding:22px 18px;margin-bottom:6px">
      <div style="font-size:22px;font-weight:800;line-height:1.2">🤍 Apoya la plataforma</div>
      <p style="margin:10px 0 0;opacity:.95">Red de Acopio y Refugio es <b>gratis para todos</b> y se mantiene con <b>aportes voluntarios</b> que cubren dominio, servidor y costos de operación.</p>
    </div>
    <div class="aviso" style="margin-top:14px">✅ <b>Importante:</b> tu aporte <b>solo mantiene la plataforma funcionando</b>. No financiamos ni redistribuimos donaciones a los centros — la ayuda va <b>directa del donante al centro</b>, y registrar un centro <b>siempre es gratis</b>.</div>
    <p style="margin-top:14px">¿Quieres ayudar a sostenerla? Escríbenos y te pasamos las formas de aporte:</p>
    <div style="text-align:center;margin:16px 0 6px">
      <a class="btn" href="mailto:${MAIL}" style="background:var(--navy);color:#fff;font-weight:800;border-color:var(--navy)">✉️ Escribir a ${MAIL}</a>
    </div>
    <div class="aviso">🔒 Transparencia total: el aporte es 100% para sostener la web (dominio, servidor, IA). Sin fines de lucro.</div>
  </div>`;
}
function renderPedir(){
  let items=PEDIDOS.filter(p=>coincide((p.sector||"")+" "+(p.mensaje||"")));
  let html=`<div class="aviso">🆘 <b>Pedidos de ayuda</b>: solicitudes de personas que necesitan algo (comida, agua, medicinas, refugio, encontrar a una persona desaparecida…), enviadas <b>desde la web</b> y revisadas por nuestro equipo. Por seguridad <b>no mostramos el contacto de quien pide</b>; los centros y Manos Solidarias de la zona ya fueron avisados en privado. <b>No es información oficial</b>, verifica.</div>`;
  html+=`<div style="text-align:center;margin:8px 0 12px"><a class="btn" href="/pedir.html" style="background:#b3261e;color:#fff;font-weight:800;border-color:#b3261e">🆘 Pedir ayuda</a></div>`;
  if(!items.length){html+=vacio("No hay pedidos de ayuda publicados ahora.");return html;}
  html+=items.map(pedidoItem).join("");
  return html;
}
function pedidoItem(p){
  const msj=(p.mensaje||"");
  const sh=`🆘 Pedido de ayuda${p.sector?` — ${p.sector}`:""}: ${msj.length>140?msj.slice(0,139).trim()+"…":msj}`;
  const urlPed=paginaPedido(p);
  const enAt=p.estado==="atencion";
  const n=p.n_atienden||0;
  const badge=(enAt||n>0)?`<div style="margin-top:8px"><span class="lla-resp">🤝 ${n>0?(n+" "+(n==1?"persona atendiendo":"personas atendiendo")):"Alguien lo está atendiendo"}</span></div>`:"";
  const esSalud=/salud|medicin|psic|emocional|mental/i.test(p.mensaje||"");
  const salud=esSalud?`<div class="meta" style="margin-top:8px;background:#eef6f7;border-radius:8px;padding:8px 10px;color:#005B67">🧠 Apoyo voluntario entre personas: <b>no sustituye atención médica ni psicológica profesional</b>. En crisis llama al 911.</div>`:"";
  const btnAt=`<button class="btn" data-atender="${p.id}" style="background:#1E9C94;color:#fff;font-weight:700;border-color:#1E9C94">🤝 ${enAt?"También quiero ayudar":"Atender este pedido"}</button>`;
  return `<div class="centro lla-card" id="pd-${p.id}">
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
      <span style="font-size:22px">🆘</span>
      <span style="font-weight:700;color:#b3261e">${esc(p.sector||"Zona no indicada")}</span>
    </div>
    ${p.foto?`<img class="foto" src="${esc(p.foto)}" loading="lazy" alt="">`:""}
    <div style="white-space:pre-wrap;line-height:1.45">${esc(p.mensaje||"")}</div>
    ${badge}${salud}
    <div class="meta" style="margin-top:6px">🕐 ${tiempoDesde(p.creado_en)}</div>
    <div class="acciones">${btnAt}${compartirBtn(sh,urlPed)}${copiarBtn(urlPed)}</div>
  </div>`;
}
function render(){
  poblarZonas(itemsTab());
  const c=document.getElementById("contenido");
  if(activo==="info") c.innerHTML=renderInfo();
  else if(activo==="situacion") c.innerHTML=renderSituacion();
  else if(activo==="necesidades") c.innerHTML=renderNecesidades();
  else if(activo==="oferta") c.innerHTML=renderOferta();
  else if(activo==="directorio") c.innerHTML=renderDirectorio();
  else if(activo==="servicios") c.innerHTML=renderServicios();
  else if(activo==="buscar") c.innerHTML=renderBuscar();
  else if(activo==="emergencias") c.innerHTML=renderEmergencias();
  else if(activo==="sismos") c.innerHTML=renderSismos();
  else if(activo==="angeles") c.innerHTML=renderAngeles();
  else if(activo==="apoya") c.innerHTML=renderApoya();
  else if(activo==="manos") c.innerHTML=renderManos();
  else if(activo==="llamados") c.innerHTML=renderLlamados();
  else if(activo==="pedir") c.innerHTML=renderPedir();
  else if(activo==="desaparecidos") c.innerHTML=renderDesap();
  else c.innerHTML=renderCategoria(activo==="alertas"?"info":activo);
  if(activo==="directorio") initMapa("mapa-red");
  else if(activo==="manos") initMapa("mapa-ms");
}

// Compartir (WhatsApp / hoja nativa) por delegación de eventos
document.getElementById("contenido").addEventListener("click",ev=>{
  const b=ev.target.closest("[data-share]"); if(!b)return;
  const t=b.getAttribute("data-share"), u=b.getAttribute("data-url")||"";
  if(navigator.share){navigator.share({text:t,url:u||undefined}).catch(()=>{});}
  else{window.open("https://wa.me/?text="+encodeURIComponent(t+(u?(" "+u):"")),"_blank");}
});
// Copiar enlace del punto al portapapeles (con fallback a prompt si no hay clipboard)
document.getElementById("contenido").addEventListener("click",ev=>{
  const b=ev.target.closest("[data-copiar]"); if(!b)return;
  const u=b.getAttribute("data-copiar");
  const p=(navigator.clipboard&&navigator.clipboard.writeText)?navigator.clipboard.writeText(u):Promise.reject();
  p.then(()=>{const t=b.textContent;b.textContent="✅ Enlace copiado";setTimeout(()=>{b.textContent=t;},1600);})
   .catch(()=>{prompt("Copia el enlace:",u);});
});
// Botón público: reportar novedad sobre un desaparecido → avisa a moderadores
document.getElementById("contenido").addEventListener("click",ev=>{
  const r=ev.target.closest("[data-reportar]"); if(!r) return;
  const id=Number(r.getAttribute("data-reportar"));
  r.disabled=true; r.textContent="Enviando…";
  fetch("/api/reportar",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({tg_id:id,tipo:"novedad"})})
    .then(res=>res.json()).then(()=>{r.textContent="✅ Reporte enviado, ¡gracias!";})
    .catch(()=>{r.textContent="No se pudo enviar"; r.disabled=false;});
});
// Botón público: DENUNCIAR una publicación (Manos Solidarias o centro) → avisa a moderadores.
// El público NO puede quitar; solo alerta. Los moderadores revisan y deciden.
document.getElementById("contenido").addEventListener("click",ev=>{
  const d=ev.target.closest("[data-denunciar]"); if(!d) return;
  if(!confirm("¿Denunciar esta publicación como indebida o estafa? Nuestro equipo la revisará.")) return;
  const parts=(d.getAttribute("data-denunciar")||"").split(":");
  d.disabled=true; d.textContent="Enviando…";
  fetch("/api/denunciar",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({tipo:parts[0],id:Number(parts[1])})})
    .then(res=>res.json()).then(()=>{d.textContent="✅ Denuncia enviada, ¡gracias!";})
    .catch(()=>{d.textContent="No se pudo enviar"; d.disabled=false;});
});

// 🤝 Solicitar contacto de una Mano Solidaria: sin números en la web; el aliado decide con ✅/❌ desde el bot.
let _msqId=null;
function abrirContactoMS(id){
  _msqId=Number(id);
  const al=ALIADOS.find(x=>x.id===_msqId);
  _modalAt(`<h3>🤝 Solicitar contacto</h3>
    <p style="font-size:13px;color:#5b6b80;margin:4px 0 0;line-height:1.45">Por seguridad no publicamos números. ${al?esc(al.nombre):"La persona"} recibirá tu solicitud y, si acepta, cada quien recibe el contacto del otro.</p>
    <div id="mq-err" style="display:none;color:#9b2226;font-size:13.5px;margin-top:10px"></div>
    <input type="text" id="mq-hp" style="position:absolute;left:-9999px" tabindex="-1" autocomplete="off" aria-hidden="true">
    <label>Tu nombre</label><input id="mq-nom" maxlength="80" placeholder="Tu nombre">
    <label>Tu WhatsApp</label><input id="mq-con" maxlength="60" placeholder="Ej.: 0412 1234567">
    <label>¿Qué necesitas? <span style="color:#8a99ad;font-weight:500">(breve)</span></label>
    <textarea id="mq-msg" maxlength="300" placeholder="Ej.: Necesito pan para 3 personas en Caricuao."></textarea>
    <p style="font-size:12px;color:#8a99ad;margin:10px 0 0;line-height:1.4">Al continuar aceptas el <a href="/aviso-legal.html" target="_blank" rel="noopener" style="color:#1E9C94">Aviso legal</a>. Tu solicitud queda registrada para seguridad de ambos.</p>
    <button class="mbtn" id="mq-go">Enviar solicitud</button>
    <button class="mx" onclick="cerrarAt()">Cancelar</button>`);
  document.getElementById("mq-go").onclick=_enviarMsq;
}
function _enviarMsq(){
  const nom=(document.getElementById("mq-nom").value||"").trim();
  const con=(document.getElementById("mq-con").value||"").trim();
  const msj=(document.getElementById("mq-msg").value||"").trim();
  const hp=(document.getElementById("mq-hp").value||"");
  const err=document.getElementById("mq-err");
  if(!nom||!con){err.textContent="Pon tu nombre y tu WhatsApp para continuar.";err.style.display="block";return;}
  const b=document.getElementById("mq-go"); b.disabled=true; b.textContent="Enviando…";
  fetch("/api/contactar-ms",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({aliado_id:_msqId,nombre:nom,contacto:con,mensaje:msj,website:hp})})
    .then(r=>r.json()).then(d=>{
      if(!d||!d.ok||!d.token) throw new Error((d&&d.error)||"No se pudo");
      _modalAt(`<h3>📨 ¡Solicitud enviada!</h3>
        <p style="font-size:14px;margin:8px 0;line-height:1.5">Le avisamos a la persona: tiene hasta <b>24 horas</b> para responder. Si acepta, te escribimos por WhatsApp y aquí también puedes ver la respuesta:</p>
        <a class="mbtn" style="text-decoration:none;text-align:center;box-sizing:border-box" href="/api/contacto-ms/${d.token}" target="_blank" rel="noopener">🔔 Ver estado de mi solicitud</a>
        <button class="mx" onclick="cerrarAt()">Cerrar</button>`);
    })
    .catch(e=>{err.textContent="No se pudo ("+(e.message||"error")+"). Intenta de nuevo.";err.style.display="block";b.disabled=false;b.textContent="Enviar solicitud";});
}
document.getElementById("contenido").addEventListener("click",ev=>{
  const c=ev.target.closest("[data-contactar]"); if(!c) return;
  abrirContactoMS(c.getAttribute("data-contactar"));
});

// 🆘 Atender un pedido: quien ayuda deja su contacto (queda registrado) → ve el WhatsApp del solicitante.
let _atId=null;
function _modalAt(html){document.getElementById("atender-card").innerHTML=html;document.getElementById("atender-bg").classList.add("on");}
function cerrarAt(){document.getElementById("atender-bg").classList.remove("on");_atId=null;if(activo==="pedir")render();}
document.getElementById("atender-bg").addEventListener("click",ev=>{if(ev.target.id==="atender-bg")cerrarAt();});
document.getElementById("contenido").addEventListener("click",ev=>{
  const a=ev.target.closest("[data-atender]"); if(!a) return;
  _atId=Number(a.getAttribute("data-atender"));
  _modalAt(`<h3>🤝 Atender este pedido</h3>
    <p style="font-size:13px;color:#5b6b80;margin:4px 0 0;line-height:1.45">Deja tus datos (para saber quién ayuda). Luego verás el WhatsApp de la persona para escribirle directo.</p>
    <div id="at-err" style="display:none;color:#9b2226;font-size:13.5px;margin-top:10px"></div>
    <input type="text" id="at-hp" style="position:absolute;left:-9999px" tabindex="-1" autocomplete="off" aria-hidden="true">
    <label>Tu nombre</label><input id="at-nom" maxlength="80" placeholder="Tu nombre o el de tu grupo">
    <label>Tu WhatsApp</label><input id="at-con" maxlength="60" placeholder="Ej.: 0412 1234567">
    <label>¿Cómo puedes ayudar? <span style="color:#8a99ad;font-weight:500">(opcional)</span></label>
    <textarea id="at-nota" maxlength="300" placeholder="Ej.: Tengo camioneta, puedo hacer el traslado hoy."></textarea>
    <p style="font-size:12px;color:#8a99ad;margin:10px 0 0;line-height:1.4">Al continuar aceptas el <a href="/aviso-legal.html" target="_blank" rel="noopener" style="color:#1E9C94">Aviso legal</a>: la Red solo conecta personas; no verifica ni supervisa la ayuda. Verifica identidad y coordina en lugares seguros.</p>
    <button class="mbtn" id="at-go">Ofrecer ayuda</button>
    <button class="mx" onclick="cerrarAt()">Cancelar</button>`);
  document.getElementById("at-go").onclick=_enviarAt;
});
function _enviarAt(){
  const nom=(document.getElementById("at-nom").value||"").trim();
  const con=(document.getElementById("at-con").value||"").trim();
  const nota=(document.getElementById("at-nota").value||"").trim();
  const hp=(document.getElementById("at-hp").value||"");
  const err=document.getElementById("at-err");
  if(!nom||!con){err.textContent="Pon tu nombre y tu WhatsApp para continuar.";err.style.display="block";return;}
  const b=document.getElementById("at-go"); b.disabled=true; b.textContent="Enviando…";
  fetch("/api/atender",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({llamado_id:_atId,nombre:nom,contacto:con,nota:nota,website:hp})})
    .then(r=>r.json()).then(d=>{
      if(!d||!d.ok) throw new Error((d&&d.error)||"No se pudo");
      const p=PEDIDOS.find(x=>x.id===_atId); if(p){p.estado="atencion";p.n_atienden=(p.n_atienden||0)+1;}
      _modalAt(`<h3>✅ Oferta enviada</h3>
        <p style="font-size:14px;margin:8px 0;line-height:1.5">Avisamos a la persona que pidió ayuda. Si acepta compartir su contacto, ambos recibirán los datos para coordinar.</p>
        <button class="mx" onclick="cerrarAt()">Cerrar ventana</button>`);
    })
    .catch(e=>{err.textContent="No se pudo ("+(e.message||"error")+"). Intenta de nuevo.";err.style.display="block";b.disabled=false;b.textContent="Ofrecer ayuda";});
}
function _resolverAt(tk){
  const id=_atId;
  fetch("/api/resolver",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({llamado_id:id,token:tk})})
    .then(r=>r.json()).then(d=>{
      const i=PEDIDOS.findIndex(x=>x.id===id); if(i>=0) PEDIDOS.splice(i,1);
      cerrarAt(); pintarTabs(); render();
    }).catch(()=>{cerrarAt();});
}
document.getElementById("q").oninput=e=>{q=e.target.value.toLowerCase();desapMostrar=DESAP_LOTE;render();};
document.getElementById("zona").onchange=e=>{zonaSel=e.target.value;desapMostrar=DESAP_LOTE;render();};
document.getElementById("contenido").addEventListener("click",e=>{ if(e.target.closest("#geo-btn")) pedirUbicacion(); });

// Compartir / copiar la página completa
(function(){
  const sp=document.getElementById("share-page"), cp=document.getElementById("copy-page");
  if(sp) sp.onclick=()=>{
    if(navigator.share) navigator.share({title:"Red de Acopio y Refugio",text:SHARE_MSG,url:SITE_URL}).catch(()=>{});
    else window.open("https://wa.me/?text="+encodeURIComponent(SHARE_MSG+" "+SITE_URL),"_blank");
  };
  if(cp) cp.onclick=()=>{
    navigator.clipboard.writeText(SHARE_MSG+String.fromCharCode(10)+SITE_URL).then(()=>{
      cp.textContent="✅ Copiado"; setTimeout(()=>cp.textContent="📋 Copiar texto",2000);
    }).catch(()=>{});
  };
})();

// Visor de fotos: tocar una imagen la abre grande; tocar el fondo la cierra.
document.body.addEventListener("click",ev=>{
  const im=ev.target.closest("img.fimg, img.foto");
  if(im && im.getAttribute("src")){
    document.getElementById("lbimg").src=im.getAttribute("src");
    document.getElementById("lightbox").classList.add("on");
    return;
  }
  if(ev.target.closest("#lightbox")) document.getElementById("lightbox").classList.remove("on");
});
document.getElementById("hmeta").textContent=HMETA;
// Deep link: abre la pestaña y enfoca/abre el punto compartido (#oferta:pt-5).
function enfocarPunto(target){
  if(!target) return;
  setTimeout(()=>{
    const el=document.getElementById(target);
    if(!el) return;
    el.open=true;
    el.scrollIntoView({behavior:"smooth",block:"start"});
    el.classList.add("destacado");
    setTimeout(()=>el.classList.remove("destacado"),2600);
  },70);
}
function rutearHash(){
  const m=(location.hash||"").replace(/^#/,"");
  let target="";
  if(m){
    const i=m.indexOf(":");
    const tab=decodeURIComponent(i<0?m:m.slice(0,i));
    target=i<0?"":decodeURIComponent(m.slice(i+1));
    if(tab) activo=tab;
  }
  pintarTabs(); render();
  enfocarPunto(target);
}
// «Necesitan ayuda AHORA»: las 3 necesidades activas más urgentes (por urgencia y
// faltante), visibles en TODAS las pestañas, cada una con su «Lo llevo» inline.
function renderAhora(){
  const el=document.getElementById("ahora");
  if(!el) return;
  const rank=u=>{u=(u||"media").toString().toLowerCase();
    return u.indexOf("crit")===0?0:(u==="alta"?1:(u==="media"?2:3));};
  const items=(SEGUIMIENTO||[]).flatMap(p=>(p.items||[]).map(i=>({...i,_punto:p.punto})))
    .map(i=>({...i,_falta:(+i.meta||0)-(+i.recibido||0)-(+i.en_camino||0)}))
    .filter(i=>(+i.meta||0)>0 && i._falta>0)
    .sort((a,b)=>rank(a.urgencia)-rank(b.urgencia)||b._falta-a._falta);
  if(!items.length){el.innerHTML="";return;}
  el.innerHTML=`<div class="ahora">
    <div class="ahora-head"><b>⚡ Necesitan ayuda AHORA</b><span>lo más urgente en este momento</span><a href="#necesidades" onclick="irTab('necesidades');return false">Ver todo →</a></div>
    <div class="ahora-grid">${items.slice(0,3).map(i=>{
      const u=i.unidad?(" "+esc(i.unidad)):"";
      return `<div class="ahora-card">${urgChip(i.urgencia)||""}
        <div class="a-item">${esc(i.item||"")}</div>
        <div class="a-punto">${esc(i._punto||"")}</div>
        <div class="a-falta">Faltan ${i._falta}${u}</div>
        <button class="btn b-llevar b-accweb" onclick="cmpAbrir(${i.id})">🚚 Lo llevo</button></div>`;
    }).join("")}</div></div>`;
}
renderAhora();
rutearHash();
window.addEventListener("hashchange",rutearHash);

// Contador de visitas (sin cookies; cuenta del lado del servidor, no a los bots)
fetch("/api/visita",{method:"POST"}).then(r=>r.json()).then(d=>{
  if(!d||!d.ok) return;
  const f=n=>Number(n).toLocaleString("es-VE");
  const el=document.getElementById("contador");
  if(el) el.innerHTML=`👁️ <b>${f(d.total)}</b> visitas`+(d.unicos_hoy?` · <b>${f(d.unicos_hoy)}</b> visitantes hoy`:"");
}).catch(()=>{});

// Service worker (funciona sin señal; se activa al servir por https)
if("serviceWorker" in navigator){navigator.serviceWorker.register("sw.js").catch(()=>{});}