/**
 * Đẩy email từ Gmail sang Email Report MCP server (trạng thái CHỜ DUYỆT).
 * Claude Desktop (tác vụ định kỳ) sẽ dùng MCP tools để giữ email công việc và loại email không liên quan.
 *
 * Cài đặt (chi tiết trong README.md của dự án):
 *   1. Mở https://script.google.com bằng tài khoản Gmail nhận báo cáo → New project → dán file này.
 *   2. Project Settings → Script properties → thêm:
 *        SERVER_URL = https://mcp.congty.vn      (địa chỉ server, không có /mcp)
 *        API_KEY    = <một key trong MCP_API_KEYS>
 *   3. Chọn hàm `setup` → Run → cấp quyền. Từ đó script tự chạy theo chu kỳ TRIGGER_MINUTES.
 *
 * Hàm tiện ích: testConnection() kiểm tra kết nối, resetCheckpoint() để lấy lại từ đầu,
 * removeTriggers() để dừng tự động.
 */

const CONFIG = {
  // Cú pháp tìm kiếm Gmail. Mặc định: mọi mail đến, bỏ sẵn tab Quảng cáo / Mạng xã hội / Diễn đàn
  // (Claude sẽ lọc tiếp). Muốn chỉ lấy mail báo cáo: 'subject:("báo cáo" OR report) -in:sent'
  SEARCH_QUERY: '-in:sent -in:drafts -in:spam -in:trash -in:chats ' +
    '-category:promotions -category:social -category:forums',
  // Lần chạy đầu lấy email của N ngày gần nhất
  INITIAL_DAYS_BACK: 7,
  // Chu kỳ quét (phút): 1, 5, 10, 15 hoặc 30
  TRIGGER_MINUTES: 5,
  // Bỏ qua email do chính tài khoản này gửi (vd quản lý trả lời trong luồng báo cáo)
  EXCLUDE_SELF: true,
  // Gắn nhãn cho luồng đã gửi sang server để dễ nhìn trong Gmail. Để '' nếu không muốn.
  LABEL_NAME: 'MCP/Đã gửi',
  BATCH_SIZE: 25,
  MAX_BODY_CHARS: 50000,
  // Quét lùi thêm vài phút để không sót email đến trễ (server tự bỏ trùng)
  OVERLAP_SECONDS: 300,
  SOURCE: 'gmail',
  // Dừng sớm để không vượt giới hạn 6 phút/lần chạy của Apps Script
  MAX_RUNTIME_MS: 4.5 * 60 * 1000,
};

/** Chạy 1 lần: tạo trigger định kỳ, kiểm tra kết nối, đồng bộ lần đầu. */
function setup() {
  getServerConfig_();
  removeTriggers();
  ScriptApp.newTrigger('syncReports').timeBased().everyMinutes(CONFIG.TRIGGER_MINUTES).create();
  testConnection();
  syncReports();
  console.log('Đã cài trigger chạy mỗi ' + CONFIG.TRIGGER_MINUTES + ' phút.');
}

/** Hàm chính, được trigger gọi định kỳ. */
function syncReports() {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(1000)) {
    console.log('Lượt trước chưa xong, bỏ qua lượt này.');
    return;
  }
  try {
    const started = Date.now();
    const props = PropertiesService.getScriptProperties();
    const nowSec = Math.floor(Date.now() / 1000);
    const lastTs = Number(props.getProperty('LAST_TS') || 0) || nowSec - CONFIG.INITIAL_DAYS_BACK * 86400;
    const after = lastTs - CONFIG.OVERLAP_SECONDS;
    const query = CONFIG.SEARCH_QUERY + ' after:' + after;
    const me = (Session.getEffectiveUser().getEmail() || '').toLowerCase();
    const cache = CacheService.getScriptCache();

    // 1. Gom các email mới
    const items = [];
    for (let start = 0; ; start += 100) {
      const threads = GmailApp.search(query, start, 100);
      threads.forEach(function (thread) {
        thread.getMessages().forEach(function (m) {
          const ts = Math.floor(m.getDate().getTime() / 1000);
          if (ts < after || m.isDraft() || m.isInTrash()) return;
          if (CONFIG.EXCLUDE_SELF && extractEmail_(m.getFrom()) === me) return;
          items.push({ message: m, thread: thread, ts: ts });
        });
      });
      if (threads.length < 100) break;
    }

    // 2. Bỏ những email vừa gửi ở các lượt trước (vùng quét lùi)
    const sentFlags = items.length ? cache.getAll(items.map(function (x) { return 'sent:' + x.message.getId(); })) : {};
    const pending = items
      .filter(function (x) { return !sentFlags['sent:' + x.message.getId()]; })
      .sort(function (a, b) { return a.ts - b.ts; });
    if (!pending.length) return;

    // 3. Gửi theo lô, cập nhật mốc sau mỗi lô thành công
    const label = CONFIG.LABEL_NAME
      ? (GmailApp.getUserLabelByName(CONFIG.LABEL_NAME) || GmailApp.createLabel(CONFIG.LABEL_NAME))
      : null;
    const total = { inserted: 0, duplicate: 0, failed: 0 };
    for (let i = 0; i < pending.length; i += CONFIG.BATCH_SIZE) {
      if (Date.now() - started > CONFIG.MAX_RUNTIME_MS) {
        console.log('Hết thời gian, phần còn lại sẽ gửi ở lượt sau.');
        break;
      }
      const batch = pending.slice(i, i + CONFIG.BATCH_SIZE);
      const res = postReports_(batch.map(function (x) { return toReport_(x.message, x.thread); }));
      total.inserted += res.inserted;
      total.duplicate += res.duplicate;
      total.failed += res.failed;
      (res.results || []).forEach(function (r) {
        if (r.status === 'error') console.warn('Không lưu được: ' + batch[r.index].message.getSubject() + ' — ' + r.error);
      });

      const flags = {};
      batch.forEach(function (x) { flags['sent:' + x.message.getId()] = '1'; });
      cache.putAll(flags, 6 * 3600);
      if (label) {
        const seen = {};
        batch.forEach(function (x) {
          if (!seen[x.thread.getId()]) { seen[x.thread.getId()] = true; x.thread.addLabel(label); }
        });
      }
      props.setProperty('LAST_TS', String(batch[batch.length - 1].ts));
    }
    console.log('Đã gửi: ' + JSON.stringify(total));
  } finally {
    lock.releaseLock();
  }
}

