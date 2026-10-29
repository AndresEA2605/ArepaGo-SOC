const names = {pedidos: 'Pedidos', pagos: 'Pagos', calificaciones: 'Calificaciones', catalogo: 'Catálogo'};
const api = async path => {
  const response = await fetch(path, {headers: {Accept: 'application/json'}});
  if (!response.ok) throw new Error(`API ${response.status} en ${path}`);
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
  const title = {overview: 'Resumen', services: 'Servicios', incidents: 'Incidentes'}[view];
  byId('breadcrumb-title').textContent = title;
  if (location.hash !== `#${view}`) history.replaceState(null, '', `#${view}`);
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
function serviceStatus(value) {
  return ({critical: 'Crítico', warning: 'Advertencia', ok: 'Normal', no_data: 'Sin datos'})[value] || value || 'Sin datos';
}
function renderIncidents(incidents, target, compact = false) {
  target.replaceChildren();
  if (!incidents.length) {
    target.append(node('div', 'empty-state', 'No hay incidentes para este filtro.'));
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
      target.append(row);
    }
  });
}
function severityName(value) { return ({critical: 'Crítico', warning: 'Advertencia', resolved: 'Resuelto'})[value] || value || '—'; }
function reasonName(reason) {
  const labels = {latency_p95_ms: 'Latencia p95', error_rate: 'Tasa de errores', db_pool_ratio: 'Pool DB'};
  const value = reason.signal === 'error_rate' || reason.signal === 'db_pool_ratio' ? percent(reason.value) : latency(reason.value);
  return `${labels[reason.signal] || reason.signal}: ${value}`;
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
    const [health, overview, incidentData, measurements] = await Promise.all([
      api('/health'), api('/api/views/overview'), api('/api/views/incidents?status=open'), api('/api/telemetry?limit=200')
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
    renderIncidents((await api(`/api/views/incidents?status=${encodeURIComponent(byId('incident-filter').value)}`)).incidents || [], byId('incident-rows'));
    renderServices(services, byId('services-detail'));
    drawChart(measurements.measurements || [], byId('chart-service').value);
    byId('updated-at').textContent = `Actualizado ${new Date().toLocaleTimeString('es-CO', {hour: '2-digit', minute: '2-digit', second: '2-digit'})}`;
  } catch (error) {
    setHealth('offline');
    byId('global-error').textContent = `No se pudieron cargar las vistas: ${error.message}. Verifica que los microservicios estén activos.`;
    byId('global-error').hidden = false;
  } finally {
    button.disabled = false;
  }
}
document.querySelectorAll('.nav-item').forEach(button => button.addEventListener('click', () => setView(button.dataset.view)));
document.querySelectorAll('[data-go]').forEach(button => button.addEventListener('click', () => setView(button.dataset.go)));
byId('refresh-button').addEventListener('click', refresh);
byId('chart-service').addEventListener('change', async () => {
  try { const result = await api('/api/telemetry?limit=200'); drawChart(result.measurements || [], byId('chart-service').value); }
  catch (error) { byId('global-error').textContent = `No se pudo actualizar la gráfica: ${error.message}`; byId('global-error').hidden = false; }
});
byId('incident-filter').addEventListener('change', async () => {
  try { const result = await api(`/api/views/incidents?status=${encodeURIComponent(byId('incident-filter').value)}`); renderIncidents(result.incidents || [], byId('incident-rows')); }
  catch (error) { byId('global-error').textContent = `No se pudieron cargar los incidentes: ${error.message}`; byId('global-error').hidden = false; }
});
window.addEventListener('hashchange', () => {
  const view = location.hash.slice(1);
  if (['overview', 'services', 'incidents'].includes(view)) setView(view);
});
const initialView = location.hash.slice(1);
setView(['overview', 'services', 'incidents'].includes(initialView) ? initialView : 'overview');
refresh();
refreshTimer = window.setInterval(refresh, 15000);
