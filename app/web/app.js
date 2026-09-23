/* 异环 RAG 知识助手 —— 前端逻辑（原生 JS，无构建链） */
'use strict';

const $ = (id) => document.getElementById(id);
let APP_STATE = null;
let CURRENT_CONFIG = null;
let CHAT_HISTORY = [];
let CHAT_ABORT = null;
let TOAST_TIMER = null;         // 同一时刻只允许一个 toast 倒计时，否则先来的那次会提前藏掉后来的提示
let polling = null;
let updateWasRunning = false;   // 用于捕捉「自动更新刚跑完」，好刷新推荐问题
let updatePollErrors = 0;       // 连续失败次数：只在第 3 次开始提示，避免刷屏
let updateErrorShown = false;

/* ------------------------------------------------------------------ */
/* 基础工具                                                            */
/* ------------------------------------------------------------------ */

function toast(message, kind) {
  const box = $('toast');
  box.textContent = message;
  box.className = 'toast ' + (kind || '');
  if (TOAST_TIMER) clearTimeout(TOAST_TIMER);
  TOAST_TIMER = setTimeout(() => { box.classList.add('hidden'); TOAST_TIMER = null; }, 3600);
}

async function api(path, options) {
  const opts = Object.assign({ headers: {} }, options || {});
  // 上传壁纸时 body 是 File/Blob：原样发送，不要 JSON 序列化（也不用 multipart，避免额外依赖）
  const isRaw = typeof Blob !== 'undefined' && opts.body instanceof Blob;
  if (opts.body && typeof opts.body !== 'string' && !isRaw) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(opts.body);
  }
  const response = await fetch(path, opts);
  const text = await response.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (e) { data = { detail: text }; }
  if (!response.ok) {
    const detail = (data && (data.detail || data.message)) || response.statusText;
    // 把 HTTP 状态码挂在错误对象上：humanizeError 靠它分流，
    // 免得只靠报文文本正则——服务端返回的 detail 是中文长句时，
    // 「403 令牌失效」可能被误判成别的类别。
    const error = new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
    error.status = response.status;
    throw error;
  }
  return data;
}

function esc(value) {
  return String(value === undefined || value === null ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

/**
 * 只放行 http/https 绝对地址。esc() 挡的是 HTML 注入，挡不住协议本身：
 * `href="javascript:…"` 里既没有引号也没有尖括号，转义对它毫无作用，
 * 以前只有 CSP 在兜底。来源 URL 全部来自抓取的第三方页面，所以这里
 * 从源头判协议，别的协议（javascript:/data:/vbscript: 等）一律返回空串。
 */
function safeHref(value) {
  const url = String(value === undefined || value === null ? '' : value).trim();
  return /^https?:\/\//i.test(url) ? url : '';
}

/**
 * 渲染一个站外链接。innerHtml 由调用方自行 esc()。地址不合法时降级成
 * 不可点的纯文本，而不是留下一个 href="" 会把页面滚回顶部的空锚点。
 */
function linkOrText(url, innerHtml, title) {
  const href = safeHref(url);
  if (!href) return '<span class="muted">' + innerHtml + '</span>';
  return '<a href="' + esc(href) + '" target="_blank" rel="noopener"'
    + (title ? ' title="' + esc(title) + '"' : '') + '>' + innerHtml + '</a>';
}

/**
 * 读一个数字输入框。原来到处写 `parseFloat(x) || 默认值`，填 0 会被当成
 * 「没填」而静默换成默认值；这里只有真的不是数字时才回退（0 是合法值）。
 */
function numberValue(id, fallback) {
  const raw = $(id) && $(id).value;
  const value = parseFloat(raw);
  return Number.isFinite(value) ? value : fallback;
}

/**
 * 把错误压成一行可直接放进 toast / 状态栏的话。
 * 以前非聊天的调用点都是直接拼 `error.message`，用户看到的是服务端的原始报文
 * （中文长句、甚至整个 JSON）；这里复用 humanizeError 的分类结论，只取标题与建议。
 */
function describeError(error) {
  const raw = String((error && error.message) || error || '未知错误');
  const box = humanizeError(error);
  const title = (box.match(/<div class="err-title">([\s\S]*?)<\/div>/) || [])[1] || '';
  const hint = (box.match(/<div class="err-hint">([\s\S]*?)<\/div>/) || [])[1] || '';
  const plainTitle = title.replace(/<[^>]*>/g, '').trim();
  const plainHint = hint.replace(/<[^>]*>/g, '').trim();
  if (plainTitle && plainTitle !== '请求失败') {
    return plainHint ? plainTitle + '——' + plainHint : plainTitle;
  }
  return raw.length > 160 ? raw.slice(0, 160) + '…' : raw;
}

/**
 * 列表请求防竞态：同一个 key 只认最后一次发出的响应。
 * 原来每个 loader 各写一遍 `const rows = await api(...)` 再直接渲染，
 * 快速连打搜索框时先发的慢响应会盖住后发的快响应，界面显示与输入不匹配。
 */
const LOAD_SEQ = new Map();

/**
 * 同一个 key 只认最后一次发出的请求；可选地把结果交给 paint 去渲染。
 * options.priority === 'low' 用于后台轮询：当有别的请求（用户操作）正在
 * 同一个 key 上飞行时，这次轮询整个跳过——不渲染、也不计序号，
 * 因此不会把用户操作的结果覆盖掉。
 *
 * run 会收到一个 isStale() 谓词：序号检查发生在 run() 返回之后，所以
 * 「在 run 内部直接写 DOM」的加载器（loadFacts/loadDocuments/loadTopics/
 * loadSources）必须在每次写 DOM 前自己问一句 isStale()，否则一个慢响应
 * 仍会盖掉后发的新响应。只用 opts.paint 渲染的加载器天然安全。
 */
function latestOnly(key, run, onError, options) {
  const opts = options || {};
  if (opts.priority === 'low' && LOAD_SEQ.get(key)) {
    return Promise.resolve(undefined);
  }
  const seq = (LOAD_SEQ.get(key) || 0) + 1;
  LOAD_SEQ.set(key, seq);
  const isStale = () => LOAD_SEQ.get(key) !== seq;
  return Promise.resolve()
    .then(() => run(isStale))
    .then((value) => {
      if (isStale()) return undefined;   // 已有更新的请求，丢弃这次结果
      if (value !== undefined && opts.paint) opts.paint(value);
      return value;
    })
    .catch((error) => {
      if (isStale()) return undefined;
      // onError 既可能是位置参数，也可能是 options.onError：以前只读前者，
      // 于是调用方写在 options 里的错误处理（如轮询失败要连续 3 次才提示）
      // 全成了死代码，默认 toast 每轮都弹。
      const handler = onError || opts.onError;
      if (handler) handler(error);
      else toast(describeError(error), 'error');
      return undefined;
    })
    .finally(() => {
      if (LOAD_SEQ.get(key) === seq) LOAD_SEQ.delete(key);
    });
}

/** 内联 SVG 图标（sprite 定义在 index.html，图标全部由代码绘制，不含任何图片素材） */
function icon(name, cls) {
  return '<svg class="i' + (cls ? ' ' + cls : '') + '" aria-hidden="true"><use href="#' + name + '" /></svg>';
}

/** 带图标的按钮文案：直接改 textContent 会把图标冲掉，所以统一走这里 */
function setButton(button, label, iconName, disabled) {
  if (!button) return;
  button.innerHTML = (iconName ? icon(iconName) : '') + esc(label);
  button.disabled = !!disabled;
}

/** 空态：告诉用户「为什么空」以及「下一步做什么」 */
function emptyState(iconName, title, hint) {
  return '<div class="empty">' + icon(iconName, 'lg')
    + '<div class="empty-title">' + esc(title) + '</div>'
    + (hint ? '<div class="empty-hint">' + esc(hint) + '</div>' : '')
    + '</div>';
}

/** 把技术味很重的报错翻译成「发生了什么 + 该怎么办」 */
function humanizeError(error) {
  const raw = String((error && error.message) || error || '未知错误');
  const status = Number((error && error.status) || 0);
  let title = '请求失败';
  let hint = '可以重试一次；若一直失败，请到「设置」页确认模型配置是否正确。';
  const KEY_LIKE = /密钥|api[\s_-]?key|模型服务|invalid|未授权/i;
  const TOO_LONG_LIKE = /string_too_long|too_long|at most 4000/i;
  if ((status === 422 || /422/.test(raw)) || TOO_LONG_LIKE.test(raw)) {
    // 服务端对问题长度有 4000 字上限，超长会返回 422。
    // 这个分支必须排在「密钥/无效」之前：422 的报文里常带 api/无效/未授权 这类词，
    // 否则会被误报成「模型服务拒绝、密钥无效」，把人引到完全错误的地方。
    title = '请求内容不合法或问题太长';
    hint = '单个问题上限 4000 字，请精简后重试（可以把长材料分次提问）。';
  } else if ((status === 401 || status === 403) && !KEY_LIKE.test(raw)) {
    // 本地服务的 401/403 基本只有一个来源：会话令牌失效（刷新页面即可拿到新令牌）。
    // 但模型服务商把 401/403 透传上来时报文里会带「密钥」之类的字样，那种情况走下面的分支。
    title = '本地服务拒绝了这次请求（会话令牌已失效）';
    hint = '刷新页面即可拿到新令牌；若刷新后仍失败，请重新打开程序。';
  } else if (/更换模型服务地址后需要重新填写 API Key/.test(raw)) {
    // 这句是程序自己发的（保存或探针发现「换了地址却还在用旧密钥」时的拒绝理由）。
    // 必须排在下面那条「密钥/无效」正则之前，否则它会被归成「密钥无效或无权限」，
    // 用户以为 Key 打错了，而实际要做的是重新粘贴一次。
    title = '换了模型服务地址，需要重新粘贴一次 API Key';
    hint = '为避免把已保存的密钥发送到新地址，程序不会自动带过去；粘贴后保存即可（同一台主机只改路径不用重填）。';
  } else if (/401|403|未授权|密钥|api[\s_-]?key|invalid/i.test(raw)) {
    title = '模型服务拒绝了这次请求（密钥无效或无权限）';
    hint = '到「设置」页重新粘贴 API Key，保存后点「测试连接」；也可能是余额或额度用完了。';
  } else if (/404|model.*not|模型不存在|no such model/i.test(raw)) {
    title = '模型名可能不对';
    hint = '到「设置」页点「拉取可用模型」，从服务商返回的真实列表里选一个。';
  } else if (/timeout|超时|timed out/i.test(raw)) {
    title = '请求超时';
    hint = '网络不稳定或所选模型响应较慢；可在「设置 → 高级设置」里调大超时时间再试。';
  } else if (/429|rate|限流|too many|quota/i.test(raw)) {
    title = '触发了服务商限流（429）';
    hint = '等待几十秒再试；免费额度的模型更容易被限流。';
  } else if (/Failed to fetch|NetworkError|enotfound|连接|network/i.test(raw)) {
    title = '连不上服务';
    hint = '检查网络或代理设置；如果本地服务已经退出，重新打开程序即可。';
  } else if (/尚未配置|未配置模型|没有配置/i.test(raw)) {
    title = '还没有配置模型';
    hint = '到「设置」页选服务商并粘贴 API Key 就能获得 AI 总结；不配置也可以检索本地知识库。';
  }
  return '<div class="err-box">'
    + '<div class="err-title">' + icon('i-warn') + esc(title) + '</div>'
    + '<div class="err-hint">' + esc(hint) + '</div>'
    + '<details><summary class="muted small">查看原始错误</summary><pre class="err-raw">' + esc(raw) + '</pre></details>'
    + '</div>';
}

/* 极简 Markdown 渲染：先转义再套用有限语法，避免 XSS */
function renderMarkdown(text) {
  let html = esc(text || '');
  html = html.replace(/```([\s\S]*?)```/g, (m, code) => '<pre class="log small">' + code.trim() + '</pre>');
  html = html.replace(/^### (.*)$/gm, '<h3>$1</h3>');
  html = html.replace(/^## (.*)$/gm, '<h2>$1</h2>');
  html = html.replace(/^# (.*)$/gm, '<h1>$1</h1>');
  html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
  html = html.replace(/\[(\d+)\]/g, '<span class="cite-chip">[$1]</span>');
  // 列表：连续的行合并成一个 <ul>。
  // 曾经的写法是「每行转成 <li>，然后只把最后一个 <li> 包进 <ul>」，
  // 于是模型输出「一句话引出列表」时得到
  // `<p>文字<br /><ul><li>aaa</li><br /><li>bbb</li></ul></p>`——
  // `<ul>` 嵌在 `<p>` 里是非法 HTML，只能靠浏览器纠错才显示正常。
  html = html.replace(/(?:^[ \t]*[-*] .*(?:\n|$))+/gm, (block) => {
    const items = block.split('\n')
      .filter((line) => /^[ \t]*[-*] /.test(line))
      .map((line) => '<li>' + line.replace(/^[ \t]*[-*] /, '') + '</li>')
      .join('');
    return '\u0000' + items + '\u0001';
  });
  html = html.split(/\n{2,}/).map((block) => {
    // 含占位符的块已经带着 <li> 结构，不能再包进 <p>（那正是原来产出的非法 HTML）
    if (/^\s*<(h\d|ul|pre|li)/.test(block) || block.indexOf('\u0000') >= 0) return block;
    return '<p>' + block.replace(/\n/g, '<br />') + '</p>';
  }).join('');
  html = html.replace(/\u0000/g, '<ul>').replace(/\u0001/g, '</ul>');
  return html;
}

function sourceTag(type) {
  const labels = {
    official: '官方', wiki: 'WIKI', community: '社区', seed: '内置', manual: '手动',
    narrative: '叙事', news: '公告'
  };
  const cls = type === 'official' ? 'official' : (type === 'wiki' ? 'wiki' : '');
  return '<span class="tag ' + cls + '">' + esc(labels[type] || type || '未知') + '</span>';
}

// 来源级徽标：施工中站点与叙事/世界观来源都要在列表里一眼看见
function sourceBadges(source) {
  const note = String((source && source.note) || '');
  const cls = String((source && source.source_class) || '');
  let html = '';
  if (note.indexOf('施工中') >= 0) {
    html += '<span class="tag warn" title="' + esc(note) + '">施工中·可靠性待确认</span>';
  }
  if (cls === 'narrative' || note.indexOf('叙事/世界观') >= 0) {
    html += '<span class="tag narrative">叙事/世界观·不参与字段投票</span>';
  }
  if (source && source.no_vote) {
    html += '<span class="tag warn">不参与字段投票</span>';
  }
  return html;
}

function fmtTime(value) {
  if (!value) return '';
  return String(value).replace('T', ' ').slice(0, 16);
}

/* ------------------------------------------------------------------ */
/* 主题（暗 / 亮 / 跟随系统，持久化在后端配置里）                        */
/* ------------------------------------------------------------------ */

// 本地端口每次启动都随机，localStorage 的源会变，所以状态一律存在服务端配置里。
let THEME_MODE = 'dark';
const SYSTEM_DARK = (window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null);

function resolveTheme(mode) {
  if (mode === 'system') return (SYSTEM_DARK && SYSTEM_DARK.matches) ? 'dark' : 'light';
  return mode === 'light' ? 'light' : 'dark';
}

function paintTheme() {
  const resolved = resolveTheme(THEME_MODE);
  document.documentElement.dataset.theme = resolved;
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute('content', resolved === 'light' ? '#f2f5fa' : '#0d1017');
  document.querySelectorAll('#theme-seg [data-theme-opt]').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.themeOpt === THEME_MODE);
  });
}

function applyTheme(mode, persist) {
  THEME_MODE = (mode === 'light' || mode === 'system') ? mode : 'dark';
  paintTheme();
  if (persist) saveConfig({ ui: { theme: THEME_MODE } }, '主题已保存');
}

if (SYSTEM_DARK && SYSTEM_DARK.addEventListener) {
  SYSTEM_DARK.addEventListener('change', () => { if (THEME_MODE === 'system') paintTheme(); });
}

/* ------------------------------------------------------------------ */
/* 壁纸（只读取用户本机图片；上传到用户自己的数据目录，不随程序分发）    */
/* ------------------------------------------------------------------ */

function wallpaperUrl() { return '/api/ui/wallpaper?v=' + Date.now(); }

function applyWallpaper(ui) {
  const cfgUi = ui || {};
  const hasFile = !!cfgUi.wallpaper_file;
  const enabled = hasFile && cfgUi.wallpaper_enabled !== false;
  const dim = (typeof cfgUi.wallpaper_dim === 'number') ? cfgUi.wallpaper_dim : 0.6;
  const fit = cfgUi.wallpaper_fit === 'cover' ? 'cover' : 'contain';
  const root = document.documentElement;
  const url = hasFile ? wallpaperUrl() : '';   // 背景与预览共用同一个地址（带 ?v= 破缓存）
  root.style.setProperty('--wp-dim', String(dim));
  // 背景适配：contain=整张可见（.wp-fill 模糊铺底补满留白）；cover=铺满裁切
  root.style.setProperty('--wp-fit', fit);
  document.body.classList.toggle('wp-fit-cover', fit === 'cover');
  document.querySelectorAll('#wp-fit [data-fit]').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.fit === fit);
  });
  if (enabled) {
    root.style.setProperty('--wp-image', 'url("' + url + '")');
    document.body.classList.add('has-wallpaper');
  } else {
    root.style.setProperty('--wp-image', 'none');
    document.body.classList.remove('has-wallpaper');
  }
  if ($('wp-enabled')) $('wp-enabled').checked = enabled;
  if ($('wp-state')) {
    $('wp-state').textContent = hasFile ? (enabled ? '已启用' : '已导入（未启用）') : '未设置';
  }
  if ($('wp-dim')) $('wp-dim').value = String(dim);
  if ($('wp-dim-value')) $('wp-dim-value').textContent = Math.round(dim * 100) + '%';
  const preview = $('wp-preview');
  if (preview) {
    // 预览用背景图方式画（= style.css 的 .wp-preview：contain + 居中）。
    // 早先用 <img> + object-fit，在「aspect-ratio + max-height」的网格里高度可能退化成
    // 自然高度，竖图会被 overflow:hidden 裁掉下半部分（用户实测反馈），所以改成背景图。
    if (hasFile) {
      preview.style.backgroundImage = 'url("' + url + '")';
      preview.innerHTML = '';
    } else {
      preview.style.backgroundImage = '';
      preview.innerHTML = '<span class="muted small">还没有设置壁纸</span>';
    }
  }
}

