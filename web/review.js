const $ = selector => document.querySelector(selector);
const REVIEW_PAGE_SIZE = 20;
let queue = [], index = 0, current = null, ruleBook = null;
let reviewPage = 1, reviewTotal = 0, reviewBucket = '', reviewUnit = '';
let reviewCamera = '', cameraBusy = false;
let operatorBusy = false;
const operatorLabels = {SMALL:'操作者疑似过小', HANDS_ONLY:'疑似仅见手部', PARTIAL:'人物可见不完整', VISIBLE:'检测到人物头肩及部分身体', MIXED:'部分时段有构图风险', UNKNOWN:'无法可靠判断', NOT_APPLICABLE:'当前单元未启用', UNTESTED:'尚未检测'};
const cameraLabels = {FIXED:'高度疑似固定机位', SHAKE:'疑似固定伴轻微抖动', ZOOM:'疑似变焦／缩放', MOVING:'检测到持续整体运动', MIXED:'混合片段', UNKNOWN:'无法可靠判断', UNTESTED:'尚未检测'};

async function api(url, options = {}) {
  const response = await fetch(url, {headers: {'Content-Type': 'application/json'}, ...options});
  const payload = await response.json();
  if (!response.ok || payload.ok === false) {
    const error = new Error(payload.error || '请求失败');
    error.data = payload;
    error.status = response.status;
    throw error;
  }
  return payload;
}

function notice(text, error = false) {
  const element = $('#notice');
  element.textContent = text;
  element.className = `notice show${error ? ' error' : ''}`;
  setTimeout(() => element.classList.remove('show'), 3500);
}

function esc(value) {
  const element = document.createElement('div');
  element.textContent = value ?? '';
  return element.innerHTML;
}

async function init() {
  ruleBook = await api('/api/rules');
  $('#bucket').innerHTML = '<option value="">待人工判断</option>' + Object.entries(ruleBook.buckets)
    .map(([id, bucket]) => `<option value="${id}">${id} ${bucket.name}</option>`).join('');
  $('#bucket').onchange = fillUnits;
  $('#review-bucket-filter').innerHTML = '<option value="">全部分类</option>' + Object.entries(ruleBook.buckets)
    .map(([id, bucket]) => `<option value="${id}">${id} ${bucket.name}</option>`).join('') + '<option value="unassigned">未分类</option>';
  $('#review-bucket-filter').onchange = async event => {
    reviewBucket = event.target.value;
    reviewUnit = '';
    fillReviewUnits();
    await loadQueue(1, 0);
  };
  fillReviewUnits();
  $('#review-unit-filter').onchange = async event => {
    reviewUnit = event.target.value;
    await loadQueue(1, 0);
  };
  $('#camera-filter').onchange = async event => {
    reviewCamera = event.target.value;
    await loadQueue(1, 0);
  };
  $('#camera-check').onclick = checkCamera;
  $('#operator-check').onclick = checkOperator;
  await loadQueue(1, 0);
}

function fillReviewUnits() {
  $('#review-unit-filter').innerHTML = '<option value="">全部单元</option>' + Object.entries(ruleBook.units)
    .filter(([id]) => !reviewBucket || id.startsWith(reviewBucket + '.'))
    .map(([id, unit]) => `<option value="${esc(id)}">${esc(id)} ${esc(unit.name)}</option>`).join('');
  $('#review-unit-filter').value = reviewUnit;
}

function fillUnits(selected = '') {
  const bucket = $('#bucket').value;
  $('#unit').innerHTML = '<option value="">待人工判断</option>' + Object.entries(ruleBook.units)
    .filter(([id]) => !bucket || id.startsWith(bucket + '.'))
    .map(([id, unit]) => `<option value="${id}">${id} ${unit.name}${unit.requires_confirmation ? ' ⚠' : ''}</option>`).join('');
  $('#unit').value = selected;
}

