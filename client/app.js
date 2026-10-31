const names = {pedidos: 'Pedidos', pagos: 'Pagos', calificaciones: 'Calificaciones', catalogo: 'Catálogo'};
const api = async (path, options = {}) => {
  const response = await fetch(path, {
    ...options,
    credentials: 'same-origin',
    headers: {Accept: 'application/json', ...(options.headers || {})},
  });
  if (!response.ok) {
    let detail = '';
    try { detail = (await response.json()).error || ''; } catch (_) { /* Usa el estado HTTP si no hay JSON. */ }
    const error = new Error(detail || `API ${response.status} en ${path}`);
    error.status = response.status;
    throw error;
  }
  return response.json();
};
const text = value => value == null || value === '' ? '—' : String(value);
const prettyTime = value => {
  if (!value) return 'Sin datos';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('es-CO', {dateStyle: 'short', timeStyle: 'short'});
};
const percent = value => value == null ? '—' : `${(value * 100).toLocaleString('es-CO', {maximumFractionDigits: 1})}%`;
const latency = value => value == null ? '—' : `${Number(value).toLocaleString('es-CO', {maximumFractionDigits: 1})} ms`;
const byId = id => document.getElementById(id);
let activeView = 'overview';
let latestReadModel = null;
let refreshTimer = null;
let customerCart = new Map();
let customerProducts = new Map();
let checkoutIdempotencyKey = null;
let adminProducts = new Map();
let editingProductId = null;
let adminSessionReady = false;