/** 输入框跟着内容长高（最多 40vh），避免长问题只看得到一小半 */
function autoGrowQuestion() {
  const box = $('question');
  if (!box) return;
  box.style.height = 'auto';
  const max = Math.round(window.innerHeight * 0.4);
  box.style.height = Math.min(box.scrollHeight, max) + 'px';
  box.style.overflowY = box.scrollHeight > max ? 'auto' : 'hidden';
}

async function uploadWallpaper(file) {
  const allowed = ['image/jpeg', 'image/png', 'image/webp', 'image/gif', 'image/bmp'];
  const type = (file.type || '').toLowerCase();
  if (allowed.indexOf(type) < 0) {
    toast('只支持 JPG / PNG / WebP / GIF / BMP 格式的图片', 'error');
    return;
  }
  if (file.size > 12 * 1024 * 1024) {
    toast('图片太大（上限 12 MB），请先压缩后再试', 'error');
    return;
  }
  if ($('wp-state')) $('wp-state').textContent = '正在导入…';
  try {
    // 直接发送原始字节 + 正确的 content-type，服务端不需要 multipart 解析器
    const result = await api('/api/ui/wallpaper', { method: 'POST', headers: { 'Content-Type': type }, body: file });
    applyWallpaper((result.config || {}).ui || {});
    toast('壁纸已导入', 'ok');
  } catch (error) {
    if ($('wp-state')) $('wp-state').textContent = '导入失败';
    toast('导入失败：' + describeError(error), 'error');
  } finally {
    // 清空选择框：不清的话再次选同一张图不会触发 change，看起来像「点了没反应」
    if ($('wp-file')) $('wp-file').value = '';
  }
}

/* ------------------------------------------------------------------ */
/* 标签页                                                              */
/* ------------------------------------------------------------------ */

/**
 * 切页签。focus 为真时把焦点交给新页签按钮本身——键盘用户用方向键切换后，
 * 焦点必须跟着走，否则下一次按键还作用在旧页签上。
 */
function switchTab(name, focus) {
  document.querySelectorAll('.tab').forEach((btn) => {
    const active = btn.dataset.tab === name;
    btn.classList.toggle('active', active);
    // 读屏用户需要知道「当前在哪个页签」，否则只会听到五个没有状态的按钮
    btn.setAttribute('aria-selected', active ? 'true' : 'false');
    btn.setAttribute('tabindex', active ? '0' : '-1');
    if (active && focus) btn.focus();
  });
  document.querySelectorAll('.panel').forEach((panel) => panel.classList.toggle('active', panel.id === 'panel-' + name));
  if (name === 'kb') { loadFacts(); loadDocuments(); loadStats(); }
  if (name === 'update') { loadTopics(); loadSources(); loadUpdateStatus(); }
  if (name === 'settings') { loadConfig(); }
  if (name === 'about') { renderAbout(); }
  // 回到问答页就换一批推荐问题，用户每次看到的都不一样
  if (name === 'chat') { renderStarterAsks(); }
}

/* ------------------------------------------------------------------ */
/* 状态与总览                                                          */
/* ------------------------------------------------------------------ */

function renderStatusPills() {
  if (!APP_STATE) return;
  const stats = APP_STATE.stats || {};
  const pills = [
    { text: '条目 ' + (stats.facts || 0), cls: '' },
    { text: '资料 ' + (stats.documents || 0) + ' 篇 / ' + (stats.chunks || 0) + ' 段', cls: '' },
    { text: stats.conflicts ? ('冲突 ' + stats.conflicts) : '无冲突', cls: stats.conflicts ? 'warn' : 'ok' },
    { text: APP_STATE.llm_ready ? '模型已配置' : '模型未配置', cls: APP_STATE.llm_ready ? 'ok' : 'warn' },
    { text: APP_STATE.paths && APP_STATE.paths.portable ? '便携模式' : '用户目录模式', cls: '' }
  ];
  $('status-pills').innerHTML = pills.map((p) => '<span class="pill ' + p.cls + '">' + esc(p.text) + '</span>').join('');
  $('version-line').textContent = 'v' + APP_STATE.version + ' · 本地知识库 · 自动联网更新';
  if (!APP_STATE.llm_ready) {
    $('first-run-hint').textContent = '提示：还没有配置模型 API Key，当前只能检索本地资料；到「设置」页填好后即可获得 AI 总结。';
  } else {
    $('first-run-hint').textContent = '';
  }
}

