const $ = selector => document.querySelector(selector);
const pollingJobs = new Set();
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const fmtBytes = n => n > 1024 ** 3 ? `${(n / 1024 ** 3).toFixed(2)} GB` : `${(n / 1024 ** 2).toFixed(1)} MB`;
const PAGE_SIZE = 20;
const listPages = {sources: 1, analyzed: 1, deleted: 1, accepted: 1, processed: 1, rejected: 1};
const listFilters = {sources: '', analyzed: ''};
let visibleSources = [];
const jobProgress = new Map();

function sourcePreview(source) {
  let meta = {};
  try { meta = JSON.parse(source.metadata_json || '{}'); } catch (_) {}
  const thumbnail = source.thumbnail || meta.thumbnails?.slice(-1)[0]?.url;
  const safe = value => /^https?:\/\//i.test(value || '') ? value : '';
  const picture = safe(thumbnail) ? `<img src="${escapeHtml(thumbnail)}" loading="lazy" referrerpolicy="no-referrer" style="width:144px;height:81px;object-fit:cover;border-radius:6px" alt="来源缩略图">` : '';
  return `<a href="${escapeHtml(safe(source.url))}" target="_blank" rel="noopener noreferrer">${picture}<strong>${escapeHtml(source.title || source.url)}</strong></a><small>${escapeHtml(source.uploader || '')}</small>`;
}

function preflightBadge(source) {
  let check = {};
  try { check = JSON.parse(source.metadata_json || '{}').preflight || {}; } catch (_) {}
  return `<small title="${escapeHtml(check.reason || '')}">${escapeHtml({PASS: '规格符合（仍需内容审核）', FAIL: '规格不足', UNKNOWN: '规格未知'}[check.status] || '规格待查')}${check.reason ? '：' + escapeHtml(check.reason) : ''}</small>`;
}

async function cancelDownload(id, button) {
  button.disabled = true;
  try { await api(`/api/jobs/${id}/cancel`, {method: 'POST', body: '{}'}); notice('已请求取消，正在停止任务…'); }
  catch (error) { notice(error.message, true); button.disabled = false; }
}

async function downloadTopThree(button) {
  button.disabled = true;
  const selected = visibleSources.filter(s => !s.proxy_path && !s.active_job_id && !s.analysis_completed && JSON.parse(s.metadata_json || '{}').preflight?.status !== 'FAIL')
    .sort((a,b) => Number(b.source_score || 0) - Number(a.source_score || 0)).slice(0,3);
  let queued = 0;
  const errors = [];
  try {
    for (const source of selected) {
      try { const data = await api(`/api/sources/${source.id}/proxy`, {method:'POST', body:'{}'}); queued++; pollJob(data.job_id, 'proxy'); }
      catch (error) { errors.push(`#${source.id}：${error.message}`); }
    }
    notice(`已排队 ${queued} 个来源，将先预检规格再下载。${errors.join('；')}${selected.length ? '' : '当前页没有可下载来源。'}`);
    await loadSources();
  } finally { button.disabled = false; }
}

async function api(url, options = {}) {
  const response = await fetch(url, {headers: {'Content-Type': 'application/json'}, ...options});
  const payload = await response.json();
  if (!response.ok || payload.ok === false) throw new Error(payload.error || '请求失败');
  return payload;
}

