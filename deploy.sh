#!/usr/bin/env bash
# Cài đặt / cập nhật Email Report MCP trên VPS bằng Docker Compose + Nginx + Let's Encrypt.
#
#   bash deploy.sh            cài lần đầu hoặc cập nhật sau khi chép code mới
#   bash deploy.sh status     xem trạng thái, URL kết nối
#   bash deploy.sh logs       xem log của app
#   bash deploy.sh backup     sao lưu CSDL ra thư mục backups/
#   bash deploy.sh restore F  khôi phục CSDL từ file F
set -euo pipefail
cd "$(dirname "$0")"

say()  { printf '\033[1;32m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[!] %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m[x] %s\033[0m\n' "$*" >&2; exit 1; }

get_env() { grep -E "^$1=" .env 2>/dev/null | tail -n1 | cut -d= -f2- | tr -d '\r' || true; }
set_env() {
  if grep -qE "^$1=" .env; then sed -i "s|^$1=.*|$1=$2|" .env; else printf '%s=%s\n' "$1" "$2" >> .env; fi
}

require_docker() {
  command -v docker >/dev/null 2>&1 || die "Chưa có Docker. Cài bằng:  curl -fsSL https://get.docker.com | sh"
  docker compose version >/dev/null 2>&1 || die "Thiếu Docker Compose plugin (docker compose)."
}

print_info() {
  local domain key admin_pw
  domain=$(get_env DOMAIN); key=$(get_env MCP_API_KEYS); key=${key%%,*}
  admin_pw=$(get_env ADMIN_PASSWORD)
  cat <<EOF

  Trang quản trị dữ liệu:
      https://${domain}/        (mật khẩu: ${admin_pw:-<dùng API key>})

  URL connector (claude.ai → Settings → Connectors → Add custom connector):
      https://${domain}/mcp?key=${key}

  Apps Script (Script properties):
      SERVER_URL = https://${domain}
      API_KEY    = ${key}

  Giữ bí mật URL/key này. Đổi key/mật khẩu: sửa MCP_API_KEYS / ADMIN_PASSWORD trong .env rồi chạy lại: bash deploy.sh
EOF
}

cmd_status() {
  docker compose ps
  local domain; domain=$(get_env DOMAIN)
  if curl -fsS --max-time 10 "https://${domain}/health" >/dev/null 2>&1; then
    say "https://${domain}/health OK"
  else
    warn "Không gọi được https://${domain}/health"
  fi
  print_info
}

cmd_backup() {
  mkdir -p backups
  local name="reports-$(date +%Y%m%d-%H%M%S).db"
  docker compose exec -T app python -c \
    "import sqlite3; s=sqlite3.connect('/app/data/reports.db'); d=sqlite3.connect('/app/data/backup.tmp'); s.backup(d); d.close()"
  docker compose cp app:/app/data/backup.tmp "backups/${name}"
  docker compose exec -T app rm -f /app/data/backup.tmp
  say "Đã sao lưu: backups/${name}"
}

cmd_restore() {
  local f=${1:-}; [ -f "$f" ] || die "Không thấy file sao lưu: $f"
  read -rp "Ghi đè CSDL hiện tại bằng $f? Gõ 'yes' để tiếp tục: " ok; [ "$ok" = yes ] || die "Đã huỷ."
  docker compose stop app
  docker compose cp "$f" app:/app/data/reports.db
  docker compose run --rm --no-deps --user root --entrypoint sh app -c \
    'chown app:app /app/data/reports.db && rm -f /app/data/reports.db-wal /app/data/reports.db-shm'
  docker compose start app
  say "Đã khôi phục từ $f"
}

