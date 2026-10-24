// 共通: API 呼び出しと残り時間の表示
async function api(path, opts) {
  const r = await fetch(path, Object.assign({ credentials: 'same-origin' }, opts || {}));
  if (r.status === 401) { location.href = '/'; throw new Error('401'); }
  let j = null;
  try { j = await r.json(); } catch (e) { }
  if (!r.ok) throw new Error((j && j.detail) || '通信に失敗しました。電波のよい場所でもう一度お試しください');
  return j;
}
// expires_at と now（サーバー時刻）から「あと n 時間 m 分」
function remainText(job) {
  const s = Math.max(0, job.expires_at - job.now);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h > 0 ? 'あと' + h + '時間' + m + '分' : 'あと' + m + '分';
}
function jobIdFromPath() { return location.pathname.split('/').filter(Boolean).pop(); }
// 動画: 状態の表示名と、その状態で開く画面
const VIDEO_STATUS = {
  uploading: ['送信中', '/upload?type=video'], queued: ['解析中', '/status/'], preparing: ['解析中', '/status/'],
  awaiting_pick: ['選択待ち', '/pick/'], awaiting_review: ['確認待ち', '/review/'],
  queued_export: ['書き出し中', '/status/'], exporting: ['書き出し中', '/status/'],
  done: ['完成', '/done/'], failed: ['失敗', '/status/'], deleted: ['削除済み', '/upload?type=video'],
};
function videoHref(j) {
  const s = VIDEO_STATUS[j.status] || VIDEO_STATUS.failed;
  return s[1].endsWith('/') ? s[1] + j.id : s[1];
}
function mmss(sec) {
  sec = Math.round(sec || 0);
  return sec >= 60 ? Math.floor(sec / 60) + '分' + (sec % 60) + '秒' : sec + '秒';
}
// 選択・確認の放置: 1時間たったら「あと2時間で削除」
function idleWarnText(j) { return 'このまま選ばないと、' + remainText(j) + 'で動画を削除します。'; }
