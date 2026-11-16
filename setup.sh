#!/bin/bash

set -e

echo "=========================================================="
echo "🚀 نصب و راه‌اندازی پنل MikroTik Stream Route Manager"
echo "=========================================================="

# 1. بررسی پایتون و pip
if ! command -v python3 &> /dev/null; then
    echo "❌ Python3 نصب نیست. در حال نصب..."
    sudo apt update && sudo apt install -y python3 python3-pip
fi

if ! command -v pip3 &> /dev/null; then
    echo "❌ pip3 نصب نیست. در حال نصب..."
    sudo apt update && sudo apt install -y python3-pip
fi

CURRENT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$CURRENT_DIR"

# 2. نصب کتابخانه‌ها
echo "📦 در حال نصب پیش‌نیازهای پایتون..."
pip3 install -r requirements.txt --break-system-packages 2>/dev/null || pip3 install -r requirements.txt

# 3. بررسی فایل .env
ENV_FILE="$CURRENT_DIR/.env"
if [ ! -f "$ENV_FILE" ]; then
    echo ""
    echo "⚙️ فایل تنظیمات (.env) یافت نشد. بیایید اطلاعات اولیه را وارد کنیم:"
    read -p "🌐 آدرس IP روتر میکروتیک (مثلاً 192.168.2.1): " MIKROTIK_IP
    read -p "👤 نام کاربری میکروتیک [admin]: " MIKROTIK_USER
    MIKROTIK_USER=${MIKROTIK_USER:-admin}
    read -s -p "🔑 رمز عبور میکروتیک: " MIKROTIK_PASSWORD
    echo ""
    read -p "🔌 پورت پنل وب [18542]: " WEB_PORT
    WEB_PORT=${WEB_PORT:-18542}

    cat <<EOF > "$ENV_FILE"
MIKROTIK_IP=$MIKROTIK_IP
MIKROTIK_USER=$MIKROTIK_USER
MIKROTIK_PASSWORD=$MIKROTIK_PASSWORD
MIKROTIK_PORT=8728
PORT=$WEB_PORT
WEB_USERNAME=
WEB_PASSWORD=
DEFAULT_GATEWAY=
EOF
    echo "✅ فایل .env با موفقیت ایجاد شد."
fi

# 4. ایجاد سرویس systemd جهت اجرای دائمی در پس‌زمینه
echo ""
echo "🛠️ آیا می‌خواهید وب‌پنل به عنوان یک سرویس پس‌زمینه (systemd) دائمی فعال شود؟"
read -p "اجرا شود؟ (y/n) [y]: " RUN_SERVICE
RUN_SERVICE=${RUN_SERVICE:-y}

if [[ "$RUN_SERVICE" =~ ^[Yy]$ ]]; then
    SERVICE_FILE="/etc/systemd/system/mikrotik-stream.service"
    echo "📝 در حال ساخت فایل سرویس در $SERVICE_FILE..."
    
    sudo bash -c "cat <<EOF > $SERVICE_FILE
[Unit]
Description=MikroTik Stream Route Web Panel
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$CURRENT_DIR
ExecStart=$(which python3) $CURRENT_DIR/app.py
Restart=always
RestartSec=5
EnvironmentFile=$ENV_FILE

[Install]
WantedBy=multi-user.target
EOF"

    sudo systemctl daemon-reload
    sudo systemctl enable mikrotik-stream.service
    sudo systemctl restart mikrotik-stream.service
    echo "✅ سرویس با موفقیت فعال و اجرا شد!"
fi

# دریافت آی‌پی سرور
SERVER_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
SERVER_IP=${SERVER_IP:-"192.168.2.209"}
PORT=$(grep PORT "$ENV_FILE" | cut -d '=' -f2)
PORT=${PORT:-18542}

echo ""
echo "=========================================================="
echo "🎉 نصب با موفقیت انجام شد!"
echo "🌐 آدرس پنل تحت وب شما:"
echo "👉 http://${SERVER_IP}:${PORT}"
echo ""
echo "💡 دستورات مدیریت سرویس:"
echo "  - مشاهده وضعیت: sudo systemctl status mikrotik-stream"
echo "  - ری‌استارت:     sudo systemctl restart mikrotik-stream"
echo "  - مشاهده لاگ‌ها: sudo journalctl -u mikrotik-stream -f"
echo "=========================================================="