async function loadQueue(page = reviewPage, desiredIndex = 0) {
  const bucket = encodeURIComponent(reviewBucket), unit = encodeURIComponent(reviewUnit);
  const data = await api(`/api/candidates?status=WAITING_REVIEW&bucket=${bucket}&camera=${encodeURIComponent(reviewCamera)}&unit=${unit}&page=${page}&page_size=${REVIEW_PAGE_SIZE}`);
  queue = data.items;
  reviewPage = Number(data.page || 1);
  reviewTotal = Number(data.total || 0);
  index = Math.max(0, Math.min(desiredIndex, Math.max(0, queue.length - 1)));
  renderReviewPagination(data);
  await show();
}

function renderReviewPagination(data) {
  const pageCount = Math.max(1, Math.ceil(Number(data.total || 0) / Number(data.page_size || REVIEW_PAGE_SIZE)));
  $('#review-pagination').innerHTML = `<span>共 ${reviewTotal} 条 · 第 ${reviewPage} / ${pageCount} 页</span><button class="secondary" onclick="loadQueue(${reviewPage - 1},0)" ${reviewPage <= 1 ? 'disabled' : ''}>上一页</button><button class="secondary" onclick="loadQueue(${reviewPage + 1},0)" ${reviewPage >= pageCount ? 'disabled' : ''}>下一页</button>`;
}

async function show() {
  if (!queue.length) {
    current = null;
    $('#video').hidden = true;
    $('#video-empty').hidden = false;
    $('#candidate-title').textContent = '候选判断';
    $('#queue-meta').textContent = reviewTotal ? '当前页没有待审核候选' : '当前筛选条件下没有待审核候选';
    $('#rules').innerHTML = '<div class="empty">已处理完毕</div>';
    $('#rules-summary').textContent = '暂无候选';
    $('#source-reject-meta').textContent = '';
    renderCamera();
    return;
  }
  index = Math.max(0, Math.min(index, queue.length - 1));
  const data = await api(`/api/candidates/${queue[index].id}`);
  current = data.item;
  $('#video-empty').hidden = true;
  const video = $('#video');
  video.hidden = false;
  video.onerror = () => notice('代理视频无法播放，请返回生产台重新下载代理。', true);
  video.src = `/media/candidate/${current.id}?v=${Date.now()}`;
  video.onloadedmetadata = () => {
    if (!video.videoWidth || !video.videoHeight) {
      notice('代理文件没有视频画面，请返回生产台重新下载代理。', true);
      return;
    }
    video.currentTime = current.start_time;
  };
  video.ontimeupdate = () => {
    if (video.currentTime >= current.end_time) {
      video.pause();
      video.currentTime = current.start_time;
    }
  };
  $('#source-meta').innerHTML = `<a href="${esc(current.source_url)}" target="_blank" rel="noreferrer">${esc(current.source_title || current.source_url)}</a>`;
  $('#clip-time').textContent = `${current.start_time.toFixed(2)}s → ${current.end_time.toFixed(2)}s · ${current.duration.toFixed(2)}s`;
  $('#candidate-title').textContent = `候选判断 · #${current.id}`;
  $('#source-reject-meta').textContent = `来源 #${current.source_id}`;
  const globalPosition = (reviewPage - 1) * REVIEW_PAGE_SIZE + index + 1;
  $('#queue-meta').textContent = `#${current.id} · ${globalPosition} / ${reviewTotal} · 本页 ${index + 1} / ${queue.length} · 分数 ${Number(current.score).toFixed(0)}`;
  $('#bucket').value = current.candidate_bucket || '';
  fillUnits(current.candidate_unit || '');
  $('#notes').value = '';
  $('#description').value = current.delivery_description || '';
  renderTrim();
  renderCamera();
  renderRules(current.rules);
}