function loadState(priority) {
  // 状态徽标是后台轮询的目标：优先级 'low' 时，若用户正在发起提问或保存设置
  // （同一个 key 上有更高优先级的请求在飞），这次轮询直接跳过。
  return latestOnly('state', () => api('/api/state'), undefined, {
    priority: priority,
    paint: (state) => { APP_STATE = state; renderStatusPills(); }
  });
}

/* ------------------------------------------------------------------ */
/* 问答                                                                */
/* ------------------------------------------------------------------ */

/** 聊天记录里保留的最大气泡数：超过就从头丢掉。 */
const MAX_BUBBLES = 200;

function appendBubble(role, html, extraClass) {
  const box = document.createElement('div');
  box.className = 'bubble ' + role + (extraClass ? ' ' + extraClass : '');
  box.innerHTML = '<div class="bubble-body">' + html + '</div>';
  const log = $('chat-log');
  log.appendChild(box);
  // 长会话下 DOM 只增不减：多问几十轮之后滚动与重排会越来越卡。保留最近 200 条即可。
  while (log.children.length > MAX_BUBBLES) log.removeChild(log.firstChild);
  log.scrollTop = log.scrollHeight;
  return box;
}

/**
 * 流式渲染节流：每个动画帧最多把累积的 markdown 重渲染一次。
 * 原来每来一个 token 就整体重渲染再滚到底，长回答时主线程被几千次 innerHTML
 * 解析占满，输入与滚动都会卡。定时器是兜底：标签页切到后台时 requestAnimationFrame
 * 不回调，没有它最后一段文字要等回到前台才出现。
 */
function makeStreamPainter(body) {
  let text = '';
  let ticking = false;
  let timer = null;
  let stopped = false;
  const paint = () => {
    ticking = false;
    if (timer) { clearTimeout(timer); timer = null; }
    if (stopped) return;
    body.innerHTML = renderMarkdown(text) + '<span class="typing"></span>';
    $('chat-log').scrollTop = $('chat-log').scrollHeight;
  };
  const schedule = () => {
    if (ticking) return;
    ticking = true;
    if (typeof requestAnimationFrame === 'function') requestAnimationFrame(paint);
    else timer = setTimeout(paint, 50);
  };
  return {
    push(chunk) { text += chunk; schedule(); },
    /* 收尾前必须先 flush：否则已经排队但还没执行的那一帧会在
       「最终结果 / 错误提示」写完之后把它覆盖掉。 */
    flush() { stopped = true; if (timer) { clearTimeout(timer); timer = null; } return text; },
    get text() { return text; }
  };
}

/** 来源只显示「域名 + 末段路径」，完整网址在 title 与链接里，避免一行被长 URL 占满 */
function shortUrl(url) {
  const text = String(url || '');
  try {
    const parsed = new URL(text);
    const parts = parsed.pathname.split('/').filter(Boolean);
    const tail = parts.length ? '/' + parts[parts.length - 1] : '';
    return parsed.hostname.replace(/^www\./, '') + tail.slice(0, 40);
  } catch (error) {
    return text.slice(0, 60);
  }
}

function renderSources(citations, webUsed, warnings) {
  const warnBlock = (list) => {
    if (!list || !list.length) return '';
    return '<div class="cite-group"><div class="cite-group-title">' + icon('i-warn') + '抓取过程中的提示</div>'
      + list.map((w) => '<div class="source-item">· ' + esc(w) + '</div>').join('') + '</div>';
  };
  if (!citations || !citations.length) {
    return warnBlock(warnings) ? '<div class="sources">' + warnBlock(warnings) + '</div>' : '';
  }
  let html = '<div class="sources"><div class="cite-group-title">'
    + icon('i-link') + '来源' + (webUsed ? '（含本次联网抓取）' : '（本地知识库）') + '</div>';
  citations.forEach((item) => {
    const title = item.title || '未命名';
    const badges = sourceTag(item.source_type)
      + (item.status === 'conflict' ? '<span class="tag conflict">版本冲突</span>' : '');
    const meta = [];
    if (item.url) {
      meta.push(linkOrText(item.url, icon('i-link') + esc(shortUrl(item.url)), item.url));
    }
    if (item.updated_at) meta.push('<span>' + icon('i-clock') + esc(fmtTime(item.updated_at)) + '</span>');
    html += '<div class="cite-card">'
      + '<span class="idx">[' + esc(item.index) + ']</span>'
      + '<div class="cite-body">'
      + '<div class="cite-title">' + esc(title) + badges + '</div>'
      + (meta.length ? '<div class="cite-meta">' + meta.join('') + '</div>' : '')
      + '</div></div>';
  });
  return html + warnBlock(warnings) + '</div>';
}

async function sendQuestion() {
  // 并发保护：正在流式回答时不再发起新一轮。否则第二次提问会覆盖 CHAT_ABORT，
  // 第一条请求的 signal 变成孤儿、「停止生成」也停不掉它，而且先跑完的那一轮
  // 会把按钮和控制权恢复，留下一条停不下来的幽灵流。
  if (CHAT_ABORT) return;
  const question = $('question').value.trim();
  if (!question) return;
  if (question.length > 4000) {
    toast('问题太长了（上限 4000 字），请精简后重试', 'error');
    return;
  }
  $('question').value = '';
  autoGrowQuestion();
  appendBubble('user', '<p>' + esc(question) + '</p>');

  const bubble = appendBubble('assistant', '<p class="muted">正在准备…</p>');
  const body = bubble.querySelector('.bubble-body');
  const answer = makeStreamPainter(body);
  let citations = [];
  let webUsed = false;
  let warnings = [];

  // 用局部 controller 而不是每处都读全局：finally 里据此判断「我还是当前这一轮吗」，
  // 避免已经过期的回调把新的一轮的状态清掉。
  const controller = new AbortController();
  CHAT_ABORT = controller;

  setButton($('send-btn'), '发送', 'i-send', true);
  $('stop-btn').classList.remove('hidden');

  try {
    const response = await fetch('/api/chat/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        question: question,
        history: CHAT_HISTORY.slice(-6),
        allow_web: $('allow-web').checked
      }),
      signal: controller.signal
    });
    if (!response.ok) {
      const raw = await response.text();
      // 服务端的错误体通常是 JSON（{"detail": "..."}）。直接把它当文案用，
      // 「查看原始错误」里就会显示整坨 JSON；这里先尝试解析出 detail。
      let detail = raw;
      try {
        const parsed = JSON.parse(raw);
        if (parsed && typeof parsed.detail === 'string') detail = parsed.detail;
      } catch (error) { /* 不是 JSON 就原样用 */ }
      const failure = new Error(detail || response.statusText);
      failure.status = response.status;
      throw failure;
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let statusLines = [];

    const handleEvent = (event) => {
      if (event.type === 'status') {
        statusLines.push(event.message);
        setPhase(statusLines[statusLines.length - 1]);
        body.innerHTML = '<p class="muted">' + esc(statusLines[statusLines.length - 1]) + '</p>';
      } else if (event.type === 'sources') {
        citations = event.citations || [];
        webUsed = !!event.web_used;
        warnings = event.warnings || [];
      } else if (event.type === 'token') {
        hidePhase();
        answer.push(event.text || '');
      } else if (event.type === 'done') {
        const answerText = answer.flush();
        // done 里带的是「按回答真正引用过的编号收窄过」的清单：sources 事件在全文
        // 生成完之前就要发出去，只能给完整清单，所以这里以收窄后的为准（有才覆盖）。
        if (Array.isArray(event.citations)) citations = event.citations;
        const meta = [];
        if (event.latency_ms) meta.push((event.latency_ms / 1000).toFixed(1) + ' 秒');
        if (event.retrieval) {
          meta.push('本地命中 ' + (event.retrieval.facts + event.retrieval.chunks) + ' 条');
          if (event.retrieval.web_attempted) meta.push('联网抓取 ' + event.retrieval.web_pages + ' 页');
        }
        if (event.degraded) meta.push('未启用 AI 总结');
        body.innerHTML = renderMarkdown(answerText)
          + '<div class="meta-line">' + meta.map((m) => '<span>' + esc(m) + '</span>').join('')
          + (answerText ? '<button type="button" class="ghost cite-copy" data-copy-answer="1">'
            + icon('i-copy') + '复制回答</button>' : '')
          + '</div>'
          + renderSources(citations, webUsed, warnings);
        bubble.dataset.raw = answerText;
      } else if (event.type === 'error') {
        // 流内错误（模型调用失败等）：先停掉节流绘制，
        // 否则排队中的那一帧会在错误提示之后执行，把提示盖回半截回答。
        answer.flush();
        bubble.classList.add('error');
        hidePhase();
        body.innerHTML = humanizeError(event.message);
      }
    };

    while (true) {
      const chunk = await reader.read();
      if (chunk.done) break;
      buffer += decoder.decode(chunk.value, { stream: true });
      let index;
      while ((index = buffer.indexOf('\n\n')) >= 0) {
        const raw = buffer.slice(0, index);
        buffer = buffer.slice(index + 2);
        const line = raw.split('\n').find((l) => l.startsWith('data:'));
        if (!line) continue;
        let event = null;
        try { event = JSON.parse(line.slice(5).trim()); } catch (e) { continue; }
        if (event.type === 'end') continue;
        handleEvent(event);
      }
    }

    const flushed = answer.flush();
    if (flushed) {
      CHAT_HISTORY.push({ role: 'user', content: question });
      CHAT_HISTORY.push({ role: 'assistant', content: flushed });
      if (CHAT_HISTORY.length > 12) CHAT_HISTORY = CHAT_HISTORY.slice(-12);
    }
  } catch (error) {
    // 先落定已经收到的文字：不 flush 的话，排队中的那一帧会在错误提示之后
    // 再执行一次，把「请求失败」盖回成半截回答。
    const partial = answer.flush();
    if (error && error.name === 'AbortError') {
      body.innerHTML = renderMarkdown(partial)
        + '<p class="muted small">已停止生成；上面是已经写出的部分。</p>';
      // 中止时也要写入历史：否则下一轮带上的 history 与用户屏幕上看到的内容
      // 不一致，模型会以为对话里没有这一轮。
      CHAT_HISTORY.push({ role: 'user', content: question });
      CHAT_HISTORY.push({ role: 'assistant', content: partial || '（已停止生成）' });
      if (CHAT_HISTORY.length > 12) CHAT_HISTORY = CHAT_HISTORY.slice(-12);
    } else {
      bubble.classList.add('error');
      body.innerHTML = humanizeError(error);
    }
  } finally {
    // 只有「当前这一轮」才能收尾：并发被拦住后这里是双保险，
    // 防止未来改动引入第二轮时把状态清错。
    if (CHAT_ABORT === controller) {
      CHAT_ABORT = null;
      hidePhase();
      $('stop-btn').classList.add('hidden');
      setButton($('send-btn'), '发送', 'i-send', false);
      loadState().catch(() => {});
    }
  }
}