function escapeHtml(value) {
  const element = document.createElement('div');
  element.textContent = value ?? '';
  return element.innerHTML.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function notice(text, error = false) {
  const element = $('#notice');
  element.textContent = text;
  element.className = `notice show${error ? ' error' : ''}`;
}

function humanError(raw = '') {
  if (/sign in to confirm|not a bot/i.test(raw)) return 'YouTube 要求登录确认，无法匿名下载；请换一个公开来源。';
  if (/Requested format is not available/i.test(raw)) return '来源没有可用的代理格式，请换来源或使用公开直链。';
  if (/HTTP Error 403/i.test(raw)) return '来源拒绝访问（HTTP 403），请换公开可下载的来源。';
  if (/HTTP Error 404/i.test(raw)) return '来源地址不存在（HTTP 404）。';
  if (/timed out|timeout/i.test(raw)) return '操作超时，请检查网络后重试。';
  if (/服务重启/.test(raw)) return raw;
  return raw.split('\n').filter(Boolean).slice(-1)[0] || '操作失败，请查看完整错误。';
}

function metric(name, value) { return `<div class="metric"><span>${name}</span><strong>${value}</strong></div>`; }

function renderPagination(selector, key, data) {
  const pageCount = Math.max(1, Math.ceil(Number(data.total || 0) / Number(data.page_size || PAGE_SIZE)));
  listPages[key] = Number(data.page || 1);
  $(selector).innerHTML = `<span>共 ${Number(data.total || 0)} 条 · 第 ${listPages[key]} / ${pageCount} 页</span><button onclick="changePage('${key}',${listPages[key] - 1})" ${listPages[key] <= 1 ? 'disabled' : ''}>上一页</button><button onclick="changePage('${key}',${listPages[key] + 1})" ${listPages[key] >= pageCount ? 'disabled' : ''}>下一页</button>`;
  if (['sources', 'analyzed'].includes(key)) {
    const table = $(selector).previousElementSibling;
    let top = document.getElementById(`${key}-pagination-top`);
    if (!top) {
      top = document.createElement('div');
      top.id = `${key}-pagination-top`;
      top.className = 'pagination pagination-top';
      table.before(top);
    }
    top.innerHTML = $(selector).innerHTML;
    const labels = Array.from(table.querySelectorAll('th'), th => th.textContent);
    table.querySelectorAll('tbody tr').forEach(row => Array.from(row.cells).forEach((cell, i) => { cell.dataset.label = labels[i] || ''; }));
  }
}

function changePage(key, page) {
  listPages[key] = Math.max(1, page);
  return ['sources', 'analyzed', 'deleted'].includes(key) ? loadSources() : loadQueues();
}

async function loadRules() {
  const data = await api('/api/rules');
  const options = ['<option value="">目标单元（可选）</option>', ...Object.entries(data.units).map(([id, unit]) =>
    `<option value="${id}">${id} ${unit.name}${unit.requires_confirmation ? ' ⚠' : ''}</option>`)].join('');
  $('#discover-unit').innerHTML = options;
  $('#import-unit').innerHTML = options;
  const topicParams = new URLSearchParams(location.search);
  if (topicParams.has('topic_query')) {
    $('#discover-form [name="query"]').value = topicParams.get('topic_query');
    const topicUnit = topicParams.get('topic_unit');
    if (Object.hasOwn(data.units, topicUnit)) $('#discover-unit').value = topicUnit;
    notice('已填入题材关键词和目标单元，确认后点击“自动搜索”。');
  }
  const bucketOptions = ['<option value="">全部分类</option>', ...Object.entries(data.buckets).map(([id, bucket]) => `<option value="${id}">${id} ${bucket.name}</option>`), '<option value="unassigned">未分类</option>'].join('');
  $('#sources-bucket-filter').innerHTML = bucketOptions;
  $('#analyzed-bucket-filter').innerHTML = bucketOptions;
}

async function loadDashboard() {
  const {dashboard: data, tools, quota, traffic_warning_bytes: warningBytes} = await api('/api/dashboard');
  $('#metrics').innerHTML = [metric('已发现候选', data.sources), metric('待审核', data.counts.WAITING_REVIEW || 0),
    metric('今日审核', data.reviewed_today), metric('已接受', data.accepted), metric('最终 QA 通过', data.qa_passed),
    metric('预计收入', `¥${data.estimated_income}`), metric('今日下载', fmtBytes(data.traffic.today))].join('');
  $('#tools').innerHTML = Object.entries(tools).map(([name, ok]) => `<span class="tool ${ok ? 'ok' : 'bad'}">${ok ? '●' : '○'} ${name}</span>`).join('');
  if (data.traffic.today > warningBytes) notice(`今日下载已超过软上限：${fmtBytes(data.traffic.today)}。系统仅警告，不会强制停止。`, true);
  $('#quota').innerHTML = quota.map(item => {
    const total = Math.min(100, item.total.actual / Math.max(1, item.total.target) * 100);
    const tiers = ['short', 'medium', 'long'].map(d => `${{short: '短', medium: '中', long: '长'}[d]} ${item.duration[d].actual}/${item.duration[d].recommended}`).join(' · ');
    return `<div class="quota-row"><strong>${item.bucket} ${item.name}</strong><div class="quota-item"><span>已接受 ${item.total.actual}/${item.total.target}</span><div class="progress"><i style="width:${total}%"></i></div></div><div class="quota-item"><span>时长档（实际/建议）</span>${tiers}</div><span class="badge warn">最缺：${item.largest_gap}</span></div>`;
  }).join('');
}

async function loadSources(startPolling = true) {
  const sourceBucket = encodeURIComponent(listFilters.sources);
  const analyzedBucket = encodeURIComponent(listFilters.analyzed);
  const [candidateData, analyzedData, deletedData] = await Promise.all([
    api(`/api/sources?view=candidate&bucket=${sourceBucket}&page=${listPages.sources}&page_size=${PAGE_SIZE}`),
    api(`/api/sources?view=analyzed&bucket=${analyzedBucket}&page=${listPages.analyzed}&page_size=${PAGE_SIZE}`),
    api(`/api/sources?view=deleted&page=${listPages.deleted}&page_size=${PAGE_SIZE}`)
  ]);
  const items = candidateData.items;
  visibleSources = items;
  $('#sources').innerHTML = items.length ? items.map(source => {
    const active = Boolean(source.active_job_id);
    const downloading = source.active_job_kind === 'proxy';
    const analyzing = source.active_job_kind === 'analyze';
    const queued = active && source.active_job_status === 'QUEUED';
    const running = active && source.active_job_status === 'RUNNING';
    const queueName = downloading ? '下载' : analyzing ? '分析' : '规格检查';
    const queueHint = queued ? `<small>${queueName}队列第 ${Number(source.queue_position || 1)} 位，同类前方 ${Number(source.queue_ahead || 0)} 个任务</small>` : '';
    const activity = jobProgress.get(Number(source.active_job_id));
    const activeLabel = running ? (activity?.stage || (downloading ? '正在预检或下载' : analyzing ? '正在分析' : '正在预检')) : queued ? '排队中' : source.status;
    const progressHint = activity?.bytes !== undefined ? `<small>当前文件 ${fmtBytes(activity.bytes)} · ${fmtBytes(activity.speed || 0)}/s</small>` : '';
    const proxyLabel = downloading && queued ? `排队第 ${Number(source.queue_position || 1)} 位` : downloading && running ? '下载中…' : source.proxy_path ? '代理已就绪' : source.status === 'ERROR' ? '重试下载' : '下载代理';
    const analyzeLabel = analyzing && queued ? `排队第 ${Number(source.queue_position || 1)} 位` : analyzing && running ? '分析中…' : '镜头分析';
    const error = source.error ? `<small class="source-error" title="${escapeHtml(source.error)}">${escapeHtml(humanError(source.error))}</small>` : '';
    return `<tr data-source-id="${source.id}"><td>${sourcePreview(source)}</td><td>${source.target_unit || '—'}<small>${escapeHtml(source.search_query || '')}</small></td><td>${source.duration ? `${Number(source.duration).toFixed(1)}s` : '—'}<small>${escapeHtml(source.resolution || '')}</small>${preflightBadge(source)}</td><td>${Number(source.source_score || 0).toFixed(0)}</td><td><span class="badge ${source.status === 'ERROR' ? 'fail' : active ? 'warn' : ''}">${escapeHtml(activeLabel)}</span>${progressHint}${queueHint}${error}</td><td><div class="toolbox"><button class="secondary" onclick="startSourceJob('preflight',${source.id},this)" ${active ? 'disabled' : ''}>检查规格</button><button class="secondary" onclick="proxy(${source.id},this)" ${source.proxy_path || active ? 'disabled' : ''}>${proxyLabel}</button><button onclick="analyze(${source.id},this)" ${!source.proxy_path || active ? 'disabled' : ''}>${analyzeLabel}</button>${active && !analyzing ? `<button class="secondary" onclick="cancelDownload(${source.active_job_id},this)">取消</button>` : ''}</div></td></tr>`;
  }).join('') : '<tr><td colspan="6" class="empty">尚无来源。可自动搜索或导入一个公开 URL。</td></tr>';
  $('#analyzed-sources').innerHTML = analyzedData.items.length ? analyzedData.items.map(source => {
    const error = source.error ? `<small class="source-error" title="${escapeHtml(source.error)}">${escapeHtml(humanError(source.error))}</small>` : '';
    return `<tr data-source-id="${source.id}"><td><strong>${escapeHtml(source.title || source.url)}</strong><small>${escapeHtml(source.uploader || source.url)}</small></td><td>${source.target_unit || '—'}<small>${escapeHtml(source.search_query || '')}</small></td><td>${source.duration ? `${Number(source.duration).toFixed(1)}s` : '—'}<small>${source.resolution || ''}</small></td><td>${Number(source.candidate_count || 0)}</td><td><span class="badge pass">已分析</span>${error}</td><td><button class="secondary" onclick="restoreSource(${source.id},this)">移回候选来源</button></td></tr>`;
  }).join('') : '<tr><td colspan="6" class="empty">暂无已分析来源。</td></tr>';
  renderPagination('#sources-pagination', 'sources', candidateData);
  $('#sources').querySelectorAll('tr[data-source-id]').forEach(row => {
    const source = items.find(s => s.id === Number(row.dataset.sourceId));
    const button = document.createElement('button');
    button.className = 'danger';
    button.textContent = '删除';
    button.disabled = Boolean(source.active_job_id);
    button.title = source.active_job_id ? '请先取消或等待任务完成' : '移入已删除来源，可恢复';
    button.onclick = () => setSourceDeleted(source.id, true, button);
    row.querySelector('.toolbox').append(button);
  });
  $('#deleted-sources').innerHTML = deletedData.items.length ? deletedData.items.map(source => `<tr><td>#${source.id} ${escapeHtml(source.title || source.url)}</td><td>${escapeHtml(source.target_unit || '—')}</td><td>${escapeHtml(source.resolution || '未知')}</td><td><button class="secondary" onclick="setSourceDeleted(${source.id},false,this)">恢复到候选来源</button></td></tr>`).join('') : '<tr><td colspan="4" class="empty">暂无已删除来源</td></tr>';
  renderPagination('#deleted-pagination', 'deleted', deletedData);
  renderPagination('#analyzed-pagination', 'analyzed', analyzedData);
  if (startPolling) for (const source of items) if (source.active_job_id) pollJob(Number(source.active_job_id), source.active_job_kind);
}

async function pollJob(jobId, kind) {
  if (pollingJobs.has(jobId)) return;
  pollingJobs.add(jobId);
  try {
    while (true) {
      await sleep(1500);
      const {job} = await api(`/api/jobs/${jobId}`);
      jobProgress.set(Number(jobId), job.result || {});
      if (job.status === 'CANCELLED') { notice(job.result?.message || '下载已取消'); break; }
      if (job.status === 'DONE') { notice(job.result?.message || (kind === 'proxy' ? '代理下载完成。' : '镜头分析完成。'), (kind === 'analyze' && job.result?.waiting_count === 0) || (kind === 'preflight' && job.result?.status !== 'PASS')); break; }
      if (job.status === 'FAILED') { notice(humanError(job.error), true); break; }
      await loadSources(false);
    }
  } catch (error) {
    notice(`后台任务状态读取失败：${error.message}。点击“刷新”可继续查看。`, true);
  } finally {
    pollingJobs.delete(jobId);
    jobProgress.delete(Number(jobId));
    await Promise.all([loadSources(), loadDashboard(), loadQueues()]);
  }
}

async function startSourceJob(kind, id, button, allowUnknown = false) {
  button.disabled = true;
  button.textContent = '正在排队…';
  try {
    const data = await api(`/api/sources/${id}/${kind}`, {method: 'POST', body: JSON.stringify({allow_unknown: allowUnknown})});
    notice(kind === 'proxy' ? '已排队：先检查原片规格，符合后下载代理。' : kind === 'preflight' ? '规格检查已排队，不下载视频。' : '镜头分析已进入后台；可继续操作其他来源。');
    await loadSources(false);
    pollJob(Number(data.job_id), kind);
  } catch (error) {
    notice(humanError(error.message), true);
    button.disabled = false;
    button.textContent = kind === 'proxy' ? '重试下载' : '镜头分析';
    await loadSources();
  }
}

async function setSourceDeleted(id, deleted, button) {
  button.disabled = true;
  try {
    await api(`/api/sources/${id}/deleted`, {method: 'POST', body: JSON.stringify({deleted})});
    notice(deleted ? '已移入“已删除来源”，误删可恢复；不会删除已有审核记录或文件。' : '已恢复到候选来源。');
    await loadSources();
  } catch (error) { notice(error.message, true); button.disabled = false; }
}

function proxy(id, button) {
  const source = visibleSources.find(s => s.id === id);
  const check = JSON.parse(source?.metadata_json || '{}').preflight;
  const unknown = check?.status === 'UNKNOWN';
  if (unknown && !confirm('原片规格仍未知，继续下载可能浪费时间。是否继续下载供人工检查？')) return;
  return startSourceJob('proxy', id, button, unknown);
}
function analyze(id, button) { return startSourceJob('analyze', id, button); }
async function restoreSource(id, button) { button.disabled = true; try { await api(`/api/sources/${id}/analysis-state`, {method: 'POST', body: JSON.stringify({completed: false})}); notice('已移回候选来源列表。'); await loadSources(); } catch (error) { notice(error.message, true); button.disabled = false; } }

async function loadQueues() {
  const [accepted, processed, rejected] = await Promise.all([
    api(`/api/final-candidates?state=pending&page=${listPages.accepted}&page_size=${PAGE_SIZE}`),
    api(`/api/final-candidates?state=processed&page=${listPages.processed}&page_size=${PAGE_SIZE}`),
    api(`/api/candidates?status=REJECTED&page=${listPages.rejected}&page_size=${PAGE_SIZE}`)
  ]);
  $('#accepted').innerHTML = accepted.items.length ? accepted.items.map(candidate => {
    const finalized = candidate.qa_status === 'PASS';
    const failures = JSON.parse(candidate.qa_json || '{}').rules?.filter(rule => rule.status === 'FAIL') || [];
    const details = failures.map(rule => `<small class="fail">${escapeHtml(rule.rule_id + '：' + rule.reason)}</small>`).join('');
    const resolution = candidate.width && candidate.height ? `${candidate.width}×${candidate.height}` : '原片分辨率待检测';
    const qa = candidate.qa_status === 'TRIM_REQUIRED' ? '<small><span class="badge warn">需按主体重新切分</span></small>' : candidate.qa_status && candidate.qa_status !== 'PASS' ? `<small><span class="badge fail">QA ${candidate.qa_status}</span></small>` : finalized ? '<small><span class="badge warn">已处理，等待导出</span></small>' : '';
    return `<tr><td>#${candidate.id}<small>${escapeHtml(candidate.source_title)}</small>${details}</td><td>${candidate.candidate_unit || '—'}</td><td>${Number(candidate.duration).toFixed(1)}s<small>${resolution}</small>${qa}</td><td><button onclick="finalize(${candidate.id},this)">${finalized ? '重新处理' : '最终处理'}</button></td></tr>`;
  }).join('') : '<tr><td colspan="4" class="empty">暂无待最终处理候选</td></tr>';
  $('#processed').innerHTML = processed.items.length ? processed.items.map(candidate => `<tr><td>#${candidate.id}<small>${escapeHtml(candidate.source_title)}</small></td><td>${candidate.candidate_unit || '—'}</td><td>${new Date(candidate.exported_at).toLocaleString('zh-CN')}</td><td><button class="secondary" onclick="restoreProcessed(${candidate.id},this)">移回最终处理</button></td></tr>`).join('') : '<tr><td colspan="4" class="empty">暂无已处理候选。</td></tr>';
  $('#rejected').innerHTML = rejected.items.length ? rejected.items.map(candidate => `<tr><td>#${candidate.id}<small>${escapeHtml(candidate.source_title)}</small><small>${escapeHtml(candidate.rejection_reason || '')}</small></td><td>${candidate.candidate_unit || '—'}</td><td><button class="secondary" onclick="restore(${candidate.id},this)">恢复</button></td></tr>`).join('') : '<tr><td colspan="3" class="empty">暂无拒绝记录</td></tr>';
  renderPagination('#accepted-pagination', 'accepted', accepted);
  renderPagination('#processed-pagination', 'processed', processed);
  renderPagination('#rejected-pagination', 'rejected', rejected);
}

async function finalize(id, button) { button.disabled = true; notice('正在获取最终源、剪片并执行最终 QA；大文件可能需要较长时间…'); try { const data = await api(`/api/candidates/${id}/finalize`, {method: 'POST', body: '{}'}); const trim = data.qa_status === 'TRIM_REQUIRED'; notice(trim ? '检测到主体持续离场：整条不判失败，请把来源移回候选列表后重新运行镜头分析。' : `最终 QA：${data.qa_status}${data.final_path ? '，已进入交付目录' : ''}`, data.qa_status !== 'PASS'); await Promise.all([loadQueues(), loadDashboard()]); } catch (error) { notice(error.message, true); button.disabled = false; } }
async function restore(id, button) { button.disabled = true; try { await api(`/api/candidates/${id}/review`, {method: 'POST', body: JSON.stringify({decision: 'RESTORE', notes: '从拒绝列表恢复'})}); notice('已恢复到人工审核队列。'); await Promise.all([loadQueues(), loadDashboard()]); } catch (error) { notice(error.message, true); button.disabled = false; } }
async function restoreProcessed(id, button) { button.disabled = true; try { await api(`/api/candidates/${id}/export-state`, {method: 'POST', body: JSON.stringify({processed: false})}); notice('已移回最终处理列表。'); await loadQueues(); } catch (error) { notice(error.message, true); button.disabled = false; } }

async function submitForm(form, url) {
  const data = Object.fromEntries(new FormData(form)); if (data.limit) data.limit = Number(data.limit); notice('处理中…'); [...form.elements].forEach(element => element.disabled = true);
  try { const result = await api(url, {method: 'POST', body: JSON.stringify(data)}); const excluded = Number(result.excluded || 0); const excludedLive = Number(result.excluded_live || 0); const excludedLong = Math.max(0, excluded - excludedLive); const filtered = [excludedLive ? `${excludedLive} 条直播/无限循环来源` : '', excludedLong ? `${excludedLong} 条超时长来源` : ''].filter(Boolean).join('、'); notice(result.created === false ? '该来源已存在，未重复导入。' : `完成：新增 ${result.created ?? 1}，发现 ${result.found ?? 1}${filtered ? `，已过滤 ${filtered}` : ''}。`); form.reset(); await Promise.all([loadSources(), loadDashboard()]); }
  catch (error) { notice(error.message, true); }
  finally { [...form.elements].forEach(element => element.disabled = false); }
}

$('#discover-form').addEventListener('submit', event => { event.preventDefault(); submitForm(event.currentTarget, '/api/sources/discover'); });
$('#import-form').addEventListener('submit', event => { event.preventDefault(); submitForm(event.currentTarget, '/api/sources/import'); });
$('#sources-bucket-filter').onchange = event => { listFilters.sources = event.target.value; listPages.sources = 1; loadSources(); };
$('#analyzed-bucket-filter').onchange = event => { listFilters.analyzed = event.target.value; listPages.analyzed = 1; loadSources(); };
$('#export').onclick = async () => { try { const data = await api('/api/export', {method: 'POST', body: '{}'}); notice(`已导出 ${data.rows} 条：${data.path}`); await Promise.all([loadQueues(), loadDashboard()]); } catch (error) { notice(error.message, true); } };
$('#refresh').onclick = () => Promise.all([loadSources(), loadDashboard(), loadQueues()]);
Promise.all([loadRules(), loadSources(), loadDashboard(), loadQueues()]).catch(error => notice(error.message, true));
