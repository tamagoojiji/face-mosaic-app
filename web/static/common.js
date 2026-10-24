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