/** 阶段提示：把「正在检索 / 正在联网 / 正在总结」显性化，避免看起来像卡死 */
function setPhase(text) {
  if (!text) return;
  $('chat-phase-text').textContent = text;
  $('chat-phase').classList.remove('hidden');
}

function hidePhase() {
  $('chat-phase').classList.add('hidden');
}

/* ------------------------------------------------------------------ */
/* 知识库                                                              */
/* ------------------------------------------------------------------ */

/** 知识库页的能力数字。走 latestOnly 是防竞态：切页/连点刷新时只认最后一次结果。 */
function loadStats(priority) {
  return latestOnly('kb-stats', () => api('/api/kb/stats'), (error) => toast('读取统计失败：' + describeError(error), 'error'), {
    priority: priority,
    paint: (stats) => {
      const items = [
        ['知识条目', stats.facts],
        ['冲突条目', stats.conflicts],
        ['已被取代', stats.superseded],
        ['原始资料', stats.documents],
        ['资料片段', stats.chunks],
        ['库体积(KB)', stats.db_size_kb],
        ['全文检索', stats.fts_enabled ? 'FTS5 已启用' : '降级 LIKE']
      ];
      $('kb-stats').innerHTML = items.map((pair) =>
        '<div class="stat"><div class="num">' + esc(pair[1]) + '</div><div class="lbl">' + esc(pair[0]) + '</div></div>').join('');
    }
  });
}

function loadFacts() {
  return latestOnly('kb-facts', async (isStale) => {
    const search = $('fact-search').value.trim();
    const status = $('fact-status').value;
    const data = await api('/api/kb/facts?limit=100&keyword=' + encodeURIComponent(search) + '&status=' + encodeURIComponent(status));
    if (isStale()) return;   // 期间又发起了更新的查询，别用旧结果盖掉它
    const list = $('fact-list');
    if (!data.items.length) {
      list.innerHTML = search
        ? emptyState('i-search', '没有匹配的知识条目', '换个关键词试试，或清空搜索框查看全部条目。')
        : emptyState('i-db', '知识库还是空的', '到「自动更新」页点「立即更新」，或勾选内置数据源抓取一次。');
      return;
    }
    list.innerHTML = data.items.map((fact) => {
      const badges = sourceTag(fact.source_type)
        + (fact.status === 'conflict' ? '<span class="tag conflict">冲突</span>' : '')
        + (fact.status === 'superseded' ? '<span class="tag">已被取代</span>' : '');
      return '<div class="item">'
        + '<div class="item-head"><div><div class="item-title">' + esc(fact.title) + '</div>'
        + '<div class="item-meta">' + esc(fact.topic || '未分类') + ' · 置信度 ' + (fact.confidence || 0).toFixed(2)
        + ' · ' + esc(fmtTime(fact.updated_at)) + badges + '</div></div>'
        + '<div class="item-actions"><button data-del-fact="' + fact.id + '" class="danger">删除</button></div></div>'
        + '<div class="item-body">' + esc(fact.answer) + '</div>'
        + (fact.source_url ? '<div class="item-meta">' + linkOrText(fact.source_url, esc(fact.source_url.slice(0, 90))) + '</div>' : '')
        + '</div>';
    }).join('');
  }, (error) => toast('读取条目失败：' + describeError(error), 'error'));
}

function loadDocuments() {
  return latestOnly('kb-documents', async (isStale) => {
    const search = $('doc-search').value.trim();
    const data = await api('/api/kb/documents?limit=100&keyword=' + encodeURIComponent(search));
    if (isStale()) return;
    const list = $('doc-list');
    if (!data.items.length) {
      list.innerHTML = search
        ? emptyState('i-search', '没有匹配的原始资料', '换个关键词，或清空搜索框查看全部资料。')
        : emptyState('i-file', '还没有抓取到原始资料', '到「自动更新」页抓取一次，资料会按页保存并切片。');
      return;
    }
    list.innerHTML = data.items.map((doc) => '<div class="item">'
      + '<div class="item-head"><div><div class="item-title">' + esc(doc.title || doc.url) + '</div>'
      + '<div class="item-meta">' + sourceTag(doc.source_type) + ' · ' + (doc.chunk_count || 0) + ' 段'
      + ' · 抓取于 ' + esc(fmtTime(doc.updated_at)) + '</div></div>'
      + '<div class="item-actions"><button data-del-doc="' + doc.id + '" class="danger">删除</button></div></div>'
      + '<div class="item-meta">' + linkOrText(doc.url, esc(String(doc.url).slice(0, 100))) + '</div>'
      + '</div>').join('');
  }, (error) => toast('读取资料失败：' + describeError(error), 'error'));
}

/* 删除动作抽成函数：列表用事件委托调用（见 bindEvents），
   以前是渲染完逐行绑监听器。三处都带确认与失败提示。 */
async function deleteFact(id) {
  if (!confirm('确定删除该条目？')) return;
  try {
    await api('/api/kb/facts/' + encodeURIComponent(id), { method: 'DELETE' });
  } catch (error) {
    toast('删除失败：' + describeError(error), 'error');
    return;
  }
  toast('已删除', 'ok');
  loadFacts(); loadStats();
}

async function deleteDocument(id) {
  if (!confirm('删除该资料及其所有片段？')) return;
  try {
    await api('/api/kb/documents/' + encodeURIComponent(id), { method: 'DELETE' });
  } catch (error) {
    toast('删除失败：' + describeError(error), 'error');
    return;
  }
  toast('已删除', 'ok');
  loadDocuments(); loadStats();
}

async function deleteTopic(topic) {
  // 删掉就没了，不能靠误点：先确认；失败要说出来。
  if (!confirm('确定要从「更新主题」里删掉「' + topic + '」吗？')) return;
  try {
    await api('/api/topics?topic=' + encodeURIComponent(topic), { method: 'DELETE' });
  } catch (error) {
    toast('删除失败：' + describeError(error), 'error');
    return;
  }
  toast('已删除主题：' + topic, 'ok');
  loadTopics();
}

/**
 * 导出知识库。
 * 原实现是 `window.open('/api/kb/export', '_blank')`：没有失败处理，
 * 弹窗被拦截或服务端 4xx/5xx 时用户看到的是一片空白，以为导出成功了。
 * 改成先 fetch 拿到响应，再决定是下载还是报错。
 */
