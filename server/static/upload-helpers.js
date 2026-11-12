/* DeckMeido 上传辅助纯函数
 * 只做计算不碰 DOM，tests/test_upload_aggregate.py 抽取 [FB-UPLOAD-HELPERS] 标记段验证。
 * 以普通 <script> 在 index.html 的内联脚本之前加载，函数在全局作用域，按原名调用。
 */
// [FB-UPLOAD-HELPERS-BEGIN]
// 下面是纯函数：体积格式化 / 长路径中间省略 / 整批汇总，只做计算不碰 DOM，
// 便于 tests/test_upload_aggregate.py 抽取出来用 node 直接验证。
function fbFmtBytes(n) {
  var v = Number(n) || 0;
  if (v < 0) v = 0;
  var units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
  var i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return (i === 0 ? String(Math.round(v)) : v.toFixed(1)) + ' ' + units[i];
}

// 长路径中间省略：保留头尾，中间换成 …，前缀目录多留一点便于辨认
function fbMidPath(p, max) {
  var s = String(p || '');
  max = max || 44;
  if (s.length <= max || max < 8) return s;
  var keep = max - 1;
  var head = Math.ceil(keep * 0.55);
  var tail = keep - head;
  return s.slice(0, head) + '…' + s.slice(s.length - tail);
}

// 整批汇总：文件数 / 成功数 / 失败数 / 总字节 / 已传字节 / 当前文件 / 百分比
function fbTaskStats(task) {
  var files = (task && task.files) || [];
  var out = { total: files.length, done: 0, failed: 0, cancelled: 0,
              totalBytes: 0, sentBytes: 0, percent: 0, current: '' };
  for (var i = 0; i < files.length; i++) {
    var f = files[i] || {};
    var size = Number(f.size) || 0;
    out.totalBytes += size;
    if (f.status === 'done') {
      out.done++;
      out.sentBytes += size || (Number(f.loaded) || 0);
    } else if (f.status === 'fail') {
      out.failed++;
      out.sentBytes += Number(f.loaded) || 0;
    } else if (f.status === 'cancelled') {
      out.cancelled++;
    } else if (f.status === 'uploading') {
      out.current = f.rel || f.name || '';
      out.sentBytes += Number(f.loaded) || 0;
    }
  }
  if (out.totalBytes > 0) {
    // 四舍五入到 0.1%，避免浮点误差让 55 变成 55.00000000000001
    out.percent = Math.min(100, Math.round(out.sentBytes / out.totalBytes * 1000) / 10);
  } else if (out.total > 0 && out.done === out.total) {
    out.percent = 100;
  }
  return out;
}

// 完成后的单行结果：38 个文件 · 1.2 GB · 37 成功 1 失败
function fbTaskSummary(task) {
  var st = fbTaskStats(task);
  var parts = [st.total + ' 个文件'];
  if (st.totalBytes > 0) parts.push(fbFmtBytes(st.totalBytes));
  if (task && task.status === 'cancelled') parts.push('已取消');
  // 成功/失败放在同一段里，符合「37 成功 1 失败」的读法
  var tail = st.done + ' 成功';
  if (st.failed > 0) tail += ' ' + st.failed + ' 失败';
  parts.push(tail);
  return parts.join(' · ');
}

// 进度百分比：<10% 保留一位小数，避免 4.5MB / 3.9GB 长时间停在「0%」
function fbPctText(p) {
  var v = Number(p) || 0;
  if (v <= 0) return '0%';
  if (v >= 100) return '100%';
  if (v < 10) return v.toFixed(1) + '%';
  return Math.floor(v) + '%';
}

// 任务副行 / 当前阶段行：卡片首绘和进度刷新共用，避免两处文案不一致
function fbTaskSubText(task, st) {
  st = st || fbTaskStats(task);
  if (task && task.preparing) {
    return '已发现 ' + st.total + ' 个文件' +
      (st.totalBytes ? ' · ' + fbFmtBytes(st.totalBytes) : '');
  }
  return '已完成 ' + st.done + '/' + st.total + ' · 已传 ' +
    fbFmtBytes(st.sentBytes) + (st.totalBytes ? ' / ' + fbFmtBytes(st.totalBytes) : '');
}

function fbTaskNowText(task, st) {
  st = st || fbTaskStats(task);
  if (st.current) return '正在上传：' + fbMidPath(st.current, 52);
  if (task && task.preparing) return '正在读取文件夹…';
  return (task && task.phaseText) ? task.phaseText : '准备中…';
}

// 选下一个要推进的任务：wait 和 running 都算。
// running 表示这一批还有下一个文件 —— 只看 wait 会让首个文件传完后整批停死。
function fbPickTask(tasks) {
  for (var i = 0; i < tasks.length; i++) {
    var t = tasks[i];
    if (!t || t.finished || t.cancelled) continue;
    if (t.status === 'wait' || t.status === 'running') return t;
  }
  return null;
}
// [FB-UPLOAD-HELPERS-END]