cmd_deploy() {
  require_docker

  # 1. Cấu hình .env
  if [ ! -f .env ]; then cp .env.example .env; say "Đã tạo .env từ .env.example"; fi
  sed -i 's/\r$//' .env

  local domain email key
  domain=$(get_env DOMAIN)
  if [ -z "$domain" ] || [ "$domain" = "mcp.congty.vn" ]; then
    read -rp "Domain cho MCP server (vd mcp.tencongty.vn): " domain
    [ -n "$domain" ] || die "Cần domain."
    set_env DOMAIN "$domain"
  fi
  email=$(get_env LETSENCRYPT_EMAIL)
  if [ -z "$email" ]; then
    read -rp "Email nhận thông báo hết hạn chứng chỉ (Let's Encrypt): " email
    [ -n "$email" ] || die "Cần email."
    set_env LETSENCRYPT_EMAIL "$email"
  fi
  key=$(get_env MCP_API_KEYS)
  if [ -z "$key" ]; then
    key=$(openssl rand -base64 32 | tr '+/' '-_' | tr -d '=\n')
    set_env MCP_API_KEYS "$key"
    say "Đã sinh API key mới"
  fi
  if [ -z "$(get_env ADMIN_PASSWORD)" ]; then
    set_env ADMIN_PASSWORD "$(openssl rand -base64 18 | tr '+/' '-_' | tr -d '=
')"
    say "Đã sinh mật khẩu trang quản trị (ADMIN_PASSWORD)"
  fi
  chmod 600 .env

  # 2. Kiểm tra DNS trỏ về máy này
  local resolved; resolved=$(getent ahostsv4 "$domain" 2>/dev/null | awk 'NR==1{print $1}' || true)
  if [ -z "$resolved" ]; then
    die "Domain $domain chưa phân giải được. Tạo bản ghi DNS A trỏ về IP của VPS rồi chạy lại."
  fi
  if ! hostname -I 2>/dev/null | tr ' ' '\n' | grep -qx "$resolved"; then
    warn "$domain đang trỏ về $resolved — không thấy IP này trên VPS (bình thường nếu VPS dùng NAT / có Cloudflare proxy)."
  fi

  # 3. Build & chạy app
  say "Build image..."
  docker compose build --pull app
  docker compose up -d app

  # 4. Chứng chỉ HTTPS (chỉ lần đầu)
  if ! docker compose run --rm --no-deps --entrypoint test certbot -f "/etc/letsencrypt/live/${domain}/fullchain.pem"; then
    say "Xin chứng chỉ HTTPS cho ${domain}..."
    docker compose stop nginx >/dev/null 2>&1 || true
    docker compose run --rm --no-deps -p 80:80 --entrypoint certbot certbot certonly \
      --standalone --non-interactive --agree-tos --no-eff-email \
      -m "$email" -d "$domain" \
      || die "Không xin được chứng chỉ. Kiểm tra: DNS đã trỏ đúng, cổng 80 mở (ufw allow 80,443/tcp), không có dịch vụ khác chiếm cổng 80."
  fi

  # 5. Chạy toàn bộ
  docker compose up -d --remove-orphans
  docker compose exec -T nginx nginx -s reload >/dev/null 2>&1 || true

  say "Chờ dịch vụ sẵn sàng..."
  local ok=""
  for _ in $(seq 1 30); do
    if curl -fsS --max-time 5 "https://${domain}/health" >/dev/null 2>&1; then ok=1; break; fi
    sleep 2
  done
  if [ -n "$ok" ]; then
    say "Hoạt động: https://${domain}/health"
  else
    warn "Chưa gọi được https://${domain}/health — xem log: docker compose logs nginx app"
  fi
  docker image prune -f >/dev/null 2>&1 || true
  print_info
}

case "${1:-deploy}" in
  deploy)  cmd_deploy ;;
  status)  require_docker; cmd_status ;;
  logs)    require_docker; docker compose logs -f --tail=200 app ;;
  backup)  require_docker; cmd_backup ;;
  restore) require_docker; cmd_restore "${2:-}" ;;
  *) die "Lệnh không hợp lệ: $1 (deploy | status | logs | backup | restore <file>)" ;;
esac
