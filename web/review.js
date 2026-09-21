const $ = selector => document.querySelector(selector);
const REVIEW_PAGE_SIZE = 20;
let queue = [], index = 0, current = null, ruleBook = null;
let reviewPage = 1, reviewTotal = 0, reviewBucket = '';

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
    await loadQueue(1, 0);
  };
  await loadQueue(1, 0);
}

function fillUnits(selected = '') {
  const bucket = $('#bucket').value;
  $('#unit').innerHTML = '<option value="">待人工判断</option>' + Object.entries(ruleBook.units)
    .filter(([id]) => !bucket || id.startsWith(bucket + '.'))
    .map(([id, unit]) => `<option value="${id}">${id} ${unit.name}${unit.requires_confirmation ? ' ⚠' : ''}</option>`).join('');
  $('#unit').value = selected;
}

async function loadQueue(page = reviewPage, desiredIndex = 0) {
  const bucket = encodeURIComponent(reviewBucket);
  const data = await api(`/api/candidates?status=WAITING_REVIEW&bucket=${bucket}&page=${page}&page_size=${REVIEW_PAGE_SIZE}`);
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
    $('#queue-meta').textContent = reviewTotal ? '当前页没有待审核候选' : '该分类没有待审核候选';
    $('#rules').innerHTML = '<div class="empty">已处理完毕</div>';
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
  const globalPosition = (reviewPage - 1) * REVIEW_PAGE_SIZE + index + 1;
  $('#queue-meta').textContent = `#${current.id} · ${globalPosition} / ${reviewTotal} · 本页 ${index + 1} / ${queue.length} · 分数 ${Number(current.score).toFixed(0)}`;
  $('#bucket').value = current.candidate_bucket || '';
  fillUnits(current.candidate_unit || '');
  $('#viewpoint').value = current.candidate_viewpoint || '';
  $('#notes').value = '';
  $('#description').value = current.delivery_description || '';
  renderTrim();
  renderRules(current.rules);
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
  if (decision === 'ACCEPT' && !['first_person', 'third_person'].includes($('#viewpoint').value)) {
    notice('请先选择第一人称或第三人称，再提交接受。', true);
    $('#viewpoint').focus();
    return;
  }
  const payload = {decision, final_bucket: $('#bucket').value || null, final_unit: $('#unit').value || null,
    final_viewpoint: $('#viewpoint').value || null, delivery_description: $('#description').value || null,
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
      current.candidate_viewpoint = payload.final_viewpoint;
      notice('标签已保存，候选仍在审核队列。');
      if (reviewBucket && reviewBucket !== payload.final_bucket) await loadQueue(reviewPage, index);
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

$('#accept').onclick = () => decide('ACCEPT');
$('#reject').onclick = () => decide('REJECT');
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