function renderCamera() {
  renderOperator();
  const report = current?.facts?.camera_motion || {};
  const state = report.status || 'UNTESTED';
  $('#camera-status').textContent = cameraLabels[state] || cameraLabels.UNKNOWN;
  $('#camera-status').className = `badge ${['FIXED','SHAKE','ZOOM','MIXED'].includes(state) ? 'warn' : 'unknown'}`;
  $('#camera-reason').textContent = current ? (report.reason || '该片段尚无检测结果；可补做检测，不改变审核状态。') : '当前筛选下没有待审核候选，可切换筛选。';
  $('#camera-check').disabled = !current || cameraBusy;
  $('#camera-check').textContent = cameraBusy ? '检测中…' : report.status ? '重新检测运镜' : '补做运镜检测';
  $('#camera-intervals').replaceChildren();
  for (const part of report.intervals || []) {
    const button = document.createElement('button');
    button.className = 'secondary';
    button.textContent = `${Number(part.start).toFixed(2)}–${Number(part.end).toFixed(2)}s · ${cameraLabels[part.status] || '无法判断'}`;
    button.onclick = () => { $('#video').currentTime = Math.max(current.start_time, Number(part.start)); };
    $('#camera-intervals').append(button);
  }
}

function renderOperator() {
  const report = current?.facts?.operator_framing || {};
  const state = report.status || 'UNTESTED';
  $('#operator-status').textContent = operatorLabels[state] || operatorLabels.UNKNOWN;
  $('#operator-status').className = `badge ${['SMALL','HANDS_ONLY','PARTIAL','MIXED'].includes(state) ? 'warn' : 'unknown'}`;
  $('#operator-reason').textContent = current ? report.reason || '可补检当前片段，检测操作者是否过小或仅露局部身体。' : '请选择待审核候选。';
  $('#operator-check').disabled = !current || operatorBusy;
  $('#operator-check').textContent = operatorBusy ? '检测中…' : report.status ? '重新检测操作者' : '补做操作者检测';
  $('#operator-intervals').replaceChildren();
  for (const part of report.intervals || []) {
    const button = document.createElement('button');
    button.className = 'secondary';
    button.textContent = `${Number(part.start).toFixed(2)}–${Number(part.end).toFixed(2)}s · ${operatorLabels[part.status] || '无法判断'}`;
    button.title = part.reason || '';
    button.onclick = () => { $('#video').currentTime = Math.max(current.start_time, Number(part.start)); };
    $('#operator-intervals').append(button);
  }
}

async function checkOperator() {
  if (!current || operatorBusy) return;
  const id = current.id;
  operatorBusy = true;
  renderOperator();
  try {
    const data = await api(`/api/candidates/${id}/operator-check`, {method:'POST', body:'{}'});
    if (current?.id === id) current.facts = {...current.facts, operator_framing:data.operator_framing};
    notice(`#${id}：${operatorLabels[data.operator_framing.status] || '检测完成'}，请人工复核。`);
  } catch (error) { notice(error.message, true); }
  finally { operatorBusy = false; renderOperator(); }
}

async function checkCamera() {
  if (!current || cameraBusy) return;
  const id = current.id;
  cameraBusy = true;
  renderCamera();
  try {
    const data = await api(`/api/candidates/${id}/camera-check`, {method:'POST', body:'{}'});
    if (current?.id === id) {
      current.facts = {...current.facts, camera_motion:data.camera_motion};
      const state = data.camera_motion.status;
      const matches = !reviewCamera || (reviewCamera === 'FIXED' ? ['FIXED','SHAKE'].includes(state) : reviewCamera === state);
      if (!matches) await loadQueue(reviewPage, index);
    }
    notice(`#${id}：${cameraLabels[data.camera_motion.status] || '检测完成'}，仅供人工参考。`);
  } catch (error) { notice(error.message, true); }
  finally { cameraBusy = false; renderCamera(); }
}

async function moveCandidate(delta) {
  const nextIndex = index + delta;
  if (nextIndex >= 0 && nextIndex < queue.length) {
    index = nextIndex;
    return show();
  }
  const pageCount = Math.max(1, Math.ceil(reviewTotal / REVIEW_PAGE_SIZE));
  if (delta > 0 && reviewPage < pageCount) return loadQueue(reviewPage + 1, 0);
  if (delta < 0 && reviewPage > 1) return loadQueue(reviewPage - 1, REVIEW_PAGE_SIZE - 1);
}

