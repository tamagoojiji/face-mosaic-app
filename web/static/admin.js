// 運営の管理画面の共通（DOM の組み立て・日時の表示）。利用者の入力は textContent でだけ描く
function h(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') e.className = v;
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v === true ? '' : v);
  }
  for (const c of kids.flat(Infinity)) if (c != null && c !== false) e.append(c instanceof Node ? c : String(c));
  return e;
}
const $ = id => document.getElementById(id);
const dt = ts => ts ? new Date(ts * 1000).toLocaleString('ja-JP', { year: 'numeric', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '—';
const day = ts => ts ? new Date(ts * 1000).toLocaleDateString('ja-JP') : '—';
function badge(text, tone) { return h('span', { class: 'badge ' + (tone || '') }, text); }
function errText(e) { return e ? e : h('span', { class: 'sub' }, '—'); }
function sec(v) { return v == null ? '—' : Math.round(v) + '秒'; }
// 見出しには揃えのクラス（num）だけ付ける（失敗理由の赤・ID の等幅はセルだけ）
function thClass(c) { return c && c.split(' ').includes('num') ? 'num' : null; }
// 表: cols = [[見出し, クラス]], rows = [[セル…]]（セルは文字列か要素）。rowAttrs(i) で行の属性
function table(cols, rows, rowAttrs) {
  return h('table', { class: 'tbl' },
    h('thead', {}, h('tr', {}, cols.map(c => h('th', { class: thClass(c[1]), scope: 'col' }, c[0])))),
    h('tbody', {}, rows.map((r, i) => h('tr', rowAttrs ? rowAttrs(i) : {}, r.map((cell, j) => h('td', { class: cols[j][1] || null }, cell))))));
}
function empty(title, ...rest) { return h('div', { class: 'empty' }, h('strong', {}, title), rest); }
function fill(el, ...kids) { el.replaceChildren(...kids.flat(Infinity).filter(k => k != null && k !== false)); }
function showErr(el, e) { el.textContent = e.message; el.hidden = false; }
// 動画の状態（運営向けの日本語）
const JOB_STATUS = {
  uploading: ['送信中', 'run'], queued: ['解析待ち', 'warn'], preparing: ['解析中', 'run'], awaiting_pick: ['選択待ち', ''],
  awaiting_review: ['確認待ち', ''], queued_export: ['書き出し待ち', 'warn'], exporting: ['書き出し中', 'run'],
  done: ['完成', 'ok'], failed: ['失敗', 'bad'], deleted: ['削除済み', ''],
};
function jobBadge(st) { const s = JOB_STATUS[st] || [st, '']; return badge(s[0], s[1]); }
// 工程ごとの秒: 変換 / 追跡 / 特徴 / 書き出し
function stageText(j) {
  const parts = [['変換', j.t_prepare_sec], ['追跡', j.t_track_sec], ['特徴', j.t_feature_sec], ['書き出し', j.t_export_sec]]
    .filter(p => p[1] != null).map(p => p[0] + ' ' + Math.round(p[1]) + '秒');
  return parts.length ? parts.join(' / ') : '—';
}