/** Kiểm tra server có chạy và API key đúng không. */
function testConnection() {
  const cfg = getServerConfig_();
  const health = UrlFetchApp.fetch(cfg.url + '/health', { muteHttpExceptions: true });
  if (health.getResponseCode() !== 200) {
    throw new Error('Không gọi được ' + cfg.url + '/health (HTTP ' + health.getResponseCode() + ')');
  }
  postReports_([]); // lỗi nếu sai API key
  console.log('Kết nối OK: ' + cfg.url);
}

/** Xoá mốc đã đồng bộ → lượt sau quét lại INITIAL_DAYS_BACK ngày (server tự bỏ trùng). */
function resetCheckpoint() {
  PropertiesService.getScriptProperties().deleteProperty('LAST_TS');
  console.log('Đã xoá mốc đồng bộ.');
}

/** Dừng tự động đồng bộ. */
function removeTriggers() {
  ScriptApp.getProjectTriggers()
    .filter(function (t) { return t.getHandlerFunction() === 'syncReports'; })
    .forEach(function (t) { ScriptApp.deleteTrigger(t); });
}

// ---------------------------------------------------------------------------

function getServerConfig_() {
  const props = PropertiesService.getScriptProperties();
  const url = (props.getProperty('SERVER_URL') || '').replace(/\/+$/, '').replace(/\/mcp$/, '');
  const key = props.getProperty('API_KEY') || '';
  if (!url || !key) {
    throw new Error('Thiếu SERVER_URL hoặc API_KEY trong Project Settings → Script properties.');
  }
  return { url: url, key: key };
}

function postReports_(reports) {
  const cfg = getServerConfig_();
  const res = UrlFetchApp.fetch(cfg.url + '/ingest', {
    method: 'post',
    contentType: 'application/json; charset=utf-8',
    headers: { Authorization: 'Bearer ' + cfg.key },
    payload: JSON.stringify({ source: CONFIG.SOURCE, reports: reports }),
    muteHttpExceptions: true,
  });
  const code = res.getResponseCode();
  if (code === 401) throw new Error('Sai API_KEY (HTTP 401).');
  if (code !== 200) throw new Error('Server lỗi HTTP ' + code + ': ' + res.getContentText().slice(0, 500));
  return JSON.parse(res.getContentText());
}

function toReport_(m, thread) {
  let body = m.getPlainBody() || m.getBody() || '';
  if (body.length > CONFIG.MAX_BODY_CHARS) body = body.slice(0, CONFIG.MAX_BODY_CHARS) + '\n…[đã cắt bớt]';
  let messageId = '';
  try { messageId = m.getHeader('Message-ID') || ''; } catch (e) { /* bỏ qua */ }
  return {
    message_id: messageId || 'gmail:' + m.getId(),
    sender_email: m.getFrom(),
    sender_name: extractName_(m.getFrom()),
    subject: m.getSubject(),
    body: body,
    sent_at: m.getDate().toISOString(),
    attachments: m.getAttachments({ includeInlineImages: false }).map(function (a) { return a.getName(); }),
    to: m.getTo(),
    cc: m.getCc(),
    thread_id: thread ? thread.getId() : '',
    labels: thread ? thread.getLabels().map(function (l) { return l.getName(); })
      .filter(function (n) { return n !== CONFIG.LABEL_NAME; }) : [],
  };
}

function extractEmail_(from) {
  const m = String(from || '').match(/[^\s<>"]+@[^\s<>"]+/);
  return m ? m[0].toLowerCase() : '';
}

function extractName_(from) {
  const m = String(from || '').match(/^\s*"?([^"<]*?)"?\s*</);
  return m ? m[1].trim() : '';
}