async function exportKnowledgeBase() {
  const button = $('kb-export');
  setButton(button, '导出中…', 'i-refresh', true);
  try {
    const response = await fetch('/api/kb/export');
    if (!response.ok) {
      let detail = 'HTTP ' + response.status;
      try {
        const text = await response.text();
        const parsed = JSON.parse(text);
        if (parsed && typeof parsed.detail === 'string') detail = parsed.detail;
      } catch (error) { /* 不是 JSON：保留状态码 */ }
      throw new Error(detail);
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = 'nte-rag-knowledge-' + new Date().toISOString().slice(0, 10) + '.json';
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    setTimeout(() => URL.revokeObjectURL(url), 10000);
    toast('已导出知识库', 'ok');
  } catch (error) {
    toast('导出失败：' + describeError(error), 'error');
  } finally {
    setButton(button, '导出 JSON', 'i-download', false);
  }
}

/* ------------------------------------------------------------------ */
/* 自动更新                                                            */
/* ------------------------------------------------------------------ */

function loadUpdateStatus(priority) {
  return latestOnly('update-status', () => api('/api/update/status'), undefined, {
    priority: priority,
    paint: (status) => {
      updatePollErrors = 0;
      updateErrorShown = false;
      // 数字口径与后端一致：取到正文 / 新页面 / 内容未变 / 按规则跳过分开算。
      // 以前只有一个「抓取 N 页」，用户看到「抓取 17 页却新增 0 条」会以为更新没起作用。
      const s = status.summary || {};
      let summary = '尚未运行';
      if (s.finished_at) {
        const parts = [];
        if (s.fetched === undefined || s.fetched === null) {
          // 万一这个字段没带上，退回老口径显示，别显示成「取到正文 0 页」
          parts.push('抓取 ' + (s.pages || 0) + ' 页');
        } else {
          parts.push('取到正文 ' + s.fetched + ' 页', '新页面 ' + (s.pages || 0) + ' 页');
          if (s.skipped) parts.push('内容未变 ' + s.skipped + ' 页');
          if (s.skipped_by_policy) parts.push('按规则跳过 ' + s.skipped_by_policy + ' 页');
        }
        parts.push('新增条目 ' + (s.facts_added || 0));
        if (s.failed) parts.push('抓取失败 ' + s.failed + ' 页');
        if (s.error_count) parts.push('错误 ' + s.error_count);
        summary = '上次完成：' + fmtTime(s.finished_at) + '｜' + parts.join('｜');
      }
      $('update-summary').textContent = (status.running ? '正在运行：' + (status.current_topic || '准备中') + '　' : '')
        + summary + '　|　下次检查：' + (status.next_check_hint || '—');
      $('update-log').textContent = (status.progress || []).map((line) => line.time + '  ' + line.message).join('\n') || '（暂无日志）';
      setButton($('update-run'), status.running ? '更新中…' : '立即更新', status.running ? 'i-refresh' : 'i-play', !!status.running);
      if (updateWasRunning && !status.running) {
        // 刚跑完一轮：知识库里可能多了新公告，推荐问题的版本号要跟着更新。
        // 要用 status 里带回的进度行判断「真的跑过」，不能只看上一次采样：
        // 一轮很快结束时，两次轮询之间就完成了，那一次采样永远等不到。
        const finished = status.progress || [];
        const ran = finished.some((line) => /完成|收尾|错误/.test(String(line.message || '')));
        if (ran) { renderStarterAsks(); loadStats(); }
      }
      updateWasRunning = !!status.running;
    },
    onError: (error) => {
      // 原来这里是「静默处理」：服务挂掉时更新页一直显示旧数字，
      // 用户以为还在跑。连续失败到第 3 次才提示一次，之后不再重复刷屏。
      updatePollErrors += 1;
      if (updatePollErrors >= 3 && !updateErrorShown) {
        updateErrorShown = true;
        toast('读取更新状态失败：' + describeError(error), 'error');
      }
    }
  });
}

function loadTopics() {
  return latestOnly('topics', async (isStale) => {
    const data = await api('/api/topics');
    if (isStale()) return;
    $('topic-chips').innerHTML = (data.configured || []).map((topic) =>
      '<span class="chip">' + esc(topic) + '<button data-del-topic="' + esc(topic) + '">✕</button></span>').join('')
      || '<span class="muted small">还没有配置主题</span>';
    const queue = data.queue || [];
    $('topic-queue').innerHTML = queue.length
      ? queue.map((row) => '<div class="item"><div class="item-title">' + esc(row.topic) + '</div>'
        + '<div class="item-meta">来源 ' + esc(row.origin) + ' · 命中 ' + (row.hits || 0) + ' 次'
        + (row.last_run ? ' · 上次 ' + esc(fmtTime(row.last_run)) : ' · 待处理') + '</div></div>').join('')
      : emptyState('i-clock', '没有等待处理的主题', '答不上来的问题会自动进到这里排队。');
  }, (error) => toast('读取主题失败：' + describeError(error), 'error'));
}

function loadSources() {
  return latestOnly('sources', async (isStale) => {
    const data = await api('/api/sources');
    if (isStale()) return;
    const builtin = data.builtin || [];
    $('source-list').innerHTML = builtin.length ? builtin.map((source) => {
      return '<div class="item"><div class="item-head"><div>'
        + '<label class="switch"><input type="checkbox" data-source="' + esc(source.id) + '"' + (source.enabled ? ' checked' : '') + ' />'
        + '<span class="item-title">' + esc(source.name) + '</span>' + sourceTag(source.source_type)
        + sourceBadges(source) + '</label>'
        + '<div class="item-meta">' + esc(source.note || '') + '</div>'
        + '<div class="item-meta">' + linkOrText(source.url, icon('i-link') + esc(source.url.slice(0, 90))) + '</div>'
        + '</div></div></div>';
    }).join('') : emptyState('i-plug', '没有可用的内置数据源', '可以用「更新主题」的关键词联网补充资料。');
  }, (error) => toast('读取数据源失败：' + describeError(error), 'error'));
}

/** 把抓取报告翻译成一句人话（表格数值、质量过滤、抓取失败都要说出来，否则用户以为一切正常） */
function describeReport(report) {
  const data = report || {};
  const parts = ['抓取 ' + (data.pages || 0) + ' 页', '新增条目 ' + (data.facts_added || 0) + ' 条'];
  if (data.table_facts_added) parts.push('其中表格数值 ' + data.table_facts_added + ' 条');
  if (data.filtered) parts.push('质量过滤 ' + data.filtered);
  if (data.dropped) parts.push('抓取被跳过 ' + data.dropped);
  return parts.join('，');
}

/* ------------------------------------------------------------------ */
/* 推荐问题：每次打开都从知识库里随机抽（后端抽取，避免写死后过期）      */
/* ------------------------------------------------------------------ */

/** 兜底推荐问题（接口失败时用）；本身也随机取 3 条，避免每次都一样 */
const STARTER_FALLBACK = [
  { ask: '异环的全平台公测是什么时候开启的？', label: '公测时间' },
  { ask: '游戏里有哪些可操作角色？', label: '角色一览' },
  { ask: '异环支持哪些游戏平台？', label: '支持平台' },
  { ask: '弧盘是做什么用的？', label: '弧盘系统' },
  { ask: '卡带系统怎么用？', label: '卡带系统' },
  { ask: '异象和异能者分别是什么？', label: '核心设定' },
];
const STARTER_COUNT = 3;

function pickFallbackAsks(count) {
  const pool = STARTER_FALLBACK.slice();
  for (let i = pool.length - 1; i > 0; i -= 1) {
    const j = Math.floor(Math.random() * (i + 1));
    const tmp = pool[i]; pool[i] = pool[j]; pool[j] = tmp;
  }
  return pool.slice(0, count);
}

function paintStarterAsks(asks) {
  const box = $('starter-asks');
  if (!box) return;
  box.innerHTML = asks.map((item) =>
    '<button class="chip" data-ask="' + esc(item.ask) + '">' + esc(item.label) + '</button>').join('');
}

async function renderStarterAsks() {
  if (!$('starter-asks')) return;
  let asks = [];
  try {
    // 后端从整个知识库随机抽（含「最新版本」这类问题），每次结果都不同
    const data = await api('/api/kb/starter-asks?count=' + STARTER_COUNT);
    asks = (data.items || []).filter((item) => item && item.ask);
  } catch (error) {
    /* 接口不可用就用兜底问题，不打扰用户 */
  }
  if (asks.length < STARTER_COUNT) {
    const seen = {};
    asks.forEach((item) => { seen[item.ask] = true; });
    pickFallbackAsks(STARTER_FALLBACK.length).forEach((item) => {
      if (asks.length < STARTER_COUNT && !seen[item.ask]) { asks.push(item); seen[item.ask] = true; }
    });
  }
  paintStarterAsks(asks.slice(0, STARTER_COUNT));
}

async function runSourceIngest() {
  const ids = Array.from(document.querySelectorAll('[data-source]'))
    .filter((box) => box.checked).map((box) => box.dataset.source);
  if (!ids.length) { toast('请先勾选要抓取的数据源', 'error'); return; }
  const button = $('sources-run');
  setButton(button, '抓取中…', 'i-refresh', true);
  try {
    const report = await api('/api/update/sources', { method: 'POST', body: { source_ids: ids } });
    toast('抓取完成：' + describeReport(report), 'ok');
    if (report.errors && report.errors.length) {
      $('update-log').textContent = report.errors.join('\n');
    }
    loadStats(); loadFacts(); loadDocuments(); loadState();
    renderStarterAsks();   // 抓到新公告后，推荐问题里的版本号跟着更新
  } catch (error) {
    toast('抓取失败：' + describeError(error), 'error');
  } finally {
    setButton(button, '抓取选中数据源', 'i-download', false);
  }
}

/* ------------------------------------------------------------------ */
/* 服务商预设与模型列表                                                */
/* ------------------------------------------------------------------ */

let PROVIDERS = [];
let PROVIDER_MAP = {};

async function loadProviders() {
  if (PROVIDERS.length) return;
  try {
    const data = await api('/api/providers');
    PROVIDERS = data.providers || [];
    PROVIDER_MAP = {};
    PROVIDERS.forEach((item) => { PROVIDER_MAP[item.id] = item; });
    const optionsHtml = PROVIDERS.map((item) =>
      '<option value="' + esc(item.id) + '">' + esc(item.label) + '</option>').join('');
    $('llm-preset').innerHTML = optionsHtml;
    if ($('wizard-provider')) $('wizard-provider').innerHTML = optionsHtml;
  } catch (error) {
    toast('读取服务商列表失败：' + describeError(error), 'error');
  }
}

function currentPreset() {
  return PROVIDER_MAP[$('llm-preset').value] || null;
}

// 协议显示名：设置页与向导都要用，合并成一份常量（原来是两处各写一遍，容易改漏）
const PROTOCOL_LABELS = {
  openai: 'OpenAI 兼容协议', anthropic: 'Anthropic 原生协议', gemini: 'Google Gemini 原生协议'
};

/** 切换服务商时：自动填好 Base URL；**不预填模型名**（避免过时的默认值造成误判） */
function applyPreset(keepExisting) {
  const preset = currentPreset();
  if (!preset) return;
  $('llm-protocol-label').textContent = PROTOCOL_LABELS[preset.protocol] || preset.protocol;
  const note = $('llm-preset-note');
  note.innerHTML = esc(preset.note || '')
    + (preset.docs ? '　' + linkOrText(preset.docs, '去申请 Key →') : '');
  if (keepExisting) return;
  $('llm-base').value = preset.base_url || '';
  // 模型名留空：各家模型迭代很快，预填一个过时的名字会显得配置仍然有效
  $('llm-model').value = '';
  MODEL_OPTIONS = [];
  hideModelList();
  $('llm-model-note').textContent = '换服务商后请重新点「拉取可用模型」——不同服务商的模型名不通用。';
  // 必须清掉密钥输入框。fillConfig 结束时会把用户敲进去的内容原样放回，所以切换
  // 服务商时框里通常还留着上一家的 Key；直接点保存就会把这个密钥发送并保存到新的
  // 服务商名下——恰好是服务端「payload 里带了 Key 就不拦」那道防线挡不住的路径。
  // 留空保存的语义是「不修改」，所以这里清空不会影响已经保存的 Key。
  if ($('llm-key').value) {
    $('llm-key').value = '';
    $('llm-key-state').textContent = '已清空输入框：换了服务商请粘贴新服务商的 Key（留空保存不会改动已保存的 Key）';
  }
}

/* --- 自定义模型下拉（替代 datalist，行为可控；设置页与首启向导共用同一套实现） --- */

let MODEL_OPTIONS = [];
let WIZARD_MODEL_OPTIONS = [];
let COMBO_MAIN = null;
let COMBO_WIZARD = null;

function buildCombo(opts) {
  const input = $(opts.input);
  const list = $(opts.list);
  const toggle = $(opts.toggle);
  const combo = {
    hide() { list.classList.add('hidden'); },
    render(filter) {
      const options = opts.options() || [];
      const keyword = (filter || '').trim().toLowerCase();
      const items = options.filter((name) => !keyword || name.toLowerCase().includes(keyword));
      if (!options.length) {
        list.innerHTML = '<div class="combo-empty">' + esc(opts.empty || '还没有列表') + '</div>';
      } else if (!items.length) {
        list.innerHTML = '<div class="combo-empty">没有匹配的模型；可以直接手动输入完整模型名</div>';
      } else {
        list.innerHTML = items.map((name) =>
          '<div class="combo-item" data-model="' + esc(name) + '">' + esc(name) + '</div>').join('');
        list.querySelectorAll('[data-model]').forEach((node) => {
          node.addEventListener('mousedown', (event) => {
            event.preventDefault();               // 防止 input 失焦导致列表先关闭
            input.value = node.dataset.model;
            combo.hide();
          });
        });
      }
      list.classList.remove('hidden');
    },
    show() { combo.render(input.value); }
  };
  if (toggle) {
    toggle.addEventListener('click', (event) => {
      event.preventDefault();
      if (list.classList.contains('hidden')) combo.show(); else combo.hide();
    });
  }
  input.addEventListener('focus', combo.show);
  input.addEventListener('input', combo.show);
  input.addEventListener('keydown', (event) => { if (event.key === 'Escape') combo.hide(); });
  document.addEventListener('click', (event) => {
    if (!event.target.closest || !event.target.closest(opts.root)) combo.hide();
  });
  return combo;
}

function initCombos() {
  COMBO_MAIN = buildCombo({
    root: '.combo',
    input: 'llm-model', list: 'llm-model-list', toggle: 'llm-model-toggle',
    options: () => MODEL_OPTIONS,
    empty: '还没有模型列表，先点「拉取可用模型」'
  });
  COMBO_WIZARD = buildCombo({
    root: '#wizard .combo',
    input: 'wizard-model', list: 'wizard-model-list', toggle: 'wizard-model-toggle',
    options: () => WIZARD_MODEL_OPTIONS,
    empty: '先点「拉取可用模型」'
  });
}

function hideModelList() { if (COMBO_MAIN) COMBO_MAIN.hide(); }
function renderModelList(filter) { if (COMBO_MAIN) COMBO_MAIN.render(filter); }

async function pullModels() {
  // 预检：模型列表是受保护资源，各家 /models 都要鉴权。
  // 没 Key 时不要发请求再报 401，直接引导用户先填 Key（比失败后弹错友好）。
  const typedKey = $('llm-key').value.trim();
  const savedKey = !!(CURRENT_CONFIG && CURRENT_CONFIG.llm
    && CURRENT_CONFIG.llm.key && CURRENT_CONFIG.llm.key.set);
  if (!typedKey && !savedKey) {
    $('llm-model-note').innerHTML = '还无法拉取：<b>请先在上面粘贴 API Key</b>，'
      + '再点「拉取可用模型」（换服务商或 Base URL 时要重新粘贴一次）。';
    $('llm-key').focus();
    toast('请先填写并保存 API Key', 'error');
    return;
  }

  const button = $('llm-models');
  setButton(button, '拉取中…', 'i-refresh', true);
  try {
    // 输入框为空时，服务端会自动用已保存的 Key
    const result = await api('/api/config/models', { method: 'POST', body: llmPayload() });
    if (!result.ok) {
      MODEL_OPTIONS = [];
      const message = String(result.error || '未知原因');
      // 带上程序实际请求的地址：地址没生效（比如填了没保存、或服务商选错）时，
      // 用户一眼就能看出打的不是自己填的那个，而不用猜上游为什么报错。
      const where = result.endpoint ? '（请求地址 ' + result.endpoint + '）' : '';
      $('llm-model-note').textContent = '拉取失败：' + message + where + '　（也可以手动填写模型名）';
      if (/401|密钥|未授权/.test(message)) {
        $('llm-key').focus();
      }
      toast('拉取模型列表失败', 'error');
      return;
    }
    MODEL_OPTIONS = result.models || [];
    $('llm-model-note').innerHTML = '已拉取 <b>' + MODEL_OPTIONS.length
      + '</b> 个可用模型，点输入框或右侧箭头选择。';
    // 不自动选中第一个：模型名由用户确认，避免选到不合适的模型
    renderModelList('');
    toast('拉取到 ' + MODEL_OPTIONS.length + ' 个模型', 'ok');
  } catch (error) {
    $('llm-model-note').textContent = '拉取失败：' + describeError(error);
    toast('拉取模型列表失败', 'error');
  } finally {
    setButton(button, '拉取可用模型', 'i-refresh', false);
  }
}

/* ------------------------------------------------------------------ */
/* 设置                                                                */
/* ------------------------------------------------------------------ */

function fillConfig(config) {
  CURRENT_CONFIG = config;
  // 先存下用户已经敲进去、但还没保存的密钥内容，函数末尾原样放回。
  // 以前是无条件清空：改个「搜索结果数」点保存，会把刚粘好的 Key 抹掉，
  // 看起来像「保存把 Key 清了」（服务端语义其实是「留空表示不修改」，Key 一直在）。
  const pendingKeys = {
    'llm-key': $('llm-key').value,
    'search-bocha': $('search-bocha').value,
    'search-tavily': $('search-tavily').value,
    'search-serper': $('search-serper').value
  };
  const llm = config.llm || {};
  const presetId = llm.preset || 'custom';
  $('llm-preset').value = PROVIDER_MAP[presetId] ? presetId : 'custom';
  applyPreset(true);                     // 只刷新说明文字，不覆盖已保存的值
  $('llm-base').value = llm.base_url || '';
  $('llm-model').value = llm.model || '';
  $('llm-key').value = '';
  $('llm-temp').value = llm.temperature;
  $('llm-maxtokens').value = llm.max_tokens;
  $('llm-timeout').value = llm.timeout;
  const keyInfo = llm.key || {};
  $('llm-key-state').textContent = keyInfo.set
    ? ('已保存 Key：' + keyInfo.preview + '（留空保存表示不修改）')
    : '尚未保存 API Key';

  const search = config.search || {};
  $('search-provider').value = search.provider || 'bocha';
  $('search-bocha').value = '';
  $('search-tavily').value = '';
  $('search-serper').value = '';
  $('search-max').value = search.max_results;
  $('search-fallback').checked = !!search.free_fallback;
  $('search-bocha').placeholder = (search.bocha_key && search.bocha_key.set) ? ('已保存：' + search.bocha_key.preview + '（留空不变）') : '留空表示不修改';
  $('search-tavily').placeholder = (search.tavily_key && search.tavily_key.set) ? ('已保存：' + search.tavily_key.preview + '（留空不变）') : '留空表示不修改';
  $('search-serper').placeholder = (search.serper_key && search.serper_key.set) ? ('已保存：' + search.serper_key.preview + '（留空不变）') : '留空表示不修改';

  const answer = config.answer || {};
  $('answer-auto-web').checked = !!answer.auto_web;
  $('answer-threshold').value = answer.web_trigger_score;
  $('answer-maxpages').value = answer.max_web_pages;

  const kb = config.kb || {};
  $('kb-topk').value = kb.top_k;
  $('kb-chunksize').value = kb.chunk_size;
  $('kb-overlap').value = kb.chunk_overlap;

  const auto = config.auto_update || {};
  $('auto-enabled').checked = !!auto.enabled;
  $('auto-startup').checked = !!auto.on_startup;
  $('auto-interval').value = auto.interval_hours;
  $('auto-maxpages').value = auto.max_pages_per_run;
  $('auto-maxtopics').value = auto.max_topics_per_run;
  $('update-enabled').checked = !!auto.enabled;
  const blocked = auto.blocked_domains || [];
  $('blocked-domains').value = blocked.join('\n');
  $('blocked-count').textContent = blocked.length;

  const q = config.quality || {};
  $('q-boilerplate').checked = q.filter_boilerplate !== false;
  $('q-foreign').checked = q.reject_foreign_games !== false;
  $('q-official').checked = q.official_priority !== false;
  $('q-minchars').value = q.min_page_chars;
  $('q-density').value = q.min_info_density;

  // 界面与外观（主题 + 壁纸）：状态存在服务端配置里，因为每次启动端口随机、localStorage 不通用
  const ui = config.ui || {};
  applyTheme(ui.theme || 'dark', false);
  applyWallpaper(ui);

  // 把函数开头存下的未保存密钥内容放回去（见本函数开头注释）
  Object.keys(pendingKeys).forEach((id) => {
    const el = $(id);
    if (el && pendingKeys[id]) el.value = pendingKeys[id];
  });
}

async function loadConfig() {
  try {
    const config = await api('/api/config');
    fillConfig(config);
    // 配置文件损坏时后端会在 _notice 里带一条人话说明（见 config.py 的 load_warning）：
    // 要么「已用备份恢复」，要么「已重置为默认设置，请重新填写密钥」。
    // 只在读取配置时提示：保存后也会走 fillConfig，但那时用户已经知道了。
    if (config._notice) toast(config._notice, 'error');
  }
  catch (error) { toast('读取设置失败：' + describeError(error), 'error'); }
}

async function saveConfig(payload, message) {
  try {
    const result = await api('/api/config', { method: 'POST', body: payload });
    fillConfig(result.config);
    toast(message || '已保存', 'ok');
    loadState();
    return true;
  } catch (error) {
    toast('保存失败：' + describeError(error), 'error');
    return false;
  }
}

function llmPayload() {
  const preset = currentPreset();
  const payload = {
    preset: $('llm-preset').value,
    provider: preset ? preset.protocol : 'openai',
    base_url: $('llm-base').value.trim(),
    model: $('llm-model').value.trim(),
    temperature: numberValue('llm-temp', 0.3),
    max_tokens: Math.round(numberValue('llm-maxtokens', 3000)),
    timeout: Math.round(numberValue('llm-timeout', 120))
  };
  const key = $('llm-key').value;
  if (key) payload.api_key = key.trim();
  return payload;
}

function searchPayload() {
  const payload = {
    provider: $('search-provider').value,
    max_results: Math.round(numberValue('search-max', 8)),
    free_fallback: $('search-fallback').checked
  };
  if ($('search-bocha').value) payload.bocha_key = $('search-bocha').value.trim();
  if ($('search-tavily').value) payload.tavily_key = $('search-tavily').value.trim();
  if ($('search-serper').value) payload.serper_key = $('search-serper').value.trim();
  return payload;
}

function miscPayload() {
  return {
    answer: {
      auto_web: $('answer-auto-web').checked,
      web_trigger_score: numberValue('answer-threshold', 0.35),
      max_web_pages: Math.round(numberValue('answer-maxpages', 4))
    },
    kb: {
      top_k: Math.round(numberValue('kb-topk', 8)),
      chunk_size: Math.round(numberValue('kb-chunksize', 700)),
      chunk_overlap: Math.round(numberValue('kb-overlap', 100))
    },
    auto_update: {
      enabled: $('auto-enabled').checked,
      on_startup: $('auto-startup').checked,
      interval_hours: numberValue('auto-interval', 24),
      max_pages_per_run: Math.round(numberValue('auto-maxpages', 25)),
      max_topics_per_run: Math.round(numberValue('auto-maxtopics', 6))
    },
    quality: {
      filter_boilerplate: $('q-boilerplate').checked,
      reject_foreign_games: $('q-foreign').checked,
      official_priority: $('q-official').checked,
      min_page_chars: Math.round(numberValue('q-minchars', 150)),
      min_info_density: numberValue('q-density', 0.35)
    }
  };
}

/** 保存来源黑名单：一行一个，后端会自动提取域名 */
async function saveBlockedDomains() {
  const lines = $('blocked-domains').value
    .split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  // 只发这一个请求：saveConfig 成功时内部已经用服务端返回的 config 调过 fillConfig。
  // 原先是「先弹『已保存』再发第二个 GET 刷新表单」，第二个请求失败时
  // 用户已经被告知保存成功，而表单显示的还是旧值。
  await saveConfig({ auto_update: { blocked_domains: lines } }, '来源黑名单已保存');
}

/* ------------------------------------------------------------------ */
/* 关于                                                                */
/* ------------------------------------------------------------------ */

function renderAbout() {
  if (!APP_STATE || !APP_STATE.paths) return;
  const paths = APP_STATE.paths;
  const rows = [
    ['程序版本', 'v' + APP_STATE.version],
    ['运行方式', paths.frozen ? '打包 exe' : '源码运行'],
    ['数据模式', paths.portable ? '便携模式（跟随 exe 目录）' : '用户目录模式'],
    ['数据目录', paths.data_dir],
    ['配置文件', paths.config],
    ['知识库', paths.database],
    ['日志目录', paths.logs],
    ['全文检索', APP_STATE.stats && APP_STATE.stats.fts_enabled ? 'SQLite FTS5 已启用' : '已降级为 LIKE 扫描']
  ];
  $('about-paths').innerHTML = rows.map((pair) =>
    '<div class="k">' + esc(pair[0]) + '</div><div class="v">' + esc(pair[1]) + '</div>').join('');
}

/* ------------------------------------------------------------------ */
/* 首次使用向导                                                        */
/* ------------------------------------------------------------------ */

let WIZARD_STEP = 1;
const WIZARD_MAX_STEP = 4;
let WIZARD_OPENER = null;      // 关闭向导后把焦点还给打开它的按钮

/**
 * 向导是 aria-modal 对话框，就得真的把背景挡在键盘之外，否则 Tab 会走到遮罩后面。
 *
 * #wizard 本身就在 #app 里面（见 index.html），所以**不能给 #app 加 inert**，
 * 那会把向导自己也冻住——界面看着正常，鼠标点哪儿都没反应，JS 调用却还有效。
 * 只给 #app 里除向导/提示条之外的兄弟节点加 inert。
 */
function wizardSetBackgroundInert(inert) {
  const app = $('app');
  if (!app) return;
  const wizard = $('wizard');
  Array.prototype.forEach.call(app.children, (el) => {
    if (el === wizard || el.id === 'toast') return;   // 对话框与提示条必须保持可交互/可播报
    if (inert) el.setAttribute('inert', '');
    else el.removeAttribute('inert');
  });
}

/**
 * 对话框内的 Tab 循环。inert 只管得住 #app 的子节点，管不住浏览器
 * 把焦点交给地址栏/别的顶层节点；把 Tab 圈在向导里是 aria-modal 的应有之义。
 */
const WIZARD_FOCUSABLE = 'button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

function wizardTrapTab(event) {
  const wizard = $('wizard');
  if (!wizard || wizard.classList.contains('hidden')) return;
  const items = Array.prototype.filter.call(wizard.querySelectorAll(WIZARD_FOCUSABLE), (el) => {
    // 只算当前这一屏（步骤面板是切换显示，隐藏面板里的控件不该进循环）
    return el.offsetParent !== null || el === document.activeElement;
  });
  if (!items.length) return;
  const first = items[0];
  const last = items[items.length - 1];
  if (event.shiftKey && (document.activeElement === first || !wizard.contains(document.activeElement))) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

function wizardOpen() {
  // 自动弹出（maybeShowWizard）时没有触发元素，此时不记 opener，关闭后焦点落在 body
  const active = document.activeElement;
  WIZARD_OPENER = (active && active !== document.body && $('app') && $('app').contains(active))
    ? active
    : null;
  $('wizard').classList.remove('hidden');
  wizardSetBackgroundInert(true);
  const llm = (CURRENT_CONFIG && CURRENT_CONFIG.llm) || {};
  const provider = $('wizard-provider');
  if (provider && llm.preset) {
    provider.value = llm.preset;
  }

  // 「重新打开向导」时把已有的模型名与 Key 状态回显出来，方便在原值上改
  const keySet = Boolean(llm.key && llm.key.set);
  const keyInput = $('wizard-key');
  keyInput.value = '';
  keyInput.placeholder = keySet
    ? '已保存 ' + (llm.key.preview || '') + '；留空表示不修改'
    : '把服务商控制台里的 Key 粘贴到这里';
  $('wizard-key-note').textContent = keySet
    ? '本机已保存一个 Key（' + (llm.key.preview || '') + '），留空即保持不变。'
    : '只需要两步：选服务商 → 粘贴 Key，Base URL 会自动填好。';

  const modelInput = $('wizard-model');
  modelInput.value = llm.model || '';
  $('wizard-model-note').textContent = llm.model
    ? '当前模型：' + llm.model + '；改完点「下一步」即生效。'
    : '';

  wizardShowStep(1);
  wizardSyncProvider();          // 放在最后：换服务商时要覆盖上面的 Key 提示
}

function wizardClose(markDone) {
  $('wizard').classList.add('hidden');
  wizardSetBackgroundInert(false);
  const opener = WIZARD_OPENER;
  WIZARD_OPENER = null;
  if (opener && document.contains(opener)) opener.focus();
  if (markDone) saveConfig({ ui: { wizard_done: true } }, '向导已关闭，之后可在「设置」页随时修改');
}

function wizardShowStep(step) {
  WIZARD_STEP = Math.min(Math.max(step, 1), WIZARD_MAX_STEP);
  document.querySelectorAll('#wizard .wizard-step').forEach((el) => {
    const n = Number(el.dataset.step);
    const done = n < WIZARD_STEP;
    el.classList.toggle('active', n === WIZARD_STEP);
    el.classList.toggle('done', done);
    el.setAttribute('aria-current', n === WIZARD_STEP ? 'step' : 'false');
    // 只允许往回跳：往前跳会跳过还没填的步骤
    if (done) {
      el.setAttribute('role', 'button');
      el.setAttribute('tabindex', '0');
      el.title = '回到这一步修改';
    } else {
      el.removeAttribute('role');
      el.removeAttribute('tabindex');
      el.removeAttribute('title');
    }
  });
  document.querySelectorAll('#wizard .wizard-pane').forEach((el) => {
    el.classList.toggle('hidden', Number(el.dataset.pane) !== WIZARD_STEP);
  });
  const last = WIZARD_STEP >= WIZARD_MAX_STEP;
  setButton($('wizard-next'), last ? '完成' : '下一步', last ? 'i-check' : '', false);
  $('wizard-test').classList.toggle('hidden', WIZARD_STEP !== WIZARD_MAX_STEP);
  $('wizard-back').classList.toggle('hidden', WIZARD_STEP === 1);
  $('wizard-back').disabled = WIZARD_STEP === 1;
  // 进入某一步就把焦点放到这一步的第一个控件：向导是模态框，
  // 焦点必须留在里面（配合 wizardSetBackgroundInert 把背景挡在 Tab 之外）。
  const pane = document.querySelector('#wizard .wizard-pane[data-pane="' + WIZARD_STEP + '"]');
  const first = pane && pane.querySelector('select, input, textarea, button');
  if (first && typeof first.focus === 'function') {
    try { first.focus(); } catch (error) { /* 不可聚焦时忽略：inert 已经把背景挡住了 */ }
  }
}

function wizardBack() {
  if (WIZARD_STEP > 1) wizardShowStep(WIZARD_STEP - 1);
}

function wizardSyncProvider() {
  const preset = PROVIDER_MAP[$('wizard-provider').value] || null;
  if (!preset) return;
  $('wizard-provider-note').innerHTML = esc(preset.note || '')
    + '　协议：' + esc(PROTOCOL_LABELS[preset.protocol] || preset.protocol)
    + (preset.docs ? '　' + linkOrText(preset.docs, '去申请 Key →') : '');

  // 换了服务商就得换 Key，提示一下，免得拿着上家的 Key 一直报鉴权失败
  const llm = (CURRENT_CONFIG && CURRENT_CONFIG.llm) || {};
  const savedPreset = llm.preset || '';
  if (savedPreset && savedPreset !== $('wizard-provider').value) {
    $('wizard-key-note').textContent = '你更换了服务商：请粘贴新服务商的 Key'
      + '（已保存的那个属于「' + savedPreset + '」，继续用会鉴权失败）。';
  }
}

function wizardPayload() {
  const preset = PROVIDER_MAP[$('wizard-provider').value] || null;
  const payload = {
    preset: $('wizard-provider').value,
    provider: preset ? preset.protocol : 'openai',
    base_url: preset ? (preset.base_url || '') : '',
    temperature: 0.3,
    max_tokens: 3000,
    timeout: 120
  };
  const model = $('wizard-model').value.trim();
  if (model) payload.model = model;          // 留空时不要写空值，避免把已有模型名清掉
  const key = $('wizard-key').value.trim();
  if (key) payload.api_key = key;
  return payload;
}

async function wizardNext() {
  if (WIZARD_STEP === 1) {
    await saveConfig({ llm: wizardPayload() }, '服务商已选择');
    wizardShowStep(2);
    if ($('wizard-key')) $('wizard-key').focus();
    return;
  }
  if (WIZARD_STEP === 2) {
    if ($('wizard-key').value.trim()) {
      const ok = await saveConfig({ llm: wizardPayload() }, 'API Key 已保存（仅在本机加密保存）');
      if (!ok) return;
    }
    wizardShowStep(3);
    return;
  }
  if (WIZARD_STEP === 3) {
    if ($('wizard-model').value.trim()) {
      const ok = await saveConfig({ llm: wizardPayload() }, '模型已保存');
      if (!ok) return;
    }
    wizardShowStep(4);
    await wizardTest();
    return;
  }
  wizardClose(true);
}

async function wizardTest() {
  const box = $('wizard-result');
  box.textContent = '正在测试…';
  try {
    const result = await api('/api/config/test-llm', { method: 'POST', body: wizardPayload() });
    box.textContent = JSON.stringify(result, null, 2);
    if (result.ok) toast('连接成功', 'ok');
    else toast('连接失败：' + (result.error || '未知原因'), 'error');
  } catch (error) {
    box.textContent = describeError(error);
    toast('测试失败：' + describeError(error), 'error');
  }
}

async function wizardFetchModels() {
  const button = $('wizard-fetch');
  setButton(button, '拉取中…', 'i-refresh', true);
  try {
    const result = await api('/api/config/models', { method: 'POST', body: wizardPayload() });
    if (!result.ok) {
      WIZARD_MODEL_OPTIONS = [];
      $('wizard-model-note').textContent = '拉取失败：' + String(result.error || '未知原因')
        + '　（也可以手动填写模型名）';
      toast('拉取模型列表失败', 'error');
      return;
    }
    WIZARD_MODEL_OPTIONS = result.models || [];
    $('wizard-model-note').innerHTML = '已拉取 <b>' + WIZARD_MODEL_OPTIONS.length + '</b> 个模型，点输入框选择。';
    if (COMBO_WIZARD) COMBO_WIZARD.render('');
    toast('拉取到 ' + WIZARD_MODEL_OPTIONS.length + ' 个模型', 'ok');
  } catch (error) {
    $('wizard-model-note').textContent = '拉取失败：' + describeError(error);
    toast('拉取模型列表失败', 'error');
  } finally {
    setButton(button, '拉取可用模型', 'i-refresh', false);
  }
}

/** 没配模型、且用户没关过向导时，首次打开自动弹出（只做引导，不阻塞任何功能） */
function maybeShowWizard() {
  if (!APP_STATE || APP_STATE.llm_ready) return;
  if (CURRENT_CONFIG && CURRENT_CONFIG.ui && CURRENT_CONFIG.ui.wizard_done) return;
  if (!$('wizard') || !$('wizard-provider')) return;
  if (!$('wizard-provider').options.length) return;      // 服务商预设还没加载出来
  wizardOpen();
}

/* ------------------------------------------------------------------ */
/* 事件绑定与初始化                                                    */
/* ------------------------------------------------------------------ */

function bindEvents() {
  $('tabs').addEventListener('click', (event) => {
    const btn = event.target.closest('.tab');
    if (btn) switchTab(btn.dataset.tab);
  });
  // 页签是 roving tabindex：只有当前页签在 Tab 序列里，所以必须自己处理
  // 方向键。没有这段时键盘用户根本无法切页（非活动页签 tabindex=-1，
  // 而 Tab 已经走到页签栏之外）。
  $('tabs').addEventListener('keydown', (event) => {
    const keys = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 };
    const tabs = Array.prototype.slice.call(document.querySelectorAll('.tab'));
    const current = tabs.indexOf(document.activeElement);
    if (current < 0) return;
    let next = null;
    if (event.key in keys) next = (current + keys[event.key] + tabs.length) % tabs.length;
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = tabs.length - 1;
    if (next === null) return;
    event.preventDefault();
    switchTab(tabs[next].dataset.tab, true);
  });

  $('send-btn').addEventListener('click', sendQuestion);
  $('question').addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); sendQuestion(); }
  });
  $('question').addEventListener('input', autoGrowQuestion);
  $('question').addEventListener('paste', () => setTimeout(autoGrowQuestion, 0));

  $('fact-refresh').addEventListener('click', loadFacts);
  $('fact-search').addEventListener('keydown', (e) => { if (e.key === 'Enter') loadFacts(); });
  $('fact-status').addEventListener('change', loadFacts);
  $('fact-add-toggle').addEventListener('click', () => $('fact-editor').classList.toggle('hidden'));
  $('fact-cancel').addEventListener('click', () => $('fact-editor').classList.add('hidden'));
  $('fact-save').addEventListener('click', async () => {
    const title = $('fact-title').value.trim();
    const answer = $('fact-answer').value.trim();
    if (!title || !answer) { toast('标题与内容都不能为空', 'error'); return; }
    // 保存失败（令牌失效 / 服务已退出 / 服务端拒绝）必须说出来并保留输入：
    // 原来是裸 await，失败只剩一条 console error，编辑器还开着、字段还在，
    // 用户不知道到底存没存，往往反复点「保存」。
    try {
      await api('/api/kb/facts', { method: 'POST', body: { title, answer, topic: $('fact-topic').value.trim(), tags: $('fact-tags').value.trim() } });
    } catch (error) {
      toast('保存失败：' + describeError(error), 'error');
      return;
    }
    $('fact-title').value = ''; $('fact-topic').value = ''; $('fact-tags').value = ''; $('fact-answer').value = '';
    $('fact-editor').classList.add('hidden');
    toast('已添加', 'ok');
    loadFacts(); loadStats();
  });
  $('doc-refresh').addEventListener('click', loadDocuments);
  $('doc-search').addEventListener('keydown', (e) => { if (e.key === 'Enter') loadDocuments(); });

  // 知识库/主题的删除按钮：事件委托。
  // 原来每渲染一次列表就 querySelectorAll 一遍、给每行单独 addEventListener，
  // 刷新后监听器随旧节点一起丢掉、白建一遍；改成在容器上委托一次。
  $('fact-list').addEventListener('click', (event) => {
    const btn = event.target.closest && event.target.closest('[data-del-fact]');
    if (!btn) return;
    deleteFact(btn.dataset.delFact);
  });
  $('doc-list').addEventListener('click', (event) => {
    const btn = event.target.closest && event.target.closest('[data-del-doc]');
    if (!btn) return;
    deleteDocument(btn.dataset.delDoc);
  });
  $('topic-chips').addEventListener('click', (event) => {
    const btn = event.target.closest && event.target.closest('[data-del-topic]');
    if (!btn) return;
    deleteTopic(btn.dataset.delTopic);
  });

  $('kb-export').addEventListener('click', exportKnowledgeBase);

 $('update-run').addEventListener('click', async () => {
    try {
      const result = await api('/api/update/run', { method: 'POST', body: {} });
      toast(result.started ? '已开始更新' : (result.reason || '无法启动'), result.started ? 'ok' : 'error');
      loadUpdateStatus();
    } catch (error) { toast('启动失败：' + describeError(error), 'error'); }
  });
  $('update-enabled').addEventListener('change', async () => {
    const enabled = $('update-enabled').checked;
    // 两个开关必须一致：保存失败时要把两边都退回去，
    // 否则界面显示「已启用」而服务端仍是旧值，下次打开又变回来。
    $('auto-enabled').checked = enabled;
    if (!(await saveConfig({ auto_update: { enabled: enabled } }, '自动更新设置已保存'))) {
      $('update-enabled').checked = !enabled;
      $('auto-enabled').checked = !enabled;
    }
  });
  $('topic-add').addEventListener('click', async () => {
    const topic = $('topic-input').value.trim();
    if (!topic) return;
    // 服务端拒绝时要说出来、且不要把输入框清空（原先是裸 await：
    // 添加失败后输入框被清掉、界面毫无提示，看起来像按钮坏了）。
    try {
      await api('/api/topics', { method: 'POST', body: { topic } });
    } catch (error) {
      toast('添加失败：' + describeError(error), 'error');
      return;
    }
    $('topic-input').value = '';
    loadTopics();
  });
  $('sources-refresh').addEventListener('click', loadSources);
  $('sources-run').addEventListener('click', runSourceIngest);

  $('llm-preset').addEventListener('change', () => applyPreset(false));
  $('llm-models').addEventListener('click', pullModels);
  $('blocked-save').addEventListener('click', saveBlockedDomains);

  /* --- 主题 --- */
  $('theme-toggle').addEventListener('click', () => {
    applyTheme(resolveTheme(THEME_MODE) === 'dark' ? 'light' : 'dark', true);
  });
  document.querySelectorAll('#theme-seg [data-theme-opt]').forEach((btn) => {
    btn.addEventListener('click', () => applyTheme(btn.dataset.themeOpt, true));
  });

  /* --- 壁纸（高级，默认关闭；只读取用户本机图片） --- */
  $('wp-pick').addEventListener('click', () => $('wp-file').click());
  $('wp-file').addEventListener('change', async () => {
    const file = $('wp-file').files && $('wp-file').files[0];
    $('wp-file').value = '';
    if (file) await uploadWallpaper(file);
  });
  $('wp-clear').addEventListener('click', async () => {
    // 和知识库里的删除保持一致：先确认再动手，误点不可撤销
    if (!confirm('确定要清除当前壁纸吗？')) return;
    try {
      const result = await api('/api/ui/wallpaper', { method: 'DELETE' });
      applyWallpaper((result.config || {}).ui || {});
      toast('壁纸已清除', 'ok');
    } catch (error) { toast('清除失败：' + describeError(error), 'error'); }
  });
  $('wp-enabled').addEventListener('change', async () => {
    const enabled = $('wp-enabled').checked;
    // 保存失败要把开关拨回去，否则界面显示「壁纸已启用」而服务端还是关的
    if (!(await saveConfig({ ui: { wallpaper_enabled: enabled } }, enabled ? '壁纸已启用' : '壁纸已关闭'))) {
      $('wp-enabled').checked = !enabled;
    }
  });
  $('wp-dim').addEventListener('input', () => {
    const value = parseFloat($('wp-dim').value) || 0;
    $('wp-dim-value').textContent = Math.round(value * 100) + '%';
    document.documentElement.style.setProperty('--wp-dim', String(value));
  });
  $('wp-dim').addEventListener('change', () => {
    saveConfig({ ui: { wallpaper_dim: parseFloat($('wp-dim').value) || 0 } }, '背景压暗已保存');
  });
  document.querySelectorAll('#wp-fit [data-fit]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const fit = btn.dataset.fit === 'cover' ? 'cover' : 'contain';
      applyWallpaper(Object.assign({}, (CURRENT_CONFIG || {}).ui || {}, { wallpaper_fit: fit }));
      await saveConfig(
        { ui: { wallpaper_fit: fit } },
        fit === 'cover' ? '背景改为「铺满裁切」' : '背景改为「完整显示」',
      );
    });
  });

  /* --- 问答增强：停止生成 / 复制回答 / 示例问题 --- */
  $('stop-btn').addEventListener('click', () => { if (CHAT_ABORT) CHAT_ABORT.abort(); });
  $('chat-log').addEventListener('click', (event) => {
    const button = event.target.closest && event.target.closest('[data-copy-answer]');
    if (!button) return;
    const bubble = button.closest('.bubble');
    const text = (bubble && bubble.dataset.raw) || '';
    if (!text) { toast('没有可复制的内容', 'error'); return; }
    if (navigator.clipboard) {
      navigator.clipboard.writeText(text)
        .then(() => toast('已复制回答', 'ok'), () => toast('复制失败，请手动选中文字', 'error'));
    } else { toast('当前环境不支持自动复制，请手动选中', 'error'); }
  });
  $('starter-asks').addEventListener('click', (event) => {
    const chip = event.target.closest && event.target.closest('[data-ask]');
    if (!chip) return;
    if (!$('question').value.trim()) $('question').value = chip.dataset.ask;
    autoGrowQuestion();
    sendQuestion();
  });

  /* --- 首启向导 --- */
  $('wizard-reopen').addEventListener('click', wizardOpen);
  $('wizard-provider').addEventListener('change', wizardSyncProvider);
  $('wizard-next').addEventListener('click', wizardNext);
  $('wizard-back').addEventListener('click', wizardBack);
  $('wizard-test').addEventListener('click', wizardTest);
  $('wizard-fetch').addEventListener('click', wizardFetchModels);
  $('wizard-skip').addEventListener('click', () => wizardClose(true));
  // 点已完成的步骤徽标也能回去改（等价于「上一步」）
  document.querySelectorAll('#wizard .wizard-step').forEach((el) => {
    const jumpBack = () => {
      const n = Number(el.dataset.step);
      if (n < WIZARD_STEP) wizardShowStep(n);
    };
    el.addEventListener('click', jumpBack);
    el.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); jumpBack(); }
    });
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !$('wizard').classList.contains('hidden')) wizardClose(true);
    if (event.key === 'Tab') wizardTrapTab(event);
  });
  $('llm-save').addEventListener('click', () => saveConfig({ llm: llmPayload() }, '模型设置已保存'));
  bindTestButton('llm-test', '/api/config/test-llm', llmPayload, '连接成功', '连接失败');

  $('search-save').addEventListener('click', () => saveConfig({ search: searchPayload() }, '搜索设置已保存'));
  bindTestButton('search-test', '/api/config/test-search', searchPayload, '搜索可用', '搜索不可用');

  $('misc-save').addEventListener('click', () => saveConfig(miscPayload(), '设置已保存'));
}