function renderRules(items) {
  const failed = items.filter(rule => ['FAIL','CONFLICT'].includes(rule.status)).length;
  const unknown = items.filter(rule => rule.status === 'UNKNOWN').length;
  $('#rules-summary').textContent = `${failed} 项风险 · ${unknown} 项待确认`;
  $('#rules-summary').className = `badge ${failed ? 'warn' : 'unknown'}`;
  $('#rules').innerHTML = items.map(rule => `<div class="rule" onclick="openRule('${rule.rule_id}')"><strong>${rule.rule_id}</strong><span class="badge ${rule.status.toLowerCase()}">${rule.status}</span><p>${esc(rule.reason)}</p></div>`).join('');
}

function renderTrim() {
  const facts = current?.facts || {};
  const suggestion = facts.boundary_suggestion || {};
  const lower = Number(facts.analysis_segment_start ?? facts.subject_segment_start ?? current.start_time);
  const upper = Number(facts.analysis_segment_end ?? facts.subject_segment_end ?? current.end_time);
  const start = $('#trim-start'), end = $('#trim-end');
  start.min = lower.toFixed(3); start.max = upper.toFixed(3);
  end.min = lower.toFixed(3); end.max = upper.toFixed(3);
  start.value = Number(current.start_time).toFixed(3);
  end.value = Number(current.end_time).toFixed(3);
  const suggestedStart = Number(suggestion.suggested_start), suggestedEnd = Number(suggestion.suggested_end);
  const hasSuggestion = suggestion.suggestion_available === true && Number.isFinite(suggestedStart) && Number.isFinite(suggestedEnd);
  $('#apply-boundary').disabled = !hasSuggestion;
  const risk = suggestion.end_risk === 'HIGH' || suggestion.start_risk === 'HIGH' ? '高风险' : hasSuggestion ? '需确认' : '无自动建议';
  const badge = $('#boundary-risk');
  badge.textContent = risk;
  badge.className = `badge ${risk === '高风险' ? 'fail' : 'warn'}`;
  $('#boundary-advice').textContent = hasSuggestion ? `建议 ${suggestedStart.toFixed(2)}s → ${suggestedEnd.toFixed(2)}s（${(suggestedEnd - suggestedStart).toFixed(2)}s）。${suggestion.reason || '请播放确认动作完整。'}` : `${suggestion.reason || '当前没有可靠的自动边界建议'}，请播放后手工设置起止点。`;
}

function setTrimFromPlayer(kind) {
  if (!current) return;
  const value = Number($('#video').currentTime.toFixed(3));
  $(`#trim-${kind}`).value = value.toFixed(3);
  notice(`已把当前位置 ${value.toFixed(2)}s 设为${kind === 'start' ? '起点' : '终点'}，点击“保存裁剪”后生效。`);
}

function applyBoundarySuggestion() {
  const suggestion = current?.facts?.boundary_suggestion || {};
  if (Number.isFinite(Number(suggestion.suggested_start))) $('#trim-start').value = Number(suggestion.suggested_start).toFixed(3);
  if (Number.isFinite(Number(suggestion.suggested_end))) $('#trim-end').value = Number(suggestion.suggested_end).toFixed(3);
  notice('已填入系统建议，请先播放确认，再保存裁剪。');
}

async function saveTrim() {
  if (!current) return;
  const start = Number($('#trim-start').value), end = Number($('#trim-end').value);
  if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) {
    notice('请输入有效的起止时间。', true);
    return;
  }
  try {
    await api(`/api/candidates/${current.id}/trim`, {method: 'POST', body: JSON.stringify({start_time: start, end_time: end})});
    queue[index].start_time = start; queue[index].end_time = end;
    notice('裁剪边界已保存，规则结果已重新计算。');
    await show();
  } catch (error) { notice(error.message, true); }
}