function node(tag, className, content) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (content != null) item.textContent = content;
  return item;
}
function setView(view) {
  activeView = view;
  document.querySelectorAll('.view-section').forEach(section => { section.hidden = section.id !== `view-${view}`; });
  document.querySelectorAll('.nav-item').forEach(button => button.classList.toggle('active', button.dataset.view === view));
  const title = {overview: 'Resumen', services: 'Servicios', incidents: 'Incidentes', simulator: 'Diagnóstico y acciones', orders: 'Pedidos', payments: 'Pagos', catalog: 'Catálogo', ratings: 'Calificaciones', reports: 'Reportes'}[view];
  byId('breadcrumb-title').textContent = title;
  if (location.hash !== `#${view}`) history.replaceState(null, '', `#${view}`);
  if (adminSessionReady && view === 'overview') loadAdminSummary();
  if (adminSessionReady && ['orders', 'payments', 'catalog', 'ratings', 'reports'].includes(view)) loadAdminView(view);
  if (view === 'services') loadPoolHealth();
  if (adminSessionReady && view === 'simulator') { loadSimulatorScenario(); loadOperationalDiagnosis(); }
}
function renderServices(services, target) {
  target.replaceChildren();
  if (!services.length) {
    target.append(node('div', 'empty-state', 'No hay servicios configurados.'));
    return;
  }
  services.forEach(service => {
    const card = node('article', 'service-card');
    const head = node('div', 'service-head');
    const name = node('div', 'service-name');
    name.append(node('i', `service-dot ${service.status || 'no_data'}`), node('span', '', names[service.service] || service.service));
    head.append(name, node('span', 'service-status', serviceStatus(service.status)));
    card.append(head);
    const metric = service.latest_measurement || {};
    const rows = [
      ['Latencia p95', latency(metric.latency_p95_ms)],
      ['Errores', percent(service.error_rate)],
      ['Pool DB', metric.db_pool_active == null ? '—' : `${metric.db_pool_active}/${metric.db_pool_max}`],
    ];
    rows.forEach(([label, value]) => {
      const row = node('div', 'service-metric');
      row.append(node('span', '', label), node('b', '', value));
      card.append(row);
    });
    card.append(node('div', 'service-foot', metric.timestamp ? `Última medición ${prettyTime(metric.timestamp)}` : 'Sin mediciones recibidas'));
    target.append(card);
  });
}
async function loadPoolHealth() {
  const target = byId('pool-grid');
  try {
    const result = await api('/health/detailed');
    target.replaceChildren();
    const entries = Object.entries(result.dependencies || {}).filter(([, detail]) => detail.database_pool);
    entries.forEach(([service, detail]) => {
      const pool = detail.database_pool;
      const card = node('article', 'pool-card');
      const heading = node('div', 'pool-heading');
      const pretty = names[service] || service.charAt(0).toUpperCase() + service.slice(1);
      heading.append(node('b', '', pretty), node('span', `pool-state ${pool.status === 'ok' ? 'ok' : 'degraded'}`, detail.status === 'ok' ? 'Conectado' : 'Degradado'));
      card.append(heading);
      const rows = [['Activas', `${pool.active ?? '—'} / ${pool.max ?? '—'}`], ['Disponibles', text(pool.available)],
        ['En espera', text(pool.waiting)], ['Errores de adquisición', text(pool.acquire_errors)], ['Espera acumulada', pool.wait_ms_total == null ? '—' : `${pool.wait_ms_total} ms`]];
      rows.forEach(([label, value]) => { const row = node('div','pool-row'); row.append(node('span','',label),node('b','',value)); card.append(row); });
      if (pool.max && pool.active != null && pool.active / pool.max >= .75) card.append(node('div','pool-warning','Pool con ocupación de 75 % o más.'));
      target.append(card);
    });
    if (!entries.length) target.append(node('div','empty-state','Ningún servicio informó métricas de pool.'));
  } catch (error) {
    target.replaceChildren(node('div','empty-state',`No se pudo consultar /health/detailed: ${error.message}`));
  }
}
function serviceStatus(value) {
  return ({critical: 'Crítico', warning: 'Advertencia', ok: 'Normal', no_data: 'Sin datos'})[value] || value || 'Sin datos';
}
function renderIncidents(incidents, target, compact = false) {
  target.replaceChildren();
  if (!incidents.length) {
    if (compact) target.append(node('div', 'empty-state', 'No hay incidentes abiertos.'));
    else {
      const row=document.createElement('tr'),cell=document.createElement('td');cell.colSpan=7;cell.className='empty-state';cell.textContent='No hay incidentes en esta lista.';row.append(cell);target.append(row);
    }
    return;
  }
  incidents.forEach(incident => {
    if (compact) {
      const item = node('article', 'incident-item');
      const top = node('div', 'incident-top');
      top.append(node('span', 'incident-name', names[incident.service] || incident.service), node('span', `severity ${incident.severity}`, severityName(incident.severity)));
      item.append(top, node('div', 'incident-summary', incident.reasons.map(reasonName).join(' · ') || incident.title), node('div', 'incident-time', `Desde ${prettyTime(incident.started_at)}`));
      target.append(item);
    } else {
      const row = document.createElement('tr');
      row.append(node('td', 'service-cell', names[incident.service] || incident.service));
      row.append(node('td', '', incident.status === 'open' ? 'Abierto' : 'Resuelto'));
      const severity = document.createElement('td'); severity.append(node('span', `severity ${incident.severity}`, severityName(incident.severity))); row.append(severity);
      row.append(node('td', '', prettyTime(incident.started_at)));
      row.append(node('td', '', prettyTime(incident.last_seen)));
      const causes = node('td', 'cause-list');
      (incident.reasons || []).forEach(reason => causes.append(node('span', 'cause-chip', reasonName(reason))));
      row.append(causes);
      const actionCell=document.createElement('td');
      const open=node('button','text-button incident-open-detail','Ver detalle');open.type='button';open.dataset.openIncident=incident.id;
      actionCell.append(open);row.append(actionCell);
      target.append(row);
    }
  });
}
function renderIncidentGroups(incidents) {
  renderIncidents(incidents.filter(item=>item.status==='open'),byId('incident-pending-rows'));
  renderIncidents(incidents.filter(item=>item.status==='resolved'),byId('incident-completed-rows'));
}
function severityName(value) { return ({critical: 'Crítico', warning: 'Advertencia', resolved: 'Resuelto'})[value] || value || '—'; }
function reasonName(reason) {
  const labels = {latency_p95_ms: 'Latencia p95', error_rate: 'Tasa de errores', db_pool_ratio: 'Pool DB'};
  const value = reason.signal === 'error_rate' || reason.signal === 'db_pool_ratio' ? percent(reason.value) : latency(reason.value);
  return `${labels[reason.signal] || reason.signal}: ${value}`;
}
async function loadIncidentDetail(id) {
  const panel=byId('incident-detail-panel'),target=byId('incident-detail-content');
  panel.hidden=false;panel.scrollIntoView({behavior:'smooth',block:'start'});
  target.replaceChildren(node('div','loading-row','Cargando incidente, métricas, logs y código relacionado…'));
  try {
    const incident=await api(`/api/incidents/${encodeURIComponent(id)}`);
    const start=new Date(new Date(incident.started_at).getTime()-60_000);
    const incidentEnd=incident.status==='open'?Date.now():new Date(incident.resolved_at||incident.last_seen).getTime();
    const end=new Date(incidentEnd+60_000);
    const logStart=new Date(Math.max(start.getTime(),end.getTime()-31*24*60*60*1000));
    const params=new URLSearchParams({service:incident.service,from:logStart.toISOString(),to:end.toISOString(),limit:'200'});
    const metricParams=new URLSearchParams({service:incident.service,from:logStart.toISOString(),to:end.toISOString(),limit:'500'});
    const [telemetryResult,logsResult,codeResult]=await Promise.allSettled([
      api(`/api/telemetry?${metricParams}`),
      api(`/api/telemetry/logs?${params}`),
      api(`/api/incidents/${encodeURIComponent(id)}/code-context`),
    ]);
    renderIncidentDetail(incident,{telemetry:telemetryResult.status==='fulfilled'?telemetryResult.value:null,
      telemetryError:telemetryResult.status==='rejected'?telemetryResult.reason.message:null,
      logs:logsResult.status==='fulfilled'?logsResult.value:null,
      logsError:logsResult.status==='rejected'?logsResult.reason.message:null,
      code:codeResult.status==='fulfilled'?codeResult.value:null,
      codeError:codeResult.status==='rejected'?codeResult.reason.message:null,
      from:start,to:end,logFrom:logStart});
  } catch(error) {
    target.replaceChildren(node('div','error-banner',`No se pudo cargar el incidente: ${error.message}`));
  }
}
function renderIncidentDetail(incident,data) {
  const target=byId('incident-detail-content');target.replaceChildren();
  const serviceName=names[incident.service]||incident.service;
  byId('incident-detail-title').textContent=`${serviceName} · ${incident.title}`;
  byId('incident-detail-subtitle').textContent=`ID ${incident.id} · ${incident.status==='open'?'Abierto':'Resuelto'} · ${severityName(incident.severity)}`;
  const overview=node('div','incident-detail-overview');
  const latest=incident.latest_measurement||{};
  const opening=incident.initial_measurement||incident.latest_measurement||{};
  const recoveryRequired=incident.recovery_required||3;
  const recoveryStatus=incident.status==='resolved'?(incident.resolution_reason==='inactivity_timeout'?'Cerrado por inactividad (sin tráfico)':`${incident.recovery_count||recoveryRequired}/${recoveryRequired} muestras sanas consecutivas`):`Validando ${incident.recovery_count||0}/${recoveryRequired} muestras sanas`;
  const pool=latest.db_pool_active==null?'Sin medición':`${latest.db_pool_active}/${latest.db_pool_max}`;
  const errRate=latest.request_count?`${(latest.error_count/latest.request_count*100).toLocaleString('es-CO',{maximumFractionDigits:1})}%`:'Sin tráfico';
  [['Detectado',prettyTime(incident.started_at)],['Última señal',prettyTime(incident.last_seen)],['Resuelto',prettyTime(incident.resolved_at)],['Recuperación',recoveryStatus],['Última tasa observada',errRate],['Solicitudes / errores',`${latest.request_count??'—'} / ${latest.error_count??'—'}`],['Pool observado',pool]].forEach(([label,value])=>{const item=node('div','simulator-stat');item.append(node('span','',label),node('b','',value));overview.append(item);});
  target.append(overview);
  const causes=node('section','incident-detail-section');causes.append(node('h3','','Causas detectadas al abrir (evidencia conservada)'));
  const openingReasons=incident.initial_reasons||incident.reasons||[];
  causes.append(node('p','simulator-assumptions',`Cierre automático: ${recoveryRequired} mediciones consecutivas con tráfico, dentro de los umbrales. Cualquier medición degradada reinicia el conteo; una muestra sin solicitudes no confirma recuperación ni reinicia el conteo. Si no hay tráfico durante la ventana de inactividad configurada, el incidente se cierra por inactividad.`));
  const causeList=node('div','cause-list');openingReasons.forEach(reason=>causeList.append(node('span','cause-chip',`${reasonName(reason)} · umbral ${reason.signal==='latency_p95_ms'?latency(reason.threshold):percent(reason.threshold)}`)));
  if(!openingReasons.length)causeList.append(node('p','empty-state','El registro no conserva señales de causa.'));
  causes.append(node('p','simulator-assumptions',`Medición original: ${prettyTime(opening.timestamp)} · ${opening.request_count??'—'} solicitudes · ${opening.error_count??'—'} errores · latencia p95 ${latency(opening.latency_p95_ms)}.`));
  causes.append(causeList);target.append(causes);

  const diagnostics=(opening.diagnostics||[]).filter(item=>item.errors>0);
  const routeSection=node('section','incident-detail-section');routeSection.append(node('h3','','Rutas y respuestas con error (medición original de apertura)'));
  if(diagnostics.length){
    const table=document.createElement('table');table.innerHTML='<thead><tr><th>Método / ruta</th><th>Errores / solicitudes</th><th>Status HTTP</th></tr></thead><tbody></tbody>';
    const body=table.querySelector('tbody');diagnostics.forEach(item=>{const row=document.createElement('tr');row.append(node('td','service-cell',`${item.method} ${item.route}`),node('td','',`${item.errors}/${item.requests}`),node('td','',Object.entries(item.statuses||{}).map(([status,count])=>`${status} × ${count}`).join(' · ')||'No disponible'));body.append(row);});
    const scroll=node('div','table-scroll');scroll.append(table);routeSection.append(scroll);
  } else routeSection.append(node('p','empty-state','La medición del incidente no guardó errores por ruta.'));
  target.append(routeSection);

  const metricSection=node('section','incident-detail-section');metricSection.append(node('h3','','Muestras de Telemetry en el periodo'));
  if(data.telemetryError)metricSection.append(node('p','error-banner',`No se pudieron consultar las muestras: ${data.telemetryError}`));
  else {
    const from=data.from.getTime(),to=data.to.getTime();
    const measurements=(data.telemetry?.measurements||[]).filter(item=>{const time=new Date(item.timestamp).getTime();return time>=from&&time<=to;}).slice(0,30);
    if(measurements.length){
      const table=document.createElement('table');table.innerHTML='<thead><tr><th>Hora</th><th>Solicitudes</th><th>Errores</th><th>Latencia p95</th><th>Pool muestreado</th><th>Origen</th></tr></thead><tbody></tbody>';
      const body=table.querySelector('tbody');measurements.forEach(item=>{const row=document.createElement('tr');row.append(node('td','',prettyTime(item.timestamp)),node('td','',String(item.request_count)),node('td','',String(item.error_count)),node('td','',latency(item.latency_p95_ms)),node('td','',item.db_pool_active==null?'—':`${item.db_pool_active}/${item.db_pool_max}`),node('td','',item.source));body.append(row);});
      const scroll=node('div','table-scroll');scroll.append(table);metricSection.append(scroll);
    } else metricSection.append(node('p','empty-state','No hay muestras de Telemetry dentro del periodo del incidente.'));
  }
  target.append(metricSection);

  const logsSection=node('section','incident-detail-section');logsSection.append(node('h3','','Logs estructurados relacionados'));
  if(data.logsError)logsSection.append(node('p','error-banner',`No se pudieron consultar logs: ${data.logsError}`));
  else {
    const logs=data.logs?.logs||[];
    if(!logs.length)logsSection.append(node('p','simulation-notice',`No hay logs persistidos para este intervalo (${prettyTime(data.logFrom.toISOString())} en adelante). La recolección estructurada comienza con esta versión; para incidentes previos solo queda la evidencia de Telemetry. Los contenedores también mantienen stdout mientras existan.`));
    logs.forEach(log=>{
      const card=node('article','incident-log-card');
      const head=node('div','incident-log-head');head.append(node('b','',`${log.level.toUpperCase()} · ${log.event}`),node('span','',prettyTime(log.timestamp)));card.append(head);
      card.append(node('div','incident-log-meta',`${log.service}${log.method?` · ${log.method}`:''}${log.route?` ${log.route}`:''}${log.status?` · HTTP ${log.status}`:''}${log.request_id?` · request_id ${log.request_id}`:''}`));
      if(log.error_type||log.message)card.append(node('p','',`${log.error_type?`${log.error_type}: `:''}${log.message||''}`));
      if(log.details&&Object.keys(log.details).length){const details=document.createElement('details');details.append(node('summary','','Campos estructurados'));const pre=node('pre','incident-code',JSON.stringify(log.details,null,2));details.append(pre);card.append(details);}
      if(log.stack_trace){const details=document.createElement('details');details.append(node('summary','','Stack trace'));details.append(node('pre','incident-code',log.stack_trace));card.append(details);}
      logsSection.append(card);
    });
  }
  target.append(logsSection);

  const codeSection=node('section','incident-detail-section');codeSection.append(node('h3','','Código relacionado'));
  if(data.codeError)codeSection.append(node('p','error-banner',`No se pudo obtener el contexto de código: ${data.codeError}`));
  else if(data.code){
    codeSection.append(node('p','simulator-assumptions',`${data.code.explanation} ${data.code.source_revision}`));
    data.code.snippets.forEach(snippet=>{const details=document.createElement('details');details.open=snippet.symbol==='route_request'||snippet.symbol==='do_GET';details.append(node('summary','',`${snippet.path}:${snippet.start_line} · ${snippet.symbol} (líneas ${snippet.start_line}–${snippet.end_line})`));details.append(node('pre','incident-code',snippet.source));codeSection.append(details);});
    if(!data.code.snippets.length)codeSection.append(node('p','empty-state','No se identificó un fragmento de código según la medición guardada.'));
  }
  target.append(codeSection);
}
function setHealth(status) {
  const pill = byId('health-pill');
  const label = status === 'ok' ? 'Todos los servicios operativos' : status === 'degraded' ? 'Degradación detectada' : 'Sin conexión';
  pill.className = `health-pill ${status === 'ok' ? 'ok' : status === 'degraded' ? 'degraded' : 'pending'}`;
  pill.querySelector('b').textContent = label;
  byId('sidebar-status').textContent = status === 'ok' ? 'Monitoreo activo' : status === 'degraded' ? 'Atención requerida' : 'Sin conexión';
}
function drawChart(measurements, selected) {
  const svg = byId('latency-chart');
  svg.replaceChildren();
  const data = measurements.filter(item => item.service === selected).sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp));
  byId('chart-empty').hidden = data.length > 0;
  svg.hidden = data.length === 0;
  if (!data.length) { byId('chart-peak').textContent = 'Pico: —'; return; }
  const NS = 'http://www.w3.org/2000/svg';
  const add = (tag, attrs, label) => {
    const element = document.createElementNS(NS, tag);
    Object.entries(attrs).forEach(([key, value]) => element.setAttribute(key, value));
    if (label) element.textContent = label;
    svg.append(element);
    return element;
  };
  const W = 760, H = 250, left = 55, right = 15, top = 20, bottom = 38;
  const maxObserved = Math.max(...data.map(item => Number(item.latency_p95_ms) || 0));
  const max = Math.max(100, Math.ceil(maxObserved / 100) * 100);
  const x = index => data.length === 1 ? (left + W - right) / 2 : left + (W - left - right) * index / (data.length - 1);
  const y = value => top + (H - top - bottom) * (1 - value / max);
  for (let step = 0; step <= 4; step++) {
    const value = max * step / 4, py = y(value);
    add('line', {x1: left, y1: py, x2: W - right, y2: py, stroke: '#e9edf3', 'stroke-width': 1});
    add('text', {x: left - 9, y: py + 3, 'text-anchor': 'end', fill: '#8995a5', 'font-size': 9}, `${Math.round(value).toLocaleString('es-CO')} ms`);
  }
  const line = data.map((item, index) => `${index ? 'L' : 'M'} ${x(index)} ${y(Number(item.latency_p95_ms) || 0)}`).join(' ');
  add('path', {d: line, fill: 'none', stroke: '#447ef5', 'stroke-width': 2.5, 'stroke-linejoin': 'round', 'stroke-linecap': 'round'});
  data.forEach((item, index) => {
    const circle = add('circle', {cx: x(index), cy: y(Number(item.latency_p95_ms) || 0), r: 4, fill: '#447ef5', stroke: '#fff', 'stroke-width': 2});
    circle.append(document.createElementNS(NS, 'title'));
    circle.firstChild.textContent = `${prettyTime(item.timestamp)} · ${latency(item.latency_p95_ms)}`;
  });
  const marks = [0, Math.floor((data.length - 1) / 2), data.length - 1].filter((value, index, all) => all.indexOf(value) === index);
  marks.forEach(index => add('text', {x: x(index), y: H - 10, 'text-anchor': 'middle', fill: '#8995a5', 'font-size': 9}, prettyTime(data[index].timestamp)));
  byId('chart-peak').textContent = `Pico: ${latency(maxObserved)}`;
}
async function refresh() {
  const button = byId('refresh-button');
  button.disabled = true;
  byId('global-error').hidden = true;
  try {
    const [health, overview, incidentData, measurements, allIncidents] = await Promise.all([
      api('/health'), api('/api/views/overview'), api('/api/views/incidents?status=open'), api('/api/telemetry?limit=200'), api('/api/views/incidents?status=all')
    ]);
    latestReadModel = overview;
    setHealth(health.status);
    byId('measurement-count').textContent = Number(overview.measurement_count || 0).toLocaleString('es-CO');
    byId('open-count').textContent = Number(overview.open_incident_count || 0).toLocaleString('es-CO');
    const services = overview.services || [];
    byId('alert-service-count').textContent = services.filter(service => ['critical', 'warning'].includes(service.status)).length.toLocaleString('es-CO');
    byId('incident-foot').textContent = overview.open_incident_count ? 'Requieren seguimiento' : 'Sin incidentes activos';
    byId('nav-incident-count').textContent = String(overview.open_incident_count || 0);
    renderServices(services, byId('overview-services'));
    renderIncidents(incidentData.incidents || [], byId('overview-incidents'), true);
    renderIncidentGroups(allIncidents.incidents || []);
    renderServices(services, byId('services-detail'));
    drawChart(measurements.measurements || [], byId('chart-service').value);
    byId('updated-at').textContent = `Actualizado ${new Date().toLocaleTimeString('es-CO', {hour: '2-digit', minute: '2-digit', second: '2-digit'})}`;
  } catch (error) {
    if (error.status === 401) {
      if (refreshTimer) window.clearInterval(refreshTimer);
      showLogin('La sesión expiró. Inicia sesión de nuevo.');
      return;
    }
    setHealth('offline');
    byId('global-error').textContent = `No se pudieron cargar las vistas: ${error.message}. Verifica que los microservicios estén activos.`;
    byId('global-error').hidden = false;
  } finally {
    button.disabled = false;
  }
  if (adminSessionReady && activeView === 'overview') loadAdminSummary();
}
document.querySelectorAll('.nav-item').forEach(button => button.addEventListener('click', () => setView(button.dataset.view)));
document.querySelectorAll('[data-go]').forEach(button => button.addEventListener('click', () => setView(button.dataset.go)));
byId('refresh-button').addEventListener('click', refresh);
byId('business-refresh').addEventListener('click', loadAdminSummary);
byId('business-orders').addEventListener('click', event => {
  if (event.target.closest('[data-open-orders]')) setView('orders');
});
byId('pool-refresh').addEventListener('click', loadPoolHealth);
byId('login-form').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = byId('login-button');
  const errorBox = byId('login-error');
  button.disabled = true;
  errorBox.hidden = true;
  byId('login-notice').hidden = true;
  try {
    const credentials = Object.fromEntries(new FormData(form));
    const result = await api('/api/auth/login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(credentials),
    });
    if (!['admin', 'customer'].includes(result.user?.role)) throw new Error('Rol de cuenta no reconocido.');
    form.reset();
    showDashboard(result.user);
  } catch (error) {
    errorBox.textContent = error.status === 401 ? 'Correo o contraseña incorrectos.' : error.message;
    errorBox.hidden = false;
  } finally {
    button.disabled = false;
  }
});
byId('register-form').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = byId('register-button');
  const errorBox = byId('register-error');
  button.disabled = true;
  errorBox.hidden = true;
  try {
    const credentials = Object.fromEntries(new FormData(form));
    await api('/api/auth/register', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(credentials),
    });
    form.reset();
    byId('login-email').value = credentials.email;
    byId('register-view').hidden = true;
    byId('login-view').hidden = false;
    byId('login-notice').textContent = 'Cuenta creada. Inicia sesión para continuar.';
    byId('login-notice').hidden = false;
  } catch (error) {
    errorBox.textContent = error.message;
    errorBox.hidden = false;
  } finally {
    button.disabled = false;
  }
});
byId('show-register').addEventListener('click', () => {
  byId('login-view').hidden = true;
  byId('register-view').hidden = false;
  byId('register-error').hidden = true;
});
byId('show-login').addEventListener('click', () => {
  byId('register-view').hidden = true;
  byId('login-view').hidden = false;
  byId('login-error').hidden = true;
});
byId('logout-button').addEventListener('click', async () => {
  try { await api('/api/auth/logout', {method: 'POST'}); } catch (_) { /* La vista se cierra aunque la API no responda. */ }
  if (refreshTimer) window.clearInterval(refreshTimer);
  showLogin();
});
byId('customer-logout').addEventListener('click', async () => {
  try { await api('/api/auth/logout', {method: 'POST'}); } catch (_) { /* Limpia el panel local aunque falle la revocación. */ }
  showLogin();
});
byId('orders-refresh').addEventListener('click', loadCustomerDashboard);
byId('checkout-button').addEventListener('click', checkout);
byId('customer-report-form').addEventListener('submit', async event => {
  event.preventDefault();
  const form=event.currentTarget, button=form.querySelector('button[type="submit"]'), message=byId('customer-report-form-message');
  button.disabled=true;message.hidden=true;
  const values=Object.fromEntries(new FormData(form));
  try {
    await api('/api/reports',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...values,order_id:values.order_id||null})});
    form.reset();message.textContent='Reporte enviado. Puedes consultar el seguimiento en esta sección.';message.className='customer-report-message success';message.hidden=false;
    await loadCustomerDashboard();
  } catch(error) {message.textContent=error.message;message.className='customer-report-message error';message.hidden=false;}
  finally {button.disabled=false;}
});
byId('customer-orders').addEventListener('click', async event => {
  const button = event.target.closest('[data-cancel-order]');
  if (!button) return;
  button.disabled = true;
  try {
    await api(`/api/pedidos/orders/${encodeURIComponent(button.dataset.cancelOrder)}/cancel`, {
      method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({}),
    });
    await loadCustomerDashboard();
  } catch (error) { showCustomerError(error.message); button.disabled = false; }
});
byId('customer-orders').addEventListener('submit', async event => {
  const form = event.target.closest('[data-rating-form]');
  if (!form) return;
  event.preventDefault();
  const button = form.querySelector('button[type="submit"]');
  button.disabled = true;
  try {
    const values = Object.fromEntries(new FormData(form));
    await api('/api/calificaciones/ratings', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({order_id: form.dataset.ratingForm, score: Number(values.score), comment: values.comment}),
    });
    await loadCustomerDashboard();
  } catch (error) { showCustomerError(error.message); button.disabled = false; }
});
byId('customer-products').addEventListener('click', event => {
  const button = event.target.closest('[data-add-product]');
  if (!button) return;
  const id = button.dataset.addProduct;
  const quantity = customerCart.get(id)?.quantity || 0;
  checkoutIdempotencyKey = null;
  customerCart.set(id, {product: customerProducts.get(id), quantity: quantity + 1});
  renderCart();
});
byId('cart-items').addEventListener('click', event => {
  const button = event.target.closest('[data-remove-product]');
  if (!button) return;
  checkoutIdempotencyKey = null;
  customerCart.delete(button.dataset.removeProduct);
  renderCart();
});
byId('chart-service').addEventListener('change', async () => {
  try { const result = await api('/api/telemetry?limit=200'); drawChart(result.measurements || [], byId('chart-service').value); }
  catch (error) { byId('global-error').textContent = `No se pudo actualizar la gráfica: ${error.message}`; byId('global-error').hidden = false; }
});
['incident-pending-rows','incident-completed-rows'].forEach(id=>byId(id).addEventListener('click', event => {
  const button=event.target.closest('[data-open-incident]');
  if(button)loadIncidentDetail(button.dataset.openIncident);
}));
byId('incident-detail-close').addEventListener('click',()=>{byId('incident-detail-panel').hidden=true;});
document.querySelectorAll('[data-admin-refresh]').forEach(button => button.addEventListener('click', () => loadAdminView(activeView)));
byId('simulator-reload').addEventListener('click', () => { loadSimulatorScenario(); loadOperationalDiagnosis(); });
byId('generate-orders-report').addEventListener('click', generateRealOrdersReport);
byId('apply-orders-index').addEventListener('click', async event => {
  const button=event.currentTarget;button.disabled=true;
  const target=byId('remediation-result');target.replaceChildren(node('div','loading-row','Creando índice concurrente y verificando el plan…'));
  try {
    const result=await api('/api/incidents/actions/index-orders-created-at',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
    target.replaceChildren(node('div','remediation-success',`${result.message} Se aplicó a ${result.report?.source||'soc_orders.orders'}.`));
    button.hidden=true;
    if(result.report)renderRealOrdersReport(result.report);else await generateRealOrdersReport();
    await loadOperationalDiagnosis();
  } catch(error) {target.replaceChildren(node('div','error-banner',`No se aplicó el índice: ${error.message}`));}
  finally {button.disabled=false;}
});
byId('analyze-orders-action').addEventListener('click', async event => {
  const button=event.currentTarget;button.disabled=true;
  const target=byId('remediation-result');target.replaceChildren(node('div','loading-row','Actualizando estadísticas de PostgreSQL…'));
  try {
    const result=await api('/api/incidents/actions/analyze-orders',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
    target.replaceChildren(node('div','remediation-success',`${result.status}: ${result.effect}.`),node('div','simulator-assumptions',`Tabla ${result.table} · Filas estimadas ${result.statistics.estimated_live_rows} · Último ANALYZE ${result.statistics.last_analyze || 'no disponible'}. ${result.limitation}`));
    await generateRealOrdersReport();
    await loadOperationalDiagnosis();
  } catch(error) { target.replaceChildren(node('div','error-banner',`Acción no aplicada: ${error.message}`)); }
  finally {button.disabled=false;}
});
byId('simulator-form').addEventListener('submit', async event => {
  event.preventDefault();
  const selected = [...byId('simulator-form').querySelectorAll('input[name="mitigation"]:checked')].map(input => input.value);
  const error = byId('simulator-error'); error.hidden = true;
  if (!selected.length) { error.textContent = 'Selecciona al menos una medida correctiva.'; error.hidden = false; return; }
  const button = event.currentTarget.querySelector('button[type="submit"]'); button.disabled = true;
  try {
    const rows = Number(byId('simulator-form').elements.namedItem('rows').value);
    const result = await api('/api/incidents/simulator/run', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mitigations:selected,mode:'local_benchmark',rows})});
    renderSimulatorResults(result);
  } catch (failure) { error.textContent = failure.message; error.hidden = false; }
  finally { button.disabled = false; }
});
byId('admin-orders').addEventListener('change', async event => {
  const select = event.target.closest('[data-order-status]');
  if (!select) return;
  select.disabled = true;
  try {
    await api(`/api/pedidos/orders/${encodeURIComponent(select.dataset.orderStatus)}`, {method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({status:select.value})});
    await loadAdminView('orders');
  } catch (error) { showAdminError(error.message); await loadAdminView('orders'); }
});
byId('admin-reports').addEventListener('submit', async event => {
  const form=event.target.closest('[data-admin-report]');if(!form)return;
  event.preventDefault();const button=form.querySelector('button[type="submit"]');button.disabled=true;
  const values=Object.fromEntries(new FormData(form));
  try {await api(`/api/reports/${encodeURIComponent(form.dataset.adminReport)}`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(values)});await loadAdminView('reports');}
  catch(error){showAdminError(error.message);button.disabled=false;}
});
byId('report-status-filter').addEventListener('change',()=>loadAdminView('reports'));
byId('report-category-filter').addEventListener('change',()=>loadAdminView('reports'));
byId('admin-payments').addEventListener('click', async event => {
  const button = event.target.closest('[data-confirm-payment]');
  if (!button) return;
  if (!window.confirm('Confirma que recibiste el efectivo de este pedido entregado?')) return;
  button.disabled = true;
  try { await api(`/api/pagos/payments/${encodeURIComponent(button.dataset.confirmPayment)}`, {method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({status:'paid'})}); await loadAdminView('payments'); }
  catch (error) { showAdminError(error.message); button.disabled = false; }
});
byId('admin-products').addEventListener('click', async event => {
  const edit = event.target.closest('[data-edit-product]');
  const toggle = event.target.closest('[data-toggle-product]');
  if (edit) { const product = adminProducts.get(edit.dataset.editProduct); if (product) fillProductForm(product); }
  if (toggle) {
    const product = adminProducts.get(toggle.dataset.toggleProduct);
    if (!product) return;
    const publish = !product.active;
    if (!window.confirm(publish ? '¿Publicar este producto en el catálogo?' : '¿Retirar este producto del catálogo?')) return;
    toggle.disabled = true;
    try {
      await api(`/api/catalogo/products/${encodeURIComponent(product.id)}`, {method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({active: publish})});
      await loadAdminView('catalog');
    } catch (error) { showAdminError(error.message); toggle.disabled = false; }
  }
});
byId('admin-product-form').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget, errorBox = byId('product-form-error');
  errorBox.hidden = true;
  const data = Object.fromEntries(new FormData(form));
  data.price = Number(data.price);
  if (!data.image_url) data.image_url = null;
  try {
    if (editingProductId) await api(`/api/catalogo/products/${encodeURIComponent(editingProductId)}`, {method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
    else await api('/api/catalogo/products', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
    resetProductForm(); await loadAdminView('catalog');
  } catch (error) { errorBox.textContent = error.message; errorBox.hidden = false; }
});
byId('product-form-cancel').addEventListener('click', resetProductForm);
window.addEventListener('hashchange', () => {
  const view = location.hash.slice(1);
  if (['overview', 'services', 'incidents', 'simulator', 'orders', 'payments', 'catalog', 'ratings', 'reports'].includes(view)) setView(view);
});
const initialView = location.hash.slice(1);
setView(['overview', 'services', 'incidents', 'simulator', 'orders', 'payments', 'catalog', 'ratings', 'reports'].includes(initialView) ? initialView : 'overview');

function showLogin(message = '') {
  adminSessionReady = false;
  byId('auth-screen').hidden = false;
  document.querySelector('.shell').hidden = true;
  byId('customer-dashboard').hidden = true;
  customerCart.clear();
  customerProducts.clear();
  checkoutIdempotencyKey = null;
  renderCart();
  byId('customer-products').replaceChildren();
  byId('customer-orders').replaceChildren();
  byId('customer-payments').replaceChildren();
  byId('customer-reports').replaceChildren();
  byId('measurement-count').textContent = '—';
  byId('open-count').textContent = '—';
  byId('alert-service-count').textContent = '—';
  byId('overview-services').replaceChildren();
  byId('services-detail').replaceChildren();
  byId('overview-incidents').replaceChildren();
  byId('incident-pending-rows').replaceChildren();
  byId('incident-completed-rows').replaceChildren();
  byId('incident-detail-panel').hidden = true;
  byId('incident-detail-content').replaceChildren();
  byId('business-metrics').replaceChildren();
  byId('business-orders').replaceChildren();
  byId('business-summary-error').hidden = true;
  byId('latency-chart').replaceChildren();
  const errorBox = byId('login-error');
  errorBox.textContent = message;
  errorBox.hidden = !message;
  byId('login-notice').hidden = true;
  byId('login-view').hidden = false;
  byId('register-view').hidden = true;
}
function showDashboard(user) {
  if (user.role === 'customer') {
    showCustomerDashboard(user);
    return;
  }
  adminSessionReady = true;
  byId('auth-screen').hidden = true;
  byId('customer-dashboard').hidden = true;
  document.querySelector('.shell').hidden = false;
  byId('user-email').textContent = user.email;
  if (activeView !== 'overview') setView(activeView);
  refresh();
  if (refreshTimer) window.clearInterval(refreshTimer);
  refreshTimer = window.setInterval(refresh, 15000);
}
async function initializeApp() {
  try {
    const result = await api('/api/auth/me');
    if (!['admin', 'customer'].includes(result.user?.role)) throw new Error('Rol de cuenta no reconocido.');
    showDashboard(result.user);
  } catch (error) {
    showLogin(error.status === 401 ? '' : 'No se pudo validar la sesión. Comprueba que el servidor esté activo.');
  }
}
initializeApp();

function showCustomerDashboard(user) {
  byId('auth-screen').hidden = true;
  document.querySelector('.shell').hidden = true;
  byId('customer-dashboard').hidden = false;
  byId('customer-email').textContent = user.email;
  loadCustomerDashboard();
}
function money(value) {
  return `$ ${Number(value || 0).toLocaleString('es-CO', {maximumFractionDigits: 2})}`;
}
function showCustomerError(message) {
  const box = byId('customer-error');
  box.textContent = message;
  box.hidden = false;
}
async function loadCustomerDashboard() {
  byId('customer-error').hidden = true;
  customerProducts = new Map();
  const paths = [
    ['/api/catalogo/products?limit=100', 'Catálogo'],
    ['/api/pedidos/orders', 'Pedidos'],
    ['/api/pagos/payments', 'Pagos'],
    ['/api/calificaciones/ratings', 'Calificaciones'],
    ['/api/reports', 'Reportes'],
  ];
  const results = await Promise.allSettled(paths.map(([path]) => api(path)));
  if (results.some(result => result.status === 'rejected' && result.reason?.status === 401)) {
    showLogin('La sesión expiró. Inicia sesión de nuevo.');
    return;
  }
  const values = results.map(result => result.status === 'fulfilled' ? result.value : null);
  if (values[0]) {
    customerProducts = new Map((values[0].products || []).map(product => [product.id, product]));
    renderProducts([...customerProducts.values()]);
  } else {
    byId('customer-products').replaceChildren(node('div', 'empty-state', `No se pudo cargar el catálogo (${paths[0][0]}): ${results[0].reason.message}`));
  }
  const orders = values[1]?.orders || [];
  const payments = values[2]?.payments || [];
  const ratings = values[3]?.ratings || [];
  if (values[1]) renderOrders(orders, payments, ratings);
  else byId('customer-orders').replaceChildren(node('div', 'empty-state', `No se pudieron cargar los pedidos (${paths[1][0]}): ${results[1].reason.message}`));
  if (values[2]) renderPayments(payments);
  else byId('customer-payments').replaceChildren(node('div', 'empty-state', `No se pudieron cargar los pagos (${paths[2][0]}): ${results[2].reason.message}`));
  if (values[4]) {
    renderCustomerReports(values[4].reports||[]);
    const select=byId('customer-report-order'), old=select.value;
    select.replaceChildren(new Option('Sin pedido relacionado',''));
    orders.forEach(order=>select.append(new Option(`Pedido ${order.id.slice(0,8)} · ${order.status}`,order.id)));
    if(old&&orders.some(order=>order.id===old))select.value=old;
  } else byId('customer-reports').replaceChildren(node('div','empty-state',`No se pudieron cargar tus reportes: ${results[4].reason.message}`));
  if (!values[3]) showCustomerError(`Falló ${paths[3][0]}: ${results[3].reason.message}`);
  else if (!values[0] || !values[1] || !values[2] || !values[4]) {
    const failures = results.map((result, index) => result.status === 'rejected' ? `${paths[index][1]} (${paths[index][0]}): ${result.reason.message}` : null).filter(Boolean);
    showCustomerError(`Algunas secciones no están disponibles: ${failures.join('; ')}`);
  }
  renderCart();
}
function renderProducts(products) {
  const target = byId('customer-products');
  target.replaceChildren();
  if (!products.length) {
    target.append(node('div', 'empty-state', 'Aún no hay productos publicados en el catálogo.'));
    return;
  }
  products.forEach(product => {
    const card = node('article', 'product-card');
    if (product.image_url) {
      const image = document.createElement('img');
      image.className = 'product-image';
      image.src = product.image_url;
      image.alt = product.name;
      image.loading = 'lazy';
      card.append(image);
    } else card.append(node('div', 'product-placeholder', 'ArepaGo'));
    const body = node('div', 'product-body');
    body.append(node('h3', '', product.name), node('div', 'product-description', product.description || ''));
    const footer = node('div', 'product-footer');
    footer.append(node('b', 'product-price', money(product.price)));
    const add = node('button', 'small-action', 'Agregar');
    add.type = 'button';
    add.dataset.addProduct = product.id;
    footer.append(add);
    body.append(footer);
    card.append(body);
    target.append(card);
  });
}
function renderCart() {
  const target = byId('cart-items');
  target.replaceChildren();
  const entries = [...customerCart.entries()];
  if (!entries.length) target.append(node('div', 'empty-state', 'Agrega productos del catálogo.'));
  let count = 0;
  let total = 0;
  entries.forEach(([id, item]) => {
    count += item.quantity;
    total += Number(item.product.price) * item.quantity;
    const row = node('div', 'cart-row');
    const label = node('span', '', `${item.quantity} × ${item.product.name}`);
    const remove = node('button', '', 'Quitar');
    remove.type = 'button';
    remove.dataset.removeProduct = id;
    row.append(label, remove);
    target.append(row);
  });
  byId('cart-count').textContent = `${count} ${count === 1 ? 'producto' : 'productos'}`;
  byId('cart-total').textContent = money(total);
  byId('checkout-button').disabled = count === 0;
}
async function checkout() {
  const button = byId('checkout-button');
  const deliveryAddress = byId('delivery-address').value.trim();
  if (deliveryAddress.length < 10 || deliveryAddress.length > 400) {
    showCustomerError('Escribe una dirección de entrega de entre 10 y 400 caracteres.');
    return;
  }
  button.disabled = true;
  byId('customer-error').hidden = true;
  try {
    const items = [...customerCart.entries()].map(([product_id, item]) => ({product_id, quantity: item.quantity}));
    checkoutIdempotencyKey ||= (window.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`);
    const result = await api('/api/pedidos/orders', {
      method: 'POST', headers: {'Content-Type': 'application/json', 'Idempotency-Key': checkoutIdempotencyKey},
      body: JSON.stringify({items, payment_method: 'cash_on_delivery', delivery_address: deliveryAddress}),
    });
    checkoutIdempotencyKey = null;
    customerCart.clear();
    await loadCustomerDashboard();
    byId('customer-error').textContent = `Pedido creado. Pago pendiente en efectivo contra entrega (${result.order.id}).`;
    byId('customer-error').hidden = false;
  } catch (error) {
    showCustomerError(error.message);
  } finally {
    button.disabled = customerCart.size === 0;
  }
}
const paymentStatusLabels = {pending: 'Pendiente', paid: 'Pagado', cancelled: 'Cancelado', failed: 'Fallido'};
const paymentStatusLabel = status => paymentStatusLabels[status] || status || 'Sin registro';
function orderPaymentLabel(payment, orderStatus) {
  if (!payment) return 'Sin registro';
  if (payment.status !== 'pending') return paymentStatusLabel(payment.status);
  if (orderStatus === 'cancelled') return 'Pedido cancelado';
  return orderStatus === 'delivered' ? 'Pendiente · confirma en Pagos' : 'Pendiente · se confirma al entregar';
}
function customerPaymentLabel(payment, orderStatus) {
  if (!payment) return 'sin registro';
  if (payment.status !== 'pending') return paymentStatusLabel(payment.status);
  if (orderStatus === 'cancelled') return 'pedido cancelado';
  return orderStatus === 'delivered' ? 'pendiente por confirmar' : 'pendiente al recibir';
}
function renderOrders(orders, payments, ratings) {
  const target = byId('customer-orders');
  target.replaceChildren();
  if (!orders.length) { target.append(node('div', 'empty-state', 'Aún no tienes pedidos.')); return; }
  const paymentByOrder = new Map(payments.map(payment => [payment.order_id, payment]));
  const rated = new Set(ratings.map(rating => rating.order_id));
  const statusLabels = {placed: 'Recibido', confirmed: 'Confirmado', preparing: 'En preparación', out_for_delivery: 'En camino', delivered: 'Entregado', cancelled: 'Cancelado'};
  orders.forEach(order => {
    const card = node('article', 'customer-order');
    const head = node('div', 'customer-order-head');
    head.append(node('span', 'customer-order-title', `Pedido ${order.id.slice(0, 8)}`), node('span', 'order-status', statusLabels[order.status] || order.status));
    card.append(head);
    const payment = paymentByOrder.get(order.id);
    const meta = node('div', 'order-meta');
    meta.append(node('span', '', `Total ${money(order.total)}`), node('span', '', `ETA ${prettyTime(order.eta)}`), node('span', '', `Entrega: ${order.delivery_address}`), node('span', '', `Pago: ${customerPaymentLabel(payment, order.status)}`));
    card.append(meta);
    const itemList = node('div', 'order-items');
    (order.items || []).forEach(item => itemList.append(node('div', 'order-item-row', `${item.quantity} × ${item.name} · ${money(Number(item.unit_price) * item.quantity)}`)));
    card.append(itemList);
    const actions = node('div', 'order-actions');
    if (order.status === 'placed') {
      const cancel = node('button', 'small-action', 'Cancelar pedido');
      cancel.type = 'button'; cancel.dataset.cancelOrder = order.id; actions.append(cancel);
    }
    if (order.status === 'delivered' && !rated.has(order.id)) {
      const form = node('form', 'rating-form'); form.dataset.ratingForm = order.id;
      const score = document.createElement('select'); score.name = 'score'; score.required = true;
      for (let value = 5; value >= 1; value--) { const option = document.createElement('option'); option.value = value; option.textContent = `${value} ★`; score.append(option); }
      const comment = document.createElement('input'); comment.name = 'comment'; comment.maxLength = 1000; comment.placeholder = 'Comentario opcional';
      const submit = node('button', 'small-action', 'Calificar'); submit.type = 'submit';
      form.append(score, comment, submit); actions.append(form);
    }
    if (actions.childElementCount) card.append(actions);
    target.append(card);
  });
}
function renderPayments(payments) {
  const target = byId('customer-payments');
  target.replaceChildren();
  if (!payments.length) { target.append(node('div', 'empty-state', 'Aún no hay pagos asociados a tus pedidos.')); return; }
  payments.forEach(payment => {
    const row = node('div', 'customer-payment');
    row.append(node('span', '', `Pedido ${payment.order_id.slice(0, 8)} · efectivo contra entrega`), node('b', '', `${paymentStatusLabel(payment.status)} · ${money(payment.amount)}`));
    target.append(row);
  });
}
function reportCategoryName(category) { return category==='web_issue'?'Problema de la web':'Reporte sobre la tienda'; }
function reportStatusName(status) { return ({new:'Nuevo',in_review:'En revisión',resolved:'Resuelto',closed:'Cerrado'})[status]||status; }
function renderCustomerReports(reports) {
  const target=byId('customer-reports');target.replaceChildren();
  if(!reports.length){target.append(node('div','empty-state','Aún no has enviado reportes.'));return;}
  reports.forEach(report=>{
    const card=node('article','customer-report-card');
    card.append(node('div','customer-report-title',`${reportCategoryName(report.category)} · ${reportStatusName(report.status)}`),node('b','',report.subject),node('p','',report.message));
    if(report.order_id)card.append(node('small','','Pedido relacionado '+report.order_id.slice(0,8)));
    if(report.admin_response)card.append(node('div','customer-report-response',`Respuesta de ArepaGo: ${report.admin_response}`));
    card.append(node('small','',`Enviado ${prettyTime(report.created_at)}`));target.append(card);
  });
}

function showAdminError(message) {
  const box = byId('global-error');
  box.textContent = `No se pudo completar la operación: ${message}`;
  box.hidden = false;
}
function productFormFields() { return byId('admin-product-form').elements; }
function fillProductForm(product) {
  editingProductId = product.id;
  const fields = productFormFields();
  fields.namedItem('name').value = product.name;
  fields.namedItem('price').value = product.price;
  fields.namedItem('image_url').value = product.image_url || '';
  fields.namedItem('description').value = product.description || '';
  byId('product-form-title').textContent = `Editar ${product.name}`;
  byId('product-form-cancel').hidden = false;
  window.scrollTo({top: 0, behavior: 'smooth'});
}
function resetProductForm() {
  editingProductId = null;
  byId('admin-product-form').reset();
  byId('product-form-title').textContent = 'Nuevo producto';
  byId('product-form-cancel').hidden = true;
  byId('product-form-error').hidden = true;
}
function renderAdminProducts(products) {
  adminProducts = new Map(products.map(product => [product.id, product]));
  const target = byId('admin-products'); target.replaceChildren();
  if (!products.length) { target.append(node('div', 'empty-state', 'No hay productos. Crea el primero usando el formulario.')); return; }
  products.forEach(product => {
    const card = node('article', `product-card${product.active ? '' : ' product-inactive'}`);
    card.append(node('div', 'product-placeholder', product.active ? 'Publicado' : 'Retirado'));
    const body = node('div', 'product-body');
    body.append(node('h3', '', product.name), node('div', 'product-description', product.description || 'Sin descripción'), node('b', 'product-price', money(product.price)));
    const actions = node('div', 'order-actions');
    const edit = node('button', 'small-action', 'Editar'); edit.type = 'button'; edit.dataset.editProduct = product.id;
    const toggle = node('button', `small-action${product.active ? ' danger-action' : ''}`, product.active ? 'Retirar' : 'Publicar');
    toggle.type = 'button'; toggle.dataset.toggleProduct = product.id;
    actions.append(edit, toggle); body.append(actions); card.append(body); target.append(card);
  });
}
function renderAdminOrders(orders, payments = []) {
  const target = byId('admin-orders'); target.replaceChildren();
  if (!orders.length) { target.append(node('div', 'empty-state', 'No hay pedidos registrados.')); return; }
  const paymentByOrder = new Map(payments.map(payment => [payment.order_id, payment]));
  const labels = {placed:'Recibido',confirmed:'Confirmado',preparing:'En preparación',out_for_delivery:'En camino',delivered:'Entregado',cancelled:'Cancelado'};
  const transitions = {placed:['confirmed','cancelled'],confirmed:['preparing','cancelled'],preparing:['out_for_delivery','cancelled'],out_for_delivery:['delivered']};
  orders.forEach(order => {
    const card = node('article', 'panel admin-record');
    const head = node('div','panel-heading');
    const info = node('div'); info.append(node('h2','',`Pedido ${order.id.slice(0,8)} · ${labels[order.status] || order.status}`),node('p','',`${prettyTime(order.created_at)} · ${money(order.total)} · entrega: ${order.delivery_address}`));
    head.append(info);
    const statuses = transitions[order.status] || [];
    if (statuses.length) {
      const select = document.createElement('select'); select.dataset.orderStatus = order.id;
      const placeholder = document.createElement('option'); placeholder.value = ''; placeholder.textContent = 'Cambiar estado…'; placeholder.selected = true; select.append(placeholder);
      statuses.forEach(status => { const option = document.createElement('option'); option.value = status; option.textContent = labels[status]; select.append(option); });
      head.append(select);
    }
    card.append(head, node('p','admin-record-meta',`Cliente ${order.customer_id || '—'} · ETA ${prettyTime(order.eta)}`), node('p','admin-record-meta',`Pago: ${orderPaymentLabel(paymentByOrder.get(order.id), order.status)}`));
    (order.items || []).forEach(item => card.append(node('div','order-item-row',`${item.quantity} × ${item.name} · ${money(Number(item.unit_price)*item.quantity)}`)));
    target.append(card);
  });
}
function renderAdminPayments(payments, orders) {
  const delivered = new Set(orders.filter(order => order.status === 'delivered').map(order => order.id));
  const target = byId('admin-payments'); target.replaceChildren();
  if (!payments.length) { const row = document.createElement('tr'); row.append(node('td','','No hay pagos registrados')); target.append(row); return; }
  payments.forEach(payment => {
    const row = document.createElement('tr');
    row.append(node('td','',payment.order_id.slice(0,8)),node('td','','Efectivo contra entrega'),node('td','',paymentStatusLabel(payment.status)),node('td','',money(payment.amount)),node('td','',prettyTime(payment.created_at)));
    const action = document.createElement('td');
    if (payment.status === 'pending' && delivered.has(payment.order_id)) { const button = node('button','small-action','Confirmar efectivo recibido'); button.type = 'button'; button.dataset.confirmPayment = payment.id; action.append(button); }
    else action.textContent = payment.status === 'pending' ? 'Disponible tras la entrega' : (payment.paid_at ? `Confirmado ${prettyTime(payment.paid_at)}` : '—');
    row.append(action); target.append(row);
  });
}
function renderAdminRatings(ratings) {
  const target = byId('admin-ratings'); target.replaceChildren();
  if (!ratings.length) { target.append(node('div','empty-state','Aún no hay calificaciones de pedidos entregados.')); return; }
  ratings.forEach(rating => {
    const card = node('article','panel admin-record');
    card.append(node('h2','',`${rating.score} de 5 estrellas · Pedido ${rating.order_id.slice(0,8)}`),node('p','admin-record-meta',prettyTime(rating.created_at)),node('p','',rating.comment || 'Sin comentario'));
    target.append(card);
  });
}
function renderBusinessSummary({orders,payments,ratings,catalog,failures}) {
  const ordersList = orders?.orders || [];
  const paymentsList = payments?.payments || [];
  const ratingsList = ratings?.ratings || [];
  const openOrders = ordersList.filter(order => !['delivered','cancelled'].includes(order.status)).length;
  const pendingPayments = paymentsList.filter(payment => payment.status === 'pending').length;
  const confirmedAmount = paymentsList.filter(payment => payment.status === 'paid').reduce((total,payment) => total + Number(payment.amount || 0),0);
  const average = ratingsList.length ? ratingsList.reduce((total,rating) => total + Number(rating.score || 0),0) / ratingsList.length : null;
  const cards = [
    ['Pedidos activos',orders ? openOrders.toLocaleString('es-CO') : 'No disponible','De los últimos 500 pedidos'],
    ['Pagos pendientes',payments ? pendingPayments.toLocaleString('es-CO') : 'No disponible','De los últimos 500 pagos'],
    ['Efectivo confirmado',payments ? money(confirmedAmount) : 'No disponible','Pagos confirmados entre los últimos 500'],
    ['Productos publicados',catalog ? Number(catalog.total ?? catalog.products?.length ?? 0).toLocaleString('es-CO') : 'No disponible','Total informado por Catálogo'],
    ['Calificación reciente',ratings ? (average == null ? 'Sin calificaciones' : `${average.toLocaleString('es-CO',{maximumFractionDigits:2})} / 5`) : 'No disponible',`${ratingsList.length} calificaciones consultadas`],
  ];
  const metrics = byId('business-metrics'); metrics.replaceChildren();
  cards.forEach(([label,value,foot]) => { const card=node('article','metric-card business-metric-card'); card.append(node('div','metric-label',label),node('div','metric-value',value),node('div','metric-foot',foot)); metrics.append(card); });
  const errorBox = byId('business-summary-error');
  errorBox.textContent = failures.length ? `Módulos con errores: ${failures.join('; ')}` : '';
  errorBox.hidden = failures.length === 0;
  byId('business-orders-coverage').textContent = orders ? `Consultando ${ordersList.length} registros más recientes (máximo 500).` : 'El módulo de Pedidos no está disponible.';
  renderBusinessOrders(ordersList.slice(0,5));
}
function renderBusinessOrders(orders) {
  const target = byId('business-orders'); target.replaceChildren();
  if (!orders.length) { target.append(node('div','empty-state','No hay pedidos recientes para mostrar.')); return; }
  const labels = {placed:'Recibido',confirmed:'Confirmado',preparing:'En preparación',out_for_delivery:'En camino',delivered:'Entregado',cancelled:'Cancelado'};
  orders.forEach(order => {
    const row=node('div','business-order-row');
    const detail=node('div','business-order-detail');
    detail.append(node('b','',`Pedido ${String(order.id).slice(0,8)}`),node('span','',`${prettyTime(order.created_at)} · ${Number(order.items?.length || 0)} productos`));
    row.append(detail,node('span','order-status',labels[order.status] || order.status),node('b','',money(order.total)));
    target.append(row);
  });
  const action=node('button','small-action','Ver todos los pedidos'); action.type='button'; action.dataset.openOrders='true';
  target.append(action);
}
async function loadAdminSummary() {
  if (!adminSessionReady) return;
  const paths = [
    ['/api/pedidos/orders','Pedidos'],['/api/pagos/payments','Pagos'],
    ['/api/calificaciones/ratings','Calificaciones'],['/api/catalogo/products?limit=100','Catálogo'],
  ];
  const results = await Promise.allSettled(paths.map(([path]) => api(path)));
  if (results.some(result => result.status === 'rejected' && result.reason?.status === 401)) {
    if (refreshTimer) window.clearInterval(refreshTimer);
    showLogin('La sesión expiró. Inicia sesión de nuevo.');
    return;
  }
  const values = results.map(result => result.status === 'fulfilled' ? result.value : null);
  const failures = results.map((result,index) => result.status === 'rejected' ? `${paths[index][1]}: ${result.reason.message}` : null).filter(Boolean);
  renderBusinessSummary({orders:values[0],payments:values[1],ratings:values[2],catalog:values[3],failures});
}
async function generateRealOrdersReport() {
  const button=byId('generate-orders-report'),target=byId('orders-report-result');
  button.disabled=true;target.replaceChildren(node('div','loading-row','Consultando Pedidos y el planificador de PostgreSQL…'));
  try {
    const report=await api('/api/incidents/actions/orders-report',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({days:30})});
    renderRealOrdersReport(report);
  } catch(error) {target.replaceChildren(node('div','error-banner',`No se pudo generar el reporte real: ${error.message}`));}
  finally {button.disabled=false;}
}
function renderRealOrdersReport(report) {
  const target=byId('orders-report-result'),indexButton=byId('apply-orders-index');target.replaceChildren();
  indexButton.hidden=!report.index_recommended;
  const card=node('div','orders-report-summary');
  card.append(node('div','simulation-notice',`${report.source} · ${report.query} · Últimos ${report.period_days} días. Datos consultados en vivo.`));
  const statusText={completed:'El agregado real se ejecutó dentro del límite de seguridad.',skipped_safety_limit:'Se obtuvo el plan real, pero no se ejecutó el agregado porque podría leer demasiadas filas.',stopped_by_statement_timeout:'PostgreSQL detuvo la consulta al superar 5 segundos.',unavailable:report.error||'La tabla no está disponible.'};
  card.append(node('p','simulator-mechanism',statusText[report.status]||`Estado: ${report.status}.`));
  if(report.plan){
    const facts=node('div','simulator-summary-grid');
    [['Filas estimadas',Number(report.estimated_live_rows||0).toLocaleString('es-CO')],['Filas muertas estimadas',Number(report.estimated_dead_rows||0).toLocaleString('es-CO')],['Plan',report.plan.nodes.join(' → ')],['Tiempo real de consulta',report.actual_execution_ms==null?'No ejecutada':`${report.actual_execution_ms} ms`],['Índice por fecha',report.created_at_index_present?'Presente':'No encontrado']].forEach(([label,value])=>{const item=node('div','simulator-stat');item.append(node('span','',label),node('b','',value));facts.append(item);});
    card.append(facts);
  }
  if(report.orders_by_status){
    const table=document.createElement('table');table.innerHTML='<thead><tr><th>Estado</th><th>Pedidos</th><th>Valor total de pedidos (COP)</th></tr></thead><tbody></tbody>';
    const statusNames={placed:'Recibido',confirmed:'Confirmado',preparing:'En preparación',out_for_delivery:'En camino',delivered:'Entregado',cancelled:'Cancelado'};
    const body=table.querySelector('tbody');
    report.orders_by_status.forEach(row=>{const tr=document.createElement('tr');tr.append(node('td','service-cell',statusNames[row.status]||row.status),node('td','',Number(row.orders).toLocaleString('es-CO')),node('td','',`$ ${Number(row.gross_total).toLocaleString('es-CO',{minimumFractionDigits:2,maximumFractionDigits:2})} COP`));body.append(tr);});
    if(!report.orders_by_status.length){const tr=document.createElement('tr');tr.append(node('td','empty-state','No hay pedidos en el periodo seleccionado.'));body.append(tr);}
    const scroll=node('div','table-scroll');scroll.append(table);card.append(scroll);
  }
  if(report.recommendations?.length){
    const diagnosis=node('div','simulator-assumptions');diagnosis.append(node('b','','Diagnóstico basado en esta base de datos'));
    const list=document.createElement('ul');report.recommendations.forEach(item=>list.append(node('li','',item)));
    if(report.last_analyze)list.append(node('li','',`Último ANALYZE: ${prettyTime(report.last_analyze)}.`));
    if(report.last_autoanalyze)list.append(node('li','',`Último autoanalyze: ${prettyTime(report.last_autoanalyze)}.`));
    diagnosis.append(list);
    if(report.safety)diagnosis.append(node('p','simulator-mechanism',`Protección aplicada: como máximo ${Number(report.safety.max_estimated_scan_rows).toLocaleString('es-CO')} filas estimadas, timeout ${report.safety.statement_timeout}. El reporte no modifica datos.`));
    card.append(diagnosis);
  }
  target.append(card);
}
async function loadSimulatorScenario() {
  const target = byId('simulator-baseline');
  byId('simulator-error').hidden = true;
  try {
    const scenario = await api('/api/incidents/simulator/reference');
    byId('simulator-source').textContent = `${scenario.source_classification}. No se mezcla con la telemetría ni con el historial de incidentes reales.`;
    target.replaceChildren();
    const summary = node('div','simulator-summary-grid');
    [
      ['Ventana',`${scenario.time_window.start}–${scenario.time_window.end} · ${scenario.time_window.duration_minutes} min`],
      ['Reporte',`${Number(scenario.report.orders_examined).toLocaleString('es-CO')} pedidos`],
      ['Plan observado',scenario.report.query_plan],
      ['Pools saturados',`Pedidos ${scenario.connection_pools.pedidos.configured}/${scenario.connection_pools.pedidos.configured} · Pagos ${scenario.connection_pools.pagos.configured}/${scenario.connection_pools.pagos.configured}`],
    ].forEach(([label,value])=>{const card=node('div','simulator-stat');card.append(node('span','',label),node('b','',value));summary.append(card);});
    target.append(summary);
    target.append(node('div','simulation-notice',`${scenario.cause_summary} Operaciones: ${scenario.failed_operations}`));
    const table=document.createElement('table');
    table.innerHTML='<thead><tr><th>Módulo</th><th>Operaciones fallidas de referencia</th><th>Impacto indicado</th></tr></thead><tbody></tbody>';
    const body=table.querySelector('tbody');
    Object.entries(scenario.modules).forEach(([service,detail])=>{
      const row=document.createElement('tr');
      row.append(node('td','',names[service]||service),node('td','',`${detail.failure_rate_percent}%`),node('td','',detail.impact==='affected'?'Afectado':'Sin afectación indicada'));
      body.append(row);
    });
    const scroll=node('div','table-scroll');scroll.append(table);target.append(scroll);
  } catch (error) {
    if (error.status===401) { showLogin('La sesión expiró. Inicia sesión de nuevo.'); return; }
    byId('simulator-error').textContent=`No se pudo cargar el escenario: ${error.message}`;
    byId('simulator-error').hidden=false;
  }
}
async function loadOperationalDiagnosis() {
  const target=byId('operational-diagnosis');
  target.replaceChildren(node('div','loading-row','Consultando métricas y salud actuales…'));
  try {
    const [view,health]=await Promise.all([api('/api/views/services'),api('/health/detailed')]);
    target.replaceChildren();
    (view.services||[]).forEach(service=>{
      const metric=service.latest_measurement||{};
      const dependency=health.dependencies?.[service.service]||health.dependencies?.[`api/${service.service}`]||{};
      const livePool=dependency.database_pool;
      const card=node('article','diagnosis-service');
      card.append(node('div','diagnosis-heading',`${names[service.service]||service.service} · ${serviceStatus(service.status)}`));
      const details=node('div','diagnosis-signals');
      details.append(node('span','',`Última muestra: ${metric.timestamp?prettyTime(metric.timestamp):'sin datos'}`),node('span','',`p95 ${metric.latency_p95_ms??'—'} ms`),node('span','',`HTTP 5xx ${metric.error_count??0}/${metric.request_count??0}`),node('span','',`Pool en vivo ${livePool?.active==null?'—':`${livePool.active}/${livePool.max}`}`),node('span','',`Pool muestra ${metric.db_pool_active==null?'—':`${metric.db_pool_active}/${metric.db_pool_max}`}`),node('span','',`Health ${dependency.status||'sin respuesta'}`));
      card.append(details);
      const failures=(metric.diagnostics||[]).filter(route=>route.errors>0);
      if(failures.length){const list=document.createElement('ul');failures.forEach(route=>list.append(node('li','',`${route.method} ${route.route}: ${route.errors}/${route.requests} errores; estados ${Object.entries(route.statuses||{}).filter(([status])=>Number(status)>=500).map(([status,count])=>`${status}×${count}`).join(', ')}`)));card.append(node('b','diagnosis-cause','Origen observado'),list);}
      else card.append(node('p','simulator-assumptions',metric.request_count? 'Sin fallos HTTP 5xx registrados en la última muestra.':'No hay tráfico medido todavía; ejecuta la operación y actualiza diagnóstico.'));
      const plan=[];
      if(service.db_pool_ratio!=null&&service.db_pool_ratio>=.75)plan.push('Pool con ocupación crítica: identifica la consulta o carga concurrente antes de ampliar conexiones; valida el máximo agregado de todas las réplicas.');
      if(service.error_rate>=.2)plan.push('Tasa de error elevada: prioriza las rutas anteriores y consulta logs JSON del servicio por ruta y hora para identificar la causa interna.');
      if(metric.latency_p95_ms>=1000)plan.push('Latencia p95 alta: consulta eventos slow_query del módulo y valida el plan SQL real antes de crear un índice.');
      if(plan.length){const list=document.createElement('ul');plan.forEach(item=>list.append(node('li','',item)));card.append(node('b','diagnosis-cause','Plan de acción'),list);}
      target.append(card);
    });
    if(!(view.services||[]).length)target.append(node('div','empty-state','No hay módulos en la vista de servicios.'));
  } catch(error) {target.replaceChildren(node('div','error-banner',`No se pudo obtener diagnóstico en vivo: ${error.message}`));}
}
function renderSimulatorResults(result) {
  const target=byId('simulator-results');target.replaceChildren();
  const notice=node('div','simulation-notice',result.notice);target.append(notice);
  if(result.actual_measurements){
    const measured=result.actual_measurements;
    const card=node('article','panel simulator-result');
    const indexWasRun=Boolean(measured.after_index);
    card.append(node('h2','','Qué midió PostgreSQL en esta ejecución'),node('p','simulator-mechanism',`Se generaron ${Number(measured.rows_generated).toLocaleString('es-CO')} filas de prueba en una tabla temporal${measured.storage?` (${measured.storage})`:''}. La tabla se borra al terminar. Estas cifras describen esta prueba local, no la tienda ni el incidente histórico.`));
    const table=document.createElement('table');table.innerHTML=`<thead><tr><th>Medición</th><th>Sin índice</th>${indexWasRun?'<th>Con índice temporal</th>':''}</tr></thead><tbody></tbody>`;
    const rows=[['Plan de consulta',measured.before_index.plan_nodes.join(' → '),...(indexWasRun?[measured.after_index.plan_nodes.join(' → ')]:[])],['Tiempo de ejecución SQL',`${measured.before_index.execution_time_ms} ms`,...(indexWasRun?[`${measured.after_index.execution_time_ms} ms`]:[])],['Bloques leídos',measured.before_index.shared_read_blocks,...(indexWasRun?[measured.after_index.shared_read_blocks]:[])]];
    if(indexWasRun)rows.push(['Cambio de tiempo medido','Base de comparación',measured.execution_time_change_percent==null?'No calculado':`${measured.execution_time_change_percent}%`]);
    const body=table.querySelector('tbody');rows.forEach(values=>{const row=document.createElement('tr');values.forEach((value,index)=>row.append(node('td',index?'':'service-cell',String(value))));body.append(row);});
    const scroll=node('div','table-scroll');scroll.append(table);card.append(scroll,node('p','simulator-assumptions',`La comparación del índice ${indexWasRun?'sí se ejecutó sobre la tabla temporal':'no se ejecutó porque no seleccionaste esa opción'}. No cambia ni mide los pools de Pedidos/Pagos. El cambio de horario y la réplica son explicaciones del escenario: no hay scheduler ni réplica configurados.`));target.append(card);
  }
  (result.mitigations||[]).forEach(measure=>{
    const card=node('article','panel simulator-result');
    const head=node('div','panel-heading');
    const title=node('h2','',measure.name);const badge=node('span','simulation-badge',measure.id==='index'?'MEDIDO EN TABLA TEMPORAL':'ANÁLISIS, NO EJECUTADO');head.replaceChildren(title,badge);card.append(head);
    const descriptions={
      reschedule:'En el escenario, el reporte dejaría de coincidir con el periodo crítico. Esta ejecución no cambia el horario: el sistema no tiene un programador de tareas. El reporte seguiría usando Sequential Scan.',
      index:result.actual_measurements?.after_index?'Se creó un índice únicamente en la tabla temporal y se compararon el plan y el tiempo arriba. No se tocó soc_orders.orders.':'No se probó el índice en esta ejecución. Marca “Comparar con un índice” para medirlo en la tabla temporal; nunca se aplica a los pedidos reales.',
      read_replica:'Una réplica separaría la carga de reportes de la base principal. En este entorno no hay réplica configurada, así que no se creó ni recibió consultas.'
    };
    card.append(node('p','simulator-mechanism',descriptions[measure.id]||measure.mechanism));
    const assumptions=measure.assumptions||[];
    if(assumptions.length){const note=node('div','simulator-assumptions');note.append(node('b','','Qué falta para aplicarla al servicio'));const list=document.createElement('ul');assumptions.forEach(value=>list.append(node('li','',value)));note.append(list);card.append(note);}
    target.append(card);
  });
}
function renderReports(reports) {
  const target=byId('admin-reports');target.replaceChildren();
  if(!reports.length){target.append(node('div','empty-state','No hay reportes con esos filtros.'));return;}
  reports.forEach(report=>{
    const card=node('article','panel store-report-card');
    const header=node('div','panel-heading');
    header.append(node('div','',`${reportCategoryName(report.category)} · ${reportStatusName(report.status)} · ${prettyTime(report.created_at)}`));
    if(report.order_id)header.append(node('span','',`Pedido ${report.order_id.slice(0,8)}`));
    card.append(header,node('h2','',report.subject),node('p','store-report-message',report.message),node('small','',`Cliente ${report.customer_id}`));
    const form=node('form','store-report-update');form.dataset.adminReport=report.id;
    const status=document.createElement('select');status.name='status';
    ['new','in_review','resolved','closed'].forEach(value=>{const option=new Option(reportStatusName(value),value);option.selected=report.status===value;status.append(option);});
    const statusLabel=node('label','','Estado de seguimiento');statusLabel.append(status);
    const response=document.createElement('textarea');response.name='admin_response';response.maxLength=4000;response.value=report.admin_response||'';response.placeholder='Respuesta para el cliente. Obligatoria para resolver o cerrar.';
    const responseLabel=node('label','','Respuesta al cliente');responseLabel.append(response);
    const save=node('button','small-action','Guardar seguimiento');save.type='submit';form.append(statusLabel,responseLabel,save);card.append(form);
    if(report.history?.length){const history=node('details','report-history');history.append(node('summary','','Historial de seguimiento'));report.history.forEach(item=>history.append(node('p','',`${reportStatusName(item.new_status)} · ${prettyTime(item.created_at)}${item.response?' · '+item.response:''}`)));card.append(history);}
    target.append(card);
  });
}
async function loadAdminView(view) {
  if (!['orders','payments','catalog','ratings','reports'].includes(view)) return;
  byId('global-error').hidden = true;
  try {
    if (view === 'orders') {
      const orders = (await api('/api/pedidos/orders')).orders || [];
      let payments = [];
      try { payments = (await api('/api/pagos/payments')).payments || []; }
      catch (_) { /* Pedidos sigue disponible aunque Pagos falle. */ }
      renderAdminOrders(orders, payments);
    }
    if (view === 'payments') {
      const [payments, orders] = await Promise.all([api('/api/pagos/payments'),api('/api/pedidos/orders')]);
      renderAdminPayments(payments.payments || [],orders.orders || []);
    }
    if (view === 'catalog') renderAdminProducts((await api('/api/catalogo/products?limit=100&include_inactive=true')).products || []);
    if (view === 'ratings') renderAdminRatings((await api('/api/calificaciones/ratings')).ratings || []);
    if (view === 'reports') {
      const query=new URLSearchParams({status:byId('report-status-filter').value,category:byId('report-category-filter').value});
      renderReports((await api(`/api/reports?${query}`)).reports||[]);
    }
  } catch (error) {
    if (error.status === 401) { showLogin('La sesión expiró. Inicia sesión de nuevo.'); return; }
    showAdminError(error.message);
  }
}
