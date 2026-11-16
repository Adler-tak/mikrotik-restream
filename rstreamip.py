#!/usr/bin/env python3
import sys
import argparse
from core import (
    MikroTikManager,
    load_targets,
    get_config_env,
    save_config_env,
    logger
)
import app

def run_cli_mode():
    print("==========================================")
    print("🚀 MikroTik Stream Route Sync (CLI Mode)")
    print("==========================================")
    
    cfg = get_config_env()
    ip = cfg.get("MIKROTIK_IP")
    user = cfg.get("MIKROTIK_USER")
    pwd = cfg.get("MIKROTIK_PASSWORD")
    gw = cfg.get("DEFAULT_GATEWAY")

    if not ip:
        ip = input("🌐 آدرس IP میکروتیک را وارد کنید: ").strip()
        user = input("👤 نام کاربری میکروتیک: ").strip()
        pwd = input("🔑 پسورد میکروتیک: ").strip()
        save_config_env({"MIKROTIK_IP": ip, "MIKROTIK_USER": user, "MIKROTIK_PASSWORD": pwd})

    mgr = MikroTikManager(host=ip, username=user, password=pwd)

    # 1. Fetch live interfaces to show user
    print("\n🔍 در حال دریافت اینترفیس‌های فعال میکروتیک...")
    try:
        ifaces = mgr.get_live_interfaces()
        print("\nلیست اینترفیس‌های موجود:")
        for idx, iface in enumerate(ifaces, 1):
            status = "🟢 Running" if iface['running'] else "⚪ Down"
            ips = f" [{', '.join(iface['ips'])}]" if iface['ips'] else ""
            print(f"  [{idx}] {iface['name']}{ips} ({iface['type']}) - {status}")
    except Exception as e:
        print(f"⚠️ نتوانستیم اینترفیس‌ها را دریافت کنیم: {e}")
        ifaces = []

    print("-" * 40)
    prompt_gw = f"📡 نام اینترفیس (مثلا ether2) یا IP گیت‌وی خروجی [{gw}]: " if gw else "📡 نام اینترفیس یا IP گیت‌وی خروجی: "
    chosen_gw = input(prompt_gw).strip() or gw

    if not chosen_gw:
        print("❌ گیت‌وی یا اینترفیس مشخص نشد. عملیات متوقف شد.")
        return

    targets = load_targets()
    print(f"\n🔄 در حال همگام‌سازی مسیرهای {len(targets)} سرویس (Restream, Aparat, ...) به سمت {chosen_gw}...")
    
    result = mgr.sync_routes(gateway=chosen_gw, targets=targets)
    if result.get("status") == "ok":
        save_config_env({"DEFAULT_GATEWAY": chosen_gw})
        print(f"\n✅ همگام‌سازی با موفقیت انجام شد!")
        print(f"➕ مسیرهای جدید افزوده شده: {result.get('added')}")
        print(f"♻ مسیرهای بروزرسانی شده: {result.get('updated')}")
        print(f"✔ مسیرهای بدون تغییر: {result.get('unchanged')}")
    else:
        print(f"\n❌ خطا: {result.get('message')}")


def main():
    parser = argparse.ArgumentParser(description="MikroTik Stream Router & Web Panel")
    parser.add_argument("--cli", action="store_true", help="Run in CLI mode instead of Web Panel")
    parser.add_argument("--port", type=int, help="Port to run Web Panel on (default 18542)")
    args = parser.parse_args()

    if args.cli:
        run_cli_mode()
    else:
        cfg = get_config_env()
        port = args.port or int(cfg.get("WEB_PORT", 18542))
        print("==========================================================")
        print("🚀 MikroTik Stream Route Manager (Web Panel)")
        print(f"🌐 باز کردن در مرورگر: http://0.0.0.0:{port}")
        print(f"💻 یا با آی‌پی این سرور: http://<SERVER_IP>:{port}")
        print("💡 برای اجرای حالت متنی می‌توانید از آرگومان --cli استفاده کنید.")
        print("==========================================================")
        app.uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)

if __name__ == "__main__":
    main()