function openRule(id) {
  const reason = (current.rules.find(item => item.rule_id === id) || {}).reason || '';
  const rule = ruleBook.redlines[id];
  const unit = id === 'UNIT' && current ? ruleBook.units[current.candidate_unit] : null;
  $('#drawer-title').textContent = rule ? `${id} · ${rule.title}` : `${id} · 单元规则`;
  $('#drawer-body').innerHTML = rule ? `<p><span class="badge">PDF 第 ${rule.page} 页</span></p><h3>PDF 原文</h3><div class="original">${esc(rule.original)}</div><h3>系统解析</h3><p>硬规则：${rule.hard ? '是' : '否'}<br>确定性检测：${rule.deterministic || '无，默认人工判断'}</p><h3>当前依据</h3><div class="original">${esc(reason)}</div>` : `<h3>第四章详细规则</h3><div class="original">${esc(JSON.stringify(unit, null, 2))}</div><h3>当前依据</h3><p>${esc(reason)}</p>`;
  $('#drawer').classList.add('open');
}

$('#close-drawer').onclick = () => $('#drawer').classList.remove('open');

async function decide(decision, confirmHard = false) {
  if (!current) return;
  const payload = {decision, final_bucket: $('#bucket').value || null, final_unit: $('#unit').value || null,
    delivery_description: $('#description').value || null,
    notes: $('#notes').value,
    manual_rule_overrides: {}, confirm_hard_fail: confirmHard};
  try {
    await api(`/api/candidates/${current.id}/review`, {method: 'POST', body: JSON.stringify(payload)});
    if (decision !== 'RESTORE') {
      notice(decision === 'ACCEPT' ? '已接受，等待最终源处理。' : '已拒绝。');
      await loadQueue(reviewPage, index);
    } else {
      current.candidate_bucket = payload.final_bucket;
      current.candidate_unit = payload.final_unit;
      notice('标签已保存，候选仍在审核队列。');
      if ((reviewBucket && reviewBucket !== payload.final_bucket) || (reviewUnit && reviewUnit !== payload.final_unit)) {
        await loadQueue(reviewPage, index);
      }
    }
  } catch (error) {
    if (error.status === 409 && error.data.requires_confirmation) {
      const message = error.data.hard_failures.map(item => `${item.rule_id}: ${item.reason}`).join('\n');
      if (confirm(`该视频存在确定硬规则失败：\n${message}\n\n确定仍然保留？`)) return decide(decision, true);
    }
    notice(error.message, true);
  }
}

async function saveLabels() {
  if (!current) return;
  await decide('RESTORE');
}

async function rejectSource() {
  if (!current) return;
  const sourceId = current.source_id, notes = $('#notes').value.trim();
  const title = current.source_title || current.source_url || '该来源';
  if (!confirm(`将打回来源 #${sourceId}「${title}」下全部待审候选（已接受、已交付的不受影响）。\n备注：${notes || '（无）'}\n\n确定打回？`)) return;
  try {
    const data = await api(`/api/sources/${sourceId}/reject-waiting`, {method: 'POST', body: JSON.stringify({notes})});
    notice(`来源 #${sourceId}：已打回 ${data.count} 条待审候选。`);
    await loadQueue(reviewPage, index);
  } catch (error) { notice(error.message, true); }
}

$('#accept').onclick = () => decide('ACCEPT');
$('#reject').onclick = () => decide('REJECT');
$('#reject-source').onclick = rejectSource;
$('#edit').onclick = saveLabels;
$('#prev').onclick = () => moveCandidate(-1);
$('#next').onclick = () => moveCandidate(1);
$('#set-start').onclick = () => setTrimFromPlayer('start');
$('#set-end').onclick = () => setTrimFromPlayer('end');
$('#apply-boundary').onclick = applyBoundarySuggestion;
$('#save-trim').onclick = saveTrim;

document.addEventListener('keydown', event => {
  if (['INPUT', 'TEXTAREA', 'SELECT'].includes(event.target.tagName)) return;
  if (event.code === 'Space') {
    event.preventDefault();
    const video = $('#video');
    video.paused ? video.play() : video.pause();
  } else if (event.key.toLowerCase() === 'a') decide('ACCEPT');
  else if (event.key.toLowerCase() === 'r') decide('REJECT');
  else if (event.key.toLowerCase() === 'e') saveLabels();
  else if (event.key === 'ArrowLeft') moveCandidate(-1);
  else if (event.key === 'ArrowRight') moveCandidate(1);
});

init().catch(error => notice(error.message, true));