/**
 * 「测试连接」按钮：三个测试按钮原来是三段几乎一样的复制粘贴，
 * 只要有一处改了提示或错误处理，另外两处就会不一致。统一走这里。
 */
function bindTestButton(buttonId, endpoint, payload, okText, failText) {
  const button = $(buttonId);
  if (!button) return;
  button.addEventListener('click', async () => {
    const box = $(buttonId + '-result');
    if (box) {
      box.classList.remove('hidden');
      box.textContent = '正在测试…';
    }
    try {
      const result = await api(endpoint, { method: 'POST', body: payload() });
      if (box) box.textContent = JSON.stringify(result, null, 2);
      toast(result.ok ? okText : failText, result.ok ? 'ok' : 'error');
    } catch (error) {
      if (box) box.textContent = describeError(error);
      else toast(describeError(error), 'error');
    }
  });
}

async function init() {
  bindEvents();
  initCombos();
  try {
    // 先拿到状态与服务商预设，再回填设置，否则下拉框是空的。
    // 三者内部都会自己提示失败，不会把异常抛到这里。
    await loadState();
    await loadProviders();
    await loadConfig();
  } catch (error) {
    toast('初始化失败：' + describeError(error), 'error');
  }
  maybeShowWizard();
  renderStarterAsks();
  polling = setInterval(() => {
    // 轮询用 'low'：用户正在提问或保存时，这两次请求直接跳过，
    // 免得把用户操作刚拿到的状态覆盖成轮询那一份。
    if (document.querySelector('#panel-update.active')) loadUpdateStatus('low');
    if (document.querySelector('#panel-kb.active')) loadStats('low');
  }, 3000);
}

// 关窗/刷新时收掉定时器与向导留下的 inert，避免重新加载后页面还是「不可交互」的
window.addEventListener('beforeunload', () => {
  if (polling) { clearInterval(polling); polling = null; }
  wizardSetBackgroundInert(false);
});

document.addEventListener('DOMContentLoaded', init);
